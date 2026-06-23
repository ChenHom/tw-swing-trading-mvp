"""公司行動（除息、配股）調整單元測試。

單位慣例（見記憶 unit-conventions）：
  quantity=股、price/highest_close=元×10000、cash_ledger/cash_balances=整數元。
"""
import pytest
from datetime import date

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection


@pytest.fixture
def clean_db(tmp_path):
    """建立對帳乾淨的測試 DB：以 apply_fill_transaction 建倉，
    使 cash_ledger / cash_balances / fills / position_lots 一致，調整前 reconcile 即 OK。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    conn = get_db_connection(db_path)
    proj = PortfolioProjection(conn)

    # 先存入足額現金（避免 BUY 後負餘額）：以 cash_ledger + cash_balances 一致方式種子
    conn.execute(
        """
        INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency,
            source_type, source_id, occurred_at, idempotency_key, created_at)
        VALUES ('seed-deposit', 'test-acc', 'seed', 'DEPOSIT', 1000000, 'TWD',
            'MANUAL', 'seed', '2026-06-01', 'seed-deposit', datetime('now'))
        """
    )
    conn.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('test-acc', 1000000, 'TWD', '2026-06-01')"
    )
    conn.commit()

    # 以 apply_fill_transaction 建兩筆 BUY（自動處理 cash_ledger/balance/lots 一致）
    # lot 1: 100 股 @ 100 元（price=1000000）
    proj.apply_fill_transaction({
        "fill_id": "f1", "account_id": "test-acc", "run_id": "r1",
        "order_id": "o1", "execution_key": "k1", "symbol": "2330",
        "side": "BUY", "quantity": 100, "price": 1000000,
        "filled_at": "2026-06-10T09:00:00+08:00", "is_long_term": 0,
        "source": "STRATEGY", "strategy_id": "trend_breakout",
    })
    # lot 2: 50 股 @ 99 元（price=990000）
    proj.apply_fill_transaction({
        "fill_id": "f2", "account_id": "test-acc", "run_id": "r1",
        "order_id": "o2", "execution_key": "k2", "symbol": "2330",
        "side": "BUY", "quantity": 50, "price": 990000,
        "filled_at": "2026-06-11T09:00:00+08:00", "is_long_term": 0,
        "source": "STRATEGY", "strategy_id": "trend_breakout",
    })

    # watermark（100 元最高收盤）
    conn.execute(
        """
        INSERT INTO position_high_watermarks
        (account_id, strategy_id, symbol, trade_date, highest_close, created_at)
        VALUES ('test-acc', 'trend_breakout', '2330', '2026-06-12', 1000000, '2026-06-12')
        """
    )
    conn.commit()

    return conn, "test-acc", proj


def test_fixture_is_reconcile_clean(clean_db):
    """前置驗證：fixture 調整前對帳即通過。"""
    conn, account_id, proj = clean_db
    assert proj.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()


def test_cash_dividend_adjustment(clean_db):
    """現金股利：price/watermark 各減 cash_per_share(×10000)，配息以整數元入帳。"""
    conn, account_id, proj = clean_db

    # 每股配 10 元（cash_per_share = 10 × 10000 = 100000）
    action = {
        "action_id": "act-1", "symbol": "2330", "action_type": "CASH_DIVIDEND",
        "ex_date": "2026-06-20", "cash_per_share": 100000,
    }
    cash_before = proj.get_cash_balance(account_id)
    proj.apply_corporate_action(account_id, action)

    cursor = conn.cursor()
    cursor.execute("SELECT lot_id, price FROM position_lots WHERE account_id = ? AND fill_id IN ('f1','f2') ORDER BY lot_id", (account_id,))
    prices = {row["lot_id"]: row["price"] for row in cursor.fetchall()}
    # lot1: 100 元 - 10 元 = 90 元；lot2: 99 元 - 10 元 = 89 元
    assert set(prices.values()) == {900000, 890000}

    # watermark：100 元 - 10 元 = 90 元
    cursor.execute("SELECT highest_close FROM position_high_watermarks WHERE account_id = ?", (account_id,))
    assert cursor.fetchone()["highest_close"] == 900000

    # 配息入帳（整數元）：150 股 × 10 元 = 1500 元（非 1500 萬）
    cash_after = proj.get_cash_balance(account_id)
    assert cash_after - cash_before == 1500

    conn.close()


def test_stock_dividend_adjustment(clean_db):
    """配股：price/watermark 按 1/(1+ratio) 縮，qty 增加，並寫合成 fill。"""
    conn, account_id, proj = clean_db

    # 配股 0.1（每股配 0.1 股）
    action = {
        "action_id": "act-2", "symbol": "2330", "action_type": "STOCK_DIVIDEND",
        "ex_date": "2026-06-21", "stock_ratio": 0.1,
    }
    proj.apply_corporate_action(account_id, action)

    cursor = conn.cursor()
    # 原 lot1 價格 100 / 1.1
    cursor.execute("SELECT price FROM position_lots WHERE fill_id = 'f1'")
    assert cursor.fetchone()["price"] == int(1000000 / 1.1)

    # 新增配股 lot（fill_id 以 STOCK_DIVIDEND 開頭）
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM position_lots WHERE account_id = ? AND fill_id LIKE 'STOCK_DIVIDEND%'",
        (account_id,)
    )
    assert cursor.fetchone()["cnt"] >= 1

    # 合成 fill 存在（維持 reconcile 數量不變式）
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM fills WHERE account_id = ? AND source = 'CORP_ACTION'",
        (account_id,)
    )
    assert cursor.fetchone()["cnt"] >= 1

    conn.close()


def test_corporate_action_idempotent(clean_db):
    """冪等性：套用兩次結果相同。"""
    conn, account_id, proj = clean_db

    action = {
        "action_id": "act-3", "symbol": "2330", "action_type": "CASH_DIVIDEND",
        "ex_date": "2026-06-22", "cash_per_share": 100000,
    }
    proj.apply_corporate_action(account_id, action)
    cash_after_first = proj.get_cash_balance(account_id)

    proj.apply_corporate_action(account_id, action)
    cash_after_second = proj.get_cash_balance(account_id)

    assert cash_after_first == cash_after_second

    conn.close()


def test_reconcile_ok_after_cash_dividend(clean_db):
    """現金股利套用後 reconcile 仍 RECONCILE_OK（核心不變式）。"""
    conn, account_id, proj = clean_db

    action = {
        "action_id": "act-4", "symbol": "2330", "action_type": "CASH_DIVIDEND",
        "ex_date": "2026-06-23", "cash_per_share": 100000,
    }
    proj.apply_corporate_action(account_id, action)

    assert proj.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()


def test_reconcile_ok_after_stock_dividend(clean_db):
    """股票股利套用後 reconcile 仍 RECONCILE_OK（fills↔lots 數量平衡）。"""
    conn, account_id, proj = clean_db

    action = {
        "action_id": "act-5", "symbol": "2330", "action_type": "STOCK_DIVIDEND",
        "ex_date": "2026-06-24", "stock_ratio": 0.1,
    }
    proj.apply_corporate_action(account_id, action)

    assert proj.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()


def test_split_adjustment(clean_db):
    """拆股（1股拆2股，stock_ratio=2.0）：qty 倍增、price 減半，watermark 同步，reconcile 仍 OK。"""
    conn, account_id, proj = clean_db

    action = {
        "action_id": "act-split-1", "symbol": "2330", "action_type": "SPLIT",
        "ex_date": "2026-06-25", "stock_ratio": 2.0,
    }
    proj.apply_corporate_action(account_id, action)

    cursor = conn.cursor()
    cursor.execute("SELECT SUM(quantity) as qty FROM position_lots WHERE account_id = ? AND symbol = '2330'", (account_id,))
    assert cursor.fetchone()["qty"] == (100 + 50) * 2  # 原 150 股 -> 300 股

    cursor.execute("SELECT price FROM position_lots WHERE fill_id = 'f1'")
    assert cursor.fetchone()["price"] == int(1000000 / 2.0)

    cursor.execute("SELECT highest_close FROM position_high_watermarks WHERE account_id = ?", (account_id,))
    assert cursor.fetchone()["highest_close"] == int(1000000 / 2.0)

    assert proj.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()


def test_capital_reduction_loss_offsetting(clean_db):
    """彌補虧損減資（無現金退還，stock_ratio=0.7）：qty 減少、price 增加維持市值，現金不變。"""
    conn, account_id, proj = clean_db

    action = {
        "action_id": "act-cr-1", "symbol": "2330", "action_type": "CAPITAL_REDUCTION",
        "ex_date": "2026-06-25", "stock_ratio": 0.7,
    }
    cash_before = proj.get_cash_balance(account_id)
    proj.apply_corporate_action(account_id, action)

    cursor = conn.cursor()
    cursor.execute("SELECT SUM(quantity) as qty FROM position_lots WHERE account_id = ? AND symbol = '2330'", (account_id,))
    assert cursor.fetchone()["qty"] == int(150 * 0.7)

    cursor.execute("SELECT price FROM position_lots WHERE fill_id = 'f1'")
    assert cursor.fetchone()["price"] == int(1000000 / 0.7)

    assert proj.get_cash_balance(account_id) == cash_before  # 無現金退還
    assert proj.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()


def test_capital_reduction_cash_back(clean_db):
    """現金減資（退還現金，stock_ratio=0.7、cash_per_share=每股退 5 元）：現金入帳 + reconcile OK。"""
    conn, account_id, proj = clean_db

    action = {
        "action_id": "act-cr-2", "symbol": "2330", "action_type": "CAPITAL_REDUCTION",
        "ex_date": "2026-06-25", "stock_ratio": 0.7, "cash_per_share": 50000,
    }
    cash_before = proj.get_cash_balance(account_id)
    proj.apply_corporate_action(account_id, action)

    # 150 股（減資前）× 5 元 = 750 元
    assert proj.get_cash_balance(account_id) - cash_before == 750
    assert proj.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()
