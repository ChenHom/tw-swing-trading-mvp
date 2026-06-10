import pytest
from datetime import date
from unittest.mock import MagicMock
from src.portfolio.db import init_db, get_db_connection
from src.cli import cmd_signal_list, cmd_simulation_reset

class MockArgs:
    def __init__(self, date_str=None):
        self.date = date_str

@pytest.fixture
def temp_db_path(tmp_path):
    db_file = tmp_path / "test_cli.db"
    init_db(str(db_file))
    return str(db_file)

def test_cmd_signal_list_empty(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.get_settings", lambda: mock_settings)
    
    # Run command with no signals
    args = MockArgs(date_str=None)
    cmd_signal_list(args)
    
    captured = capsys.readouterr()
    assert "資料庫中無任何訊號紀錄。" in captured.out

def test_cmd_signal_list_with_data(temp_db_path, monkeypatch, capsys):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.trading.database_path = temp_db_path
    monkeypatch.setattr("src.cli.get_settings", lambda: mock_settings)
    
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
    monkeypatch.setattr("src.cli.get_settings", lambda: mock_settings)
    
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
