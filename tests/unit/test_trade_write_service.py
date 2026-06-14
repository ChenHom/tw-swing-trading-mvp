"""寫入側 service 直測（不經 CLI argparse / print）。

驗證 trade_write.record_fill / reject_signal / un_reject_signal 的純資料行為：
落正確 bucket、回傳 monitor_status 枚舉、驗證錯誤拋 TradeWriteError。
"""
import pytest

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.application.services import trade_write


@pytest.fixture
def conn(tmp_path):
    db_file = tmp_path / "trade_write.db"
    init_db(str(db_file))
    c = get_db_connection(str(db_file))
    yield c
    c.close()


def _insert_signal(conn, signal_id, *, symbol="2330", action="BUY", user_override=None):
    conn.execute(
        """
        INSERT INTO signal_items
            (item_id, bundle_id, signal_id, symbol, action, reference_price,
             reason_code, created_at, user_override)
        VALUES (?, 'b1', ?, ?, ?, 5000000, 'TEST', '2026-06-12T00:00:00', ?)
        """,
        (f"item-{signal_id}", signal_id, symbol, action, user_override),
    )
    conn.commit()


# --- record_fill -----------------------------------------------------------

def test_record_fill_attributes_to_strategy_and_monitored(conn):
    result = trade_write.record_fill(
        conn, account_id="acc-strat", symbol="2317", side="BUY",
        quantity=100, price=50.0, strategy_id="trend_breakout",
        exit_strategy_ids={"trend_breakout"},
    )
    assert result["strategy_id"] == "trend_breakout"
    assert result["monitor_status"] == "monitored"
    assert result["trade_value"] == 5000  # 100 股 × 50 元
    assert result["tax"] == 0  # BUY 無交易稅

    # fill / lot 落在 trend_breakout bucket。
    fill = conn.execute(
        "SELECT strategy_id FROM fills WHERE account_id = 'acc-strat'"
    ).fetchone()
    assert fill["strategy_id"] == "trend_breakout"
    proj = PortfolioProjection(conn)
    positions = proj.get_strategy_positions("acc-strat", include_long_term=False)
    assert ("trend_breakout", "2317") in positions


def test_record_fill_default_strategy_is_manual_excluded(conn):
    result = trade_write.record_fill(
        conn, account_id="acc-def", symbol="2330", side="BUY",
        quantity=100, price=50.0,  # strategy_id 省略
    )
    assert result["strategy_id"] == MANUAL_STRATEGY_ID
    assert result["monitor_status"] == "manual_excluded"
    lot = conn.execute(
        "SELECT strategy_id FROM position_lots WHERE account_id = 'acc-def'"
    ).fetchone()
    assert lot["strategy_id"] == "MANUAL"


def test_record_fill_strategy_without_exit_block_not_monitored(conn):
    # 傳入空集合（呼叫端已成功載入、但該策略無 exit 區塊）。
    result = trade_write.record_fill(
        conn, account_id="acc-noexit", symbol="2317", side="BUY",
        quantity=100, price=50.0, strategy_id="trend_pullback",
        exit_strategy_ids=set(),
    )
    assert result["monitor_status"] == "not_monitored"


def test_record_fill_none_exit_ids_is_indeterminate(conn):
    # exit_strategy_ids=None 表示呼叫端無法判定（載入失敗）。
    result = trade_write.record_fill(
        conn, account_id="acc-ind", symbol="2317", side="BUY",
        quantity=100, price=50.0, strategy_id="trend_breakout",
        exit_strategy_ids=None,
    )
    assert result["monitor_status"] == "indeterminate"


def test_record_fill_long_term_excluded_regardless_of_strategy(conn):
    # 長期持有優先：即使指定具 exit 區塊的策略，仍結構性排除。
    result = trade_write.record_fill(
        conn, account_id="acc-lt", symbol="2317", side="BUY",
        quantity=100, price=50.0, strategy_id="trend_breakout",
        is_long_term=True, exit_strategy_ids={"trend_breakout"},
    )
    assert result["monitor_status"] == "long_term_excluded"
    lot = conn.execute(
        "SELECT is_long_term FROM position_lots WHERE account_id = 'acc-lt'"
    ).fetchone()
    assert lot["is_long_term"] == 1


def test_record_fill_unknown_strategy_raises_and_writes_nothing(conn):
    with pytest.raises(trade_write.TradeWriteError) as ei:
        trade_write.record_fill(
            conn, account_id="acc-bogus", symbol="2317", side="BUY",
            quantity=100, price=50.0, strategy_id="not_a_real_strategy",
        )
    assert ei.value.code == "UNKNOWN_STRATEGY"
    assert "未知策略" in ei.value.message
    # 驗證失敗於寫入前：無任何 fill。
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM fills WHERE account_id = 'acc-bogus'"
    ).fetchone()
    assert cnt["c"] == 0


def test_record_fill_sell_computes_tax(conn):
    # 先建倉再賣，確認 SELL 計交易稅。
    trade_write.record_fill(
        conn, account_id="acc-sell", symbol="2317", side="BUY",
        quantity=100, price=50.0, strategy_id="trend_breakout",
        exit_strategy_ids={"trend_breakout"},
    )
    result = trade_write.record_fill(
        conn, account_id="acc-sell", symbol="2317", side="SELL",
        quantity=100, price=50.0, strategy_id="trend_breakout",
        exit_strategy_ids={"trend_breakout"},
    )
    assert result["side"] == "SELL"
    assert result["tax"] == int(round(result["trade_value"] * 0.003))


# --- reject_signal / un_reject_signal --------------------------------------

def test_reject_signal_marks_rejected(conn):
    _insert_signal(conn, "sig-1", symbol="2330", action="BUY")
    result = trade_write.reject_signal(conn, signal_id="sig-1", reason="估值過高")
    assert result["status"] == "rejected"
    assert result["reason"] == "估值過高"
    row = conn.execute(
        "SELECT user_override, override_reason FROM signal_items WHERE signal_id = 'sig-1'"
    ).fetchone()
    assert row["user_override"] == "REJECTED"
    assert row["override_reason"] == "估值過高"


def test_reject_signal_default_reason(conn):
    _insert_signal(conn, "sig-2")
    result = trade_write.reject_signal(conn, signal_id="sig-2")  # reason 省略
    assert result["reason"] == "手動拒絕"


def test_reject_signal_not_found_raises(conn):
    with pytest.raises(trade_write.TradeWriteError) as ei:
        trade_write.reject_signal(conn, signal_id="nope")
    assert ei.value.code == "SIGNAL_NOT_FOUND"


def test_reject_signal_already_rejected_is_noop(conn):
    _insert_signal(conn, "sig-3", user_override="REJECTED")
    result = trade_write.reject_signal(conn, signal_id="sig-3")
    assert result["status"] == "already_rejected"


def test_un_reject_signal_restores(conn):
    _insert_signal(conn, "sig-4", user_override="REJECTED")
    result = trade_write.un_reject_signal(conn, signal_id="sig-4")
    assert result["status"] == "restored"
    row = conn.execute(
        "SELECT user_override FROM signal_items WHERE signal_id = 'sig-4'"
    ).fetchone()
    assert row["user_override"] is None


def test_un_reject_signal_not_rejected_is_noop(conn):
    _insert_signal(conn, "sig-5", user_override=None)
    result = trade_write.un_reject_signal(conn, signal_id="sig-5")
    assert result["status"] == "not_rejected"
    assert result["user_override"] is None


def test_un_reject_signal_not_found_raises(conn):
    with pytest.raises(trade_write.TradeWriteError) as ei:
        trade_write.un_reject_signal(conn, signal_id="ghost")
    assert ei.value.code == "SIGNAL_NOT_FOUND"
