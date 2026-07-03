"""A1 量化診斷：raw 序列的除息缺口有多大、離典型停損多近。

對 research.db 的 corporate_actions（現金股利）逐事件計算：
  gap_pct = 每股息 / ex 日前一交易日 raw close
再對照各策略 YAML 的 fixed_stop_loss_bps / trailing_stop_bps，統計「單次除息缺口
就吃掉停損緩衝 X% 以上」的事件數——回答「raw 回測的停損假觸發風險量級」。

用法：.venv/bin/python scripts/diagnose_dividend_impact.py [db_path]
"""
import sqlite3
import sys


def main(db_path: str = "data/research.db") -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    events = conn.execute(
        """
        SELECT ca.symbol, ca.ex_date, ca.cash_per_share,
               (SELECT close FROM market_bars mb
                WHERE mb.symbol = ca.symbol AND mb.price_basis = 'raw'
                  AND mb.trade_date < ca.ex_date
                ORDER BY mb.trade_date DESC LIMIT 1) AS prev_close
        FROM corporate_actions ca
        WHERE ca.action_type = 'CASH_DIVIDEND' AND ca.cash_per_share > 0
        ORDER BY ca.ex_date
        """
    ).fetchall()

    gaps = []
    for e in events:
        if e["prev_close"] and e["prev_close"] > 0:
            gaps.append({
                "symbol": e["symbol"],
                "ex_date": e["ex_date"],
                "gap_pct": e["cash_per_share"] / e["prev_close"],
            })

    if not gaps:
        print("corporate_actions 無可對照 raw bar 的現金股利事件（先跑 market backfill-history）。")
        return

    gaps.sort(key=lambda g: -g["gap_pct"])
    n = len(gaps)
    print(f"=== 除息缺口診斷（{db_path}）===")
    print(f"現金股利事件（有前一日 raw close 可對照）：{n} 件")
    for pct in (0.01, 0.02, 0.03, 0.05):
        cnt = sum(1 for g in gaps if g["gap_pct"] >= pct)
        print(f"  缺口 >= {pct:.0%}: {cnt} 件（{cnt / n:.0%}）")
    print("\n最大 15 件（raw 回測中這些日子會憑空出現同幅下跌）：")
    for g in gaps[:15]:
        print(f"  {g['ex_date']}  {g['symbol']}: -{g['gap_pct']:.2%}")

    # 對照策略停損參數
    try:
        import yaml
        from pathlib import Path
        print("\n=== 對照策略停損緩衝 ===")
        for f in sorted(Path("config/strategies").glob("*.yaml")):
            cfg = yaml.safe_load(f.read_text())
            exit_cfg = (cfg or {}).get("exit") or {}
            fixed = exit_cfg.get("fixed_stop_loss_bps")
            trailing = exit_cfg.get("trailing_stop_bps")
            if not fixed:
                continue
            # 除息缺口吃掉固定停損緩衝 1/3 以上＝顯著扭曲該筆交易的出場行為
            threshold = fixed / 10000 / 3
            cnt = sum(1 for g in gaps if g["gap_pct"] >= threshold)
            print(
                f"  {f.stem}: fixed_stop={fixed}bps trailing={trailing}bps → "
                f"缺口吃掉固定停損緩衝 ≥1/3 的事件 {cnt} 件（{cnt / n:.0%}）"
            )
    except Exception as e:
        print(f"（策略 YAML 對照略過: {e}）")

    print(
        "\n結論指引：占比高＝raw 回測的停損/報酬失真顯著，應以 market build-adj + "
        "backtest --price-basis adj 重跑再裁決。"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/research.db")
