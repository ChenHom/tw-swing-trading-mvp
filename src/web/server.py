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

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import AppSettings
from src.portfolio.db import get_db_connection
from src.portfolio.projection import PortfolioProjection
from src.application.services import dashboard as dash

BASE_DIR = Path(__file__).resolve().parent
ROOT_PATH = os.environ.get("TRADING_WEB_ROOT_PATH", "/trading")

app = FastAPI(title="tw-day-trading 儀表板", root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# 子路徑前綴：模板以 {{ base }}/... 產生連結，nginx 子路徑與本機直連皆正確。
templates.env.globals["base"] = ROOT_PATH.rstrip("/")


def _conn():
    return get_db_connection(AppSettings().trading.database_path)


@app.get("/", response_class=HTMLResponse)
def index(request: Request,
          account: str | None = Query(default=None),
          view_date: str | None = Query(default=None)):
    conn = _conn()
    try:
        accounts = dash.list_accounts(conn)
        account_id = account or (accounts[0] if accounts else "simulation-main")
        if view_date:
            d = date.fromisoformat(view_date)
        else:
            # 預設落在最近有 run 的日期，避免假日/未跑日全空
            latest = dash.latest_run_date(conn, account_id)
            d = date.fromisoformat(latest) if latest else date.today()
        projection = PortfolioProjection(conn)
        data = dash.build_dashboard(conn, projection, account_id, d)
        return templates.TemplateResponse(
            request, "dashboard.html",
            {"d": data, "accounts": accounts, "view_date": d.isoformat()},
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


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
