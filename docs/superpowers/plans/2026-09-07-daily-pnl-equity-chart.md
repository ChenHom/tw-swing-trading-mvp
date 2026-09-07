# Daily P&L Equity Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add flow-adjusted daily profit/loss bars to the existing equity chart and move completed trades into a persistent standalone tab between capital and positions.

**Architecture:** Enrich the existing `read_equity_curve()` read model with a nullable `daily_pnl` derived from consecutive equity snapshots after subtracting external capital flows. Reuse the existing JSON payload and Chart.js file, selecting a mixed line/bar configuration only when `daily_pnl` is present so backtest pages keep their old chart. Move the existing completed-trade markup without changing its read model, and preserve the selected tab in the URL hash.

**Tech Stack:** Python 3.11, SQLite, FastAPI/Jinja2, vanilla JavaScript, Chart.js, pytest, Playwright CLI.

---

### Task 1: Add flow-adjusted daily P&L to the equity read model

**Files:**
- Modify: `tests/unit/test_equity_snapshots_service.py`
- Modify: `src/application/services/equity_snapshots.py`

- [x] **Step 1: Write failing service tests**

Update the existing mapping assertion to require `daily_pnl: None` on the first point and the arithmetic delta on the second. Add one test with consecutive snapshots and ledger rows proving:

```python
assert rows[0]["daily_pnl"] is None
assert rows[1]["daily_pnl"] == 2_000   # +12k equity minus +10k INITIAL_DEPOSIT
assert rows[2]["daily_pnl"] == -1_000  # -6k equity minus -5k withdrawal
assert rows[3]["daily_pnl"] == 5_000   # DIVIDEND remains investment income
```

Add 181 ordered snapshots and assert `read_equity_curve()` returns all 181 rows, proving the old 180-point cap is gone.

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_equity_snapshots_service.py -q
```

Expected: failures because `daily_pnl` is absent and only 180 rows are returned.

- [x] **Step 3: Implement the minimal read-model change**

Change `read_equity_curve()` to select snapshots in ascending order without a limit. Read external capital flows once:

```sql
SELECT substr(occurred_at, 1, 10) AS flow_date, SUM(amount) AS amount
FROM cash_ledger
WHERE account_id = ?
  AND event_type IN ('INITIAL_DEPOSIT', 'CASH_ADJUSTMENT')
GROUP BY substr(occurred_at, 1, 10)
ORDER BY flow_date
```

Walk the ordered snapshots and ordered flows with one pointer. Set the first `daily_pnl` to `None`; for every later row, sum flows in `(previous_date, current_date]` and calculate:

```python
daily_pnl = current_equity - previous_equity - interval_external_flow
```

Keep `cash`, `position_value`, and `equity` unchanged for existing consumers.

- [x] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_equity_snapshots_service.py -q
```

Expected: all tests pass.

- [x] **Step 5: Commit the read-model change**

```bash
git add src/application/services/equity_snapshots.py tests/unit/test_equity_snapshots_service.py
git commit -m "feat(web): calculate daily account pnl"
```

### Task 2: Move completed trades into a persistent standalone tab

**Files:**
- Modify: `tests/unit/test_completed_trade_web.py`
- Modify: `src/web/templates/dashboard.html`

- [x] **Step 1: Replace the old layout test with failing tab tests**

Require six buttons and this exact order:

```python
assert body.count('class="tab-btn') == 6
assert (
    body.index("資金總覽")
    < body.index("交易紀錄")
    < body.index("持倉部位")
    < body.index("策略別損益")
)
assert 'id="tab-trades" class="tab-content"' in body
```

Extract the `tab-capital` and `tab-trades` regions and assert `completed-trades-card` is absent from capital but present in trades. Require trade navigation controls to include `#tab-trades`, and require the tab script to read/write `window.location.hash`.

- [x] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_completed_trade_web.py -q
```

Expected: failure because there are five tabs and completed trades still live in capital.

- [x] **Step 3: Implement the tab move**

Add the button immediately after capital:

```html
<button type="button" class="tab-btn" data-tab="trades" onclick="switchTab('trades', this)">
  <span class="tab-text-desktop">交易紀錄</span>
  <span class="tab-text-mobile">紀錄</span>
</button>
```

Move the complete `{% set h = d.completed_trade_history %}` card, unchanged, into `<div id="tab-trades" class="tab-content">`. Append `#tab-trades` to its form action and previous/next URLs. Give every tab button `type="button"`, `data-tab`, and pass `this` to `switchTab`.

Replace the implicit global-event handler with:

```javascript
function switchTab(tabId, button, updateHash) {
  // toggle active button/content
  if (updateHash !== false) history.replaceState(null, '', '#tab-' + tabId);
}
var initialTab = window.location.hash.replace('#tab-', '');
var initialButton = document.querySelector('.tab-btn[data-tab="' + initialTab + '"]');
if (initialButton) switchTab(initialTab, initialButton, false);
```

- [x] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_completed_trade_web.py -q
```

Expected: all tests pass and existing FIFO/unknown-net assertions remain green.

- [x] **Step 5: Commit the tab change**

```bash
git add src/web/templates/dashboard.html tests/unit/test_completed_trade_web.py
git commit -m "feat(web): move trade history to tab"
```

### Task 3: Render daily P&L as bars without breaking backtest charts

**Files:**
- Create: `tests/unit/test_equity_chart_script.py`
- Modify: `src/web/static/js/backtest-charts.js`
- Modify: `src/web/templates/dashboard.html`

- [x] **Step 1: Write a failing static contract test**

Read `backtest-charts.js` and require tokens that prove the dual behavior exists:

```python
assert "daily_pnl" in script
assert "type: 'bar'" in script
assert "yPnl" in script
assert "#e53e3e" in script and "#38a169" in script
assert "position_value" in script and "cash" in script
```

Also require the dashboard chart’s accessible label to mention both equity and daily P&L.

- [x] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_equity_chart_script.py -q
```

Expected: failure because the script has no daily-P&L dataset or secondary axis.

- [x] **Step 3: Implement the mixed chart configuration**

Detect dashboard data with `Object.prototype.hasOwnProperty.call(rows[0], 'daily_pnl')`.

For dashboard rows, create:

```javascript
[
  { type: 'line', label: '總權益', data: ..., yAxisID: 'y', borderColor: '#1e293b' },
  { type: 'bar', label: '每日損益', data: ..., yAxisID: 'yPnl',
    backgroundColor: rows.map(r => r.daily_pnl >= 0 ? '#e53e3e' : '#38a169') }
]
```

Configure `yPnl` on the right with `beginAtZero: true`, currency ticks, and no grid overlay except a visible zero line. Format tooltip values as signed TWD for the P&L dataset. If `daily_pnl` is absent, retain the current total-equity/cash/position-value datasets and single-axis behavior used by backtest detail pages.

Change the dashboard canvas accessible label to `歷史總權益與每日損益圖`.

- [x] **Step 4: Run chart and Web tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_equity_chart_script.py tests/unit/test_completed_trade_web.py tests/unit/test_web_server.py -q
```

Expected: all runnable tests pass; any project-documented module skip remains an explicit skip.

- [x] **Step 5: Commit the chart change**

```bash
git add src/web/static/js/backtest-charts.js src/web/templates/dashboard.html tests/unit/test_equity_chart_script.py
git commit -m "feat(web): plot daily pnl with equity"
```

### Task 4: Documentation, regression verification, and live deployment

**Files:**
- Modify: `docs/development/engineering-log.md`
- Modify: `docs/development/ui-development.md`

- [x] **Step 1: Update canonical UI documentation**

Document the adjusted daily-P&L formula, external-flow event list, first-point `null`, mixed chart semantics, all-history behavior, new Tab order, and hash persistence. Add a dated engineering-log entry with the chosen mock option and verification evidence.

- [x] **Step 2: Run focused and full regression checks**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_equity_snapshots_service.py tests/unit/test_completed_trade_web.py tests/unit/test_equity_chart_script.py -q
.venv/bin/python -m pytest tests/ -q
git diff --check
```

Expected: focused tests all pass; full suite has zero failures, with only already documented skips/warnings.

- [x] **Step 3: Perform browser verification**

Start or reuse the local Web app. With Playwright, verify desktop and 390px mobile layouts for `simulation-main`: the mixed chart canvas exists, the trade tab is second, trade-date navigation remains on `#tab-trades`, FIFO rows expand, and the document does not overflow horizontally.

- [ ] **Step 4: Commit documentation**

```bash
git add docs/development/engineering-log.md docs/development/ui-development.md docs/superpowers/specs/2026-09-07-daily-pnl-equity-chart-design.md docs/superpowers/plans/2026-09-07-daily-pnl-equity-chart.md
git commit -m "docs: record daily pnl dashboard behavior"
```

- [ ] **Step 5: Deploy and read back live endpoints**

Run:

```bash
sudo systemctl restart trading-web.service
curl -fsS http://127.0.0.1:8800/healthz
curl -fsS -o /dev/null -w "首頁: %{http_code}\n" http://127.0.0.1:8800/
```

Expected: health endpoint succeeds and homepage returns 200. If interactive sudo is unavailable, report the exact blocker and do not claim live deployment complete.
