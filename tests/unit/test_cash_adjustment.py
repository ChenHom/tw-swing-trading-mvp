"""現金異動（append-only CASH_ADJUSTMENT）與 rebuild 開帳算法的單元測試。

單位慣例（見記憶 unit-conventions）：cash_ledger/cash_balances=整數元。

涵蓋：
  - account adjust（提領/補入）使可用現金正確增減、reconcile 恆平。
  - 異動為 append-only：原 INITIAL_DEPOSIT 列不被改寫。
  - CASH_ADJUSTMENT 在 rebuild_from_ledger 後存活（開帳改算非 FILL 事件）。
  - 回歸：DIVIDEND 配息在 rebuild 後存活且 reconcile OK（修好潛在 bug）。
"""
import pytest
from datetime import date

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection


@pytest.fixture
def acct(tmp_path):
    """以 ledger.deposit 建一個只有初始入金的乾淨帳戶並 rebuild。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    conn = get_db_connection(db_path)
    ledger = PortfolioLedger(conn)
    proj = PortfolioProjection(conn)
    ledger.deposit("acc-1", "init-1", 500000, "TWD", date(2026, 6, 9))
    proj.rebuild_from_ledger("acc-1")
    return conn, ledger, proj


def _initial_deposit_rows(conn, account_id):
    return conn.execute(
        "SELECT ledger_id, amount FROM cash_ledger WHERE account_id = ? AND event_type = 'INITIAL_DEPOSIT'",
        (account_id,)
    ).fetchall()


def test_withdraw_reduces_cash_and_reconciles(acct):
    conn, ledger, proj = acct
    assert proj.get_cash_balance("acc-1") == 500000

    ledger.adjust_cash("acc-1", "adj-1", -20000, "TWD", date(2026, 6, 16), memo="提領測試")
    proj.rebuild_from_ledger("acc-1")

    assert proj.get_cash_balance("acc-1") == 480000
    assert proj.reconcile("acc-1")["status"] == "RECONCILE_OK"


def test_deposit_increases_cash_and_reconciles(acct):
    conn, ledger, proj = acct
    ledger.adjust_cash("acc-1", "adj-2", 30000, "TWD", date(2026, 6, 16), memo="補入")
    proj.rebuild_from_ledger("acc-1")

    assert proj.get_cash_balance("acc-1") == 530000
    assert proj.reconcile("acc-1")["status"] == "RECONCILE_OK"


def test_adjustment_is_append_only(acct):
    """異動不得改寫既有 INITIAL_DEPOSIT 列（append-only）。"""
    conn, ledger, proj = acct
    before = _initial_deposit_rows(conn, "acc-1")
    assert len(before) == 1 and before[0]["amount"] == 500000

    ledger.adjust_cash("acc-1", "adj-3", -20000, "TWD", date(2026, 6, 16), memo="提領")
    proj.rebuild_from_ledger("acc-1")

    after = _initial_deposit_rows(conn, "acc-1")
    assert len(after) == 1
    assert after[0]["ledger_id"] == before[0]["ledger_id"]
    assert after[0]["amount"] == 500000  # 原始入金未被動到

    # memo 有落檔
    memo = conn.execute(
        "SELECT memo FROM cash_ledger WHERE account_id = ? AND event_type = 'CASH_ADJUSTMENT'",
        ("acc-1",)
    ).fetchone()["memo"]
    assert memo == "提領"


def test_adjustment_survives_rebuild(acct):
    """CASH_ADJUSTMENT 在 rebuild 後不消失（開帳改算非 FILL 事件）。"""
    conn, ledger, proj = acct
    ledger.adjust_cash("acc-1", "adj-4", -20000, "TWD", date(2026, 6, 16), memo="提領")
    proj.rebuild_from_ledger("acc-1")
    assert proj.get_cash_balance("acc-1") == 480000

    # 再 rebuild 一次，餘額不得改變、reconcile 仍平
    proj.rebuild_from_ledger("acc-1")
    assert proj.get_cash_balance("acc-1") == 480000
    assert proj.reconcile("acc-1")["status"] == "RECONCILE_OK"


def test_dividend_survives_rebuild(tmp_path):
    """回歸：現金股利在 rebuild 後仍計入餘額、reconcile OK。

    修正前 rebuild 開帳只認 INITIAL_DEPOSIT，會把 DIVIDEND 從餘額丟掉並打破對帳。
    """
    db_path = str(tmp_path / "div.db")
    init_db(db_path)
    conn = get_db_connection(db_path)
    proj = PortfolioProjection(conn)

    # 種子現金 + 一筆持倉（仿 test_corporate_actions 的 clean_db）
    conn.execute(
        """
        INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency,
            source_type, source_id, occurred_at, idempotency_key, created_at)
        VALUES ('seed', 'acc-d', 'seed', 'DEPOSIT', 1000000, 'TWD', 'MANUAL', 'seed',
            '2026-06-01', 'seed', datetime('now'))
        """
    )
    conn.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('acc-d', 1000000, 'TWD', '2026-06-01')"
    )
    conn.commit()
    proj.apply_fill_transaction({
        "fill_id": "f1", "account_id": "acc-d", "run_id": "r1", "order_id": "o1",
        "execution_key": "k1", "symbol": "2330", "side": "BUY", "quantity": 100,
        "price": 1000000, "filled_at": "2026-06-10T09:00:00+08:00", "is_long_term": 0,
        "source": "STRATEGY", "strategy_id": "trend_breakout",
    })

    proj.apply_corporate_action("acc-d", {
        "action_id": "act-1", "symbol": "2330", "action_type": "CASH_DIVIDEND",
        "ex_date": "2026-06-22", "cash_per_share": 100000,  # 每股 10 元（×10000）
    })
    cash_with_dividend = proj.get_cash_balance("acc-d")
    assert proj.reconcile("acc-d")["status"] == "RECONCILE_OK"

    # rebuild 後配息不得消失、對帳仍平
    proj.rebuild_from_ledger("acc-d")
    assert proj.get_cash_balance("acc-d") == cash_with_dividend
    assert proj.reconcile("acc-d")["status"] == "RECONCILE_OK"
