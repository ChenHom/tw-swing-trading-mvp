"""LLM 進場顧問 P1：提示詞由自家行情組成（防幻覺、PIT-safe），回填記帳可往返。"""
from datetime import date, timedelta

from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.contracts.models import MarketBar
from src.application.services import llm_advisor


def _bar(symbol, d, close, vol):
    px = int(round(close * 10000))
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type="STOCK", trade_date=d,
        open=px, high=px, low=px, close=px, volume=vol, amount=px * vol // 10000,
        source="test", source_fetched_at="now", raw_payload_checksum="chk",
    )


def _seed(tmp_path):
    init_db(str(tmp_path / "t.db"))
    conn = get_db_connection(str(tmp_path / "t.db"))
    repo = SqliteMarketBarRepository(conn)
    d0 = date(2026, 1, 1)
    days = [d0 + timedelta(days=i) for i in range(65)]
    for d in days:
        repo.upsert(_bar("2330", d, 100.0, 2000))  # 全 100 元 → 各均線=100
    sig_date = days[-1]
    conn.execute(
        """INSERT INTO signal_bundles (bundle_id, run_id, approval_id, strategy_id, strategy_version,
           params_hash, signal_date, target_execution_date, market_data_cutoff, created_at)
           VALUES ('b1','r1','a1','trend_breakout','1.0.0','h', ?, ?, ?, 'now')""",
        (sig_date.isoformat(), (sig_date + timedelta(days=1)).isoformat(), sig_date.isoformat()))
    conn.execute(
        """INSERT INTO signal_items (item_id, bundle_id, signal_id, symbol, action, reference_price,
           reason_code, created_at, signal_source)
           VALUES ('i1','b1','sig-2330-buy','2330','BUY', 1000000, 'TREND_BREAKOUT_ENTRY','now','ENTRY')""")
    conn.commit()
    return conn, repo


def test_build_prompt_uses_own_real_data(tmp_path):
    conn, repo = _seed(tmp_path)
    out = llm_advisor.build_prompt(conn, repo, "sig-2330-buy")
    assert out is not None
    p = out["prompt"]
    assert "2330" in p
    assert "最新收盤：100.0" in p
    assert "60 日均線：約 100.0（收盤站上）" in p  # 100>=100 → 站上
    assert "當日成交量：2,000 張" in p  # Shioaji 量已是張，不再除 1000
    assert p.count("開") >= 12  # 近 12 日 K 線
    assert out["signal"]["target_date"] == "2026-03-07"  # 訊號日次日
    conn.close()


def test_build_prompt_exit_signal_asks_to_sell(tmp_path):
    conn, repo = _seed(tmp_path)
    # 出場訊號：獨立 exit bundle 加一筆 SELL（規則停損）
    conn.execute(
        """INSERT INTO signal_bundles (bundle_id, run_id, approval_id, strategy_id, strategy_version,
           params_hash, signal_date, target_execution_date, market_data_cutoff, created_at)
           SELECT 'b1-exit', run_id, approval_id, strategy_id, strategy_version, params_hash,
           signal_date, target_execution_date, market_data_cutoff, created_at
           FROM signal_bundles WHERE bundle_id='b1'""")
    conn.execute(
        """INSERT INTO signal_items (item_id, bundle_id, signal_id, symbol, action, reference_price,
           reason_code, created_at, signal_source)
           VALUES ('i2','b1-exit','sig-2330-sell','2330','SELL', 1000000, 'STOP_LOSS_HIT','now','EXIT')""")
    conn.commit()
    out = llm_advisor.build_prompt(conn, repo, "sig-2330-sell")
    assert out["signal"]["is_exit"] is True
    assert out["decisions"] == llm_advisor.DECISIONS_EXIT  # 賣出/減碼/續抱
    p = out["prompt"]
    assert "這筆觸發的賣出訊號是否該執行" in p
    assert "賣出 / 減碼 / 續抱" in p
    assert "進場" not in p  # 不該再用進場框架
    conn.close()


def test_build_prompt_unknown_signal(tmp_path):
    conn, repo = _seed(tmp_path)
    assert llm_advisor.build_prompt(conn, repo, "nope") is None
    conn.close()


def test_build_prompt_includes_chips_when_present(tmp_path):
    conn, repo = _seed(tmp_path)
    # 籌碼（PIT：≤ 訊號日 2026-03-06；單位股，提示詞會 ÷1000 成張）
    conn.execute("INSERT INTO chip_institutional VALUES ('2330','2026-03-06',-2726298,165097,1192045,-1369156,'finmind','now')")
    conn.execute("INSERT INTO chip_margin VALUES ('2330','2026-03-06',28223,84,'finmind','now')")
    conn.commit()
    p = llm_advisor.build_prompt(conn, repo, "sig-2330-buy")["prompt"]
    assert "【籌碼】" in p
    assert "三大法人合計：-1,369 張" in p
    assert "外資 -2,726 投信 +165 自營 +1,192" in p
    assert "融資餘額 28,223 張；融券餘額 84 張" in p
    conn.close()


def test_save_and_get_review_overwrites_keeps_created_at(tmp_path):
    conn, _ = _seed(tmp_path)
    llm_advisor.save_review(conn, "sig-2330-buy", "國泰", "PROMPT", "回應A", "進場", "ChatGPT")
    r1 = llm_advisor.get_review(conn, "sig-2330-buy", "國泰")
    assert r1["decision"] == "進場" and r1["llm_response"] == "回應A"

    llm_advisor.save_review(conn, "sig-2330-buy", "國泰", "PROMPT", "回應B", "不進場", "ChatGPT")
    r2 = llm_advisor.get_review(conn, "sig-2330-buy", "國泰")
    assert r2["decision"] == "不進場" and r2["llm_response"] == "回應B"
    assert r2["created_at"] == r1["created_at"]  # 首次建立時間保留

    # 帳號隔離：另一帳號查不到
    assert llm_advisor.get_review(conn, "sig-2330-buy", "simulation-main") is None
    conn.close()
