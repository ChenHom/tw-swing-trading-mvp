# Completed Trade History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only completed-trade history card below the existing historical equity curve, grouped one row per SELL and navigable by close date.

**Architecture:** A new `completed_trades` query service reads existing `fifo_matches` and `fills`, aggregates by `sell_fill_id`, and propagates unknown historical net P&L instead of estimating it. The existing dashboard route passes an independent `trade_date` into the dashboard aggregator; Jinja renders server-side HTML with native `<details>` rows and CSS-only motion, leaving the current Tab navigation and trading paths untouched.

**Tech Stack:** Python 3.11, SQLite, FastAPI, Jinja2, pytest, native HTML `<details>`, CSS transitions, Playwright CLI for browser verification.

---

## File map

- Create `src/application/services/completed_trades.py`: all close-date listing, SELL aggregation, summary, and navigation logic.
- Create `tests/unit/test_completed_trades_service.py`: direct service tests using temporary SQLite facts.
- Modify `src/application/services/dashboard.py`: attach the new service payload to `build_dashboard` without embedding new SQL.
- Modify `src/web/server.py`: validate the independent `trade_date` query parameter and pass it to the dashboard service.
- Modify `src/web/templates/dashboard.html`: insert the card after the existing equity curve and preserve account/view-date query state.
- Modify `src/web/static/style.css`: scoped table, disclosure indicator, motion, contrast, and narrow-screen styles.
- Modify `tests/unit/test_web_server.py`: rendering, invalid-date, empty-state, placement, and existing-nav regression coverage.
- Modify `docs/development/ui-development.md`: document query semantics and UI behavior.
- Modify `docs/development/todo.md`: record completion and verification evidence.

### Task 1: Build the completed-trades read model

**Files:**
- Create: `tests/unit/test_completed_trades_service.py`
- Create: `src/application/services/completed_trades.py`

- [ ] **Step 1: Write failing tests for close dates, SELL identity, FIFO aggregation, and isolation**

Create fixtures through `init_db` and use these test-local fact helpers:

```python
@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "completed-trades.db"
    init_db(str(path))
    connection = get_db_connection(str(path))
    yield connection
    connection.close()


def insert_fill(conn, fill_id, account, symbol, side, quantity, price, filled_at, strategy):
    conn.execute(
        """
        INSERT INTO fills (
            fill_id, account_id, run_id, order_id, execution_key, symbol, side,
            quantity, price, filled_at, reverses_fill_id, created_at,
            is_long_term, source, strategy_id
        ) VALUES (?, ?, 'run-test', ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, 'STRATEGY', ?)
        """,
        (fill_id, account, f"order-{fill_id}", f"key-{fill_id}", symbol, side,
         quantity, price, filled_at, filled_at, strategy),
    )
    conn.commit()


def insert_match(conn, match_id, account, symbol, buy_id, sell_id, quantity,
                 buy_price, sell_price, gross, net, strategy, matched_at="2026-06-10T09:00:00+08:00"):
    conn.execute(
        """
        INSERT INTO fifo_matches (
            match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
            buy_price, sell_price, matched_at, realized_pnl, created_at,
            strategy_id, net_realized_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, account, symbol, buy_id, sell_id, quantity, buy_price,
         sell_price, matched_at, gross, matched_at, strategy, net),
    )
    conn.commit()


def seed_sell_with_two_matches(conn, net_values):
    insert_fill(conn, "buy-1", "a", "2330", "BUY", 60, 1000000,
                "2026-06-01T09:00:00+08:00", "s1")
    insert_fill(conn, "buy-2", "a", "2330", "BUY", 40, 1100000,
                "2026-06-03T09:00:00+08:00", "s1")
    insert_fill(conn, "sell-1", "a", "2330", "SELL", 100, 1200000,
                "2026-06-10T09:00:00+08:00", "s1")
    insert_match(conn, "m1", "a", "2330", "buy-1", "sell-1", 60,
                 1000000, 1200000, 200, net_values[0], "s1")
    insert_match(conn, "m2", "a", "2330", "buy-2", "sell-1", 40,
                 1100000, 1200000, 100, net_values[1], "s1")


def seed_close_dates(conn, close_dates):
    for index, close_date in enumerate(close_dates):
        buy_id, sell_id = f"date-buy-{index}", f"date-sell-{index}"
        insert_fill(conn, buy_id, "a", str(2300 + index), "BUY", 1, 1000000,
                    "2026-06-01T09:00:00+08:00", "s1")
        insert_fill(conn, sell_id, "a", str(2300 + index), "SELL", 1, 1100000,
                    f"{close_date}T09:00:00+08:00", "s1")
        insert_match(conn, f"date-match-{index}", "a", str(2300 + index),
                     buy_id, sell_id, 1, 1000000, 1100000, 10, 8, "s1",
                     matched_at=f"{close_date}T09:00:00+08:00")
```

Then assert:

```python
def test_one_sell_consuming_two_buy_lots_is_one_trade(conn):
    insert_fill(conn, "buy-1", "a", "2330", "BUY", 60, 1000000, "2026-06-01T09:00:00+08:00", "s1")
    insert_fill(conn, "buy-2", "a", "2330", "BUY", 40, 1100000, "2026-06-03T09:00:00+08:00", "s1")
    insert_fill(conn, "sell-1", "a", "2330", "SELL", 100, 1200000, "2026-06-10T09:00:00+08:00", "s1")
    insert_match(conn, "m1", "a", "2330", "buy-1", "sell-1", 60, 1000000, 1200000, 1200, 1100, "s1")
    insert_match(conn, "m2", "a", "2330", "buy-2", "sell-1", 40, 1100000, 1200000, 400, 350, "s1")

    trades = read_completed_trades(conn, "a", date(2026, 6, 10))

    assert len(trades) == 1
    assert trades[0]["sell_fill_id"] == "sell-1"
    assert trades[0]["quantity"] == 100
    assert trades[0]["weighted_buy_price"] == 104.0
    assert trades[0]["gross_pnl"] == 1600
    assert trades[0]["net_pnl"] == 1450
    assert len(trades[0]["lots"]) == 2
```

Also add tests where two SELL fills on the same date remain two rows, a partial SELL only reports matched quantity, another account and another strategy do not leak in, BUY-only facts do not create close dates, and midnight/end-of-day matches stay on the requested date.

- [ ] **Step 2: Run the new tests and verify the import fails**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_completed_trades_service.py
```

Expected: FAIL during collection because `src.application.services.completed_trades` does not exist.

- [ ] **Step 3: Implement deterministic read-model functions**

Create the service with these exact functions and field names:

```python
import sqlite3
from datetime import date

from src.contracts.stock_names import stock_name
from src.contracts.strategy_names import strategy_name


def _iso_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def list_close_dates(conn: sqlite3.Connection, account_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT substr(matched_at, 1, 10) AS close_date
        FROM fifo_matches
        WHERE account_id = ?
        ORDER BY close_date DESC
        """,
        (account_id,),
    ).fetchall()
    return [row["close_date"] for row in rows]


def read_completed_trades(
    conn: sqlite3.Connection, account_id: str, close_date: date | str
) -> list[dict]:
    selected = _iso_date(close_date)
    rows = conn.execute(
        """
        SELECT fm.match_id, fm.symbol, fm.strategy_id, fm.buy_fill_id,
               fm.sell_fill_id, fm.quantity, fm.buy_price, fm.sell_price,
               fm.matched_at, fm.realized_pnl, fm.net_realized_pnl,
               bf.filled_at AS buy_filled_at, sf.filled_at AS sell_filled_at
        FROM fifo_matches fm
        JOIN fills bf ON bf.fill_id = fm.buy_fill_id
        JOIN fills sf ON sf.fill_id = fm.sell_fill_id
        WHERE fm.account_id = ? AND substr(fm.matched_at, 1, 10) = ?
        ORDER BY sf.filled_at, fm.sell_fill_id, bf.filled_at, fm.match_id
        """,
        (account_id, selected),
    ).fetchall()

    grouped: dict[str, dict] = {}
    for row in rows:
        sell_date = date.fromisoformat(row["sell_filled_at"][:10])
        buy_date = date.fromisoformat(row["buy_filled_at"][:10])
        trade = grouped.setdefault(row["sell_fill_id"], {
            "sell_fill_id": row["sell_fill_id"],
            "sell_filled_at": row["sell_filled_at"],
            "symbol": row["symbol"],
            "name": stock_name(row["symbol"]),
            "strategy_id": row["strategy_id"],
            "strategy_name": strategy_name(row["strategy_id"]),
            "sell_price": row["sell_price"] / 10000.0,
            "quantity": 0,
            "buy_value_x10000": 0,
            "gross_pnl": 0,
            "net_values": [],
            "earliest_buy_date": buy_date,
            "sell_date": sell_date,
            "lots": [],
        })
        trade["quantity"] += row["quantity"]
        trade["buy_value_x10000"] += row["quantity"] * row["buy_price"]
        trade["gross_pnl"] += row["realized_pnl"]
        trade["net_values"].append(row["net_realized_pnl"])
        trade["earliest_buy_date"] = min(trade["earliest_buy_date"], buy_date)
        trade["lots"].append({
            "match_id": row["match_id"],
            "buy_fill_id": row["buy_fill_id"],
            "sell_fill_id": row["sell_fill_id"],
            "buy_date": buy_date.isoformat(),
            "quantity": row["quantity"],
            "buy_price": row["buy_price"] / 10000.0,
            "holding_days": (sell_date - buy_date).days,
            "gross_pnl": row["realized_pnl"],
            "net_pnl": row["net_realized_pnl"],
        })

    trades = []
    for trade in grouped.values():
        net_known = all(value is not None for value in trade.pop("net_values"))
        net_pnl = sum(lot["net_pnl"] for lot in trade["lots"]) if net_known else None
        trade["weighted_buy_price"] = (
            trade["buy_value_x10000"] / trade["quantity"] / 10000.0
        )
        trade["net_pnl"] = net_pnl
        trade["cost_twd"] = trade["gross_pnl"] - net_pnl if net_known else None
        trade["return_pct"] = (
            net_pnl * 1_000_000 / trade["buy_value_x10000"] if net_known else None
        )
        trade["holding_days"] = (
            trade.pop("sell_date") - trade.pop("earliest_buy_date")
        ).days
        trade.pop("buy_value_x10000")
        trades.append(trade)
    return trades


def build_trade_day_summary(trades: list[dict]) -> dict:
    all_net_known = all(trade["net_pnl"] is not None for trade in trades)
    return {
        "count": len(trades),
        "gross_pnl": sum(trade["gross_pnl"] for trade in trades),
        "cost_twd": sum(trade["cost_twd"] for trade in trades) if all_net_known else None,
        "net_pnl": sum(trade["net_pnl"] for trade in trades) if all_net_known else None,
        "has_unknown_net": not all_net_known,
    }


def build_completed_trade_history(
    conn: sqlite3.Connection,
    account_id: str,
    requested_date: date | str | None = None,
) -> dict:
    dates = list_close_dates(conn, account_id)
    selected = _iso_date(requested_date) if requested_date is not None else (dates[0] if dates else None)
    trades = read_completed_trades(conn, account_id, selected) if selected else []
    newer_date = older_date = None
    if selected in dates:
        index = dates.index(selected)
        newer_date = dates[index - 1] if index > 0 else None
        older_date = dates[index + 1] if index + 1 < len(dates) else None
    return {
        "dates": dates,
        "selected_date": selected,
        "newer_date": newer_date,
        "older_date": older_date,
        "trades": trades,
        "summary": build_trade_day_summary(trades),
    }
```

Fetch FIFO rows joined to both fills, ordered by sell timestamp, sell ID, buy timestamp, and match ID. Group in Python by `sell_fill_id`; accumulate integer `quantity * buy_price` numerators and preserve `lots` in FIFO order. Finalize each group with:

```python
net_known = all(lot["net_pnl"] is not None for lot in lots)
net_pnl = sum(lot["net_pnl"] for lot in lots) if net_known else None
cost_twd = gross_pnl - net_pnl if net_known else None
return_pct = (net_pnl * 1_000_000 / buy_notional_x10000) if net_known else None
```

`build_completed_trade_history` returns:

```python
{
    "dates": dates,
    "selected_date": selected_date,
    "older_date": older_date,
    "newer_date": newer_date,
    "trades": trades,
    "summary": summary,
}
```

Dates are newest first. With no requested date, select `dates[0]`; with a requested date absent from the account's dates, preserve it and return empty trades with no adjacent navigation. Summary net and cost are `None` if any row is unknown.

- [ ] **Step 4: Run service tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_completed_trades_service.py
```

Expected: all new service tests PASS.

- [ ] **Step 5: Commit the read model**

```bash
git add src/application/services/completed_trades.py tests/unit/test_completed_trades_service.py
git commit -m "feat(web): add completed trade history read model"
```

### Task 2: Make unknown net P&L and date navigation explicit

**Files:**
- Modify: `tests/unit/test_completed_trades_service.py`
- Modify: `src/application/services/completed_trades.py`

- [ ] **Step 1: Add failing negative and boundary tests**

Add exact assertions for legacy NULL propagation and navigation:

```python
def test_any_null_match_makes_sell_and_day_net_unknown(conn):
    seed_sell_with_two_matches(conn, net_values=[100, None])
    history = build_completed_trade_history(conn, "a", date(2026, 6, 10))
    trade = history["trades"][0]
    assert trade["gross_pnl"] == 300
    assert trade["net_pnl"] is None
    assert trade["cost_twd"] is None
    assert trade["return_pct"] is None
    assert history["summary"]["gross_pnl"] == 300
    assert history["summary"]["net_pnl"] is None
    assert history["summary"]["has_unknown_net"] is True

def test_navigation_only_uses_dates_with_matches(conn):
    seed_close_dates(conn, ["2026-06-10", "2026-06-12", "2026-06-20"])
    history = build_completed_trade_history(conn, "a", date(2026, 6, 12))
    assert history["newer_date"] == "2026-06-20"
    assert history["older_date"] == "2026-06-10"
```

Add same-day holding period = 0, Friday-to-Monday calendar holding period = 3, empty-account behavior, and a valid non-trade date returning an honest empty result.

- [ ] **Step 2: Run only the new boundary tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_completed_trades_service.py -k 'null or navigation or holding or empty'
```

Expected: FAIL on unimplemented NULL propagation or navigation fields.

- [ ] **Step 3: Complete NULL propagation and navigation without estimates**

Do not `COALESCE(net_realized_pnl, realized_pnl)`. Preserve unknown values at lot, SELL, and day levels. Compute holding days with parsed ISO dates:

```python
holding_days = (sell_date - earliest_buy_date).days
lot_holding_days = (sell_date - buy_date).days
```

- [ ] **Step 4: Run the whole service test file**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_completed_trades_service.py
```

Expected: PASS with no skipped cases.

- [ ] **Step 5: Commit boundary behavior**

```bash
git add src/application/services/completed_trades.py tests/unit/test_completed_trades_service.py
git commit -m "test(web): cover completed trade history boundaries"
```

### Task 3: Wire the independent trade date into the dashboard

**Files:**
- Modify: `tests/unit/test_web_server.py`
- Modify: `src/application/services/dashboard.py`
- Modify: `src/web/server.py`

- [ ] **Step 1: Write failing route and regression tests**

Seed a temporary DB and assert:

```python
response = client.get("/?account=a&view_date=2026-06-21&trade_date=2026-06-10")
assert response.status_code == 200
assert "歷史交易紀錄" in response.text
assert "2026-06-10" in response.text
assert "sell-1" in response.text
```

Also assert malformed `trade_date=not-a-date` returns 422, an account change without `trade_date` selects that account's latest close date, and the existing Tab labels remain in their current order exactly once.

- [ ] **Step 2: Run the focused Web tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_web_server.py -k 'completed_trade or malformed_trade_date or tab_nav'
```

Expected: FAIL because the route does not accept `trade_date` and no card is rendered.

- [ ] **Step 3: Add typed routing and dashboard aggregation**

Change the route signature to:

```python
def index(
    request: Request,
    account: str | None = Query(default=None),
    view_date: str | None = Query(default=None),
    trade_date: date | None = Query(default=None),
):
```

Extend `build_dashboard` with a final `trade_date=None` parameter and attach:

```python
"completed_trade_history": build_completed_trade_history(
    conn, account_id, requested_date=trade_date
),
```

Keep the existing `view_date` handling and all existing dictionary keys unchanged.

- [ ] **Step 4: Run route and existing dashboard tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_web_server.py -k 'dashboard or completed_trade or trade_date or tab_nav'
```

Expected: focused tests PASS; if the repository's existing TestClient hang reproduces, capture the hanging test name, terminate it, and verify the route with a local uvicorn HTTP probe instead of reporting a false pass.

- [ ] **Step 5: Commit route wiring**

```bash
git add src/application/services/dashboard.py src/web/server.py tests/unit/test_web_server.py
git commit -m "feat(web): expose completed trades by close date"
```

### Task 4: Render the card and interaction without changing Tabs

**Files:**
- Modify: `src/web/templates/dashboard.html`
- Modify: `src/web/static/style.css`
- Modify: `tests/unit/test_web_server.py`

- [ ] **Step 1: Add failing HTML structure tests**

Assert the response contains the existing equity data block before the new heading, native disclosure rows, preserved query parameters, honest NULL text, and unchanged Tabs:

```python
body = response.text
assert body.index('id="equityCurveData"') < body.index("歷史交易紀錄")
assert '<details name="completed-trade-row"' in body
assert "舊資料無法回算完整費稅" in unknown_net_response.text
assert body.count('class="tab-btn') == 5
```

- [ ] **Step 2: Run the new rendering tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_web_server.py -k 'completed_trade_history_render or completed_trade_unknown_net or tab_nav_unchanged'
```

Expected: FAIL because the template card and styles are absent.

- [ ] **Step 3: Add the server-rendered card below the equity chart**

Use a GET form with hidden `account` and `view_date`, a date selector populated only from `history.dates`, and links for `older_date`/`newer_date`. Render summary cards, then one `<details name="completed-trade-row">` per trade. Render `—` plus the legacy explanation whenever `net_pnl is none`. Show full fill IDs only inside the expanded lot rows.

Do not add, remove, reorder, or rename anything inside the existing `.tab-nav`.

- [ ] **Step 4: Add scoped CSS and motion**

Add `.completed-trades-*` classes only. The disclosure icon uses two pseudo-element bars; `details[open]` rotates/fades the vertical bar. Include:

```css
@media (prefers-reduced-motion: reduce) {
  .completed-trades-toggle,
  .completed-trades-toggle::before,
  .completed-trades-toggle::after,
  .completed-trades-detail {
    transition: none;
    animation: none;
  }
}
```

Use dark text on white rows and the existing horizontal-scroll pattern below the project's mobile breakpoint.

- [ ] **Step 5: Run focused service and Web tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_completed_trades_service.py tests/unit/test_web_server.py -k 'completed_trade or trade_date or tab_nav'
```

Expected: all selected tests PASS, with no writes to application DB.

- [ ] **Step 6: Commit UI behavior**

```bash
git add src/web/templates/dashboard.html src/web/static/style.css tests/unit/test_web_server.py
git commit -m "feat(web): render completed trade history card"
```

### Task 5: Browser, live-data, docs, and regression verification

**Files:**
- Modify: `docs/development/ui-development.md`
- Modify: `docs/development/todo.md`

- [ ] **Step 1: Start an isolated Web server against a copied DB**

Copy `data/app.db` to a temporary path, point a temporary config at that copy, and start uvicorn on a non-live port. Never point implementation verification at a writable production DB.

- [ ] **Step 2: Verify desktop and mobile interaction with Playwright**

Check `simulation-main&trade_date=2026-08-27`:

- The existing five Tabs retain labels and order.
- The equity chart precedes the new card vertically.
- Three SELL rows render.
- Rows start collapsed.
- Clicking a row opens it and changes the icon from＋to−.
- Clicking another row closes the first.
- Keyboard Enter/Space operates disclosure.
- At mobile width the table scrolls and the Tab bar remains unchanged.

- [ ] **Step 3: Perform read-only live-data reconciliation**

Run a Python SQLite query against `data/app.db` and compare service output for `simulation-main / 2026-08-27`:

```text
SELL rows: 3
gross P&L: +1,015 TWD
cost: 404 TWD
net P&L: +611 TWD
```

Record SQLite `total_changes == 0` and unchanged row counts for `fills`, `fifo_matches`, and `cash_ledger`.

- [ ] **Step 4: Run regression tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_completed_trades_service.py tests/unit/test_web_server.py
.venv/bin/python -m pytest tests/
```

Expected: focused tests PASS. Full suite should PASS; if the pre-existing Shioaji SDK segmentation fault or unrelated baseline integration failure reproduces, report the exact command, failing test, and exit code separately rather than claiming full green.

- [ ] **Step 5: Update documentation with verified behavior and counts**

Document `trade_date`, one-SELL-per-row grouping, legacy NULL behavior, disclosure interaction, unchanged Tab scope, and the actual test results. Do not write a green test count until the command has completed successfully.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/development/ui-development.md docs/development/todo.md
git commit -m "docs: document completed trade history"
```

- [ ] **Step 7: Final branch audit**

Run:

```bash
git diff master...HEAD --check
git status --short
git log --oneline --decorate -6
```

Expected: only planned feature commits exist in the isolated branch; the main workspace's unrelated `.superpowers/`, forward-test report, and review script are absent from the feature commits.
