"""resolve_run_universe 單元測試：per-account universe policy 解析 + 安全退回固定清單。"""
from datetime import date
from types import SimpleNamespace

from src.portfolio.db import init_db, get_db_connection
from src.cli.common import resolve_run_universe


def _settings(overrides):
    symbols = [SimpleNamespace(code=c) for c in ("2330", "2317", "2454")]
    pipeline = SimpleNamespace(universe_overrides=overrides)
    return SimpleNamespace(
        universe=SimpleNamespace(symbols=symbols),
        trading=SimpleNamespace(pipeline=pipeline),
    )


def _conn(tmp_path):
    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    return get_db_connection(db_path)


def test_no_override_returns_fixed_list(tmp_path):
    conn = _conn(tmp_path)
    syms = resolve_run_universe(_settings({}), conn, "simulation-main", date(2026, 6, 30))
    assert syms == ["2330", "2317", "2454"]
    conn.close()


def test_override_returns_policy_constituents_as_of(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        """INSERT INTO universe_policy
           (policy_version, symbol, effective_from, effective_to, known_at, created_at)
           VALUES ('liq-v1','2603','2026-06-01',NULL,'2026-06-01',datetime('now')),
                  ('liq-v1','3008','2026-06-01',NULL,'2026-06-01',datetime('now'))"""
    )
    conn.commit()
    syms = resolve_run_universe(
        _settings({"simulation-main": "liq-v1"}), conn, "simulation-main", date(2026, 6, 30)
    )
    assert syms == ["2603", "3008"]  # policy, not the fixed 2330/2317/2454
    conn.close()


def test_empty_policy_falls_back_to_fixed(tmp_path, capsys):
    conn = _conn(tmp_path)
    # 設定指向某 policy，但資料未回補（known_at 在 run_date 之後 → as_of 讀不到）
    conn.execute(
        """INSERT INTO universe_policy
           (policy_version, symbol, effective_from, effective_to, known_at, created_at)
           VALUES ('liq-v1','2603','2026-06-01',NULL,'2027-01-01',datetime('now'))"""
    )
    conn.commit()
    syms = resolve_run_universe(
        _settings({"simulation-main": "liq-v1"}), conn, "simulation-main", date(2026, 6, 30)
    )
    assert syms == ["2330", "2317", "2454"]
    assert "退回固定" in capsys.readouterr().out
    conn.close()
