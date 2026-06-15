"""每日影子報告產生器。

`run-daily` 本身即 paper-trading（FakeBroker，永不串實盤），其 Stage 4「Reporting」
原為空殼。本模組補上實際的文字報告：把當日 run 結果、risk_exit 監控部位數、成交、
下次執行訊號、執行事件、策略別損益與對帳結果落成一份文字檔，供 cron 影子先行與人工核對。

設計刻意獨立於 1853 行的 src/cli.py（CEO review 2026-06-14 列其為 churn 之冠），
純讀資料庫 + projection，無副作用、可單元測試。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID

ORCHESTRATOR_STRATEGY_ID = "MULTI"
DEFAULT_REPORT_DIR = "artifacts/reports/daily"


def _d(report_date) -> str:
    return report_date.isoformat() if isinstance(report_date, date) else str(report_date)


def build_daily_report(
    conn: sqlite3.Connection,
    projection: PortfolioProjection,
    account_id: str,
    report_date,
    *,
    manifests: Optional[dict] = None,
    exit_strategy_ids: Optional[set] = None,
    generated_at: Optional[str] = None,
) -> str:
    """組出當日文字報告（純讀，不寫檔）。

    manifests / exit_strategy_ids 可選；缺省時對應區段會以「未提供」處理，
    讓本函式不依賴 settings 即可單元測試。
    """
    rdate = _d(report_date)
    cur = conn.cursor()
    lines: list[str] = []
    w = lines.append

    w("=" * 64)
    w(f"  每日影子報告 — 帳戶 {account_id} — {rdate}")
    w(f"  產生時間：{generated_at or datetime.now().astimezone().isoformat(timespec='seconds')}")
    w("=" * 64)

    # ── 1. RUN 狀態 ────────────────────────────────────────────────
    w("\n[1] RUN 狀態")
    cur.execute(
        """
        SELECT status, market_sync_status, execution_status,
               signal_generation_status, report_status, last_error_code
        FROM daily_runs
        WHERE run_date = ? AND account_id = ? AND strategy_id = ?
        """,
        (rdate, account_id, ORCHESTRATOR_STRATEGY_ID),
    )
    row = cur.fetchone()
    if row is None:
        w("  ⚠ 查無當日 run 紀錄（run-daily 尚未對此日期執行）。")
    else:
        w(f"  整體狀態      : {row['status']}")
        w(f"  行情同步      : {row['market_sync_status']}")
        w(f"  執行          : {row['execution_status']}")
        w(f"  訊號產生      : {row['signal_generation_status']}")
        w(f"  報告          : {row['report_status']}")
        if row["last_error_code"]:
            w(f"  ⚠ 最後錯誤    : {row['last_error_code']}")

    # ── 2. 授權狀態 ────────────────────────────────────────────────
    w("\n[2] 策略授權")
    if manifests is None:
        w("  （未提供 manifests，略過）")
    elif not manifests:
        w("  ⚠ 無任何有效授權 — 所有策略 BUY 將被擋（SELL/停損不受影響）。")
    else:
        for sid in sorted(manifests):
            m = manifests[sid]
            w(f"  [{sid}] 授權={getattr(m, 'approval_id', '?')}")

    # ── 3. RISK_EXIT 監控部位（§7 觀測缺口的主要補強）────────────────
    w("\n[3] RISK_EXIT 監控中部位")
    positions = projection.get_strategy_positions(account_id, include_long_term=False)
    monitored = {
        k: v for k, v in positions.items()
        if k[0] != MANUAL_STRATEGY_ID
        and (exit_strategy_ids is None or k[0] in exit_strategy_ids)
    }
    w(f"  監控中部位數：{len(monitored)}")
    if not monitored:
        w("  （目前無策略持倉受 risk_exit 監控）")
    for (sid, symbol), pos in sorted(monitored.items()):
        cur.execute(
            """
            SELECT MAX(highest_close) AS hw FROM position_high_watermarks
            WHERE account_id = ? AND strategy_id = ? AND symbol = ?
              AND trade_date >= ?
            """,
            (account_id, sid, symbol, (pos.get("first_acquired_at") or "")[:10]),
        )
        hw_row = cur.fetchone()
        hw = hw_row["hw"] if hw_row and hw_row["hw"] is not None else None
        hw_str = f"{hw / 10000.0:.2f}" if hw is not None else "—（⚠ 無水位，移動停利失效）"
        w(
            f"  [{sid}] {symbol}: {pos['quantity']} 股 @ 均價 "
            f"{pos['wavg_price'] / 10000.0:.2f} | 移動停利水位最高收盤: {hw_str}"
        )

    # ── 4. 今日成交 ────────────────────────────────────────────────
    w("\n[4] 今日成交")
    cur.execute(
        """
        SELECT side, symbol, quantity, price, strategy_id, source
        FROM fills
        WHERE account_id = ? AND filled_at LIKE ?
        ORDER BY filled_at
        """,
        (account_id, f"{rdate}%"),
    )
    fills = cur.fetchall()
    if not fills:
        w("  （無）")
    for f in fills:
        w(
            f"  {f['side']:>4} {f['symbol']} x{f['quantity']} @ "
            f"{f['price'] / 10000.0:.2f} [{f['strategy_id'] or '-'}/{f['source']}]"
        )

    # ── 5. 下次執行（當日收盤產生，target = 次一交易日）──────────
    w("\n[5] 下次執行")
    # signal_bundles 無帳戶欄位（依 run_id 關聯），以 signal_date 取當日產生的訊號。
    cur.execute(
        """
        SELECT si.action, si.symbol, si.reason_code, sb.strategy_id, sb.bundle_id,
               sb.target_execution_date
        FROM signal_items si
        JOIN signal_bundles sb ON si.bundle_id = sb.bundle_id
        WHERE sb.signal_date = ?
        ORDER BY sb.target_execution_date, sb.strategy_id, si.action, si.symbol
        """,
        (rdate,),
    )
    signals = cur.fetchall()
    if not signals:
        w("  （無）")
    for s in signals:
        exit_tag = " (EXIT)" if str(s["bundle_id"]).endswith("-exit") else ""
        w(
            f"  {s['action']:>4} {s['symbol']} [{s['strategy_id']}{exit_tag}] "
            f"{s['reason_code']} → {s['target_execution_date']}"
        )

    # ── 6. 執行事件（netting / approval 阻擋等審計）──────────────────
    w("\n[6] 執行事件（審計）")
    cur.execute(
        """
        SELECT event_type, strategy_id, symbol, detail
        FROM execution_events
        WHERE account_id = ? AND occurred_at = ?
        ORDER BY event_type
        """,
        (account_id, rdate),
    )
    events = cur.fetchall()
    if not events:
        w("  （無）")
    for e in events:
        w(f"  {e['event_type']} [{e['strategy_id'] or '-'}/{e['symbol'] or '-'}] {e['detail'] or ''}")

    # ── 7. 策略別損益 ──────────────────────────────────────────────
    w("\n[7] 策略別損益")
    cash = projection.get_cash_balance(account_id)
    w(f"  可用現金：{cash:,} TWD")
    cur.execute(
        "SELECT strategy_id, SUM(realized_pnl) AS gross FROM fifo_matches WHERE account_id = ? GROUP BY strategy_id",
        (account_id,),
    )
    gross_by = {r["strategy_id"]: (r["gross"] or 0) for r in cur.fetchall()}
    cur.execute(
        """
        SELECT f.strategy_id AS sid, SUM(cl.amount) AS fees
        FROM cash_ledger cl JOIN fills f ON cl.source_id = f.fill_id
        WHERE cl.account_id = ? AND cl.event_type IN ('BROKER_FEE', 'TRANSACTION_TAX')
        GROUP BY f.strategy_id
        """,
        (account_id,),
    )
    fees_by = {r["sid"]: (r["fees"] or 0) for r in cur.fetchall()}
    all_sids = sorted(set(gross_by) | set(fees_by) | {sid for (sid, _s) in positions})
    if not all_sids:
        w("  （無策略活動）")
    for sid in all_sids:
        gross = gross_by.get(sid, 0)
        fees = fees_by.get(sid, 0)
        net = gross + fees  # fees 為負
        open_n = sum(1 for (psid, _s) in positions if psid == sid)
        w(f"  [{sid}] 已實現淨損益 {net:+,} TWD（毛 {gross:+,} / 規費 {fees:,}）| 持倉 {open_n} 檔")

    # ── 8. 對帳 ────────────────────────────────────────────────────
    w("\n[8] 對帳 reconcile")
    result = projection.reconcile(account_id)
    if isinstance(result, dict) and result.get("status") == "RECONCILE_OK":
        w("  ✅ 通過：現金流水與投影一致。")
    elif not result:
        w("  ✅ 通過：現金流水與投影一致。")
    else:
        w(f"  ❌ 失敗：{result}")

    # ── 9. 公司行動 / 除權息提醒（近窗 ±7 日；未套用以 ⚠ 標示）──────────
    w("\n[9] 公司行動 / 除權息提醒")
    held_symbols = {sym for (_sid, sym) in positions}
    try:
        cur.execute(
            """
            SELECT ca.symbol, ca.action_type, ca.ex_date, ca.cash_per_share, ca.stock_ratio,
                   (SELECT COUNT(*) FROM position_cost_adjustments pca WHERE pca.action_id = ca.action_id) AS applied_cnt
            FROM corporate_actions ca
            WHERE ca.ex_date BETWEEN date(?, '-7 day') AND date(?, '+7 day')
            ORDER BY ca.ex_date
            """,
            (rdate, rdate),
        )
        ca_rows = cur.fetchall()
    except Exception:
        ca_rows = []  # 舊 DB 未 migration 時容錯
    if not ca_rows:
        w("  （無登錄之公司行動）")
    for r in ca_rows:
        if r["action_type"] == "CASH_DIVIDEND":
            detail = f"現金股利 {(r['cash_per_share'] or 0) / 10000:.2f} 元/股"
        else:
            detail = f"配股 {(r['stock_ratio'] or 0):.2%}"
        status = "已套用" if r["applied_cnt"] > 0 else "⚠未套用"
        held = "（持倉中）" if r["symbol"] in held_symbols else ""
        w(f"  [{r['ex_date']}] {r['symbol']}: {detail} — {status} {held}")

    w("\n" + "=" * 64)
    w(f"  END — {account_id} {rdate}")
    w("=" * 64)
    return "\n".join(lines) + "\n"


def write_daily_report(
    report_text: str,
    account_id: str,
    report_date,
    *,
    base_dir: str = DEFAULT_REPORT_DIR,
    run_status: str = "",
) -> Path:
    """落檔並記錄路徑：

    - 報告本體：<base_dir>/<account>_<date>.txt
    - LATEST.txt：最新一份報告的絕對路徑（單行，供 cron / 人工快速定位）
    - INDEX.tsv ：date<TAB>account<TAB>status<TAB>path 逐行追加（歷史索引）
    回傳報告檔 Path。
    """
    rdate = _d(report_date)
    out_dir = Path(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = (out_dir / f"{account_id}_{rdate}.txt").resolve()
    report_path.write_text(report_text, encoding="utf-8")

    (out_dir / "LATEST.txt").write_text(str(report_path) + "\n", encoding="utf-8")

    index_path = out_dir / "INDEX.tsv"
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(f"{rdate}\t{account_id}\t{run_status or '-'}\t{report_path}\n")

    return report_path
