import unittest

from src.application.reporting.sector_flow import TaxonomyEntry, _aggregate_period, build_category_detail
from src.market_data.sector_flow_sources import InstitutionalFlowRow

D1, D2 = "2026-09-24", "2026-09-25"
BROAD, SEMI, OPTO, FIN = "電子工業", "半導體業", "光電業", "金融保險"


def _row(day, symbol, foreign=0, trust=0, dealer=0):
    return InstitutionalFlowRow(day, "twse", symbol, symbol, foreign, trust, dealer, foreign + trust + dealer)


def _tax(symbol, *categories):
    return TaxonomyEntry(symbol=symbol, name=f"N{symbol}", categories=tuple(sorted(categories)), market="twse", snapshot_date="2026-09-23")


TAXONOMY = {
    "1111": _tax("1111", BROAD, SEMI),
    "2222": _tax("2222", BROAD, SEMI, OPTO),
    "3333": _tax("3333", BROAD),
    "4444": _tax("4444", FIN),
    "5555": _tax("5555", BROAD),
    "6666": _tax("6666", BROAD),
    "7777": _tax("7777", BROAD),
}
DAY_INPUTS = [
    (D1, [_row(D1, "1111", 4000, 1000), _row(D1, "2222", 100), _row(D1, "3333", -3000), _row(D1, "4444", 9999),
          _row(D1, "5555", 500), _row(D1, "6666", -200), _row(D1, "7777", 100)],
     {("twse", "1111"): 100.0, ("twse", "2222"): 1000.0, ("twse", "3333"): 10.0, ("twse", "4444"): 10.0,
      ("twse", "5555"): 2000.0, ("twse", "6666"): 100.0, ("twse", "7777"): 10.0}),
    (D2, [_row(D2, "1111", 1000), _row(D2, "2222", -50), _row(D2, "7777", 100)],
     {("twse", "1111"): 110.0, ("twse", "2222"): 1000.0}),  # 7777 has no close on D2
]


def detail(categories, method="estimated_amount", top=10):
    summary = _aggregate_period(DAY_INPUTS, TAXONOMY, method)
    return build_category_detail(day_inputs=DAY_INPUTS, taxonomy=TAXONOMY, ranking_method=method, period_summary=summary, categories=categories, top=top)


class CategoryDetailTest(unittest.TestCase):
    def test_totals_equal_period_summary_row_exactly(self):
        summary = {row["category"]: row for row in _aggregate_period(DAY_INPUTS, TAXONOMY, "estimated_amount")}
        item = detail([BROAD])[0]
        for field in ("foreign_net_shares", "investment_trust_net_shares", "dealer_net_shares", "institutional_net_shares",
                      "estimated_foreign_net_amount_twd", "estimated_investment_trust_net_amount_twd",
                      "estimated_dealer_net_amount_twd", "estimated_institutional_net_amount_twd"):
            self.assertEqual(item[field], summary[BROAD][field], field)
        self.assertEqual(item["member_count"], 6)
        self.assertTrue(item["is_broad"])
        self.assertEqual(item["top"], 10)

    def test_amount_mode_ranks_by_amount_and_skips_stocks_missing_a_price(self):
        item = detail([BROAD])[0]
        self.assertEqual([s["symbol"] for s in item["top_inflows"]], ["5555", "1111", "2222"])  # 5555: fewer shares, higher price
        self.assertEqual([s["symbol"] for s in item["top_outflows"]], ["3333", "6666"])  # 7777 lacks a D2 price -> skipped
        self.assertEqual(item["top_inflows"][0]["estimated_net_amount_twd"], 1_000_000.0)

    def test_net_shares_mode_ranks_by_shares_and_keeps_unpriced_stocks(self):
        item = detail([BROAD], method="net_shares")[0]
        self.assertEqual([s["symbol"] for s in item["top_inflows"]], ["1111", "5555", "7777", "2222"])
        seven = item["top_inflows"][2]
        self.assertEqual(seven["institutional_net_shares"], 200)
        self.assertIsNone(seven["estimated_net_amount_twd"])

    def test_top_limits_both_lists(self):
        item = detail([BROAD], top=1)[0]
        self.assertEqual(len(item["top_inflows"]), 1)
        self.assertEqual(len(item["top_outflows"]), 1)
        self.assertEqual(item["top"], 1)

    def test_share_of_side_uses_all_members_and_sums_to_100(self):
        full = detail([BROAD])[0]
        self.assertAlmostEqual(sum(s["share_of_side_pct"] for s in full["top_inflows"]), 100.0, places=1)
        self.assertAlmostEqual(sum(s["share_of_side_pct"] for s in full["top_outflows"]), 100.0, places=1)
        self.assertEqual(full["top_inflows"][0]["share_of_side_pct"], 60.24)  # 1,000,000 / 1,660,000
        clipped = detail([BROAD], top=1)[0]
        self.assertEqual(clipped["top_inflows"][0]["share_of_side_pct"], 60.24)  # denominator is not just the top N

    def test_stock_fields_and_daily_list_cover_every_observed_date(self):
        stock = next(s for s in detail([BROAD])[0]["top_inflows"] if s["symbol"] == "1111")
        self.assertEqual((stock["foreign_net_shares"], stock["investment_trust_net_shares"], stock["dealer_net_shares"], stock["institutional_net_shares"]), (5000, 1000, 0, 6000))
        self.assertEqual(stock["daily"], [
            {"trading_date": D1, "institutional_net_shares": 5000, "estimated_net_amount_twd": 500_000.0},
            {"trading_date": D2, "institutional_net_shares": 1000, "estimated_net_amount_twd": 110_000.0},
        ])
        three = next(s for s in detail([BROAD])[0]["top_outflows"] if s["symbol"] == "3333")
        self.assertEqual(three["daily"][1], {"trading_date": D2, "institutional_net_shares": 0, "estimated_net_amount_twd": 0.0})
        seven = next(s for s in detail([BROAD], method="net_shares")[0]["top_inflows"] if s["symbol"] == "7777")
        self.assertEqual(seven["daily"][1], {"trading_date": D2, "institutional_net_shares": 100, "estimated_net_amount_twd": None})

    def test_broad_category_groups_members_by_other_categories(self):
        item = detail([BROAD])[0]
        groups = {g["category"]: g for g in item["subcategories"]}
        self.assertEqual(set(groups), {SEMI, OPTO, "（僅大類）"})
        self.assertEqual(groups["（僅大類）"]["member_count"], 4)
        self.assertEqual(groups[SEMI]["member_count"], 2)
        self.assertEqual(groups[SEMI]["institutional_net_shares"], 6050)
        self.assertEqual(groups[SEMI]["estimated_institutional_net_amount_twd"], 660_000.0)
        self.assertEqual(groups[OPTO]["estimated_institutional_net_amount_twd"], 50_000.0)
        self.assertEqual(groups["（僅大類）"]["estimated_institutional_net_amount_twd"], 951_000.0)  # priced rows only, like period_summary
        self.assertEqual([g["category"] for g in item["subcategories"]], ["（僅大類）", SEMI, OPTO])
        self.assertTrue(item["subcategory_overlap"])
        self.assertEqual(item["subcategory_overlap_count"], 1)  # 2222 sits in 半導體業 and 光電業

    def test_another_broad_category_is_never_a_subcategory(self):
        taxonomy = {"1111": _tax("1111", BROAD, "化學生技醫療", "生技醫療業")}
        day_inputs = [(D1, [_row(D1, "1111", 1000)], {("twse", "1111"): 10.0})]
        summary = _aggregate_period(day_inputs, taxonomy, "estimated_amount")
        item = build_category_detail(day_inputs=day_inputs, taxonomy=taxonomy, ranking_method="estimated_amount", period_summary=summary, categories=[BROAD], top=10)[0]
        self.assertEqual([g["category"] for g in item["subcategories"]], ["生技醫療業"])
        self.assertEqual(item["subcategory_overlap_count"], 0)

    def test_non_broad_category_has_no_subcategory_keys(self):
        item = detail([SEMI])[0]
        self.assertFalse(item["is_broad"])
        for key in ("subcategories", "subcategory_overlap", "subcategory_overlap_count"):
            self.assertNotIn(key, item)
        self.assertEqual(item["member_count"], 2)

    def test_synonym_input_order_and_dedupe(self):
        items = detail(["金融業", SEMI, FIN, SEMI])
        self.assertEqual([i["category"] for i in items], [FIN, SEMI])

    def test_unknown_category_error_lists_available_names(self):
        with self.assertRaises(ValueError) as raised:
            detail(["不存在"])
        message = str(raised.exception)
        for name in (BROAD, SEMI, OPTO, FIN, "不存在"):
            self.assertIn(name, message)


if __name__ == "__main__":
    unittest.main()
