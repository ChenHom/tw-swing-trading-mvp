import pytest
from datetime import date
from unittest.mock import MagicMock
from src.portfolio.db import init_db, get_db_connection
from src.cli import cmd_signal_list, cmd_simulation_reset, cmd_trade_plan, cmd_trade_record_fill, cmd_account_adjust_cash, cmd_report_pnl, cmd_portfolio_reconcile, resolve_account_id

class MockArgs:
    def __init__(self, date_str=None, account=None):
        self.date = date_str
        self.account = account


def test_cmd_portfolio_reconcile_reports_success_on_ok(temp_db_path, monkeypatch, capsys):
    """回歸：reconcile() 成功回傳 {"status":"RECONCILE_OK"}（非空 dict），
    CLI 不可再用 `if not errors` 而誤報失敗。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    # 空且一致的帳戶（ledger 總額 0 == balance 0）→ RECONCILE_OK
    args = MockArgs(account="recon-acct")
    cmd_portfolio_reconcile(args)  # 不應 SystemExit

    captured = capsys.readouterr()
    assert "Reconciliation successful" in captured.out
    assert "failed" not in captured.out.lower()

@pytest.fixture
def temp_db_path(tmp_path):
    db_file = tmp_path / "test_cli.db"
    init_db(str(db_file))
    return str(db_file)

def test_cmd_signal_list_empty(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # Run command with no signals
    args = MockArgs(date_str=None)
    cmd_signal_list(args)
    
    captured = capsys.readouterr()
    assert "資料庫中無任何訊號紀錄。" in captured.out

def test_cmd_signal_list_with_data(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # Insert mock signal data
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("b-1", "r-1", "app-1", "trend_pullback", "1.0", "hash-1", "2026-06-10", "2026-06-11", "2026-06-10")
    )
    cursor.execute(
        """
        INSERT INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("i-1", "b-1", "s-1", "2330", "BUY", 1025000, "PULLBACK")
    )
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("b-2", "r-2", "app-1", "trend_pullback", "1.0", "hash-1", "2026-06-11", "2026-06-12", "2026-06-11")
    )
    cursor.execute(
        """
        INSERT INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("i-2", "b-2", "s-2", "2317", "SELL", 1500000, "EXIT")
    )
    conn.commit()
    conn.close()
    
    # 1. Test list all signals
    args = MockArgs(date_str=None)
    cmd_signal_list(args)
    
    captured = capsys.readouterr()
    assert "訊號日期" in captured.out
    assert "名稱" in captured.out
    assert "台積電" in captured.out
    assert "鴻海" in captured.out
    assert "2330" in captured.out
    assert "2317" in captured.out
    assert "102.50" in captured.out
    assert "150.00" in captured.out
    assert "共計: 2 筆訊號。" in captured.out
    
    # 2. Test filter by date
    args_date = MockArgs(date_str="2026-06-10")
    cmd_signal_list(args_date)
    
    captured_date = capsys.readouterr()
    assert "2330" in captured_date.out
    assert "台積電" in captured_date.out
    assert "2317" not in captured_date.out
    assert "鴻海" not in captured_date.out
    assert "共計: 1 筆訊號。" in captured_date.out

def test_cmd_simulation_reset(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # 1. Insert dummy records for 2026-06-10
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    
    # daily_runs
    cursor.execute(
        """
        INSERT INTO daily_runs (
            run_id, run_date, account_id, strategy_id, status, market_sync_status,
            execution_status, signal_generation_status, report_status, started_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        ("r-1", "2026-06-10", "acc-1", "trend_pullback", "COMPLETED", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS")
    )
    
    # signal_bundles
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("b-1", "r-1", "app-1", "trend_pullback", "1.0", "hash-1", "2026-06-10", "2026-06-11", "2026-06-10")
    )
    
    # signal_items
    cursor.execute(
        """
        INSERT INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("i-1", "b-1", "s-1", "2330", "BUY", 1025000, "PULLBACK")
    )
    
    conn.commit()
    
    # Verify records exist
    cursor.execute("SELECT count(*) FROM daily_runs WHERE run_date = '2026-06-10'")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT count(*) FROM signal_bundles WHERE signal_date = '2026-06-10'")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT count(*) FROM signal_items WHERE bundle_id = 'b-1'")
    assert cursor.fetchone()[0] == 1
    
    # 2. Run reset command
    args = MockArgs(date_str="2026-06-10")
    cmd_simulation_reset(args)
    
    captured = capsys.readouterr()
    assert "Successfully reset database records for 2026-06-10" in captured.out
    
    # 3. Verify records are deleted
    cursor.execute("SELECT count(*) FROM daily_runs WHERE run_date = '2026-06-10'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM signal_bundles WHERE signal_date = '2026-06-10'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM signal_items WHERE bundle_id = 'b-1'")
    assert cursor.fetchone()[0] == 0
    
    conn.close()

def test_cmd_trade_plan(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # 1. Setup account cash
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES (?, ?, ?, datetime('now'))",
        ("acc-test", 100000, "TWD")
    )
    
    # signal_bundles & signal_items
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("b-test", "r-test", "app-test", "trend_pullback", "1.0", "hash-test", "2026-06-10", "2026-06-11", "2026-06-10")
    )
    cursor.execute(
        """
        INSERT INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("i-test", "b-test", "s-test", "2330", "BUY", 1000000, "PULLBACK") # ref price: 100.0 (1,000,000 scaled)
    )
    conn.commit()
    conn.close()
    
    # Mock args for trade plan
    class MockTradePlanArgs:
        def __init__(self):
            self.bundle = "2026-06-10"
            self.account = "acc-test"
            
    cmd_trade_plan(MockTradePlanArgs())
    
    captured = capsys.readouterr()
    assert "代號" in captured.out
    assert "台積電" in captured.out
    # Budget for dummy/test is 35,000 TWD.
    # 35,000 / 100 = 350 shares
    assert "350" in captured.out
    assert "股" in captured.out
    assert "35,000" in captured.out
    assert "成功 (待執行)" in captured.out

def test_cmd_trade_record_fill(temp_db_path, tmp_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    mock_settings.config_dir = tmp_path
    
    # Create dummy universe.yaml in tmp_path
    universe_file = tmp_path / "universe.yaml"
    with open(universe_file, "w", encoding="utf-8") as f:
        f.write("symbols:\n  - code: \"2330\"\n    exchange: \"TSE\"\n    instrument_type: \"STOCK\"\n")
        
    class MockSymbol:
        def __init__(self, code):
            self.code = code
            
    mock_settings.universe.symbols = [MockSymbol("2330")]
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # Mock args for record-fill
    class MockRecordFillArgs:
        def __init__(self):
            self.symbol = "2317"
            self.side = "BUY"
            self.quantity = 100
            self.price = 150.0
            self.account = "acc-test"
            
    cmd_trade_record_fill(MockRecordFillArgs())
    
    captured = capsys.readouterr()
    assert "成功錄入成交資料" in captured.out
    assert "acc-test" in captured.out
    assert "2317" in captured.out
    assert "100 股" in captured.out
    assert "150.00" in captured.out
    # Verify universe.yaml was NOT modified
    with open(universe_file, "r", encoding="utf-8") as f:
        yaml_content = f.read()
    assert "2317" not in yaml_content
    
    # Verify DB contains the recorded fill
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, side, quantity, price FROM fills WHERE account_id = 'acc-test'")
    row = cursor.fetchone()
    assert row is not None
    assert row["symbol"] == "2317"
    assert row["side"] == "BUY"
    assert row["quantity"] == 100
    assert row["price"] == 1500000
    
    # Verify projection positions updated
    cursor.execute("SELECT symbol, quantity, price FROM position_lots WHERE account_id = 'acc-test'")
    pos_row = cursor.fetchone()
    assert pos_row is not None
    assert pos_row["symbol"] == "2317"
    assert pos_row["quantity"] == 100
    assert pos_row["price"] == 1500000
    
    # Verify universe.yaml content was NOT updated
    import yaml
    with open(universe_file, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)
    codes = [s["code"] for s in content.get("symbols", [])]
    assert "2317" not in codes
    
    conn.close()

def test_cmd_account_adjust_cash(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # 1. Initialize account with 300,000 cash first
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO cash_ledger (
            ledger_id, account_id, run_id, event_type, amount, currency,
            source_type, source_id, occurred_at, idempotency_key, created_at
        ) VALUES (?, ?, ?, 'INITIAL_DEPOSIT', ?, ?, 'SYSTEM', 'DEPOSIT', ?, ?, datetime('now'))
        """,
        ("led-1", "acc-adjust-test", "run-1", 300000, "TWD", "2026-06-10T00:00:00+08:00", "dep-1")
    )
    cursor.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES (?, ?, ?, datetime('now'))",
        ("acc-adjust-test", 300000, "TWD")
    )
    conn.commit()
    
    # Verify DB state
    cursor.execute("SELECT SUM(amount) FROM cash_ledger WHERE account_id = 'acc-adjust-test'")
    assert cursor.fetchone()[0] == 300000
    
    # 2. Call adjust-cash to reset it to 500,000 cash
    class MockAdjustCashArgs:
        def __init__(self):
            self.account = "acc-adjust-test"
            self.amount = 500000
            
    cmd_account_adjust_cash(MockAdjustCashArgs())
    
    captured = capsys.readouterr()
    assert "成功將帳戶 'acc-adjust-test' 的初始金調整為：500,000 TWD" in captured.out
    
    # 3. Verify cash_ledger only has one INITIAL_DEPOSIT for 500,000 cash
    cursor.execute("SELECT count(*), SUM(amount) FROM cash_ledger WHERE account_id = 'acc-adjust-test' AND event_type = 'INITIAL_DEPOSIT'")
    cnt_row = cursor.fetchone()
    assert cnt_row[0] == 1
    assert cnt_row[1] == 500000
    
    # Verify cash_balances is updated to 500000
    cursor.execute("SELECT balance FROM cash_balances WHERE account_id = 'acc-adjust-test'")
    assert cursor.fetchone()[0] == 500000
    
    conn.close()

def test_cmd_report_pnl(temp_db_path, monkeypatch, capsys):
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    # Initialize database and account
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    # Insert cash
    cursor.execute("INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('acc-pnl-test', 100000, 'TWD', datetime('now'))")
    # Insert a position lot
    cursor.execute(
        """
        INSERT INTO position_lots (
            lot_id, account_id, symbol, quantity, price, acquired_at, fill_id, created_at
        ) VALUES ('lot-1', 'acc-pnl-test', '2330', 100, 10000000, '2026-06-10T09:00:00', 'fill-1', datetime('now'))
        """
    )
    # Insert a daily bar for 2330 close at 1050.0 (10500000 scaled)
    cursor.execute(
        """
        INSERT INTO market_bars (
            symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at
        ) VALUES ('2330', 'TSE', 'STOCK', '2026-06-10', 10000000, 10600000, 9900000, 10500000, 100, 10000, 'test', 'Asia/Taipei', 1, 'now', 'chk', datetime('now'), datetime('now'))
        """
    )
    conn.commit()
    conn.close()
    
    class MockPnLArgs:
        def __init__(self):
            self.account = "acc-pnl-test"
            self.date = "2026-06-10"
            
    cmd_report_pnl(MockPnLArgs())
    
    captured = capsys.readouterr()
    assert "損益報告" in captured.out
    assert "可用現金：100,000 TWD" in captured.out
    assert "部位價值：105,000 TWD" in captured.out
    assert "總資產淨值：205,000 TWD" in captured.out
    assert "2330 台積電: 100 股 @ 均價 1000.00 (現價: 1050.00) - 價值: 105,000 TWD" in captured.out

def test_resolve_account_id(temp_db_path, capsys):
    conn = get_db_connection(temp_db_path)
    
    # Case 1: Specified account explicitly
    assert resolve_account_id(conn, "custom-acc") == "custom-acc"
    
    # Case 2: No specified account, DB has 1 account
    conn.execute("INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('only-one-acc', 100, 'TWD', datetime('now'))")
    conn.commit()
    assert resolve_account_id(conn, None) == "only-one-acc"
    
    # Case 3: No specified account, DB has multiple accounts
    conn.execute("INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('second-acc', 200, 'TWD', datetime('now'))")
    conn.commit()
    with pytest.raises(SystemExit):
        resolve_account_id(conn, None)
        
    captured = capsys.readouterr()
    assert "偵測到資料庫中有多個帳戶" in captured.out
    
    conn.close()


def test_resolve_account_id_non_interactive(temp_db_path, monkeypatch, capsys):
    """Non-interactive environment must require explicit --account."""
    import sys
    conn = get_db_connection(temp_db_path)
    conn.execute("INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('acc-only', 100, 'TWD', datetime('now'))")
    conn.commit()

    # Simulate non-interactive (no tty) but NOT pytest → should exit
    monkeypatch.setattr("sys.stdin", open("/dev/null"))  # isatty() → False
    pytest_mod = sys.modules.pop("pytest", None)
    try:
        with pytest.raises(SystemExit):
            resolve_account_id(conn, None)
        captured = capsys.readouterr()
        assert "非互動式環境" in captured.out
    finally:
        if pytest_mod is not None:
            sys.modules["pytest"] = pytest_mod
    conn.close()


def test_record_fill_sets_source_manual_import(temp_db_path, monkeypatch, capsys):
    """record-fill should persist source = 'MANUAL_IMPORT' in the fills table."""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    class MockRecordFillArgs:
        symbol = "2330"
        side = "BUY"
        quantity = 10
        price = 100.0
        account = "acc-src-test"
        long_term = False

    cmd_trade_record_fill(MockRecordFillArgs())

    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT source FROM fills WHERE account_id = 'acc-src-test'")
    row = cursor.fetchone()
    assert row is not None
    assert row["source"] == "MANUAL_IMPORT"
    conn.close()


def test_record_fill_default_strategy_is_manual(temp_db_path, monkeypatch, capsys):
    """未指定 --strategy-id 時應沿用舊行為，歸 MANUAL 且註明排除於監控。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    class MockRecordFillArgs:
        symbol = "2330"
        side = "BUY"
        quantity = 10
        price = 100.0
        account = "acc-manual-default"
        long_term = False
        strategy_id = None

    cmd_trade_record_fill(MockRecordFillArgs())

    captured = capsys.readouterr()
    assert "策略歸屬：MANUAL" in captured.out
    assert "結構性排除於 risk_exit 監控" in captured.out

    conn = get_db_connection(temp_db_path)
    row = conn.execute(
        "SELECT strategy_id FROM fills WHERE account_id = 'acc-manual-default'"
    ).fetchone()
    assert row["strategy_id"] == "MANUAL"
    lot = conn.execute(
        "SELECT strategy_id FROM position_lots WHERE account_id = 'acc-manual-default'"
    ).fetchone()
    assert lot["strategy_id"] == "MANUAL"
    conn.close()


def test_record_fill_attributes_to_strategy_and_is_monitored(temp_db_path, monkeypatch, capsys):
    """指定一個具 exit 區塊的策略：fill / lot 落在該 bucket，且提示已納入監控。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    # 隔離 YAML 載入：聲明 trend_breakout 具 exit 區塊（受監控）。
    monkeypatch.setattr(
        "src.cli.common.strategy_registry.load_exit_managed_definitions",
        lambda settings: {"trend_breakout": object()},
    )

    class MockRecordFillArgs:
        symbol = "2317"
        side = "BUY"
        quantity = 100
        price = 50.0
        account = "acc-strat"
        long_term = False
        strategy_id = "trend_breakout"

    cmd_trade_record_fill(MockRecordFillArgs())

    captured = capsys.readouterr()
    assert "策略歸屬：trend_breakout" in captured.out
    assert "已納入 risk_exit 停損監控" in captured.out

    conn = get_db_connection(temp_db_path)
    fill = conn.execute(
        "SELECT strategy_id FROM fills WHERE account_id = 'acc-strat'"
    ).fetchone()
    assert fill["strategy_id"] == "trend_breakout"
    lot = conn.execute(
        "SELECT strategy_id FROM position_lots WHERE account_id = 'acc-strat'"
    ).fetchone()
    assert lot["strategy_id"] == "trend_breakout"

    # get_strategy_positions（非長期）應將其視為受監控的策略部位。
    from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
    proj = PortfolioProjection(conn)
    positions = proj.get_strategy_positions("acc-strat", include_long_term=False)
    assert ("trend_breakout", "2317") in positions
    assert all(sid != MANUAL_STRATEGY_ID for (sid, _s) in positions)
    conn.close()


def test_record_fill_strategy_without_exit_block_not_monitored(temp_db_path, monkeypatch, capsys):
    """歸入已登錄但無 exit 區塊的策略：仍寫入該 bucket，但提示不受監控。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    monkeypatch.setattr(
        "src.cli.common.strategy_registry.load_exit_managed_definitions",
        lambda settings: {},  # 無任何受監控策略
    )

    class MockRecordFillArgs:
        symbol = "2317"
        side = "BUY"
        quantity = 100
        price = 50.0
        account = "acc-noexit"
        long_term = False
        strategy_id = "trend_pullback"

    cmd_trade_record_fill(MockRecordFillArgs())

    captured = capsys.readouterr()
    assert "策略歸屬：trend_pullback" in captured.out
    assert "此策略無 exit 區塊，不受 risk_exit 監控" in captured.out


def test_record_fill_long_term_with_strategy_excluded(temp_db_path, monkeypatch, capsys):
    """長期持有即使指定策略亦結構性排除於監控（且不觸發 exit 定義查詢）。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    def _boom(settings):
        raise AssertionError("長期持有路徑不應查詢 exit 定義")

    monkeypatch.setattr(
        "src.cli.common.strategy_registry.load_exit_managed_definitions", _boom
    )

    class MockRecordFillArgs:
        symbol = "2317"
        side = "BUY"
        quantity = 100
        price = 50.0
        account = "acc-lt-strat"
        long_term = True
        strategy_id = "trend_breakout"

    cmd_trade_record_fill(MockRecordFillArgs())

    captured = capsys.readouterr()
    assert "策略歸屬：trend_breakout" in captured.out
    assert "長期持有，結構性排除於 risk_exit 監控" in captured.out

    conn = get_db_connection(temp_db_path)
    lot = conn.execute(
        "SELECT strategy_id, is_long_term FROM position_lots WHERE account_id = 'acc-lt-strat'"
    ).fetchone()
    assert lot["strategy_id"] == "trend_breakout"
    assert lot["is_long_term"] == 1
    conn.close()


def test_record_fill_exit_config_load_failure_is_indeterminate(temp_db_path, monkeypatch, capsys):
    """exit 設定載入失敗時：fill 仍寫入（不誤報失敗），且提示為不確定語氣而非斷言不受監控。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    def _raise(settings):
        raise ValueError("STRATEGY_ID_MISMATCH: 模擬設定錯誤")

    monkeypatch.setattr(
        "src.cli.common.strategy_registry.load_exit_managed_definitions", _raise
    )

    class MockRecordFillArgs:
        symbol = "2317"
        side = "BUY"
        quantity = 100
        price = 50.0
        account = "acc-cfgfail"
        long_term = False
        strategy_id = "trend_breakout"

    cmd_trade_record_fill(MockRecordFillArgs())  # 不應 SystemExit

    captured = capsys.readouterr()
    assert "成功錄入成交資料" in captured.out
    assert "無法判定監控狀態" in captured.out
    # 不可出現與事實相反的「無 exit 區塊」斷言
    assert "無 exit 區塊" not in captured.out

    conn = get_db_connection(temp_db_path)
    row = conn.execute(
        "SELECT strategy_id FROM fills WHERE account_id = 'acc-cfgfail'"
    ).fetchone()
    assert row is not None and row["strategy_id"] == "trend_breakout"
    conn.close()


def test_record_fill_unknown_strategy_rejected(temp_db_path, monkeypatch, capsys):
    """未登錄的 strategy_id 應被拒絕，且不寫入任何 fill。"""
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    class MockRecordFillArgs:
        symbol = "2317"
        side = "BUY"
        quantity = 100
        price = 50.0
        account = "acc-bogus"
        long_term = False
        strategy_id = "not_a_real_strategy"

    with pytest.raises(SystemExit):
        cmd_trade_record_fill(MockRecordFillArgs())

    captured = capsys.readouterr()
    assert "未知策略" in captured.out

    conn = get_db_connection(temp_db_path)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM fills WHERE account_id = 'acc-bogus'"
    ).fetchone()
    assert row["c"] == 0
    conn.close()


def test_simulation_run_daily_locked(temp_db_path, monkeypatch, capsys):
    """Second concurrent simulation run-daily should exit with SIMULATION_ALREADY_RUNNING."""
    import fcntl
    from pathlib import Path
    from src.cli import cmd_simulation_run_daily

    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    lock_path = Path(temp_db_path).parent / "simulation_daily.lock"
    lock_file = open(lock_path, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)

    try:
        class MockRunDailyArgs:
            date = "2026-06-10"
            account = "acc-lock-test"

        with pytest.raises(SystemExit):
            cmd_simulation_run_daily(MockRunDailyArgs())

        captured = capsys.readouterr()
        assert "SIMULATION_ALREADY_RUNNING" in captured.out
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


# ── reject-signal / un-reject-signal tests ────────────────────────────────────

def _insert_signal_item(conn, signal_id="sig-r1", bundle_id="b-r1", symbol="2330", action="BUY"):
    """Helper: insert a minimal signal_item row."""
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, 'run-r1', 'app-r1', 'trend_pullback', '1.0', 'hash-r1',
                  '2026-06-10', '2026-06-11', '2026-06-10', datetime('now'))
        """,
        (bundle_id,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
        ) VALUES (?, ?, ?, ?, ?, 1000000, 'PULLBACK', datetime('now'))
        """,
        (f"item-{signal_id}", bundle_id, signal_id, symbol, action),
    )
    conn.commit()


def test_reject_signal(temp_db_path, monkeypatch, capsys):
    """reject-signal should set user_override=REJECTED in signal_items."""
    from src.cli import cmd_trade_reject_signal

    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    conn = get_db_connection(temp_db_path)
    _insert_signal_item(conn, signal_id="sig-rej1")
    conn.close()

    class Args:
        signal_id = "sig-rej1"
        reason = "今日不適合買入"

    cmd_trade_reject_signal(Args())
    captured = capsys.readouterr()
    assert "已拒絕訊號" in captured.out
    assert "sig-rej1" in captured.out

    # Verify DB
    conn = get_db_connection(temp_db_path)
    row = conn.execute(
        "SELECT user_override, override_reason FROM signal_items WHERE signal_id = 'sig-rej1'"
    ).fetchone()
    assert row["user_override"] == "REJECTED"
    assert row["override_reason"] == "今日不適合買入"
    conn.close()


def test_reject_signal_idempotent(temp_db_path, monkeypatch, capsys):
    """Rejecting an already-rejected signal should be a no-op with friendly message."""
    from src.cli import cmd_trade_reject_signal

    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    conn = get_db_connection(temp_db_path)
    _insert_signal_item(conn, signal_id="sig-rej2")
    # Pre-reject it
    conn.execute(
        "UPDATE signal_items SET user_override='REJECTED', override_reason='先前拒絕' WHERE signal_id='sig-rej2'"
    )
    conn.commit()
    conn.close()

    class Args:
        signal_id = "sig-rej2"
        reason = "再次拒絕"

    cmd_trade_reject_signal(Args())
    captured = capsys.readouterr()
    assert "已經是拒絕狀態" in captured.out


def test_un_reject_signal(temp_db_path, monkeypatch, capsys):
    """un-reject-signal should clear user_override back to NULL."""
    from src.cli import cmd_trade_un_reject_signal

    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    conn = get_db_connection(temp_db_path)
    _insert_signal_item(conn, signal_id="sig-rej3")
    conn.execute(
        "UPDATE signal_items SET user_override='REJECTED', override_reason='測試' WHERE signal_id='sig-rej3'"
    )
    conn.commit()
    conn.close()

    class Args:
        signal_id = "sig-rej3"

    cmd_trade_un_reject_signal(Args())
    captured = capsys.readouterr()
    assert "已恢復訊號" in captured.out

    conn = get_db_connection(temp_db_path)
    row = conn.execute(
        "SELECT user_override FROM signal_items WHERE signal_id = 'sig-rej3'"
    ).fetchone()
    assert row["user_override"] is None
    conn.close()


def test_reject_signal_unknown_id(temp_db_path, monkeypatch, capsys):
    """Rejecting a non-existent signal_id should exit with error."""
    from src.cli import cmd_trade_reject_signal

    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    class Args:
        signal_id = "does-not-exist"
        reason = "whatever"

    with pytest.raises(SystemExit):
        cmd_trade_reject_signal(Args())
    captured = capsys.readouterr()
    assert "找不到訊號 ID" in captured.out


def test_cmd_report_pnl_source_split(temp_db_path, monkeypatch, capsys):
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    
    # Insert cash
    cursor.execute("INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('acc-split-test', 200000, 'TWD', datetime('now'))")
    
    # Insert two fills (one STRATEGY, one MANUAL_IMPORT)
    cursor.execute(
        """
        INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side, quantity, price, filled_at, created_at, source)
        VALUES ('fill-strat', 'acc-split-test', 'run-1', 'ord-1', 'key-1', '2330', 'BUY', 100, 10000000, '2026-06-10T09:00:00', datetime('now'), 'STRATEGY')
        """
    )
    cursor.execute(
        """
        INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side, quantity, price, filled_at, created_at, source)
        VALUES ('fill-man', 'acc-split-test', 'run-1', 'ord-2', 'key-2', '2327', 'BUY', 50, 5000000, '2026-06-10T09:00:00', datetime('now'), 'MANUAL_IMPORT')
        """
    )
    
    # Insert two position lots
    cursor.execute(
        """
        INSERT INTO position_lots (lot_id, account_id, symbol, quantity, price, acquired_at, fill_id, created_at)
        VALUES ('lot-strat', 'acc-split-test', '2330', 100, 10000000, '2026-06-10T09:00:00', 'fill-strat', datetime('now'))
        """
    )
    cursor.execute(
        """
        INSERT INTO position_lots (lot_id, account_id, symbol, quantity, price, acquired_at, fill_id, created_at)
        VALUES ('lot-man', 'acc-split-test', '2327', 50, 5000000, '2026-06-10T09:00:00', 'fill-man', datetime('now'))
        """
    )
    
    # Insert daily bars for close prices: 2330 at 1050.0, 2327 at 510.0
    cursor.execute(
        """
        INSERT INTO market_bars (symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at)
        VALUES ('2330', 'TSE', 'STOCK', '2026-06-10', 10000000, 10600000, 9900000, 10500000, 100, 10000, 'test', 'Asia/Taipei', 1, 'now', 'chk', datetime('now'), datetime('now'))
        """
    )
    cursor.execute(
        """
        INSERT INTO market_bars (symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at)
        VALUES ('2327', 'TSE', 'STOCK', '2026-06-10', 5000000, 5200000, 4900000, 5100000, 100, 10000, 'test', 'Asia/Taipei', 1, 'now', 'chk', datetime('now'), datetime('now'))
        """
    )
    conn.commit()
    conn.close()
    
    class MockPnLArgs:
        def __init__(self, source="all"):
            self.account = "acc-split-test"
            self.date = "2026-06-10"
            self.source = source

    # 1. Test strategy filter
    cmd_report_pnl(MockPnLArgs(source="strategy"))
    captured = capsys.readouterr()
    assert "僅看策略交易" in captured.out
    assert "2330" in captured.out
    assert "2327" not in captured.out
    assert "部位價值：105,000 TWD" in captured.out
    assert "未實現損益：+5,000 TWD" in captured.out

    # 2. Test manual filter
    cmd_report_pnl(MockPnLArgs(source="manual"))
    captured = capsys.readouterr()
    assert "僅看手動錄入" in captured.out
    assert "2327" in captured.out
    assert "2330" not in captured.out
    assert "部位價值：25,500 TWD" in captured.out
    assert "未實現損益：+500 TWD" in captured.out

    # 3. Test all (default)
    cmd_report_pnl(MockPnLArgs(source="all"))
    captured = capsys.readouterr()
    assert "策略自動交易 (STRATEGY)" in captured.out
    assert "手動錄入交易 (MANUAL_IMPORT)" in captured.out
    assert "2330" in captured.out
    assert "2327" in captured.out
    assert "部位總價值：130,500 TWD" in captured.out


def test_execute_pending_skips_rejected_signal(temp_db_path, monkeypatch, capsys):
    from src.cli import cmd_simulation_execute_pending
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    mock_settings.issuer_allowlist = ["manual-research-review"]
    mock_settings.revoked_approvals = []
    mock_settings.backtest.slippage_bps = 10
    mock_settings.trading.pipeline.entry_strategies = ["trend_breakout", "pullback_rebound"]
    mock_settings.trading.global_limits.max_open_positions = 8
    mock_settings.trading.global_limits.max_daily_buy_value = 200000
    mock_settings.trading.global_limits.max_new_positions_per_day = 2
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)

    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    
    # 1. Initialize cash
    cursor.execute("INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('acc-exec-test', 200000, 'TWD', datetime('now'))")
    
    # 2. Insert mock approval manifest
    from src.cli import sign_manifest
    from src.contracts.models import StrategyApprovalManifest
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": "app-1",
        "issuer_id": "manual-research-review",
        "strategy": {
            "strategy_id": "trend_pullback",
            "strategy_version": "1.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": "hash-1"
        },
        "permissions": {
            "execution_modes": ["backtest", "simulation"],
            "risk_increasing_actions": ["open_long", "increase_long"]
        },
        "limits": {
            "currency": "TWD",
            "max_order_value": 35000,
            "max_daily_buy_value": 150000,
            "max_open_positions": 5
        },
        "validity": {
            "valid_from": "2026-06-01T00:00:00+08:00",
            "expires_at": "2026-06-30T00:00:00+08:00"
        },
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "manifest-v1",
            "digest": ""
        }
    }
    signed_dict = sign_manifest(manifest_dict)
    dummy_manifest = StrategyApprovalManifest(**signed_dict)
    monkeypatch.setattr("src.cli.common.load_active_manifests", lambda s: {"trend_pullback": dummy_manifest})

    # 3. Create signal bundle targeting 2026-06-11
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("b-exec", "r-exec", "app-1", "trend_pullback", "1.0", "hash-1", "2026-06-10", "2026-06-11", "2026-06-10")
    )
    
    # Normal BUY signal for 2330
    cursor.execute(
        """
        INSERT INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at, user_override
        ) VALUES (?, ?, ?, ?, ?, 1000000, 'PULLBACK', datetime('now'), NULL)
        """,
        ("item-normal", "b-exec", "sig-normal", "2330", "BUY")
    )
    # Rejected BUY signal for 2327
    cursor.execute(
        """
        INSERT INTO signal_items (
            item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at, user_override
        ) VALUES (?, ?, ?, ?, ?, 5000000, 'PULLBACK', datetime('now'), 'REJECTED')
        """,
        ("item-rejected", "b-exec", "sig-rejected", "2327", "BUY")
    )
    
    # 4. Insert market bars so FakeBroker can fill
    cursor.execute(
        """
        INSERT INTO market_bars (symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at)
        VALUES ('2330', 'TSE', 'STOCK', '2026-06-11', 1000000, 1050000, 990000, 1000000, 100, 10000, 'shioaji', 'Asia/Taipei', 1, 'now', 'chk', datetime('now'), datetime('now'))
        """
    )
    cursor.execute(
        """
        INSERT INTO market_bars (symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at)
        VALUES ('2327', 'TSE', 'STOCK', '2026-06-11', 500000, 520000, 490000, 500000, 100, 10000, 'shioaji', 'Asia/Taipei', 1, 'now', 'chk', datetime('now'), datetime('now'))
        """
    )
    conn.commit()
    conn.close()

    class Args:
        execution_date = "2026-06-11"
        account = "acc-exec-test"

    cmd_simulation_execute_pending(Args())
    captured = capsys.readouterr()
    assert "b-exec" in captured.out
    
    # 5. Verify fills in database
    conn = get_db_connection(temp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT symbol FROM fills WHERE account_id = 'acc-exec-test'")
    rows = cursor.fetchall()
    symbols = [r["symbol"] for r in rows]
    # Normal signal should be executed (filled)
    assert "2330" in symbols
    # Rejected signal should NOT be executed (not filled)
    assert "2327" not in symbols
    conn.close()
