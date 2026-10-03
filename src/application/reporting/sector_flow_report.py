from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.application.reporting.sector_flow import CATEGORY_OVERLAP_NOTE


def _integer(value: object) -> str:
    return f"{int(value or 0):,}"


def _amount(value: object) -> str:
    if value is None:
        return "N/A"
    return f"NT$ {float(value):,.0f}"


def _name(row: Mapping[str, Any]) -> str:
    return f"{row['category']}（大類）" if row.get("is_broad") else str(row["category"])


def _ranking(
    rows: Sequence[Mapping[str, Any]],
    metric: str,
    amount_metric: str,
    *,
    positive: bool,
    ranking_method: str,
) -> list[str]:
    ranking_field = amount_metric if ranking_method == "estimated_amount" else metric
    selected = [row for row in rows if (float(row.get(ranking_field, 0)) > 0 if positive else float(row.get(ranking_field, 0)) < 0)]
    selected.sort(key=lambda row: (float(row.get(ranking_field, 0)), str(row.get("category", ""))), reverse=positive)
    lines = ["| 排名 | 族群 | 精確淨買賣股數 | 估算金額（淨股數 × 收盤價） |", "|---:|---|---:|---:|"]
    for index, row in enumerate(selected[:10], 1):
        lines.append(f"| {index} | {_name(row)} | {_integer(row.get(metric))} | {_amount(row.get(amount_metric))} |")
    if not selected:
        lines.append("| - | 無 | 0 | NT$ 0 |")
    return lines


def _daily_outflows(rows: Sequence[Mapping[str, Any]], ranking_method: str) -> list[Mapping[str, Any]]:
    field = "estimated_institutional_net_amount_twd" if ranking_method == "estimated_amount" else "institutional_net_shares"
    negatives = [row for row in rows if float(row.get(field, 0)) < 0]
    return sorted(negatives, key=lambda row: (float(row[field]), str(row.get("category", ""))))[:10]


def _daily_row(index: int, row: Mapping[str, Any]) -> str:
    return (
        f"| {index} | {_name(row)} | {_integer(row['institutional_net_shares'])} | "
        f"{_integer(row['foreign_net_shares'])} | {_integer(row['investment_trust_net_shares'])} | "
        f"{_integer(row['dealer_net_shares'])} | {_amount(row['estimated_institutional_net_amount_twd'])} |"
    )


DAILY_TABLE_MAX_DATES = 10


def _detail_stock_table(rows: Sequence[Mapping[str, Any]], share_label: str) -> list[str]:
    lines = [f"| 排名 | 市場 | 代號 | 名稱 | 外資 | 投信 | 自營商 | 法人淨股數 | 估算金額 | {share_label} |", "|---:|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for index, row in enumerate(rows, 1):
        lines.append(
            f"| {index} | {row['market']} | {row['symbol']} | {row['name']} | {_integer(row['foreign_net_shares'])} | "
            f"{_integer(row['investment_trust_net_shares'])} | {_integer(row['dealer_net_shares'])} | "
            f"{_integer(row['institutional_net_shares'])} | {_amount(row.get('estimated_net_amount_twd'))} | {float(row['share_of_side_pct']):.2f}% |"
        )
    if not rows:
        lines.append("| - | - | - | 無 | 0 | 0 | 0 | 0 | NT$ 0 | - |")
    return lines


def _detail_section(item: Mapping[str, Any], dates: Sequence[str]) -> list[str]:
    method = str(item["ranking_method"])
    top = item["top"]
    lines = [
        f"## 族群細看：{_name(item)}",
        "",
        f"成員 {item['member_count']} 檔；法人淨股數 {_integer(item['institutional_net_shares'])}；"
        f"估算金額 {_amount(item['estimated_institutional_net_amount_twd'])}；排名方法 `{method}`。",
        "",
    ]
    if item.get("is_broad"):
        lines.extend(["### 子類小計", "", "| 子類 | 成員數 | 法人淨股數 | 外資 | 投信 | 自營商 | 估算金額 |", "|---|---:|---:|---:|---:|---:|---:|"])
        for row in item.get("subcategories", []):
            lines.append(
                f"| {row['category']} | {row['member_count']} | {_integer(row['institutional_net_shares'])} | {_integer(row['foreign_net_shares'])} | "
                f"{_integer(row['investment_trust_net_shares'])} | {_integer(row['dealer_net_shares'])} | {_amount(row['estimated_institutional_net_amount_twd'])} |"
            )
        lines.extend(["", f"註：一檔股票若屬於兩個子類，會同時出現在兩個群組，子類之間不可加總；本次同時計入 {item.get('subcategory_overlap_count', 0)} 檔股票的多個子類。", ""])
    inflows, outflows = item.get("top_inflows", []), item.get("top_outflows", [])
    lines.extend([f"### 流入前 {top} 名", "", *_detail_stock_table(inflows, "佔流入比重"), "", f"### 流出前 {top} 名", "", *_detail_stock_table(outflows, "佔流出比重"), ""])
    if len(dates) > DAILY_TABLE_MAX_DATES:
        lines.extend([f"交易日超過 {DAILY_TABLE_MAX_DATES} 天，不列每日明細；逐日數據見 JSON `category_detail[].top_*[].daily`。", ""])
    elif inflows or outflows:
        by_amount = method == "estimated_amount"
        lines.extend([f"### 每日明細（{'估算金額' if by_amount else '法人淨股數'}）", "", "| 代號 | 名稱 | " + " | ".join(dates) + " |", "|---|---|" + "---:|" * len(dates)])
        for row in [*inflows, *outflows]:
            cells = [_amount(day["estimated_net_amount_twd"]) if by_amount else _integer(day["institutional_net_shares"]) for day in row["daily"]]
            lines.append(f"| {row['symbol']} | {row['name']} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def render_sector_flow_markdown(payload: Mapping[str, Any]) -> str:
    """Render one deterministic human-readable sector-flow report."""
    period = payload["requested_period"]
    observed = ", ".join(payload.get("observed_trading_dates", [])) or "無"
    lines = [
        "# 族群資金流 V1",
        "",
        f"- 請求期間：{period['start_date']} ～ {period['end_date']}",
        f"- 實際交易日：{observed}",
        f"- 資料狀態：`{payload.get('status', 'blocked')}`",
        f"- 排名方法：`{payload.get('ranking_method', 'net_shares')}`",
        f"- 收盤價覆蓋率：{float(payload.get('price_coverage', 0)):.1%}",
        f"- 產業分類 snapshot：{payload.get('taxonomy', {}).get('snapshot_date') or '無'}（覆蓋率 {float(payload.get('taxonomy', {}).get('coverage', 0)):.1%}）",
        "",
        "> 股數是官方資料的精確淨買賣股數；金額是估算金額（淨股數 × 收盤價），不是法人實際成交現金流。",
        "",
        f"> {payload.get('category_overlap_note') or CATEGORY_OVERLAP_NOTE}",
        "",
    ]
    if "dates_without_data" in payload:
        lines.extend([f"- 休市日（四個來源皆無資料，視為休市）：{', '.join(payload['dates_without_data']) or '無'}", ""])
    period_rows = payload.get("period_summary", [])
    ranking_method = str(payload.get("ranking_method", "net_shares"))
    metrics = [
        ("法人", "institutional_net_shares", "estimated_institutional_net_amount_twd"),
        ("外資", "foreign_net_shares", "estimated_foreign_net_amount_twd"),
        ("投信", "investment_trust_net_shares", "estimated_investment_trust_net_amount_twd"),
        ("自營商", "dealer_net_shares", "estimated_dealer_net_amount_twd"),
    ]
    for label, metric, amount_metric in metrics:
        lines.extend([f"## 區間{label}流入排行", ""])
        lines.extend(_ranking(period_rows, metric, amount_metric, positive=True, ranking_method=ranking_method))
        lines.extend(["", f"## 區間{label}流出排行", ""])
        lines.extend(_ranking(period_rows, metric, amount_metric, positive=False, ranking_method=ranking_method))
        lines.append("")
    if ranking_method == "estimated_amount":
        lines.extend(["> 排名依估算金額（淨股數 × 收盤價），因此族群的淨股數與淨金額可能正負相反（例如低價股淨買超、高價股淨賣超）。", ""])

    lines.extend(["## 每日族群排行", ""])
    for day in payload.get("daily", []):
        lines.extend([f"### {day['trading_date']}", "", "| 排名 | 族群 | 法人淨股數 | 外資 | 投信 | 自營商 | 估算法人金額 |", "|---:|---|---:|---:|---:|---:|---:|"])
        lines.extend(_daily_row(index, row) for index, row in enumerate(day.get("categories", [])[:10], 1))
        lines.extend(["", "最大流出族群：", "", "| 排名 | 族群 | 法人淨股數 | 外資 | 投信 | 自營商 | 估算法人金額 |", "|---:|---|---:|---:|---:|---:|---:|"])
        outflows = _daily_outflows(day.get("categories", []), ranking_method)
        lines.extend(_daily_row(index, row) for index, row in enumerate(outflows, 1))
        if not outflows:
            lines.append("| - | 無 | 0 | 0 | 0 | 0 | NT$ 0 |")
        lines.append("")

    lines.extend(["## 主要個股貢獻", ""])
    for category in period_rows:
        contributors = list(category.get("top_positive_contributors", [])) + list(category.get("top_negative_contributors", []))
        if not contributors:
            continue
        lines.extend([f"### {_name(category)}", "", "| 市場 | 股票 | 名稱 | 法人淨股數 | 估算金額 |", "|---|---|---|---:|---:|"])
        for row in contributors:
            lines.append(f"| {row['market']} | {row['symbol']} | {row['name']} | {_integer(row['institutional_net_shares'])} | {_amount(row.get('estimated_net_amount_twd'))} |")
        lines.append("")

    lines.extend(["## 大戶持股變化代理", ""])
    large_holder = payload.get("large_holder", {})
    if large_holder.get("status") != "ok":
        latest = large_holder.get("latest_as_of_date") or "無"
        lines.append(f"資料不足（`{large_holder.get('reason', 'unknown')}`）；目前最新 snapshot：{latest}。不可解讀為大戶淨流入。")
    else:
        lines.extend([
            f"比較 {large_holder['prior_as_of_date']} → {large_holder['latest_as_of_date']}；這是持股變化代理，不是淨流入。",
            "",
            "| 族群 | 大戶持股股數變化 | 持股比例變化 | 估算持股價值變化 |",
            "|---|---:|---:|---:|",
        ])
        for row in large_holder.get("categories", []):
            lines.append(f"| {_name(row)} | {_integer(row['large_holder_share_delta'])} | {float(row['sum_stock_percent_point_delta']):.4f} 個百分點加總 | {_amount(row['estimated_change_twd'])} |")
    for item in payload.get("category_detail", []):
        lines.extend(["", *_detail_section(item, payload.get("observed_trading_dates", []))])
    lines.extend(["", "## 資料品質與限制", ""])
    warnings = payload.get("warnings", [])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    elif payload.get("status") == "degraded":
        lines.append("- 資料狀態為 `degraded`，但 payload 未附原因；請檢查 source_status。")
    else:
        lines.append("- 無額外警告。")
    lines.extend([
        f"- 普通股篩選規則：`{payload.get('exclusions', {}).get('symbol_rule', '未提供')}`。",
        "- 法人資料來自 TWSE／TPEx；分類來自日期受限的 FinMind `TaiwanStockInfo` raw-cache snapshot。",
        "- TDCC 是週資料，不能用來回答逐日大戶下單或精確資金流。",
        "",
    ])
    return "\n".join(lines)
