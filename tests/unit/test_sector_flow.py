import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.application.reporting.sector_flow import (
    build_large_holder_proxy,
    build_sector_flow_report,
    load_taxonomy,
)
from src.market_data.sector_flow_sources import HoldingDistributionRow


FIXTURES = Path(__file__).parents[1] / "fixtures" / "sector-flow"


class SectorFlowAggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmpdir.name)
        self._copy("twse-t86.json", "twse/T86/2026-09-24/market.json")
        self._copy("twse-mi-index.json", "twse/MI_INDEX/2026-09-24/market.json")
        self._copy("tpex-institutional.json", "tpex/institutional/2026-09-24/market.json")
        self._copy("tpex-daily-close.json", "tpex/daily_close/2026-09-24/market.json")
        self._copy("tdcc-holding-distribution.json", "tdcc/holding_distribution/2026-09-24/market.json")
        self._write_taxonomy("2026-09-23", "舊半導體")
        self._write_taxonomy("2026-09-30", "半導體業")
        self._write_taxonomy("2026-10-02", "未來分類")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _copy(self, fixture, relative):
        target = self.cache_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / fixture, target)

    def _write_taxonomy(self, snapshot_date, category):
        path = self.cache_dir / "finmind" / "TaiwanStockInfo" / snapshot_date / "market.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"date": "1962-02-09", "stock_id": "2330", "stock_name": "台積電", "industry_category": category, "type": "twse"},
            {"date": "2015-09-25", "stock_id": "6488", "stock_name": "環球晶", "industry_category": category, "type": "tpex"},
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def test_taxonomy_uses_latest_snapshot_not_after_end_date(self):
        taxonomy = load_taxonomy(self.cache_dir, end_date="2026-10-01")
        self.assertEqual(taxonomy["2330"].categories, ("半導體業",))
        self.assertEqual(taxonomy["2330"].snapshot_date, "2026-09-30")

    def test_daily_category_sums_exact_shares_and_estimated_amounts(self):
        payload = build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-10-01")
        category = payload["daily"][0]["categories"][0]
        self.assertEqual(category["institutional_net_shares"], 2_225_000)
        self.assertEqual(category["estimated_institutional_net_amount_twd"], 1_463_750_000.0)
        self.assertEqual(category["amount_method"], "net_shares_times_close")
        self.assertEqual(payload["observed_trading_dates"], ["2026-09-24"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source_status"]["2026-09-24"]["twse_institutional"]["embedded_date"], "2026-09-24")

    def test_top_contributors_keep_market_and_symbol(self):
        payload = build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-10-01")
        positive = payload["daily"][0]["categories"][0]["top_positive_contributors"]
        self.assertEqual(positive[0]["symbol"], "2330")
        self.assertEqual({row["market"] for row in positive}, {"twse", "tpex"})

    def test_estimated_amounts_are_rounded_to_two_decimal_places(self):
        close_path = self.cache_dir / "tpex/daily_close/2026-09-24/market.json"
        close_payload = json.loads(close_path.read_text(encoding="utf-8"))
        close_payload["tables"][0]["data"][0][2] = "0.1"
        close_path.write_text(json.dumps(close_payload, ensure_ascii=False), encoding="utf-8")
        flow_path = self.cache_dir / "tpex/institutional/2026-09-24/market.json"
        flow_payload = json.loads(flow_path.read_text(encoding="utf-8"))
        row = flow_payload["tables"][0]["data"][0]
        for index, value in {4: "3", 7: "0", 10: "3", 13: "0", 16: "0", 19: "0", 22: "0", 23: "3"}.items():
            row[index] = value
        flow_path.write_text(json.dumps(flow_payload, ensure_ascii=False), encoding="utf-8")
        taxonomy_path = self.cache_dir / "finmind/TaiwanStockInfo/2026-09-23/market.jsonl"
        taxonomy_rows = [json.loads(line) for line in taxonomy_path.read_text(encoding="utf-8").splitlines()]
        taxonomy_rows[1]["industry_category"] = "測試族群"
        taxonomy_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in taxonomy_rows) + "\n", encoding="utf-8")

        report = build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24")
        amount = next(row for row in report["period_summary"] if row["category"] == "測試族群")["estimated_institutional_net_amount_twd"]

        self.assertEqual(amount, 0.3)

    def test_missing_close_keeps_shares_and_degrades_amount_ranking(self):
        (self.cache_dir / "tpex/daily_close/2026-09-24/market.json").unlink()
        payload = build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24")
        category = payload["daily"][0]["categories"][0]
        self.assertEqual(category["institutional_net_shares"], 2_225_000)
        self.assertEqual(category["missing_price_count"], 1)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["ranking_method"], "net_shares")

    def test_report_does_not_depend_on_how_cache_dir_is_spelled(self):
        absolute = build_sector_flow_report(cache_dir=self.cache_dir.resolve(), start_date="2026-09-24", end_date="2026-09-24")
        relative = build_sector_flow_report(
            cache_dir=Path(os.path.relpath(self.cache_dir)), start_date="2026-09-24", end_date="2026-09-24"
        )
        self.assertEqual(json.dumps(absolute, sort_keys=True), json.dumps(relative, sort_keys=True))
        self.assertEqual(
            absolute["source_status"]["2026-09-24"]["twse_institutional"]["cache_path"],
            "twse/T86/2026-09-24/market.json",
        )

    def test_no_valid_institutional_rows_marks_report_blocked(self):
        empty = Path(self.tmpdir.name) / "empty"
        payload = build_sector_flow_report(cache_dir=empty, start_date="2026-09-24", end_date="2026-09-24")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["daily"], [])

    # ---- helpers for multi-category / degrade tests ----
    def _write_taxonomy_rows(self, pairs):
        types = {"2330": "twse", "6488": "tpex"}
        rows = [{"date": "2000-01-01", "stock_id": sid, "stock_name": sid, "industry_category": cat, "type": types[sid]} for sid, cat in pairs]
        for snapshot in ("2026-09-23", "2026-09-30"):
            path = self.cache_dir / "finmind" / "TaiwanStockInfo" / snapshot / "market.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def _report(self, start="2026-09-24", end="2026-09-24"):
        return build_sector_flow_report(cache_dir=self.cache_dir, start_date=start, end_date=end)

    def _copy_day(self, trading_date):
        compact = trading_date.replace("-", "")
        for fixture, relative in (
            ("twse-t86.json", "twse/T86"), ("twse-mi-index.json", "twse/MI_INDEX"),
            ("tpex-institutional.json", "tpex/institutional"), ("tpex-daily-close.json", "tpex/daily_close"),
        ):
            target = self.cache_dir / relative / trading_date / "market.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((FIXTURES / fixture).read_text(encoding="utf-8").replace("20260924", compact).replace("115/09/24", f"115/09/{trading_date[-2:]}"), encoding="utf-8")

    def _rows(self, report):
        return {row["category"]: row for row in report["period_summary"]}

    def test_multi_category_symbol_counts_in_every_category_in_either_row_order(self):
        results = []
        for pairs in (
            [("2330", "半導體業"), ("2330", "電子工業"), ("6488", "半導體業")],
            [("6488", "半導體業"), ("2330", "電子工業"), ("2330", "半導體業")],
        ):
            self._write_taxonomy_rows(pairs)
            report = self._report()
            results.append(json.dumps(report, sort_keys=True, ensure_ascii=False))
            rows = self._rows(report)
            self.assertEqual(rows["半導體業"]["institutional_net_shares"], 2_225_000)
            self.assertEqual(rows["電子工業"]["institutional_net_shares"], 1_250_000)
            self.assertTrue(rows["電子工業"]["is_broad"])
            self.assertFalse(rows["半導體業"]["is_broad"])
            self.assertTrue(report["category_overlap"])
            self.assertIn("category_overlap_note", report)
        self.assertEqual(results[0], results[1])

    def test_tpex_and_twse_synonym_names_merge_into_one_category(self):
        self._write_taxonomy_rows([("2330", "其他電子業"), ("6488", "其他電子類")])
        report = self._report()
        self.assertEqual([row["category"] for row in report["period_summary"]], ["其他電子業"])
        self.assertEqual(report["period_summary"][0]["institutional_net_shares"], 2_225_000)

    def test_board_labels_and_catch_all_other_are_dropped(self):
        self._write_taxonomy_rows([("2330", "創新板股票"), ("2330", "其他"), ("2330", "半導體業"), ("6488", "其他"), ("6488", "創新版股票")])
        taxonomy = load_taxonomy(self.cache_dir, end_date="2026-10-01")
        self.assertEqual(taxonomy["2330"].categories, ("半導體業",))
        self.assertEqual(taxonomy["6488"].categories, ("其他",))
        self._write_taxonomy_rows([("2330", "半導體業"), ("6488", "創新板股票")])
        report = self._report()
        self.assertEqual(self._rows(report)["未分類"]["institutional_net_shares"], 975_000)
        self.assertLess(report["taxonomy"]["coverage"], 1.0)
        self.assertEqual(report["status"], "degraded")

    def test_trading_day_with_missing_institutional_files_degrades_report(self):
        (self.cache_dir / "twse/T86/2026-09-24/market.json").unlink()
        (self.cache_dir / "tpex/institutional/2026-09-24/market.json").unlink()
        self._copy_day("2026-09-25")
        report = self._report(end="2026-09-25")
        self.assertEqual(report["status"], "degraded")
        self.assertTrue(any("incomplete sources on 2026-09-24" in w and "twse_institutional=missing" in w for w in report["warnings"]), report["warnings"])

    def test_day_with_no_source_is_listed_not_degraded(self):
        self._copy_day("2026-09-25")
        report = self._report(end="2026-09-26")
        self.assertEqual(report["dates_without_data"], ["2026-09-26"])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["warnings"], [])

    def test_day_where_every_source_is_corrupt_degrades_not_no_data(self):
        self._copy_day("2026-09-25")
        for relative in ("twse/T86", "twse/MI_INDEX", "tpex/institutional", "tpex/daily_close"):
            (self.cache_dir / relative / "2026-09-25" / "market.json").write_text("{}", encoding="utf-8")
        report = self._report(end="2026-09-25")
        self.assertEqual(report["dates_without_data"], [])
        self.assertEqual(report["status"], "degraded")
        self.assertTrue(any("incomplete sources on 2026-09-25" in w and "schema_error" in w for w in report["warnings"]), report["warnings"])

    def test_amount_mode_contributors_follow_estimated_amount(self):
        close_path = self.cache_dir / "tpex/daily_close/2026-09-24/market.json"
        payload = json.loads(close_path.read_text(encoding="utf-8"))
        payload["tables"][0]["data"][0][2] = "5000"
        close_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        report = self._report()
        self.assertEqual(report["ranking_method"], "estimated_amount")
        for rows in (report["daily"][0]["categories"], report["period_summary"]):
            self.assertEqual([c["symbol"] for c in rows[0]["top_positive_contributors"]], ["6488", "2330"])

    def test_period_counts_are_distinct_symbols_not_stock_days(self):
        self._copy_day("2026-09-25")
        report = self._report(end="2026-09-30")
        row = self._rows(report)["半導體業"]
        self.assertEqual(row["covered_symbol_count"], 2)
        self.assertEqual(row["missing_price_count"], 0)
        self.assertEqual(report["daily"][0]["categories"][0]["covered_symbol_count"], 2)
        self.assertEqual(len(report["daily"]), 2)
        for day in ("2026-09-24", "2026-09-25"):
            (self.cache_dir / "tpex/daily_close" / day / "market.json").unlink()
        row = self._rows(self._report(end="2026-09-30"))["半導體業"]
        self.assertEqual(row["missing_price_count"], 1)
        self.assertEqual(row["covered_symbol_count"], 1)

    # ---- review fixes: coverage rule, TDCC cache errors, malformed cache ----
    def _extend_day(self, trading_date, n_extra, n_priced):
        names = {"twse/T86": "t86", "twse/MI_INDEX": "mi"}
        t86_path = self.cache_dir / "twse/T86" / trading_date / "market.json"
        mi_path = self.cache_dir / "twse/MI_INDEX" / trading_date / "market.json"
        t86 = json.loads(t86_path.read_text(encoding="utf-8"))
        mi = json.loads(mi_path.read_text(encoding="utf-8"))
        table = next(item for item in mi["tables"] if "每日收盤行情" in item["title"])
        for index in range(n_extra):
            symbol = str(1101 + index)
            t86["data"].append([symbol] + t86["data"][0][1:])
            if index < n_priced:
                table["data"].append([symbol, symbol, "1", "1", "1", "1", "1", "1", "10.0"])
        t86_path.write_text(json.dumps(t86, ensure_ascii=False), encoding="utf-8")
        mi_path.write_text(json.dumps(mi, ensure_ascii=False), encoding="utf-8")

    def _taxonomy_for_extras(self, n):
        rows = [{"stock_id": "2330", "stock_name": "台積電", "industry_category": "半導體業", "type": "twse"}, {"stock_id": "6488", "stock_name": "環球晶", "industry_category": "半導體業", "type": "tpex"}]
        rows += [{"stock_id": str(1101 + i), "stock_name": str(1101 + i), "industry_category": "半導體業", "type": "twse"} for i in range(n)]
        for snapshot in ("2026-09-23", "2026-09-30"):
            (self.cache_dir / "finmind/TaiwanStockInfo" / snapshot / "market.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def test_one_low_coverage_day_degrades_status_even_when_overall_coverage_is_high(self):
        self._copy_day("2026-09-25")
        self._extend_day("2026-09-24", 50, 40)
        self._extend_day("2026-09-25", 100, 100)
        self._taxonomy_for_extras(100)
        report = self._report(end="2026-09-25")
        self.assertGreaterEqual(report["price_coverage"], 0.9)
        self.assertEqual(report["ranking_method"], "net_shares")
        self.assertEqual(report["status"], "degraded")
        self.assertIn("price coverage below 90% on 2026-09-24 (80.8%); rankings use exact net shares", report["warnings"])

    # ---- per-category drill-down ----
    def test_default_report_has_no_category_detail(self):
        self.assertNotIn("category_detail", self._report())
        self.assertNotIn("category_detail", build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", detail_categories=()))

    def test_report_with_detail_categories_only_adds_category_detail(self):
        self._write_taxonomy_rows([("2330", "半導體業"), ("2330", "電子工業"), ("6488", "半導體業")])
        self._copy_day("2026-09-25")
        base = self._report(end="2026-09-25")
        detailed = build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-25", detail_categories=["電子工業", "半導體業"], top=3)
        extra = dict(detailed)
        detail = extra.pop("category_detail")
        self.assertEqual(extra, base)
        self.assertEqual([item["category"] for item in detail], ["電子工業", "半導體業"])
        broad = detail[0]
        self.assertEqual(broad["institutional_net_shares"], self._rows(base)["電子工業"]["institutional_net_shares"])
        self.assertEqual([s["symbol"] for s in broad["top_inflows"]], ["2330"])
        self.assertEqual([row["trading_date"] for row in broad["top_inflows"][0]["daily"]], ["2026-09-24", "2026-09-25"])
        self.assertEqual([g["category"] for g in broad["subcategories"]], ["半導體業"])

    def test_unknown_detail_category_raises_before_any_output(self):
        with self.assertRaises(ValueError) as raised:
            build_sector_flow_report(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", detail_categories=["不存在"])
        self.assertIn("舊半導體", str(raised.exception))  # 2026-09-23 snapshot applies on 09-24

    def test_report_status_is_ok_when_every_day_has_enough_price_coverage(self):
        self.assertEqual(self._report()["status"], "ok")

    def _holdings_payload(self, as_of, rows):
        compact = as_of.replace("-", "")
        return [{"證券代號": symbol, "持股分級": "12", "人數": "1", "股數": str(shares), "占集保庫存數比例%": "1.0", "資料日期": compact} for symbol, shares in rows]

    def _write_holdings(self, as_of, payload):
        path = self.cache_dir / "tdcc/holding_distribution" / as_of / "market.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_report_large_holder_counts_only_listed_symbols_present_in_both_snapshots(self):
        self._write_holdings("2026-09-17", self._holdings_payload("2026-09-17", [("2330", 100), ("6488", 100), ("9999", 5_000_000)]))
        self._write_holdings("2026-09-24", self._holdings_payload("2026-09-24", [("2330", 150), ("9999", 9_000_000), ("1234", 70)]))
        holder = self._report(end="2026-10-01")["large_holder"]
        self.assertEqual(holder["status"], "ok")
        self.assertEqual(sum(row["large_holder_share_delta"] for row in holder["categories"]), 50)
        self.assertEqual(holder["excluded_symbols"], {"outside_listed_universe": 2, "not_in_both_snapshots": 1, "custody_shares_changed": 0})

    def test_corrupt_tdcc_snapshot_in_cache_is_schema_error_not_skipped(self):
        self._write_holdings("2026-09-17", "{not json")
        report = self._report(end="2026-10-01")
        holder = report["large_holder"]
        self.assertEqual(holder["status"], "schema_error")
        self.assertEqual([item["as_of_date"] for item in holder["errors"]], ["2026-09-17"])
        self.assertNotIn("categories", holder)
        self.assertEqual(report["status"], "degraded")
        self.assertTrue(any("2026-09-17" in warning for warning in report["warnings"]))

    def test_old_corrupt_tdcc_snapshot_not_compared_does_not_block_proxy(self):
        self._write_holdings("2026-06-05", "{not json")
        self._write_holdings("2026-09-17", self._holdings_payload("2026-09-17", [("2330", 100)]))
        self._write_holdings("2026-09-24", self._holdings_payload("2026-09-24", [("2330", 150)]))
        holder = self._report(end="2026-10-01")["large_holder"]
        self.assertEqual(holder["status"], "ok")
        self.assertEqual((holder["prior_as_of_date"], holder["latest_as_of_date"]), ("2026-09-17", "2026-09-24"))

    def test_non_utf8_tdcc_snapshot_is_schema_error(self):
        path = self.cache_dir / "tdcc/holding_distribution/2026-09-17/market.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self._report(end="2026-10-01")["large_holder"]["status"], "schema_error")

    def test_corrupt_tdcc_snapshot_after_end_date_or_with_non_date_name_is_ignored(self):
        self._write_holdings("2026-10-09", "{not json")
        self._write_holdings("scratch", "{not json")
        self.assertNotEqual(self._report(end="2026-10-01")["large_holder"]["status"], "schema_error")

    def test_malformed_daily_cache_files_become_schema_error_not_traceback(self):
        for content in (b"[]", b"\xff\xfe\x00"):
            with self.subTest(content=content):
                (self.cache_dir / "twse/T86/2026-09-24/market.json").write_bytes(content)
                report = self._report()
                self.assertEqual(report["source_status"]["2026-09-24"]["twse_institutional"]["state"], "schema_error")
                self.assertEqual(report["status"], "degraded")


class SectorFlowLargeHolderTest(unittest.TestCase):
    def test_two_snapshots_use_levels_12_through_15_only(self):
        holdings = {
            "2026-09-18": [
                HoldingDistributionRow("2026-09-18", "2330", 12, 10, 100_000, 1.0),
                HoldingDistributionRow("2026-09-18", "2330", 17, 100, 1_000_000, 100.0),
            ],
            "2026-09-24": [
                HoldingDistributionRow("2026-09-24", "2330", 12, 11, 150_000, 1.5),
                HoldingDistributionRow("2026-09-24", "2330", 17, 100, 1_005_000, 100.0),
            ],
        }
        taxonomy = {"2330": type("Entry", (), {"categories": ("半導體業",), "name": "台積電"})()}
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy=taxonomy, closes={"2330": 820.0}, end_date="2026-10-01", start_date="2026-09-24", universe={"2330"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"], "holding_change_proxy")
        self.assertEqual(result["categories"][0]["large_holder_share_delta"], 50_000)
        self.assertEqual(result["categories"][0]["sum_stock_percent_point_delta"], 0.5)
        self.assertNotIn("large_holder_percent_delta", result["categories"][0])
        self.assertEqual(result["categories"][0]["estimated_change_twd"], 41_000_000.0)

    def test_one_snapshot_is_explicitly_insufficient(self):
        result = build_large_holder_proxy(holdings_by_date={"2026-09-24": []}, taxonomy={}, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe=set())
        self.assertEqual(result, {"status": "insufficient_data", "reason": "fewer_than_two_eligible_snapshots", "latest_as_of_date": "2026-09-24"})

    def test_snapshot_after_requested_end_date_is_not_used(self):
        result = build_large_holder_proxy(holdings_by_date={"2026-10-02": []}, taxonomy={}, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe=set())
        self.assertEqual(result["latest_as_of_date"], None)

    def test_symbols_outside_universe_and_in_one_snapshot_are_excluded_and_counted(self):
        holdings = {
            "2026-09-17": [HoldingDistributionRow("2026-09-17", "2330", 12, 1, 100, 1.0), HoldingDistributionRow("2026-09-17", "9999", 12, 1, 7_000, 1.0), HoldingDistributionRow("2026-09-17", "1111", 12, 1, 50, 1.0)],
            "2026-09-24": [HoldingDistributionRow("2026-09-24", "2330", 12, 1, 130, 1.5), HoldingDistributionRow("2026-09-24", "9999", 12, 1, 9_000, 1.0), HoldingDistributionRow("2026-09-24", "2222", 12, 1, 60, 1.0)],
        }
        taxonomy = {"2330": type("Entry", (), {"categories": ("半導體業",), "name": "台積電"})()}
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy=taxonomy, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe={"2330", "1111", "2222"})
        self.assertEqual(result["categories"][0]["large_holder_share_delta"], 30)
        self.assertEqual(sum(row["large_holder_share_delta"] for row in result["categories"]), 30)
        self.assertEqual(result["excluded_symbols"], {"outside_listed_universe": 1, "not_in_both_snapshots": 2, "custody_shares_changed": 0})

    def test_stock_whose_custody_total_moved_one_percent_is_left_out_of_that_week(self):
        def rows(day, total_2330, total_2303):
            return [HoldingDistributionRow(day, "2330", 12, 1, 100_000 if day < "2026-09-24" else 190_000, 1.0), HoldingDistributionRow(day, "2330", 17, 1, total_2330, 100.0),
                    HoldingDistributionRow(day, "2303", 12, 1, 100_000 if day < "2026-09-24" else 105_000, 1.0), HoldingDistributionRow(day, "2303", 17, 1, total_2303, 100.0)]
        taxonomy = {s: type("Entry", (), {"categories": ("半導體業",), "name": s})() for s in ("2330", "2303")}
        # 2330: stock dividend +10% (1,000,000 -> 1,100,000) is out; 2303: +0.99% conversion stays in
        holdings = {"2026-09-18": rows("2026-09-18", 1_000_000, 1_000_000), "2026-09-24": rows("2026-09-24", 1_100_000, 1_009_900)}
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy=taxonomy, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe={"2330", "2303"}, include_stocks=True)
        self.assertEqual((result["categories"][0]["large_holder_share_delta"], [s["symbol"] for s in result["stocks"]]), (5_000, ["2303"]))
        self.assertEqual(result["excluded_symbols"]["custody_shares_changed"], 1)
        holdings["2026-09-24"] = rows("2026-09-24", 990_000, 1_000_000)  # a 1% drop (capital reduction) is out too
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy=taxonomy, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe={"2330", "2303"})
        self.assertEqual((result["categories"][0]["large_holder_share_delta"], result["excluded_symbols"]["custody_shares_changed"]), (5_000, 1))

    def test_stale_latest_snapshot_is_insufficient_without_numbers(self):
        holdings = {"2026-08-25": [], "2026-09-01": []}
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy={}, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe=set())
        self.assertEqual(result, {"status": "insufficient_data", "reason": "latest_snapshot_too_old", "latest_as_of_date": "2026-09-01", "prior_as_of_date": "2026-08-25"})

    def test_snapshot_exactly_seven_days_before_start_is_still_fresh(self):
        holdings = {"2026-09-10": [], "2026-09-17": []}
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy={}, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe=set())
        self.assertEqual(result["status"], "ok")

    def test_non_consecutive_snapshots_are_insufficient_without_numbers(self):
        holdings = {"2026-09-03": [], "2026-09-24": []}
        result = build_large_holder_proxy(holdings_by_date=holdings, taxonomy={}, closes={}, end_date="2026-10-01", start_date="2026-09-24", universe=set())
        self.assertEqual(result, {"status": "insufficient_data", "reason": "snapshots_not_consecutive_weeks", "latest_as_of_date": "2026-09-24", "prior_as_of_date": "2026-09-03"})


if __name__ == "__main__":
    unittest.main()
