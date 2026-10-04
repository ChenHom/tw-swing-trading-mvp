import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.application.reporting.sector_flow import build_sector_flow_report
from src.application.reporting.sector_flow_dashboard import build_sector_flow_dashboard

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sector-flow"
# (source, fixture) -> two trading days 35 days apart, beyond the CLI's 31-day cap
SOURCES = [("twse/T86", "twse-t86.json"), ("twse/MI_INDEX", "twse-mi-index.json"),
           ("tpex/institutional", "tpex-institutional.json"), ("tpex/daily_close", "tpex-daily-close.json")]
D1, D2, END = "2026-08-20", "2026-09-24", "2026-09-25"


def _index_payload(day, value):
    base = json.loads((FIXTURES / "twse-mi-index.json").read_text(encoding="utf-8"))
    base["date"] = day.replace("-", "")
    base["tables"][0] = {"title": "115年 價格指數(臺灣證券交易所)", "fields": ["指數", "收盤指數"],
                         "data": [["發行量加權股價指數", f"{value:,}"], ["半導體類指數", "1,500.5"]]}
    return base


class SectorFlowDashboardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)
        tax = self.cache / "finmind" / "TaiwanStockInfo" / "2026-08-01" / "market.jsonl"
        tax.parent.mkdir(parents=True)
        tax.write_text("\n".join(json.dumps({"stock_id": s, "stock_name": n, "industry_category": "半導體業", "type": t}, ensure_ascii=False)
                                 for s, n, t in (("2330", "台積電", "twse"), ("6488", "環球晶", "tpex"))) + "\n", encoding="utf-8")
        for day in (D1, D2):
            for src, fixture in SOURCES:
                text = (FIXTURES / fixture).read_text(encoding="utf-8")
                y, m, d = day.split("-")
                text = text.replace("20260924", day.replace("-", "")).replace("115/09/24", f"{int(y) - 1911}/{m}/{d}")
                target = self.cache / src / day / "market.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        (self.cache / "twse/MI_INDEX" / D2 / "market.json").write_text(json.dumps(_index_payload(D2, 30000)), encoding="utf-8")

    def test_stock_sum_matches_daily(self):
        out = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)
        self.assertEqual(out["dates"], [D1, D2])
        row = next(r for r in out["rows"] if r["name"] == "半導體業")
        detail = out["stocks"]["20"]["半導體業"]
        total = sum(s["amt"] for s in detail["in"] + detail["out"])
        self.assertNotEqual(total, 0)
        self.assertAlmostEqual(total, sum(row["daily"]), delta=2)

    def test_window_matches_direct_report_and_cli_cap_stays(self):
        with self.assertRaisesRegex(ValueError, "31 calendar days"):
            build_sector_flow_report(cache_dir=self.cache, start_date=D1, end_date=END)
        out = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)
        ref = build_sector_flow_report(cache_dir=self.cache, start_date=D1, end_date=END, detail_categories=["半導體業"],
                                       top=5, max_days=None)["category_detail"][0]
        got = out["stocks"]["20"]["半導體業"]
        self.assertEqual([(s["sym"], s["amt"]) for s in got["in"] + got["out"]],
                         [(s["symbol"], round(s["estimated_net_amount_twd"])) for s in ref["top_inflows"] + ref["top_outflows"]])
        self.assertEqual(got["members"], ref["member_count"])

    def test_window_larger_than_data_uses_all_dates(self):
        out = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)
        self.assertEqual(out["stocks"]["90"]["半導體業"], out["stocks"]["20"]["半導體業"])
        self.assertEqual(len(out["rows"][0]["daily"]), len(out["dates"]))

    def test_index_mapping_and_missing_index_is_null(self):
        out = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)
        row = next(r for r in out["rows"] if r["name"] == "半導體業")
        self.assertEqual(row["idx"], "半導體類指數")
        self.assertEqual(row["idxv"], [None, 1500.5])  # D1 payload has no matching index table
        self.assertEqual(out["taiex"], [None, 30000.0])

    def _tdcc(self, day, rows):
        path = self.cache / "tdcc" / "holding_distribution" / day / "market.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"證券代號": s, "持股分級": "12", "人數": "1", "股數": f"{n:,}", "占集保庫存數比例%": str(pct),
                                     "資料日期": day.replace("-", "")} for s, n, pct in rows], ensure_ascii=False), encoding="utf-8")

    def test_large_holder_ranks_weekly_change_by_value(self):
        self._tdcc("2026-09-18", [("2330", 400_000, 1.0), ("6488", 300_000, 2.0)])
        self._tdcc("2026-09-24", [("2330", 600_000, 1.5), ("6488", 250_000, 1.6)])
        lh = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)["large_holder"]
        self.assertEqual((lh["status"], lh["prior"], lh["latest"], lh["snapshots"]), ("ok", "2026-09-18", "2026-09-24", 2))
        row = next(r for r in lh["rows"] if r["name"] == "半導體業")
        self.assertEqual((row["up"], row["down"]), (1, 1))
        self.assertEqual(([s["sym"] for s in row["in"]], [s["sym"] for s in row["out"]]), (["2330"], ["6488"]))
        self.assertEqual((row["in"][0]["shares"], row["in"][0]["pp"], row["out"][0]["shares"]), (200_000, 0.5, -50_000))
        self.assertEqual(row["amt"], row["in"][0]["amt"] + row["out"][0]["amt"])
        self.assertEqual((lh["weeks"], row["wk"], lh["window"], lh["start"], lh["reshaped"]), (["2026-09-24"], [row["amt"]], 20, D1, 0))

    def test_large_holder_sums_weeks_in_window(self):
        self._tdcc("2026-09-10", [("2330", 400_000, 1.0), ("6488", 300_000, 2.0)])
        self._tdcc("2026-09-18", [("2330", 500_000, 1.2), ("6488", 200_000, 1.5)])
        self._tdcc("2026-09-24", [("2330", 450_000, 1.1), ("6488", 300_000, 2.0)])
        lh = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)["large_holder"]
        self.assertEqual((lh["status"], lh["weeks"], lh["snapshots"]), ("ok", ["2026-09-18", "2026-09-24"], 3))
        row = next(r for r in lh["rows"] if r["name"] == "半導體業")
        weeks = [build_sector_flow_report(cache_dir=self.cache, start_date=D1, end_date=d, max_days=None, taxonomy_date=END)["large_holder"] for d in lh["weeks"]]
        direct = [round(next(c for c in w["categories"] if c["category"] == "半導體業")["estimated_change_twd"]) for w in weeks]
        self.assertEqual((row["wk"], row["amt"]), (direct, sum(direct)))
        # 2330 net +50,000 shares; 6488 falls then recovers to net 0, so it counts neither way
        self.assertEqual((row["up"], row["down"], [s["sym"] for s in row["in"]], row["out"]), (1, 0, ["2330"], []))
        self.assertEqual((row["in"][0]["shares"], row["in"][0]["pp"]), (50_000, 0.1))

    def test_large_holder_weeks_use_the_tab_industry_snapshot(self):
        tax = self.cache / "finmind" / "TaiwanStockInfo" / END / "market.jsonl"
        tax.parent.mkdir(parents=True)
        tax.write_text(json.dumps({"stock_id": "2330", "stock_name": "台積電", "industry_category": "光電業", "type": "twse"}, ensure_ascii=False) + "\n", encoding="utf-8")
        self._tdcc("2026-09-10", [("2330", 400_000, 1.0)])
        self._tdcc("2026-09-18", [("2330", 500_000, 1.2)])
        self._tdcc("2026-09-24", [("2330", 450_000, 1.1)])
        lh = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)["large_holder"]
        row = next(r for r in lh["rows"] if r["name"] == "光電業")
        self.assertEqual((lh["weeks"], [w is not None for w in row["wk"]]), (["2026-09-18", "2026-09-24"], [True, True]))
        self.assertNotIn("半導體業", [r["name"] for r in lh["rows"]])

    def test_large_holder_week_with_gap_is_null(self):
        self._tdcc("2026-08-21", [("2330", 400_000, 1.0)])
        self._tdcc("2026-09-18", [("2330", 500_000, 1.2)])
        self._tdcc("2026-09-24", [("2330", 450_000, 1.1)])
        lh = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)["large_holder"]
        row = next(r for r in lh["rows"] if r["name"] == "半導體業")
        self.assertEqual((lh["status"], lh["weeks"], row["wk"][0], row["amt"]), ("ok", ["2026-09-18", "2026-09-24"], None, row["wk"][1]))

    def test_large_holder_without_week_in_window(self):
        self._tdcc("2026-08-10", [("2330", 400_000, 1.0)])
        self._tdcc("2026-08-14", [("2330", 500_000, 1.2)])
        lh = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)["large_holder"]
        self.assertEqual((lh["status"], lh["reason"], lh["weeks"], lh["rows"]), ("insufficient_data", "no_snapshot_in_window", [], []))

    def test_large_holder_needs_two_snapshots(self):
        self._tdcc("2026-09-24", [("2330", 600_000, 1.5)])
        lh = build_sector_flow_dashboard(cache_dir=self.cache, end_date=END)["large_holder"]
        self.assertEqual((lh["status"], lh["rows"], lh["snapshots"]), ("insufficient_data", [], 1))

    def test_blocked_without_data(self):
        out = build_sector_flow_dashboard(cache_dir=self.cache / "empty", end_date=END)
        self.assertEqual((out["status"], out["dates"], out["rows"]), ("blocked", [], []))

    def test_cli_registered(self):
        root = Path(__file__).parents[2]
        r = subprocess.run([sys.executable, "-m", "app", "report", "sector-flow-dashboard", "--help"], cwd=root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--end-date", r.stdout)


if __name__ == "__main__":
    unittest.main()
