"""籌碼聚合與 PIT 讀取（FinMind 三大法人/融資券 → app.db）。"""
from datetime import date, timedelta

from src.portfolio.db import init_db, get_db_connection
from src.market_data import chip_sync


def test_aggregate_institutional_buckets_categories():
    rows = [
        {"date": "2026-06-23", "name": "Foreign_Investor", "buy": 1000, "sell": 3000},   # 外資 net -2000
        {"date": "2026-06-23", "name": "Foreign_Dealer_Self", "buy": 0, "sell": 0},      # 外資 0
        {"date": "2026-06-23", "name": "Investment_Trust", "buy": 500, "sell": 100},      # 投信 +400
        {"date": "2026-06-23", "name": "Dealer_self", "buy": 200, "sell": 100},           # 自營 +100
        {"date": "2026-06-23", "name": "Dealer_Hedging", "buy": 50, "sell": 0},           # 自營 +50
        {"date": "2026-06-23", "name": "未知類別", "buy": 999, "sell": 0},                 # 非三大法人→忽略
    ]
    agg = chip_sync.aggregate_institutional(rows)["2026-06-23"]
    assert agg["foreign"] == -2000
    assert agg["trust"] == 400
    assert agg["dealer"] == 150
    assert agg["total"] == -1450  # 未知類別不計入


class _FakeProvider:
    def __init__(self, inst, marg):
        self._inst, self._marg = inst, marg

    def fetch_institutional(self, sym, s, e):
        return self._inst.get(sym, [])

    def fetch_margin(self, sym, s, e):
        return self._marg.get(sym, [])


def test_sync_and_get_chips_pit(tmp_path):
    init_db(str(tmp_path / "t.db"))
    conn = get_db_connection(str(tmp_path / "t.db"))
    inst = {"2330": [
        {"date": "2026-06-22", "name": "Foreign_Investor", "buy": 0, "sell": 1000},
        {"date": "2026-06-23", "name": "Foreign_Investor", "buy": 2000, "sell": 0},
    ]}
    marg = {"2330": [
        {"date": "2026-06-22", "MarginPurchaseTodayBalance": 28223, "ShortSaleTodayBalance": 84},
        {"date": "2026-06-23", "MarginPurchaseTodayBalance": 28039, "ShortSaleTodayBalance": 84},
    ]}
    summary = chip_sync.sync_chips(conn, ["2330"], date(2026, 6, 22), date(2026, 6, 23),
                                   provider=_FakeProvider(inst, marg))
    assert summary == {"institutional_rows": 2, "margin_rows": 2, "symbols": 1}

    # as_of 06-23：看得到兩日法人（由舊到新）+ 最新融資餘額 28039
    c = chip_sync.get_chips(conn, "2330", date(2026, 6, 23))
    assert [r["trade_date"] for r in c["institutional"]] == ["2026-06-22", "2026-06-23"]
    assert c["institutional"][-1]["total_net"] == 2000
    assert c["margin"]["margin_balance"] == 28039  # 最新一日

    # PIT：as_of 06-22 不得看到 06-23 的資料（融資餘額應為 28223）
    c22 = chip_sync.get_chips(conn, "2330", date(2026, 6, 22))
    assert [r["trade_date"] for r in c22["institutional"]] == ["2026-06-22"]
    assert c22["margin"]["margin_balance"] == 28223

    # 無資料的標的回 None
    assert chip_sync.get_chips(conn, "9999", date(2026, 6, 23)) is None

    # finmind_cache：記錄了原始 API 回應（兩 dataset × 1 檔）
    import json
    cache = conn.execute(
        "SELECT dataset, data_id, response_json, row_count FROM finmind_cache ORDER BY dataset").fetchall()
    assert {r["dataset"] for r in cache} == {
        "TaiwanStockInstitutionalInvestorsBuySell", "TaiwanStockMarginPurchaseShortSale"}
    inst_cache = next(r for r in cache if r["dataset"].startswith("TaiwanStockInstitutional"))
    assert inst_cache["data_id"] == "2330"
    assert inst_cache["row_count"] == 2
    assert json.loads(inst_cache["response_json"])[0]["name"] == "Foreign_Investor"  # 原始未聚合
    conn.close()
