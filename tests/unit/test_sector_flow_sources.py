import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.market_data.sector_flow_sources import (
    ProviderNoData,
    ProviderSchemaError,
    UrllibJsonHttpClient,
    build_daily_requests,
    ingest_sector_flow,
    parse_tdcc_holdings,
    parse_tpex_closes,
    parse_tpex_institutional,
    parse_twse_closes,
    parse_twse_institutional,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "sector-flow"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SectorFlowSourceParserTest(unittest.TestCase):
    def test_twse_t86_parses_exact_components_and_filters_non_common(self):
        rows = parse_twse_institutional(load_fixture("twse-t86.json"), "2026-09-24")
        self.assertEqual([row.symbol for row in rows], ["2330"])
        self.assertEqual(rows[0].institutional_net_shares, 1_250_000)

    def test_taiwan_depositary_receipts_are_excluded(self):
        payload = load_fixture("twse-t86.json")
        payload["data"].append(["9103"] + payload["data"][0][1:])
        rows = parse_twse_institutional(payload, "2026-09-24")
        self.assertEqual([row.symbol for row in rows], ["2330"])

    def test_tpex_group_layout_parses_components_and_filters_non_common(self):
        rows = parse_tpex_institutional(load_fixture("tpex-institutional.json"), "2026-09-24")
        self.assertEqual([row.symbol for row in rows], ["6488"])
        self.assertEqual(rows[0].foreign_net_shares, 900_000)
        self.assertEqual(rows[0].dealer_net_shares, -25_000)
        self.assertEqual(rows[0].institutional_net_shares, 975_000)

    def test_close_parsers_find_daily_table_and_close_column(self):
        twse = parse_twse_closes(load_fixture("twse-mi-index.json"), "2026-09-24")
        tpex = parse_tpex_closes(load_fixture("tpex-daily-close.json"), "2026-09-24")
        self.assertEqual([(row.symbol, row.close) for row in twse], [("2330", 820.0)])
        self.assertEqual([(row.symbol, row.close) for row in tpex], [("6488", 450.0)])

    def test_close_parsers_skip_official_no_trade_placeholders(self):
        twse_payload = copy.deepcopy(load_fixture("twse-mi-index.json"))
        twse_payload["tables"][1]["data"].append(["2340", "台亞", "0", "0", "0", "--", "--", "--", "--"])
        tpex_payload = copy.deepcopy(load_fixture("tpex-daily-close.json"))
        tpex_payload["tables"][0]["data"].append(["5209", "新鼎", "----", "---", "----", "----", "----", "0", "0"])

        twse = parse_twse_closes(twse_payload, "2026-09-24")
        tpex = parse_tpex_closes(tpex_payload, "2026-09-24")

        self.assertEqual([row.symbol for row in twse], ["2330"])
        self.assertEqual([row.symbol for row in tpex], ["6488"])

    def test_tpex_parser_ignores_trailing_empty_table_object(self):
        payload = copy.deepcopy(load_fixture("tpex-institutional.json"))
        payload["tables"].append({})

        rows = parse_tpex_institutional(payload, "2026-09-24")

        self.assertEqual([row.symbol for row in rows], ["6488"])

    def test_tpex_empty_tables_are_no_data_but_nonempty_malformed_table_is_schema_error(self):
        empty = {"date": "20260924", "stat": "ok", "tables": [{}, {}]}
        malformed = {"date": "20260924", "stat": "ok", "tables": [{"date": "115/09/24", "fields": ["代號"], "unexpected": []}]}
        with self.assertRaises(ProviderNoData):
            parse_tpex_institutional(empty, "2026-09-24")
        with self.assertRaises(ProviderSchemaError):
            parse_tpex_institutional(malformed, "2026-09-24")

    def test_invalid_numeric_value_is_schema_error_not_zero(self):
        payload = copy.deepcopy(load_fixture("twse-t86.json"))
        payload["data"][0][4] = "--"
        with self.assertRaises(ProviderSchemaError):
            parse_twse_institutional(payload, "2026-09-24")

    def test_component_total_mismatch_is_schema_error(self):
        payload = copy.deepcopy(load_fixture("tpex-institutional.json"))
        payload["tables"][0]["data"][0][23] = "1"
        with self.assertRaises(ProviderSchemaError):
            parse_tpex_institutional(payload, "2026-09-24")

    def test_provider_date_mismatch_is_schema_error(self):
        with self.assertRaises(ProviderSchemaError):
            parse_twse_closes(load_fixture("twse-mi-index.json"), "2026-09-25")

    def test_tdcc_date_and_levels_are_normalized(self):
        rows = parse_tdcc_holdings(load_fixture("tdcc-holding-distribution.json"))
        self.assertEqual(rows[0].as_of_date, "2026-09-24")
        self.assertEqual(rows[0].shares, 400_000)
        self.assertEqual(rows[0].level, 12)

    def test_declared_count_mismatch_is_schema_error_but_absent_or_equal_count_passes(self):
        t86 = load_fixture("twse-t86.json")
        parse_twse_institutional(t86, "2026-09-24")
        t86["total"] = len(t86["data"])
        parse_twse_institutional(t86, "2026-09-24")
        t86["total"] = len(t86["data"]) + 1
        with self.assertRaisesRegex(ProviderSchemaError, "3.*2"):
            parse_twse_institutional(t86, "2026-09-24")
        for name, parser in (("tpex-institutional.json", parse_tpex_institutional), ("tpex-daily-close.json", parse_tpex_closes)):
            payload = load_fixture(name)
            table = payload["tables"][0]
            table["totalCount"] = len(table["data"])
            parser(payload, "2026-09-24")
            table["totalCount"] = len(table["data"]) + 1
            with self.assertRaisesRegex(ProviderSchemaError, "3.*2"):
                parser(payload, "2026-09-24")

    def test_non_mapping_payload_is_schema_error_for_every_daily_parser(self):
        for parser in (parse_twse_institutional, parse_twse_closes, parse_tpex_institutional, parse_tpex_closes):
            with self.subTest(parser=parser.__name__):
                with self.assertRaises(ProviderSchemaError):
                    parser([], "2026-09-24")


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        if "T86" in url:
            return load_fixture("twse-t86.json")
        if "MI_INDEX" in url:
            return load_fixture("twse-mi-index.json")
        if "dailyTrade" in url:
            return load_fixture("tpex-institutional.json")
        if "afterTrading/otc" in url:
            return load_fixture("tpex-daily-close.json")
        return load_fixture("tdcc-holding-distribution.json")


class FakeResponse:
    def __init__(self, body, content_length=None):
        self.body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class SectorFlowIngestionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_endpoint_builders_include_requested_date(self):
        requests = build_daily_requests(self.cache_dir, "2026-09-24")
        self.assertIn("date=20260924", requests[0].url)
        self.assertIn("date=2026%2F09%2F24", requests[2].url)

    def test_existing_successful_cache_is_not_fetched_again(self):
        first_client = FakeHttpClient()
        first = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=first_client)
        second_client = FakeHttpClient()
        second = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=second_client)
        self.assertEqual(first["fetched"], 5)
        self.assertEqual(second["cached"], 5)
        self.assertEqual(len(second_client.calls), 1)
        daily_items = [item for item in second["sources"] if item["provider"] != "tdcc"]
        self.assertTrue(all(item["embedded_date"] == "2026-09-24" for item in daily_items))

    def test_invalid_existing_cache_is_refetched_and_replaced(self):
        path = self.cache_dir / "twse" / "T86" / "2026-09-24" / "market.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"broken":true}\n', encoding="utf-8")
        client = FakeHttpClient()

        summary = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=client)

        item = next(row for row in summary["sources"] if row["dataset"] == "T86")
        self.assertEqual(item["state"], "ok")
        self.assertEqual(item["via"], "network")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["date"], "20260924")

    def test_unreadable_or_list_daily_cache_is_refetched(self):
        for content in (b"[]", b"\xff\xfe\x00"):
            with self.subTest(content=content):
                path = self.cache_dir / "twse" / "T86" / "2026-09-24" / "market.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                summary = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=FakeHttpClient())
                item = next(row for row in summary["sources"] if row["dataset"] == "T86")
                self.assertEqual(item["via"], "network")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["date"], "20260924")

    def test_corrupt_or_mismatched_tdcc_cache_is_repaired_from_fresh_payload(self):
        path = self.cache_dir / "tdcc" / "holding_distribution" / "2026-09-24" / "market.json"
        other_date = json.dumps(load_fixture("tdcc-holding-distribution.json")).replace("20260924", "20260917")
        for content in (b"{not json", b"\xff\xfe\x00", b"[]", other_date.encode()):
            with self.subTest(content=content):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                summary = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=FakeHttpClient())
                item = next(row for row in summary["sources"] if row["provider"] == "tdcc")
                self.assertEqual((item["state"], item["via"], item.get("repaired_cache")), ("ok", "network", True))
                self.assertEqual(parse_tdcc_holdings(json.loads(path.read_text(encoding="utf-8")))[0].as_of_date, "2026-09-24")

    def test_valid_tdcc_cache_stays_cache_without_repair_flag(self):
        ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=FakeHttpClient())
        summary = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=FakeHttpClient())
        item = next(row for row in summary["sources"] if row["provider"] == "tdcc")
        self.assertEqual(item["via"], "cache")
        self.assertNotIn("repaired_cache", item)

    def test_schema_error_does_not_write_cache(self):
        class BrokenClient(FakeHttpClient):
            def get_json(self, url):
                payload = super().get_json(url)
                if "T86" in url:
                    payload["data"][0][18] = "1"
                return payload

        summary = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=BrokenClient())
        path = self.cache_dir / "twse" / "T86" / "2026-09-24" / "market.json"
        self.assertFalse(path.exists())
        self.assertTrue(any(item["state"] == "schema_error" for item in summary["sources"]))

    def test_prior_date_response_is_classified_as_no_data(self):
        class PriorDateClient(FakeHttpClient):
            def get_json(self, url):
                payload = super().get_json(url)
                if "T86" in url or "MI_INDEX" in url:
                    payload["date"] = "20260923"
                return payload

        summary = ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-09-24", end_date="2026-09-24", client=PriorDateClient())
        twse = [item for item in summary["sources"] if item["provider"] == "twse"]
        self.assertEqual([item["state"] for item in twse], ["no_data", "no_data"])
        self.assertEqual(summary["failed"], 0)

    def test_range_is_capped_at_31_calendar_days(self):
        with self.assertRaisesRegex(ValueError, "31 calendar days"):
            ingest_sector_flow(cache_dir=self.cache_dir, start_date="2026-01-01", end_date="2026-02-01", client=FakeHttpClient())

    def test_declared_oversize_response_is_rejected(self):
        response = FakeResponse(b"{}", content_length=101)
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ProviderSchemaError, "size limit"):
                UrllibJsonHttpClient(max_bytes=100).get_json("https://example.invalid")

    def test_requests_are_spaced_by_min_interval(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        client = UrllibJsonHttpClient(min_interval_seconds=3.0, clock=lambda: now[0], sleep=sleep)
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: FakeResponse(b"{}")):
            client.get_json("https://example.invalid/1")
            now[0] += 1.0
            client.get_json("https://example.invalid/2")
            now[0] += 5.0
            client.get_json("https://example.invalid/3")
        self.assertEqual(slept, [2.0])


if __name__ == "__main__":
    unittest.main()
