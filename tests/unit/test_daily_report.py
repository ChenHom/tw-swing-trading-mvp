"""每日影子報告產生器測試。"""
import pytest

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection
from src.application.reporting.daily_report import (
    build_daily_report, write_daily_report, ORCHESTRATOR_STRATEGY_ID,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "report.db"
    init_db(str(db))
    c = get_db_connection(str(db))
    yield c
    c.close()


def _seed_run(c, account, rdate, status="COMPLETED", err=None):
    c.execute(
        """
        INSERT INTO daily_runs (run_id, run_date, account_id, strategy_id, status,
            market_sync_status, execution_status, signal_generation_status,
            report_status, started_at, completed_at, last_error_code)
        VALUES (?, ?, ?, ?, ?, 'COMPLETED','COMPLETED','COMPLETED','COMPLETED',?,?,?)
        """,
        (f"sim-{rdate}", rdate, account, ORCHESTRATOR_STRATEGY_ID, status, rdate, rdate, err),
    )
    c.commit()


def test_report_has_all_sections_when_empty(conn):
    proj = PortfolioProjection(conn)
    text = build_daily_report(conn, proj, "simulation-main", "2026-06-12")
    for header in ["[1] RUN 狀態", "[3] RISK_EXIT 監控中部位", "[7] 策略別損益", "[8] 對帳 reconcile"]:
        assert header in text
    # 查無 run 紀錄時明確標示
    assert "尚未對此日期執行" in text
    # 空帳戶對帳應通過
    assert "✅ 通過" in text


def test_report_reflects_run_status_and_error(conn):
    proj = PortfolioProjection(conn)
    _seed_run(conn, "simulation-main", "2026-06-12", status="FAILED", err="DATASET_INCOMPLETE")
    text = build_daily_report(conn, proj, "simulation-main", "2026-06-12")
    assert "FAILED" in text
    assert "DATASET_INCOMPLETE" in text


def test_monitored_position_count_and_missing_watermark_warning(conn):
    proj = PortfolioProjection(conn)
    # 一筆策略持倉（非 MANUAL、非長期），但無 watermark → 應計入監控數且警示水位失效
    conn.execute(
        """
        INSERT INTO position_lots (lot_id, account_id, fill_id, symbol, quantity,
            price, acquired_at, created_at, is_long_term, strategy_id)
        VALUES ('lot-1','simulation-main','f1','2330',1000,5000000,'2026-06-10T09:00:00+08:00','2026-06-10T09:00:00+08:00',0,'trend_breakout')
        """
    )
    conn.commit()
    text = build_daily_report(
        conn, proj, "simulation-main", "2026-06-12",
        exit_strategy_ids={"trend_breakout"},
    )
    assert "監控中部位數：1" in text
    assert "移動停利失效" in text  # 無水位警示


def test_manual_position_excluded_from_monitoring(conn):
    proj = PortfolioProjection(conn)
    conn.execute(
        """
        INSERT INTO position_lots (lot_id, account_id, fill_id, symbol, quantity,
            price, acquired_at, created_at, is_long_term, strategy_id)
        VALUES ('lot-m','simulation-main','fm','2317',2000,3000000,'2026-06-10T09:00:00+08:00','2026-06-10T09:00:00+08:00',0,'MANUAL')
        """
    )
    conn.commit()
    text = build_daily_report(conn, proj, "simulation-main", "2026-06-12")
    assert "監控中部位數：0" in text


def test_write_daily_report_records_path(conn, tmp_path):
    proj = PortfolioProjection(conn)
    text = build_daily_report(conn, proj, "simulation-main", "2026-06-12")
    out = tmp_path / "reports"
    path = write_daily_report(text, "simulation-main", "2026-06-12", base_dir=str(out), run_status="COMPLETED")

    assert path.exists()
    assert path.read_text(encoding="utf-8") == text
    # LATEST 指向本報告
    assert (out / "LATEST.txt").read_text(encoding="utf-8").strip() == str(path)
    # INDEX 追加一行且含狀態
    index_line = (out / "INDEX.tsv").read_text(encoding="utf-8").strip()
    assert "2026-06-12" in index_line and "COMPLETED" in index_line and str(path) in index_line
