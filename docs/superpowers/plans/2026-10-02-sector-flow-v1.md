> 注意：本計畫為 tw-day-trading-lab 時期的歷史紀錄，2026-10-03 已搬入本 repo；原檔案路徑與指令已改寫為本 repo 版本，TDD 步驟中提到的 lab 專屬指令（如 `samples summary`）僅為歷史。

# Sector Flow V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a cache-first JSON and Markdown report showing daily and period institutional sector flows for 2026-09-24 through 2026-10-01, with a fail-closed weekly large-holder proxy.

**Architecture:** A provider adapter fetches and validates official TWSE, TPEx, and TDCC payloads into immutable raw cache files. Pure normalization and aggregation code joins exact net-share observations to official closes and date-bounded FinMind taxonomy metadata; a separate renderer turns the resulting payload into Markdown. CLI composition is the only network boundary, and report generation is offline and deterministic.

**Tech Stack:** Python 3 standard library (`argparse`, `dataclasses`, `datetime`, `json`, `urllib`), existing `unittest` suite, official TWSE/TPEx/TDCC JSON endpoints.

---

## File map

- Create `src/market_data/sector_flow_sources.py`: endpoint builders, bounded HTTP port, raw-cache writes, schema/date validation, and provider parsers.
- Create `src/application/reporting/sector_flow.py`: normalized records, taxonomy loading, category aggregation, coverage/status decisions, and output payload.
- Create `src/application/reporting/sector_flow_report.py`: deterministic Markdown rendering only.
- Modify `src/cli/{main,market,report}.py`: compose `market sync-sector-flow` and `report sector-flow` without importing network code into core logic.
- Create `tests/unit/test_sector_flow_sources.py`: official-payload-shaped parser and cache tests.
- Create `tests/unit/test_sector_flow.py`: taxonomy, daily/period aggregation, coverage, exclusions, and large-holder tests.
- Create `tests/unit/test_sector_flow_report.py`: report labels, sections, and deterministic rendering tests.
- Create `fixtures/sector-flow/*.json`: minimal TWSE, TPEx, and TDCC provider-shaped payloads.
- Modify `README.md`, `docs/data-contracts.md`, `docs/development-work.md`, and `docs/mvp-roadmap.md`: commands, contract, evidence, status, and residual risks.

### Task 1: Normalize official provider payloads

**Files:**
- Create: `fixtures/sector-flow/twse-t86.json`
- Create: `fixtures/sector-flow/twse-mi-index.json`
- Create: `fixtures/sector-flow/tpex-institutional.json`
- Create: `fixtures/sector-flow/tpex-daily-close.json`
- Create: `fixtures/sector-flow/tdcc-holding-distribution.json`
- Create: `tests/unit/test_sector_flow_sources.py`
- Create: `src/market_data/sector_flow_sources.py`

- [ ] **Step 1: Add minimal official-payload-shaped fixtures**

Use two common stocks (`2330`, `6488`) plus one ETF-like code per market. Preserve the real provider envelope and exact Chinese field labels. The TWSE T86 fixture uses top-level `fields/data`; TWSE MI_INDEX uses `tables`; both TPEx fixtures use `tables[0]`; TDCC uses object rows with `證券代號`, `持股分級`, `股數`, `人數`, `占集保庫存數比例%`, and `資料日期`.

- [ ] **Step 2: Write failing parser tests**

```python
class SectorFlowSourceParserTest(unittest.TestCase):
    def test_twse_t86_parses_exact_components_and_filters_non_common(self):
        payload = load_fixture("twse-t86.json")
        rows = parse_twse_institutional(payload, "2026-09-24")
        self.assertEqual([row.symbol for row in rows], ["2330"])
        self.assertEqual(rows[0].institutional_net_shares, 1_250_000)

    def test_tpex_roc_date_and_repeated_fields_are_parsed_by_semantic_group(self):
        payload = load_fixture("tpex-institutional.json")
        rows = parse_tpex_institutional(payload, "2026-09-24")
        self.assertEqual(rows[0].foreign_net_shares, 900_000)
        self.assertEqual(rows[0].dealer_net_shares, -25_000)

    def test_invalid_numeric_value_is_schema_error_not_zero(self):
        payload = load_fixture("twse-t86.json")
        payload["data"][0][4] = "--"
        with self.assertRaises(ProviderSchemaError):
            parse_twse_institutional(payload, "2026-09-24")

    def test_tdcc_bom_date_key_and_levels_are_normalized(self):
        rows = parse_tdcc_holdings(load_fixture("tdcc-holding-distribution.json"))
        self.assertEqual(rows[0].as_of_date, "2026-09-24")
        self.assertEqual(rows[0].shares, 400_000)
```

- [ ] **Step 3: Run the parser tests and confirm RED**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_sources`.py

Expected: import failure because `sector_flow_sources` does not exist.

- [ ] **Step 4: Implement normalized records and strict parsers**

Define immutable `InstitutionalFlowRow`, `ClosePriceRow`, `HoldingDistributionRow`, `ProviderSchemaError`, `_parse_int`, `_parse_float`, `_roc_to_iso`, `_field_index`, and these public functions:

```python
def parse_twse_institutional(payload: Mapping[str, Any], requested_date: str) -> list[InstitutionalFlowRow]: ...
def parse_twse_closes(payload: Mapping[str, Any], requested_date: str) -> list[ClosePriceRow]: ...
def parse_tpex_institutional(payload: Mapping[str, Any], requested_date: str) -> list[InstitutionalFlowRow]: ...
def parse_tpex_closes(payload: Mapping[str, Any], requested_date: str) -> list[ClosePriceRow]: ...
def parse_tdcc_holdings(payload: Sequence[Mapping[str, Any]]) -> list[HoldingDistributionRow]: ...
```

Normalize whitespace and `<br>` in headers before matching. Reject bad dates, missing fields, row-width mismatches, and component/total identity mismatches. Match unique fields by normalized name. For TPEx institutional data, validate its complete repeated three-column investor-group layout and field count before using documented group offsets. Keep only symbols matching `^[1-9][0-9]{3}$`.

- [ ] **Step 5: Run source tests and confirm GREEN**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_sources`.py

Expected: all parser tests pass.

- [ ] **Step 6: Commit normalized parser slice**

```bash
git add fixtures/sector-flow tests/unit/test_sector_flow_sources.py src/market_data/sector_flow_sources.py
git commit -m "feat: parse official sector flow sources"
```

### Task 2: Add bounded ingestion and immutable raw cache

**Files:**
- Modify: `tests/unit/test_sector_flow_sources.py`
- Modify: `src/market_data/sector_flow_sources.py`

- [ ] **Step 1: Write failing endpoint, validation, and cache tests**

```python
class SectorFlowIngestionTest(unittest.TestCase):
    def test_endpoint_builders_include_requested_date(self):
        requests = build_daily_requests("2026-09-24")
        self.assertIn("date=20260924", requests[0].url)
        self.assertIn("date=2026%2F09%2F24", requests[2].url)

    def test_existing_successful_cache_is_not_fetched_again(self):
        first = ingest_sector_flow(self.cache_dir, "2026-09-24", "2026-09-24", self.client)
        second = ingest_sector_flow(self.cache_dir, "2026-09-24", "2026-09-24", self.client)
        self.assertEqual(first["fetched"], 4)
        self.assertEqual(second["cached"], 4)

    def test_no_data_does_not_overwrite_valid_cache(self): ...
    def test_oversize_response_is_rejected_before_json_decode(self): ...
    def test_provider_date_mismatch_is_schema_error(self): ...
```

- [ ] **Step 2: Run the ingestion tests and confirm RED**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_sources.py

Expected: missing request/client/ingestion symbols.

- [ ] **Step 3: Implement the HTTP port and request builders**

```python
@dataclass(frozen=True)
class SourceRequest:
    provider: str
    dataset: str
    requested_date: str | None
    url: str
    cache_path: Path

class JsonHttpClient(Protocol):
    def get_json(self, url: str) -> Any: ...

class UrllibJsonHttpClient:
    def __init__(self, timeout_seconds: float = 20, max_bytes: int = 20_000_000): ...
    def get_json(self, url: str) -> Any: ...
```

Lock endpoints to:

```text
https://www.twse.com.tw/rwd/zh/fund/T86?date={YYYYMMDD}&selectType=ALLBUT0999&response=json
https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={YYYYMMDD}&type=ALLBUT0999&response=json
https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?date={YYYY%2FMM%2FDD}&type=Daily&sect=EW
https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={YYYY%2FMM%2FDD}&type=EW
https://openapi.tdcc.com.tw/v1/opendata/1-5
```

- [ ] **Step 4: Implement fail-closed caching and range orchestration**

```python
def ingest_sector_flow(
    *, cache_dir: Path, start_date: str, end_date: str, client: JsonHttpClient
) -> dict[str, Any]: ...
```

Validate ISO dates and the 31-day cap. Fetch four daily sources for every calendar date, classifying empty/non-trading responses as `no_data`. Fetch TDCC once, derive its embedded `as_of_date`, and cache under that date. Write validated JSON through a same-directory temporary file plus `Path.replace`; never overwrite a valid file with `no_data` or schema errors. Return per-source state, cache path, embedded date, and row count without response bodies or secrets.

- [ ] **Step 5: Run source tests and confirm GREEN**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_sources`.py

Expected: all source and ingestion tests pass.

- [ ] **Step 6: Commit ingestion slice**

```bash
git add tests/unit/test_sector_flow_sources.py src/market_data/sector_flow_sources.py
git commit -m "feat: cache official sector flow data"
```

### Task 3: Aggregate daily and period sector flows

**Files:**
- Create: `tests/unit/test_sector_flow.py`
- Create: `src/application/reporting/sector_flow.py`

- [ ] **Step 1: Write failing taxonomy and aggregation tests**

```python
class SectorFlowAggregationTest(unittest.TestCase):
    def test_taxonomy_uses_latest_row_not_after_end_date(self):
        taxonomy = load_taxonomy(self.cache_dir, end_date="2026-10-01")
        self.assertEqual(taxonomy["2330"].category, "半導體業")
        self.assertEqual(taxonomy["2330"].snapshot_date, "2026-09-30")

    def test_daily_category_sums_exact_shares_and_estimated_amounts(self):
        payload = build_sector_flow_report(
            cache_dir=self.cache_dir,
            start_date="2026-09-24",
            end_date="2026-10-01",
        )
        semi = payload["daily"][0]["categories"][0]
        self.assertEqual(semi["institutional_net_shares"], 1_250_000)
        self.assertEqual(semi["estimated_institutional_net_amount_twd"], 1_250_000 * 820.0)
        self.assertEqual(semi["amount_method"], "net_shares_times_close")

    def test_missing_close_keeps_shares_and_suppresses_amount(self): ...
    def test_period_summary_sums_only_observed_dates(self): ...
    def test_missing_market_marks_report_degraded(self): ...
    def test_no_valid_institutional_rows_marks_report_blocked(self): ...
```

- [ ] **Step 2: Run aggregation tests and confirm RED**

Run: `.venv/bin/pytest tests/unit/test_sector_flow`.py

Expected: import failure because `sector_flow` does not exist.

- [ ] **Step 3: Implement date-bounded taxonomy loading**

```python
@dataclass(frozen=True)
class TaxonomyEntry:
    symbol: str
    name: str
    category: str
    market: str
    snapshot_date: str

def load_taxonomy(cache_dir: Path, *, end_date: str) -> dict[str, TaxonomyEntry]: ...
```

Scan `finmind/TaiwanStockInfo/{snapshot_date}/*.jsonl`, choose the latest snapshot directory at or before `end_date`, and map missing values to `未分類`. Do not use the row-level `date` as the metadata version: FinMind uses that field for the security's own date, not the cache observation time. Do not alter `candidate_builder.py`.

- [ ] **Step 4: Implement pure aggregation and quality decisions**

```python
def build_sector_flow_report(
    *, cache_dir: Path, start_date: str, end_date: str
) -> dict[str, Any]: ...
```

Load and parse cached daily sources, join closes by `(market, symbol)`, join taxonomy by symbol, aggregate foreign/trust/dealer/total exact shares and estimated amounts, and retain top five positive and negative stock contributors. Emit daily rows, period summaries, observed dates, exclusions, warnings, taxonomy coverage, price coverage, and source statuses. Rank by estimated amount only when coverage is at least 90%; otherwise rank by exact shares and record the fallback.

- [ ] **Step 5: Run aggregation tests and confirm GREEN**

Run: `.venv/bin/pytest tests/unit/test_sector_flow`.py

Expected: all taxonomy, aggregation, and status tests pass.

- [ ] **Step 6: Commit aggregation slice**

```bash
git add tests/unit/test_sector_flow.py src/application/reporting/sector_flow.py
git commit -m "feat: aggregate institutional sector flows"
```

### Task 4: Add the weekly large-holder proxy

**Files:**
- Modify: `tests/unit/test_sector_flow.py`
- Modify: `src/application/reporting/sector_flow.py`

- [ ] **Step 1: Write failing large-holder tests**

```python
def test_large_holder_uses_levels_12_through_15_only(self): ...

def test_two_snapshots_emit_holding_change_proxy(self):
    result = build_large_holder_proxy(...)
    self.assertEqual(result["status"], "ok")
    self.assertEqual(result["method"], "holding_change_proxy")
    self.assertEqual(result["latest_as_of_date"], "2026-09-24")

def test_one_snapshot_is_explicitly_insufficient(self):
    result = build_large_holder_proxy(...)
    self.assertEqual(result, {
        "status": "insufficient_data",
        "reason": "fewer_than_two_eligible_snapshots",
        "latest_as_of_date": "2026-09-24",
    })

def test_snapshot_after_requested_end_date_is_not_used(self): ...
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv/bin/pytest tests/unit/test_sector_flow.py

Expected: missing `build_large_holder_proxy` or incorrect report section.

- [ ] **Step 3: Implement the fail-closed weekly proxy**

```python
def build_large_holder_proxy(
    *, holdings_by_date: Mapping[str, Sequence[HoldingDistributionRow]],
    taxonomy: Mapping[str, TaxonomyEntry],
    closes: Mapping[tuple[str, str], float],
    end_date: str,
) -> dict[str, Any]: ...
```

Choose the newest two snapshots at or before the end date. Sum levels 12-15 per stock, calculate share/percentage deltas, aggregate by category, and estimate value from the latest eligible close. Never label it `net_inflow`; return the exact insufficient-data object when fewer than two snapshots exist.

- [ ] **Step 4: Run aggregation tests and confirm GREEN**

Run: `.venv/bin/pytest tests/unit/test_sector_flow`.py

Expected: all tests pass, including one-snapshot insufficiency.

- [ ] **Step 5: Commit large-holder slice**

```bash
git add tests/unit/test_sector_flow.py src/application/reporting/sector_flow.py
git commit -m "feat: add weekly large holder proxy"
```

### Task 5: Render deterministic Markdown

**Files:**
- Create: `tests/unit/test_sector_flow_report.py`
- Create: `src/application/reporting/sector_flow_report.py`

- [ ] **Step 1: Write failing report tests**

```python
class SectorFlowReportTest(unittest.TestCase):
    def test_report_distinguishes_exact_shares_from_estimated_amount(self):
        markdown = render_sector_flow_markdown(sample_payload())
        self.assertIn("精確淨買賣股數", markdown)
        self.assertIn("估算金額（淨股數 × 收盤價）", markdown)
        self.assertNotIn("精確淨流入金額", markdown)

    def test_report_shows_insufficient_large_holder_data(self): ...
    def test_report_contains_daily_and_period_inflow_and_outflow_tables(self): ...
    def test_same_payload_renders_identically(self): ...
```

- [ ] **Step 2: Run renderer tests and confirm RED**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_report`.py

Expected: import failure because `sector_flow_report` does not exist.

- [ ] **Step 3: Implement the renderer**

```python
def render_sector_flow_markdown(payload: Mapping[str, Any]) -> str: ...
```

Render requested/observed dates, overall status, period institutional/foreign/trust/dealer inflow and outflow top tens, daily rankings, top stock contributors, large-holder status, source coverage, exclusions, and warnings. Format amounts as TWD but keep method labels explicit.

- [ ] **Step 4: Run renderer tests and confirm GREEN**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_report`.py

Expected: all renderer tests pass.

- [ ] **Step 5: Commit renderer slice**

```bash
git add tests/unit/test_sector_flow_report.py src/application/reporting/sector_flow_report.py
git commit -m "feat: render sector flow report"
```

### Task 6: Wire CLI commands and offline determinism

**Files:**
- Modify: `tests/unit/test_sector_flow_sources.py`
- Modify: `tests/unit/test_sector_flow_report.py`
- Modify: `src/cli/{main,market,report}.py`

- [ ] **Step 1: Write failing CLI composition tests**

```python
def test_parser_accepts_sector_flow_ingest_range(self):
    args = build_parser().parse_args([
        "ingest", "sector-flow", "--start-date", "2026-09-24",
        "--end-date", "2026-10-01", "--cache-dir", "data/raw",
    ])
    self.assertIs(args.func, cmd_ingest_sector_flow)

def test_parser_accepts_offline_sector_flow_report(self): ...
def test_report_command_does_not_construct_http_client(self): ...
def test_report_json_and_markdown_are_byte_stable_on_rerun(self): ...
```

- [ ] **Step 2: Run CLI tests and confirm RED**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_sources.py tests/unit/test_sector_flow_report`.py

Expected: sector-flow subcommands are rejected.

- [ ] **Step 3: Implement CLI composition**

Add imports and:

```python
def cmd_ingest_sector_flow(args: argparse.Namespace) -> None:
    summary = ingest_sector_flow(
        cache_dir=Path(args.cache_dir),
        start_date=args.start_date,
        end_date=args.end_date,
        client=UrllibJsonHttpClient(),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

def cmd_report_sector_flow(args: argparse.Namespace) -> None:
    payload = build_sector_flow_report(
        cache_dir=Path(args.cache_dir),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    write_json(Path(args.output), payload)
    write_text(Path(args.report_output), render_sector_flow_markdown(payload))
    print(args.output)
```

Register both commands with required ISO range arguments, `--cache-dir`, and required report output paths. Keep report composition network-free.

- [ ] **Step 4: Run all sector-flow tests and confirm GREEN**

Run: `.venv/bin/pytest tests/unit/test_sector_flow_sources.py tests/unit/test_sector_flow.py tests/unit/test_sector_flow_report`.py

Expected: all sector-flow tests pass.

- [ ] **Step 5: Commit CLI slice**

```bash
git add src/cli/{main,market,report}.py tests/unit/test_sector_flow_sources.py tests/unit/test_sector_flow_report.py
git commit -m "feat: expose sector flow CLI"
```

### Task 7: Document the contract and implementation status

**Files:**
- Modify: `README.md`
- Modify: `docs/data-contracts.md`
- Modify: `docs/development-work.md`
- Modify: `docs/mvp-roadmap.md`

- [ ] **Step 1: Add operator commands and data semantics**

Document the exact two commands for `2026-09-24` through `2026-10-01`, raw-cache locations, exact-share versus estimated-amount semantics, category taxonomy cutoff, common-stock filter, and large-holder weekly limitation.

- [ ] **Step 2: Record phase status without overstating external validation**

Before real ingestion, record code-complete status separately from data-run validation. Keep Shioaji, Telegram, GitHub publication, candidate scoring, and dashboard integration explicitly out of scope.

- [ ] **Step 3: Run documentation checks**

Run: `rg -n '2026-09-24|net_shares_times_close|holding_change_proxy|insufficient_data' README.md docs/data-contracts.md docs/development-work.md docs/mvp-roadmap.md`

Expected: commands and semantics are present in every relevant status/contract document.

- [ ] **Step 4: Commit documentation slice**

```bash
git add README.md docs/data-contracts.md docs/development-work.md docs/mvp-roadmap.md
git commit -m "docs: document sector flow v1"
```

### Task 8: Verify, run the requested period, review, and deliver

**Files:**
- Create at runtime: `data/raw/twse/T86/2026-09-24..2026-10-01/market.json`
- Create at runtime: `data/raw/twse/MI_INDEX/2026-09-24..2026-10-01/market.json`
- Create at runtime: `data/raw/tpex/institutional/2026-09-24..2026-10-01/market.json`
- Create at runtime: `data/raw/tpex/daily_close/2026-09-24..2026-10-01/market.json`
- Create at runtime: `data/raw/tdcc/holding_distribution/{as_of_date}/market.json`
- Create: `reports/2026-09-24_2026-10-01-sector-flow.json`
- Create: `reports/2026-09-24_2026-10-01-sector-flow.md`
- Modify: `docs/development-work.md`
- Modify: `docs/mvp-roadmap.md`

- [ ] **Step 1: Run focused and full verification**

```bash
.venv/bin/pytest tests/unit/test_sector_flow_sources.py tests/unit/test_sector_flow.py tests/unit/test_sector_flow_report.py
.venv/bin/pytest tests/ -q
python3 -m compileall -q src tests
git diff --check
```

Expected: all tests pass, compile succeeds silently, and diff check is clean.

- [ ] **Step 2: Run read-only official ingestion for the requested range**

```bash
python3 -m app market sync-sector-flow \
  --start-date 2026-09-24 \
  --end-date 2026-10-01 \
  --cache-dir data/raw
```

Expected: trading days are cached; weekend/non-trading dates report `no_data`; TDCC records its embedded snapshot date.

- [ ] **Step 3: Generate JSON and Markdown offline**

```bash
python3 -m app report sector-flow \
  --start-date 2026-09-24 \
  --end-date 2026-10-01 \
  --cache-dir data/raw \
  --output reports/2026-09-24_2026-10-01-sector-flow.json \
  --report-output reports/2026-09-24_2026-10-01-sector-flow.md
```

Expected: both artifacts exist, list observed trading dates, and show `large_holder.status=insufficient_data` unless two eligible TDCC snapshots are already cached.

- [ ] **Step 4: Prove deterministic offline replay**

Record checksums, rerun only the report command, and compare checksums:

```bash
sha256sum reports/2026-09-24_2026-10-01-sector-flow.json reports/2026-09-24_2026-10-01-sector-flow.md
```

Expected: checksums are unchanged after the second render.

- [ ] **Step 5: Run grill-me, code, data-semantics, and security reviews**

Review at minimum: schema drift failure mode, date validation, cache overwrite behavior, response-size bound, formula identities, share units, price coverage fallback, taxonomy look-ahead, weekly-versus-daily language, secret/log exposure, and absence of Shioaji side effects. Resolve all Critical and Important findings and rerun affected tests.

- [ ] **Step 6: Record actual evidence and residual risks**

Update both status documents with exact test count, source dates/row counts, observed trading dates, report status, coverage, artifact paths, large-holder outcome, and any unavailable provider/date. Do not claim exact cash flow or daily large-holder activity.

- [ ] **Step 7: Commit generated evidence and final documentation**

```bash
git add reports/2026-09-24_2026-10-01-sector-flow.json reports/2026-09-24_2026-10-01-sector-flow.md docs/development-work.md docs/mvp-roadmap.md
git commit -m "feat: deliver September sector flow report"
```

- [ ] **Step 8: Push master and verify remote head**

```bash
git push origin master
git rev-parse HEAD
git rev-parse origin/master
```

Expected: both revisions match. Report local test evidence separately from live provider availability.
