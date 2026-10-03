import argparse
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cli.main import main
from src.cli.market import cmd_market_sync_sector_flow as cmd_ingest_sector_flow
from src.cli.report import cmd_report_sector_flow
from src.application.reporting.sector_flow_report import render_sector_flow_markdown
from tests.unit.test_sector_flow_detail import BROAD, SEMI, D1, D2, detail


def sample_payload():
    category = {
        "category": "半導體業",
        "institutional_net_shares": 2_225_000,
        "foreign_net_shares": 1_900_000,
        "investment_trust_net_shares": 300_000,
        "dealer_net_shares": 25_000,
        "estimated_institutional_net_amount_twd": 1_463_750_000.0,
        "estimated_foreign_net_amount_twd": 1_225_000_000.0,
        "estimated_investment_trust_net_amount_twd": 225_000_000.0,
        "estimated_dealer_net_amount_twd": 13_750_000.0,
        "top_positive_contributors": [{"market": "twse", "symbol": "2330", "name": "台積電", "institutional_net_shares": 1_250_000, "estimated_net_amount_twd": 1_025_000_000.0}],
        "top_negative_contributors": [],
    }
    return {
        "requested_period": {"start_date": "2026-09-24", "end_date": "2026-10-01"},
        "observed_trading_dates": ["2026-09-24"],
        "status": "ok",
        "ranking_method": "estimated_amount",
        "price_coverage": 1.0,
        "taxonomy": {"snapshot_date": "2026-09-30", "coverage": 1.0},
        "period_summary": [category],
        "daily": [{"trading_date": "2026-09-24", "price_coverage": 1.0, "categories": [category]}],
        "large_holder": {"status": "insufficient_data", "reason": "fewer_than_two_eligible_snapshots", "latest_as_of_date": "2026-09-24"},
        "warnings": [],
        "exclusions": {"symbol_rule": "^[1-9][0-9]{3}$"},
    }


def _parse(argv):
    """Run the real `main()` parser with both handlers stubbed, and return the parsed Namespace."""
    seen = []
    with patch.object(sys, "argv", ["app"] + argv), \
            patch("src.cli.main.cmd_report_sector_flow", seen.append), \
            patch("src.cli.main.cmd_market_sync_sector_flow", seen.append):
        main()
    return seen[0]


class SectorFlowReportTest(unittest.TestCase):
    def test_parser_accepts_sector_flow_commands(self):
        ingest = _parse(["market", "sync-sector-flow", "--start-date", "2026-09-24", "--end-date", "2026-10-01"])
        report = _parse([
            "report", "sector-flow", "--start-date", "2026-09-24", "--end-date", "2026-10-01",
            "--output", "out.json", "--report-output", "out.md",
        ])
        self.assertEqual((ingest.command, ingest.subcommand, ingest.cache_dir), ("market", "sync-sector-flow", "data/raw"))
        self.assertEqual((report.command, report.subcommand, report.output), ("report", "sector-flow", "out.json"))

    def test_report_command_writes_outputs_without_close_report_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(
                start_date="2026-09-24",
                end_date="2026-09-24",
                cache_dir=str(root / "cache"),
                output=str(root / "report.json"),
                report_output=str(root / "report.md"),
            )

            with self.assertRaises(SystemExit) as raised:
                cmd_report_sector_flow(args)

            self.assertEqual(raised.exception.code, 1)  # empty cache is blocked
            self.assertTrue(Path(args.output).exists())
            self.assertTrue(Path(args.report_output).exists())

    def test_degraded_report_exits_zero_and_ingest_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(start_date="2026-09-24", end_date="2026-09-24", cache_dir=str(root), output=str(root / "r.json"), report_output=str(root / "r.md"))
            with patch("src.cli.report.build_sector_flow_report", return_value={**sample_payload(), "status": "degraded"}):
                cmd_report_sector_flow(args)
            ingest_args = argparse.Namespace(start_date="2026-09-24", end_date="2026-09-24", cache_dir=str(root))
            with patch("src.cli.market.ingest_sector_flow", return_value={"failed": 0}):
                cmd_ingest_sector_flow(ingest_args)
            with patch("src.cli.market.ingest_sector_flow", return_value={"failed": 1}):
                with self.assertRaises(SystemExit) as raised:
                    cmd_ingest_sector_flow(ingest_args)
            self.assertEqual(raised.exception.code, 1)

    def test_amount_ranking_explains_opposite_share_and_amount_signs_only_in_amount_mode(self):
        note = "排名依估算金額"
        self.assertIn(note, render_sector_flow_markdown(sample_payload()))
        payload = sample_payload()
        payload["ranking_method"] = "net_shares"
        self.assertNotIn(note, render_sector_flow_markdown(payload))

    def test_daily_section_lists_largest_outflows_by_ranking_metric(self):
        payload = sample_payload()
        template = payload["period_summary"][0]
        rows = [dict(template, category=f"族群{i:02d}", institutional_net_shares=-i, estimated_institutional_net_amount_twd=float(i if i % 2 else -i) * 1000) for i in range(1, 13)]
        payload["daily"][0]["categories"] = sorted(rows, key=lambda row: row["estimated_institutional_net_amount_twd"], reverse=True)
        daily = render_sector_flow_markdown(payload).split("## 每日族群排行", 1)[1].split("## 主要個股貢獻", 1)[0]
        outflow = daily.split("最大流出", 1)[1]
        self.assertEqual(len(re.findall(r"族群\d\d", outflow)), 6)  # only the 6 negative-amount categories, not the net-share signs
        self.assertLess(outflow.index("族群12"), outflow.index("族群02"))

    def test_report_distinguishes_exact_shares_from_estimated_amount(self):
        markdown = render_sector_flow_markdown(sample_payload())
        self.assertIn("精確淨買賣股數", markdown)
        self.assertIn("估算金額（淨股數 × 收盤價）", markdown)
        self.assertNotIn("精確淨流入金額", markdown)

    def test_report_shows_insufficient_large_holder_data(self):
        markdown = render_sector_flow_markdown(sample_payload())
        self.assertIn("資料不足", markdown)
        self.assertIn("2026-09-24", markdown)
        self.assertIn("不可解讀為大戶淨流入", markdown)

    def test_report_contains_period_daily_and_contributor_sections(self):
        markdown = render_sector_flow_markdown(sample_payload())
        self.assertIn("## 區間法人流入排行", markdown)
        self.assertIn("## 區間法人流出排行", markdown)
        self.assertIn("## 每日族群排行", markdown)
        self.assertIn("台積電", markdown)

    def test_report_states_overlap_marks_broad_and_lists_dates_without_data(self):
        payload = sample_payload()
        payload["period_summary"][0]["is_broad"] = True
        payload["dates_without_data"] = ["2026-09-26"]
        markdown = render_sector_flow_markdown(payload)
        self.assertIn("一檔股票可能同時計入多個族群（例如大類「電子工業」與細類「半導體業」），族群之間互有重疊，不可加總。", markdown)
        self.assertIn("半導體業（大類）", markdown)
        self.assertIn("休市日（四個來源皆無資料，視為休市）：2026-09-26", markdown)

    def test_degraded_report_never_prints_no_extra_warning(self):
        payload = sample_payload()
        payload["status"] = "degraded"
        self.assertNotIn("無額外警告", render_sector_flow_markdown(payload))

    def test_same_payload_renders_identically(self):
        payload = sample_payload()
        self.assertEqual(render_sector_flow_markdown(payload), render_sector_flow_markdown(payload))

    def test_estimated_amount_ranking_uses_amount_sign_for_inflow_and_outflow(self):
        payload = sample_payload()
        contradictory = dict(payload["period_summary"][0])
        contradictory.update({
            "category": "股數正但金額負",
            "institutional_net_shares": 9_000_000,
            "estimated_institutional_net_amount_twd": -900_000_000.0,
        })
        payload["period_summary"] = [payload["period_summary"][0], contradictory]

        markdown = render_sector_flow_markdown(payload)
        inflow = markdown.split("## 區間法人流入排行", 1)[1].split("## 區間法人流出排行", 1)[0]
        outflow = markdown.split("## 區間法人流出排行", 1)[1].split("## 區間外資流入排行", 1)[0]

        self.assertNotIn("股數正但金額負", inflow)
        self.assertIn("股數正但金額負", outflow)

    # ---- per-category drill-down ----
    def _detail_payload(self, categories=(BROAD,), method="estimated_amount", extra_dates=0):
        payload = sample_payload()
        payload["ranking_method"] = method
        payload["observed_trading_dates"] = [D1, D2] + [f"2026-10-{i + 1:02d}" for i in range(extra_dates)]
        payload["category_detail"] = detail(list(categories), method=method, top=10)
        if extra_dates:
            for item in payload["category_detail"]:
                for side in ("top_inflows", "top_outflows"):
                    for stock in item[side]:
                        stock["daily"] += [{"trading_date": d, "institutional_net_shares": 0, "estimated_net_amount_twd": 0.0} for d in payload["observed_trading_dates"][2:]]
        return payload

    def test_parser_accepts_category_and_top(self):
        args = _parse(["report", "sector-flow", "--start-date", "a", "--end-date", "b", "--output", "o", "--report-output", "r",
                                          "--category", "電子工業", "--category", "金融業", "--top", "5"])
        self.assertEqual((args.category, args.top), (["電子工業", "金融業"], 5))
        default = _parse(["report", "sector-flow", "--start-date", "a", "--end-date", "b", "--output", "o", "--report-output", "r"])
        self.assertEqual((default.category, default.top), (None, 10))
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            _parse(["report", "sector-flow", "--start-date", "a", "--end-date", "b", "--output", "o", "--report-output", "r", "--top", "0"])

    def test_report_command_passes_category_and_top_to_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(start_date="2026-09-24", end_date="2026-09-24", cache_dir=str(root), output=str(root / "r.json"),
                                      report_output=str(root / "r.md"), category=["金融業"], top=3)
            with patch("src.cli.report.build_sector_flow_report", return_value=self._detail_payload()) as builder:
                cmd_report_sector_flow(args)
            self.assertEqual(builder.call_args.kwargs["detail_categories"], ["金融業"])
            self.assertEqual(builder.call_args.kwargs["top"], 3)
            self.assertIn("## 族群細看：電子工業（大類）", Path(args.report_output).read_text(encoding="utf-8"))

    def test_unknown_category_exits_with_available_names_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(start_date="2026-09-24", end_date="2026-09-24", cache_dir=str(root / "cache"), output=str(root / "r.json"),
                                      report_output=str(root / "r.md"), category=["不存在"], top=10)
            with self.assertRaises(SystemExit) as raised:
                cmd_report_sector_flow(args)
            self.assertIn("不存在", str(raised.exception.code))
            self.assertFalse(Path(args.output).exists())
            self.assertFalse(Path(args.report_output).exists())

    def test_default_markdown_has_no_drilldown_section(self):
        self.assertNotIn("族群細看", render_sector_flow_markdown(sample_payload()))

    def test_drilldown_section_precedes_quality_section_and_has_tables(self):
        markdown = render_sector_flow_markdown(self._detail_payload(categories=(BROAD, SEMI)))
        self.assertLess(markdown.index("## 族群細看：電子工業（大類）"), markdown.index("## 族群細看：半導體業"))
        self.assertLess(markdown.index("## 族群細看：半導體業"), markdown.index("## 資料品質與限制"))
        broad = markdown.split("## 族群細看：電子工業（大類）", 1)[1].split("## 族群細看：半導體業", 1)[0]
        self.assertIn("成員 6 檔", broad)
        self.assertIn("| 子類 | 成員數 | 法人淨股數 | 外資 | 投信 | 自營商 | 估算金額 |", broad)
        self.assertIn("（僅大類）", broad)
        self.assertIn("同時計入 1 檔", broad)
        self.assertIn("### 流入前 10 名", broad)
        self.assertIn("### 流出前 10 名", broad)
        self.assertIn("| 排名 | 市場 | 代號 | 名稱 | 外資 | 投信 | 自營商 | 法人淨股數 | 估算金額 | 佔流入比重 |", broad)
        self.assertIn("佔流出比重", broad)
        self.assertIn("60.24%", broad)
        self.assertIn(f"| {D1} |", broad)  # per-day table header column
        semi = markdown.split("## 族群細看：半導體業", 1)[1].split("## 資料品質與限制", 1)[0]
        self.assertNotIn("子類", semi)

    def test_per_day_table_is_omitted_beyond_ten_trading_dates(self):
        markdown = render_sector_flow_markdown(self._detail_payload(extra_dates=9))  # 11 dates
        self.assertNotIn("### 每日明細", markdown)
        self.assertIn("category_detail[].top_*[].daily", markdown)
        self.assertNotIn("| 代號 | 名稱 | 2026-09-24 |", markdown)
        shown = render_sector_flow_markdown(self._detail_payload(extra_dates=8))  # 10 dates
        self.assertNotIn("category_detail[].top_*[].daily", shown)
        self.assertIn("| 代號 | 名稱 | 2026-09-24 |", shown)


if __name__ == "__main__":
    unittest.main()
