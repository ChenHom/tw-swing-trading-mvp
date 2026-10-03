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
