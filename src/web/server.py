"""唯讀 Web 儀表板（FastAPI + Jinja，掛在 nginx 子路徑 /trading）。

設計：
- 純讀，沿用 src.application.services.dashboard 讀取 service，不直接寫 DB。
- root_path 由環境變數 TRADING_WEB_ROOT_PATH 控制（預設 /trading），對應 nginx 子路徑。
- 每請求開關 DB 連線；單人區網使用、不加認證（信任區網）。

啟動：scripts/web_ui.sh （或 uvicorn src.web.server:app --root-path /trading）
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import AppSettings
from src.portfolio.db import get_db_connection
from src.portfolio.projection import PortfolioProjection
from src.market_data.repository import SqliteMarketBarRepository
from src.application.services import dashboard as dash
from src.application.services import intraday_dashboard as intraday
from src.application.services import llm_advisor
from src.strategy import registry as strategy_registry

from src.contracts.strategy_names import strategy_name

BASE_DIR = Path(__file__).resolve().parent
ROOT_PATH = os.environ.get("TRADING_WEB_ROOT_PATH", "/trading")
# 族群資金頁籤資料（由 report sector-flow-dashboard 產生）；放常數讓測試可 monkeypatch。
SECTOR_FLOW_PATH = BASE_DIR.parents[1] / "data" / "sector_flow" / "dashboard.json"
INTRADAY_SNAPSHOT_PATH = BASE_DIR.parents[1] / "data" / "intraday" / "dashboard.json"

app = FastAPI(title="台股波段交易儀表板", root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# 子路徑前綴：模板以 {{ base }}/... 產生連結，nginx 子路徑與本機直連皆正確。
templates.env.globals["base"] = ROOT_PATH.rstrip("/")
# style.css 版本＝檔案 mtime，改 CSS 後 ?v 變動自動破瀏覽器快取（免手動硬刷新）。
templates.env.globals["static_v"] = int((BASE_DIR / "static" / "style.css").stat().st_mtime)
# 註冊策略名稱中文化 filter
templates.env.filters["strategy_name"] = strategy_name


def _conn():
    return get_db_connection(AppSettings().trading.database_path)


def _exit_strategy_ids():
    """具 exit 區塊（受 risk_exit 監控）的 strategy_id 集合，與引擎/CLI 一致。

    載入失敗時回 None（沿用寬鬆判定，不讓唯讀儀表板因設定問題 500）。
    """
    try:
        return set(strategy_registry.load_exit_managed_definitions(AppSettings()))
    except Exception:
        return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request,
          account: str | None = Query(default=None),
          view_date: str | None = Query(default=None),
          trade_date: date | None = Query(default=None)):
    conn = _conn()
    try:
        accounts = dash.list_accounts(conn)
        account_id = account or (accounts[0] if accounts else "simulation-main")
        today = date.today()
        if view_date:
            d = date.fromisoformat(view_date)
        else:
            # 預設今天：日期欄反映當下。日期只影響「當日 RUN 狀態 / 今日成交 /
            # 執行事件」三塊，交易日盤前自然為空（正確）；現金/持倉/損益/對帳為即時
            # 狀態不受影響；「下次執行」面板已與日期解耦，仍顯示最近一批待執行訊號。
            d = today
        projection = PortfolioProjection(conn)
        # market repo 由路由注入（比照 _exit_strategy_ids），connection 生命週期仍由路由 own。
        market_repo = SqliteMarketBarRepository(conn)
        data = dash.build_dashboard(
            conn, projection, account_id, d, _exit_strategy_ids(), market_repo,
            trade_date=trade_date,
        )
        cap = dash.build_capital_overview(conn, projection, account_id, d, market_repo)
        return templates.TemplateResponse(
            request, "dashboard.html",
            {"d": data, "cap": cap, "accounts": accounts,
             "view_date": d.isoformat(), "today": today.isoformat()},
        )
    finally:
        conn.close()


@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request):
    items = dash.list_reports()
    return templates.TemplateResponse(request, "reports.html", {"reports": items})


@app.get("/reports/{name}", response_class=PlainTextResponse)
def report_detail(name: str):
    text = dash.read_report(name)
    if text is None:
        return PlainTextResponse("報告不存在。", status_code=404)
    return PlainTextResponse(text)


@app.get("/backtests", response_class=HTMLResponse)
def backtests(request: Request):
    items = dash.list_backtest_results(dash.BACKTEST_REPORT_DIR)
    return templates.TemplateResponse(request, "backtests.html", {"results": items})


@app.get("/backtests/{name}", response_class=HTMLResponse)
def backtest_detail(request: Request, name: str):
    result = dash.read_backtest_result(name, dash.BACKTEST_REPORT_DIR)
    if result is None:
        return templates.TemplateResponse(
            request, "backtests.html",
            {"results": dash.list_backtest_results(dash.BACKTEST_REPORT_DIR), "error": "回測結果不存在。"},
            status_code=404,
        )
    return templates.TemplateResponse(request, "backtest_detail.html", {"result": result, "name": name})


@app.get("/llm/{signal_id}", response_class=HTMLResponse)
def llm_review(request: Request, signal_id: str, account: str | None = Query(default=None)):
    """LLM 進場顧問：顯示某訊號的 PIT-safe 提示詞（可複製去問 LLM）+ 回填表單。"""
    conn = _conn()
    try:
        account_id = account or "國泰"
        market_repo = SqliteMarketBarRepository(conn)
        pd = llm_advisor.build_prompt(conn, market_repo, signal_id)
        if pd is None:
            return HTMLResponse("訊號不存在。", status_code=404)
        review = llm_advisor.get_review(conn, signal_id, account_id)
        return templates.TemplateResponse(
            request, "llm_review.html",
            {"signal": pd["signal"], "prompt": pd["prompt"], "review": review,
             "account": account_id, "decisions": pd["decisions"]},
        )
    finally:
        conn.close()


@app.post("/llm/{signal_id}")
def llm_review_save(signal_id: str, account: str = Form("國泰"),
                    llm_response: str = Form(""), decision: str = Form(""),
                    model_note: str = Form("")):
    """回填 LLM 回應與決定。提示詞由系統重建（確定性，與顯示一致）後一併存檔。"""
    conn = _conn()
    try:
        market_repo = SqliteMarketBarRepository(conn)
        pd = llm_advisor.build_prompt(conn, market_repo, signal_id)
        if pd is None:
            return HTMLResponse("訊號不存在。", status_code=404)
        llm_advisor.save_review(conn, signal_id, account, pd["prompt"], llm_response, decision, model_note)
        base = ROOT_PATH.rstrip("/")
        return RedirectResponse(f"{base}/llm/{signal_id}?account={account}", status_code=303)
    finally:
        conn.close()


@app.get("/api/sector-flow")
def sector_flow():
    """原樣回傳族群資金 dashboard.json；檔案不存在回 404 JSON。"""
    try:
        body = SECTOR_FLOW_PATH.read_bytes()
    except FileNotFoundError:
        return JSONResponse({"error": "尚無族群資金資料，等平日 22:00 排程產生"}, status_code=404)
    return Response(body, media_type="application/json")




def _intraday_read():
    """No application trading DB, Shioaji SDK or broker login on read routes."""
    enabled = os.environ.get("INTRADAY_DASHBOARD_ENABLED", "0") == "1"
    return intraday.load_snapshot(INTRADAY_SNAPSHOT_PATH, enabled=enabled)


@app.get("/intraday", response_class=HTMLResponse)
def intraday_page(request: Request):
    data = _intraday_read()
    return templates.TemplateResponse(request, "intraday.html", {"intraday": data})


@app.get("/api/intraday/status")
def intraday_status():
    data = _intraday_read()
    return {
        "schema_version": 1, "status": data["status"],
        "market_session": data["market_session"], "freshness": data["freshness"],
        "generated_at": data["generated_at"], "last_heartbeat_at": data["last_heartbeat_at"],
        "not_trade_signal": True,
    }


@app.get("/api/intraday/symbols")
def intraday_symbols():
    data = _intraday_read()
    return {"status": data["status"], "symbols": [
        {"symbol": r["symbol"], "exchange": r["exchange"], "lot_type": r["lot_type"],
         "freshness": r["freshness"]} for r in data["symbols"]
    ]}


@app.get("/api/intraday/{symbol}/snapshot")
def intraday_symbol_snapshot(symbol: str, lot_type: str = Query("BOARD")):
    if lot_type not in intraday.LOT_TYPES:
        return JSONResponse({"error": "invalid lot_type"}, status_code=422)
    data = _intraday_read()
    item = intraday.find_symbol(data, symbol, lot_type)
    if item is None:
        return JSONResponse({"error": "symbol not found or disabled"}, status_code=404)
    return {
        "schema_version": 1, "status": data["status"], "freshness": item["freshness"],
        "generated_at": data["generated_at"], "not_trade_signal": True,
        "symbol": item,
    }

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
