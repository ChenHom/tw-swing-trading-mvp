# Sector Flow v1 Design

## Goal

Build a cache-first CLI report that answers, for each available trading day in
`2026-09-24` through `2026-10-01`:

- which industry categories had the largest institutional net buying;
- how much of that came from foreign investors, investment trusts, and dealers;
- which stocks contributed most to each category;
- whether the latest weekly large-holder distribution moved toward or away from
  those categories.

The first delivery is JSON plus Markdown. It does not change candidate scores,
the dashboard, daily automation, Telegram, or any Shioaji path.

## Why official-first

FinMind can return a whole market for one date only on paid tiers for the
institutional and holding-distribution datasets, and its industry-chain money
flow dataset is Sponsor-only. A per-stock loop would exceed the project's
quota and reliability expectations.

V1 therefore uses official, no-key market-wide sources for the observations and
the existing FinMind stock-info cache only as taxonomy metadata:

| Data | Primary source | Frequency |
|---|---|---|
| Listed institutional trades | TWSE T86 daily report | Daily after close |
| OTC institutional trades | TPEx institutional daily report | Daily after close |
| Listed close prices | TWSE all-stock daily close | Daily after close |
| OTC close prices | TPEx all-stock daily close | Daily after close |
| Large-holder distribution | TDCC OpenAPI `/v1/opendata/1-5` | Weekly |
| Industry category and name | latest cached `TaiwanStockInfo` row per stock | Metadata |

The optional Sponsor dataset `TaiwanStockIndustryChainMoneyFlow` is not part of
V1. Its `trading_money` is turnover distribution, not institutional net flow.

## Commands

External access and deterministic reporting stay separate:

```bash
python3 -m app market sync-sector-flow \
  --start-date 2026-09-24 \
  --end-date 2026-10-01 \
  --cache-dir data/raw

python3 -m app report sector-flow \
  --start-date 2026-09-24 \
  --end-date 2026-10-01 \
  --cache-dir data/raw \
  --output reports/2026-09-24_2026-10-01-sector-flow.json \
  --report-output reports/2026-09-24_2026-10-01-sector-flow.md
```

`market sync-sector-flow` performs HTTP GET requests and writes raw responses.
`report sector-flow` never uses the network and can be replayed entirely from
cache.

## Raw cache contract

Raw provider payloads are preserved without destructive rewriting:

```text
data/raw/twse/T86/{date}/market.json
data/raw/twse/MI_INDEX/{date}/market.json
data/raw/tpex/institutional/{date}/market.json
data/raw/tpex/daily_close/{date}/market.json
data/raw/tdcc/holding_distribution/{as_of_date}/market.json
```

Each successful fetch first validates the provider status, payload shape, and
embedded date, then writes atomically. An HTTP 200 response with no rows or a
provider-level error does not overwrite an existing successful cache file.
Non-trading dates are recorded in the ingestion summary as `no_data` and do not
produce fabricated empty trading-day files.

The HTTP adapter has a finite timeout, a response-size limit, and no credential
or cookie support. Tests inject a fake client and never reach external hosts.

## Normalized records

Provider-specific parsing ends at immutable normalized records:

```python
@dataclass(frozen=True)
class InstitutionalFlowRow:
    trading_date: str
    market: str
    symbol: str
    name: str
    foreign_net_shares: int
    investment_trust_net_shares: int
    dealer_net_shares: int
    institutional_net_shares: int


@dataclass(frozen=True)
class ClosePriceRow:
    trading_date: str
    market: str
    symbol: str
    close: float


@dataclass(frozen=True)
class HoldingDistributionRow:
    as_of_date: str
    symbol: str
    level: int
    people: int
    shares: int
    percent: float
```

Numeric strings may contain commas, whitespace, or parentheses. Invalid
numeric fields are schema errors, not zero. Unique provider fields are matched
by normalized field name. TPEx institutional payloads repeat the same three
labels for several investor groups, so that parser first validates the complete
documented group order and field count before using group offsets; a changed
layout is a schema error rather than a silent remap.

Only common-stock-looking symbols matching `^[1-9][0-9]{3}$` enter V1. ETFs,
ETNs, warrants, bonds, TDRs, indexes, and unclassified non-common instruments
are excluded.

## Institutional definitions

All quantities are shares, not lots:

- `foreign_net_shares`: foreign and Mainland investors, excluding foreign
  dealer proprietary trading.
- `investment_trust_net_shares`: domestic investment trusts.
- `dealer_net_shares`: the exchange-provided dealer total, including its
  proprietary and hedging components.
- `institutional_net_shares`: foreign + investment trust + dealer.

When a provider supplies both the total and components, the parser verifies the
identity. A mismatch turns that provider/date into `schema_error`; it is never
silently corrected.

The exact metric is net shares. Because the official per-stock institutional
reports do not publish execution value, V1 also calculates:

```text
estimated_net_amount_twd = net_shares * official daily close
```

Every JSON row includes `amount_method="net_shares_times_close"`, and Markdown
labels the result as an estimate. It must not be described as exact cash flow.

## Taxonomy and aggregation

V1 uses `TaiwanStockInfo.industry_category`, choosing the latest raw-cache
snapshot directory whose date is at or before the requested end date. The
row-level `date` in `TaiwanStockInfo` is not treated as a metadata observation
timestamp. This produces one non-overlapping category per stock, avoids
double-counting across multiple industry-chain memberships, and prevents
historical reports from using a future cache snapshot. A stock with no eligible
metadata row maps to `未分類`; the report records the taxonomy snapshot date and
coverage.

For each trading date and category, aggregate:

- exact foreign, investment-trust, dealer, and total institutional net shares;
- estimated TWD amounts for those four metrics;
- covered symbol count and missing-price count;
- top five positive and negative stock contributors.

The period summary sums only the available daily rows. Rankings use estimated
institutional net amount when price coverage is at least 90%; otherwise the
report is `degraded` and ranks by exact net shares with a visible reason.

Both inflow and outflow top-ten tables are emitted. A positive value means net
buy; a negative value means net sell.

## Large-holder proxy

TDCC holding distribution is a weekly ownership snapshot, not an order-flow
dataset. V1 defines the large-holder bucket as levels 12 through 15, equivalent
to holdings above 400 lots. Levels 16 and 17 are not included in the bucket;
level 17 is the total row.

For two consecutive cached snapshots, V1 calculates per stock:

```text
large_holder_share_delta = latest_large_holder_shares - prior_large_holder_shares
large_holder_percent_delta = latest_large_holder_percent - prior_large_holder_percent
```

The category estimate multiplies share delta by the latest available close and
is labelled `holding_change_proxy`, never `net_inflow`. If there are fewer than
two snapshots, or no snapshot at or before the requested end date, the large
holder section returns `status="insufficient_data"` with no numeric delta.

For the requested first-run range, this fail-closed result is acceptable and
must be shown rather than backfilled from a later snapshot.

## Output contract

The JSON artifact has this top-level shape:

```json
{
  "schema_version": 1,
  "requested_period": {
    "start_date": "2026-09-24",
    "end_date": "2026-10-01"
  },
  "observed_trading_dates": [],
  "status": "ok",
  "source_status": {},
  "taxonomy": {},
  "daily": [],
  "period_summary": [],
  "large_holder": {},
  "exclusions": {},
  "warnings": []
}
```

`status` is:

- `ok`: both markets loaded and price coverage is at least 90% for every
  observed day;
- `degraded`: at least one market, price set, or taxonomy mapping is incomplete;
- `blocked`: there are no valid institutional rows in the requested range.

Every provider status contains requested date, embedded/as-of date, row count,
cache path, and `ok`, `no_data`, `missing`, or `schema_error` state.

## Markdown report

The report contains:

1. requested range, observed trading dates, and overall data quality;
2. period institutional inflow and outflow top ten;
3. period foreign inflow and outflow top ten;
4. period investment-trust and dealer rankings;
5. per-day category rankings;
6. top contributing stocks per category;
7. weekly large-holder proxy or an explicit insufficient-data message;
8. source dates, coverage, exclusions, and warnings.

Values display shares and estimated TWD separately. The report never labels
turnover, ownership change, or an estimated amount as an exact net cash flow.

## Error handling and safety

- Requested dates are parsed as ISO dates and the start must not exceed the end.
- The range is capped at 31 calendar days in V1.
- HTTP 200 is not sufficient: provider status and embedded dates must validate.
- A provider schema change fails closed for that provider/date.
- Missing one market produces `degraded`, not a false whole-market result.
- Missing taxonomy maps a row to `未分類` and lowers taxonomy coverage.
- Missing close preserves exact share metrics but suppresses the amount for that
  stock rather than substituting zero.
- No Shioaji login, quote subscription, order, cancellation, Telegram send, or
  GitHub report publishing is part of this feature.

## Components and files

- `src/market_data/sector_flow_sources.py`: HTTP client protocol,
  official endpoints, raw cache, and provider-specific parsers.
- `src/application/reporting/sector_flow.py`: normalized dataclasses, taxonomy,
  aggregation, data-quality decisions, and JSON payload construction.
- `src/application/reporting/sector_flow_report.py`: Markdown rendering only.
- `src/cli/market.py` / `src/cli/report.py` / `src/cli/main.py`: `market sync-sector-flow`
  and `report sector-flow` composition.
- `tests/unit/test_sector_flow_sources.py`: fixtures, schema drift, dates, units, and
  cache behavior.
- `tests/unit/test_sector_flow.py`: category aggregation, institutional identities,
  coverage, period summary, exclusions, and large-holder proxy.
- `tests/unit/test_sector_flow_report.py`: stable Markdown sections and labels.
- `fixtures/sector-flow/`: small official-payload-shaped fixtures with no
  credentials.
- `docs/data-contracts.md`, `README.md`, and `docs/development-work.md`: contract,
  operator commands, evidence, and residual risks.

## Acceptance criteria

1. Fixture-driven tests prove TWSE, TPEx, TDCC, and close-price parsing without
   network access.
2. The original test suite plus new sector-flow tests passes in the repository
   `.venv`.
3. A gated real read-only ingestion for `2026-09-24` through `2026-10-01`
   writes raw caches and reports actual provider availability.
4. Re-running report generation with networking disabled produces identical
   JSON and Markdown.
5. The output identifies institutional and foreign net-buy categories for each
   available trading day and for the full range.
6. Large-holder output is either a correctly dated weekly delta or explicit
   `insufficient_data`; no daily large-holder claim is allowed.
7. Grill-me, code, security, and data-semantics reviews find no unresolved
   Critical or Important issue.
8. Documentation records exact test counts, provider dates, missing coverage,
   and that estimated amount is not an exact cash-flow figure.

## Deferred work

- Dashboard integration.
- Candidate-score integration.
- Daily scheduler, Telegram, or GitHub report publication.
- Sponsor-only `TaiwanStockIndustryChainMoneyFlow` comparison.
- Multi-membership industry-chain taxonomy.
- Broker-branch concentration as a separate large-player proxy.
- Historical TDCC archive acquisition beyond snapshots captured by this repo.

## 資料合約

以下自 tw-day-trading-lab `docs/data-contracts.md` 搬入（指令名稱已改為本 repo 版本）。


`market sync-sector-flow` 保存經日期與 schema 驗證的官方原始 JSON：

```text
data/raw/twse/T86/{date}/market.json
data/raw/twse/MI_INDEX/{date}/market.json
data/raw/tpex/institutional/{date}/market.json
data/raw/tpex/daily_close/{date}/market.json
data/raw/tdcc/holding_distribution/{as_of_date}/market.json
```

`report sector-flow` 不連網，輸出 schema version 1：

| 欄位 | 說明 |
|---|---|
| `requested_period` | 使用者要求的 ISO 日期範圍，最多 31 個 calendar days |
| `observed_trading_dates` | 至少有合法法人 row 的日期，不用假資料補休市日 |
| `dates_without_data` | 四個來源全部是 `missing` 或 `no_data` 的日期（已排序），**一律視為休市**（使用者決定，2026-10-03）；Markdown 標示為「休市日」。不使用假日曆，也不使報告降級。任一來源 `schema_error` 的日期不會列在這裡 |
| `category_overlap` / `category_overlap_note` | 固定 `true` 與說明：一檔股票可能同時計入多個族群，族群之間互有重疊，不可加總 |
| `status` | `ok` / `degraded` / `blocked`；每個 `degraded` 必附至少一則 `warnings` 說明原因 |
| `source_status` | 每個日期與 provider 的 cache path（相對於 `--cache-dir`，與其寫法無關）、狀態、row count |
| `taxonomy` | `TaiwanStockInfo` raw-cache snapshot date 與 mapping coverage |
| `daily` | 每日族群的法人、外資、投信、自營商淨股數與估算金額 |
| `period_summary` | 僅加總 observed days 的區間結果與個股貢獻；每列有 `is_broad`；`covered_symbol_count` / `missing_price_count` 為不重複股票數（`missing_price_count` = 至少一個觀察日缺收盤價的股票數），每日列仍為當日數 |
| `large_holder` | TDCC 週 snapshot levels 12-15 的 `holding_change_proxy` |

只有符合 `^[1-9][0-9]{3}$` 且不符合 `^91[0-9]{2}$` 的代號進入 V1；91xx 為台灣存託憑證（TDR），一律排除。分類採不晚於
報告截止日的最新 `TaiwanStockInfo/{snapshot_date}` 目錄；row-level `date`
不是 metadata 版本。精確量為 shares；金額欄位固定標示
`amount_method=net_shares_times_close`。任一交易日價格 coverage 低於 90%
時，整份排行改用精確淨股數並標為 `degraded`。單一規則：`prices_ok = 有觀察日 且 各日價格 coverage 的最小值 >= 0.9`；
排名方法（`estimated_amount` / `net_shares`）與 `status` 都用它。任一日低於 90%（即使整體 `price_coverage` 高於 90%）
→ `degraded`，並附 warning `price coverage below 90% on {date} ({pct}); rankings use exact net shares`。
頂層 `price_coverage` 仍為整體比例。

原始 payload 完整性：官方計數欄位存在且與實際列數不同（TWSE T86 頂層 `total`；TPEx 各 table 的 `totalCount`）→
`ProviderSchemaError`（訊息含兩個數字）；欄位不存在則不檢查（MI_INDEX 無計數欄位）。payload 不是 JSON object
一律 `ProviderSchemaError`。cache 檔不是合法 JSON、不是 UTF-8、或為非 object，report 端記為 `schema_error`，
ingest 端視為無效 cache 並重新抓取，不會 traceback。TPEx table 內嵌日期與請求日期不同仍是 `schema_error`
（fail closed；離線無法驗證 TPEx 休市行為），不歸為 `no_data`。

### `large_holder`（TDCC 持股變化代理）

- 範圍：只計入該期間「上市櫃宇宙」＝任一觀察日法人 flow rows 出現過的代號；且只計入兩個 snapshot 都出現的代號
  （期間內新上市／下市的股票不會把整筆持股算成變化）。排除數量輸出於
  `excluded_symbols: {"outside_listed_universe": n, "not_in_both_snapshots": n}`。
- 視窗：配對為「不晚於 `end_date` 的最新 snapshot」與其前一個。常數 `TDCC_MAX_STALENESS_DAYS = 7`、
  `TDCC_MAX_SNAPSHOT_GAP_DAYS = 14`。`latest < start_date - 7 天` → `status=insufficient_data`,
  `reason=latest_snapshot_too_old`；`latest - prior > 14 天` → `reason=snapshots_not_consecutive_weeks`。
  這兩種情況都輸出 `latest_as_of_date` 與 `prior_as_of_date`，不輸出任何數值。不足兩個 snapshot 仍為
  `fewer_than_two_eligible_snapshots`。
- 壞 cache：目錄名為日期且不晚於 `end_date` 的 snapshot 若無法解析，不會被略過；`large_holder` 變成
  `{"status": "schema_error", "errors": [{"as_of_date", "error"}]}`（無數值），報告加 warning 並降為 `degraded`。
  晚於 `end_date` 或目錄名非日期者忽略。只有會被拿來比較的那兩期（依目錄日期取最新兩個，壞檔也算在內）
  壞掉才會 `schema_error`；更舊的壞檔不影響，因為 ingest 只能抓最新一週、修不了舊檔。
- ingest：TDCC cache 已存在時會重新解析；無法讀取／解析或 `as_of_date` 不符 → 以剛抓到並驗證過的 payload 覆寫，
  記為 `via: "network"` 並附 `repaired_cache: true`；只有有效 cache 才算 `via: "cache"`。

此欄是持股變化代理，不是下單流或精確淨流入。

### CLI exit code

- `report sector-flow`：先寫出 JSON 與 Markdown，再於 `status == "blocked"` 時 exit 1；`degraded` 仍 exit 0。
- `market sync-sector-flow`：印出 summary 後，`failed > 0` 時 exit 1。

### 族群細看（`--category` / `--top`）

`report sector-flow --category NAME [--category NAME ...] [--top N]`（`--top` 預設 10，須 >= 1）。
名稱先經 `CATEGORY_SYNONYMS` 正規化（`金融業` = `金融保險`），重複者去除、保留請求順序。
若某分類在該期間沒有任何有法人資料的成員，exit 並印出可用分類名稱，**不寫任何輸出檔**。
**未帶 `--category` 時 JSON 與 Markdown 與先前完全相同（沒有任何新 key）。**

僅在帶 `--category` 時，頂層多一個 `category_detail`，每個請求分類一個物件：

- `category`、`is_broad`、`ranking_method`、`top`、`member_count`（期間內有法人資料的相異 (market, symbol)），
  以及與該分類 `period_summary` 列完全相同的股數／金額欄位（含 `covered_symbol_count`、`missing_price_count`、`amount_method`）。
- `subcategories`（僅大類）：成員依其「其他分類」分組（其他大類不算子類，例如 `化學生技醫療` 不會出現在 `電子工業` 的子類），只屬大類者歸入 `（僅大類）`；
  欄位 `category`、`member_count`、四種股數、`estimated_institutional_net_amount_twd`（僅計有收盤價的列，與 `period_summary` 算法一致），
  依排名指標由大到小。`subcategory_overlap: true` 與 `subcategory_overlap_count`（落在多個群組的成員數）：
  一檔股票有兩個子類時會同時計入兩組，子類之間不可加總。
- `top_inflows` / `top_outflows`：最多 N 檔，選取與排序規則同 `period_summary` 的 contributors
  （`estimated_amount` 模式用個股期間估算金額，任一觀察日缺收盤價者略過；`net_shares` 模式用法人淨股數；同值以代號排序）。
  每檔含 `market`、`symbol`、`name`、外資／投信／自營商／法人淨股數、`estimated_net_amount_twd`（任一觀察日缺價為 `null`）、
  `share_of_side_pct`、`daily`。
- `share_of_side_pct` = 該股排名指標 ÷ 該分類**全部成員**中同號（流入為正、流出為負）指標總和 × 100，四捨五入到 2 位；
  分母不限於前 N 檔，N 涵蓋全部成員時單邊加總為 100。
- `daily`：每個觀察交易日一筆 `{trading_date, institutional_net_shares, estimated_net_amount_twd}`；
  該日無資料列為 `0` / `0.0`，有資料列但缺收盤價時金額為 `null`。

Markdown 在 `## 資料品質與限制` 之前為每個分類加 `## 族群細看：{名稱}`：摘要行、大類的子類小計表與重疊註記、
流入／流出前 N 名表、以及前 N 名個股的逐日表（數值為排名指標）。觀察交易日超過 10 天時省略逐日表，
改印一行說明逐日數據在 JSON `category_detail[].top_*[].daily`。

### Sector Flow V1 分類與降級規則

- 多分類：`TaiwanStockInfo` 同一股票可有多列分類，該股票計入其全部（正規化後）分類，
  股數與估算金額加到每一個分類。因此族群之間互有重疊，**不可跨族群加總**。
  `TaxonomyEntry.categories` 為排序、去重後的 tuple，與 cache 列順序無關。
- 名稱正規化（TPEx／舊名 → 標準名）：其他電子類→其他電子業、居家生活類→居家生活、
  數位雲端類→數位雲端、綠能環保類→綠能環保、運動休閒類→運動休閒、金融業→金融保險、
  農業科技業→農業科技、觀光事業→觀光餐旅。
- 非產業標籤：`創新板股票`／`創新版股票` 是板別，一律捨棄；`其他` 為 catch-all，
  只在該股票沒有其他分類時保留。全部被捨棄者歸 `未分類`。`taxonomy.coverage`
  = 至少有一個分類的 flow row 比例。
- `is_broad`：`電子工業`、`化學生技醫療` 為大類（`BROAD_CATEGORIES`），與細類放在同一排行，
  Markdown 於名稱加註「（大類）」。
- 交易日判定：四個來源（twse／tpex 法人、twse／tpex 收盤）至少一個 `ok` 即視為交易日；
  該日任一來源非 `ok`（即使沒有 flow row）→ `incomplete_source`、報告 `degraded`，並加
  warning `incomplete sources on {date}: {source}={state}, ...`。四個來源皆為 `missing` 或
  `no_data` 的日期視為休市，只列入 `dates_without_data`，不降級；只要有一個來源 `schema_error`，
  即使沒有任何 `ok` 也算 incomplete 並降級（格式錯誤不可能是休市）。
  已知風險：某日完全沒跑 ingest 也會被當成休市；抓取失敗不受影響，因為 `market sync-sector-flow` 有失敗來源時 exit 1，
  在抓取當下就會被發現。
- Markdown：`estimated_amount` 模式在區間排行下附註「排名依估算金額，淨股數與淨金額可能正負相反」；每日區塊除前 10
  名外，另列最大流出（依同一排名指標，僅負值，最多 10 筆）。
- 個股貢獻排序：`ranking_method=estimated_amount` 時以 `estimated_net_amount_twd`
  選正負並排序（無收盤價者不入榜）；`net_shares` 時以 `institutional_net_shares`。同值依代號。
