from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


# Four-digit common stocks; 91xx are Taiwan depositary receipts (TDRs) and are excluded.
COMMON_STOCK_RE = re.compile(r"^(?!91)[1-9][0-9]{3}$")
HTML_TAG_RE = re.compile(r"<[^>]+>")


class ProviderSchemaError(ValueError):
    """Raised when an official payload no longer matches its validated schema."""


class ProviderNoData(ProviderSchemaError):
    """Raised when a provider explicitly has no data for the requested date."""


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


@dataclass(frozen=True)
class SourceRequest:
    provider: str
    dataset: str
    requested_date: str | None
    url: str
    cache_path: Path


class JsonHttpClient(Protocol):
    def get_json(self, url: str) -> Any:
        """Return one decoded JSON response."""


class UrllibJsonHttpClient:
    """Small bounded no-credential HTTP adapter for official public JSON."""

    def __init__(
        self,
        timeout_seconds: float = 20,
        max_bytes: int = 20_000_000,
        min_interval_seconds: float = 3.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        # TWSE blocks IPs that send bursts; keep every request at least this far apart.
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None

    def get_json(self, url: str) -> Any:
        if self._last_request is not None:
            wait = self.min_interval_seconds - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()
        request = urllib.request.Request(url, headers={"User-Agent": "tw-day-trading-lab/sector-flow-v1"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > self.max_bytes:
                raise ProviderSchemaError("response exceeds size limit")
            raw = response.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ProviderSchemaError("response exceeds size limit")
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderSchemaError("response is not valid UTF-8 JSON") from exc


def _header(value: object) -> str:
    return re.sub(r"\s+", "", HTML_TAG_RE.sub("", str(value))).lstrip("\ufeff")


def _parse_int(value: object) -> int:
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ProviderSchemaError(f"invalid integer: {value!r}")
    return int(text)


def _parse_float(value: object) -> float:
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        result = float(text)
    except ValueError as exc:
        raise ProviderSchemaError(f"invalid number: {value!r}") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ProviderSchemaError(f"invalid number: {value!r}")
    return result


def _field_index(fields: Sequence[object], label: str) -> int:
    normalized = [_header(item) for item in fields]
    try:
        return normalized.index(_header(label))
    except ValueError as exc:
        raise ProviderSchemaError(f"missing field: {label}") from exc


def _validate_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value}") from exc


def _compact_date(value: str) -> str:
    return value.replace("-", "")


def _roc_to_iso(value: object) -> str:
    text = str(value).strip().replace("-", "/")
    parts = text.split("/")
    if len(parts) != 3:
        raise ProviderSchemaError(f"invalid ROC date: {value!r}")
    try:
        return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2])).isoformat()
    except ValueError as exc:
        raise ProviderSchemaError(f"invalid ROC date: {value!r}") from exc


def _validate_payload_date(payload: Mapping[str, Any], requested_date: str) -> None:
    if not isinstance(payload, Mapping):
        raise ProviderSchemaError("provider payload is not an object")
    if str(payload.get("date", "")) != _compact_date(requested_date):
        raise ProviderNoData("provider date does not match requested date")


def _rows(payload: Mapping[str, Any], fields: Sequence[Any], data: Any, declared: Any = None) -> list[Sequence[Any]]:
    if not isinstance(data, list):
        raise ProviderSchemaError("provider data is not a list")
    if declared is not None and declared != len(data):
        raise ProviderSchemaError(f"provider declared {declared} rows but returned {len(data)}")
    for row in data:
        if not isinstance(row, list) or len(row) != len(fields):
            raise ProviderSchemaError("provider row width changed")
    return data


def parse_twse_institutional(
    payload: Mapping[str, Any], requested_date: str
) -> list[InstitutionalFlowRow]:
    _validate_payload_date(payload, requested_date)
    if payload.get("stat") != "OK":
        raise ProviderSchemaError("TWSE T86 status is not OK")
    fields = payload.get("fields")
    if not isinstance(fields, list):
        raise ProviderSchemaError("TWSE T86 fields missing")
    indexes = {
        "symbol": _field_index(fields, "證券代號"),
        "name": _field_index(fields, "證券名稱"),
        "foreign": _field_index(fields, "外陸資買賣超股數(不含外資自營商)"),
        "trust": _field_index(fields, "投信買賣超股數"),
        "dealer": _field_index(fields, "自營商買賣超股數"),
        "dealer_own": _field_index(fields, "自營商買賣超股數(自行買賣)"),
        "dealer_hedge": _field_index(fields, "自營商買賣超股數(避險)"),
        "total": _field_index(fields, "三大法人買賣超股數"),
    }
    result: list[InstitutionalFlowRow] = []
    for row in _rows(payload, fields, payload.get("data"), payload.get("total")):
        symbol = str(row[indexes["symbol"]]).strip()
        if not COMMON_STOCK_RE.fullmatch(symbol):
            continue
        foreign = _parse_int(row[indexes["foreign"]])
        trust = _parse_int(row[indexes["trust"]])
        dealer = _parse_int(row[indexes["dealer"]])
        if dealer != _parse_int(row[indexes["dealer_own"]]) + _parse_int(row[indexes["dealer_hedge"]]):
            raise ProviderSchemaError("TWSE dealer total does not match components")
        total = _parse_int(row[indexes["total"]])
        if total != foreign + trust + dealer:
            raise ProviderSchemaError("TWSE institutional total does not match components")
        result.append(InstitutionalFlowRow(requested_date, "twse", symbol, str(row[indexes["name"]]).strip(), foreign, trust, dealer, total))
    return result


def _twse_daily_table(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise ProviderSchemaError("TWSE MI_INDEX tables missing")
    matches = [table for table in tables if isinstance(table, dict) and "每日收盤行情" in str(table.get("title", ""))]
    if len(matches) != 1:
        raise ProviderSchemaError("TWSE daily close table missing or ambiguous")
    return matches[0]


def parse_twse_closes(payload: Mapping[str, Any], requested_date: str) -> list[ClosePriceRow]:
    _validate_payload_date(payload, requested_date)
    if payload.get("stat") != "OK":
        raise ProviderSchemaError("TWSE MI_INDEX status is not OK")
    table = _twse_daily_table(payload)
    fields = table.get("fields")
    if not isinstance(fields, list):
        raise ProviderSchemaError("TWSE close fields missing")
    symbol_idx = _field_index(fields, "證券代號")
    close_idx = _field_index(fields, "收盤價")
    result = []
    for row in _rows(payload, fields, table.get("data")):
        symbol = str(row[symbol_idx]).strip()
        close_text = str(row[close_idx]).strip()
        if COMMON_STOCK_RE.fullmatch(symbol) and close_text not in {"", "--", "---", "----"}:
            result.append(ClosePriceRow(requested_date, "twse", symbol, _parse_float(close_text)))
    return result


def _tpex_table(payload: Mapping[str, Any], requested_date: str) -> Mapping[str, Any]:
    _validate_payload_date(payload, requested_date)
    if payload.get("stat") != "ok":
        raise ProviderSchemaError("TPEx status is not ok")
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise ProviderSchemaError("TPEx table missing or ambiguous")
    candidates = [table for table in tables if isinstance(table, dict) and isinstance(table.get("fields"), list) and isinstance(table.get("data"), list)]
    if not candidates and (not tables or all(table == {} for table in tables)):
        raise ProviderNoData("TPEx has no table for requested date")
    if len(candidates) != 1:
        raise ProviderSchemaError("TPEx table missing or ambiguous")
    if _roc_to_iso(candidates[0].get("date")) != requested_date:
        raise ProviderSchemaError("TPEx embedded date does not match requested date")
    return candidates[0]


def parse_tpex_institutional(
    payload: Mapping[str, Any], requested_date: str
) -> list[InstitutionalFlowRow]:
    table = _tpex_table(payload, requested_date)
    fields = table.get("fields")
    if not isinstance(fields, list):
        raise ProviderSchemaError("TPEx institutional fields missing")
    expected = ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"]
    if [_header(item) for item in fields] != expected:
        raise ProviderSchemaError("TPEx institutional group layout changed")
    result: list[InstitutionalFlowRow] = []
    for row in _rows(payload, fields, table.get("data"), table.get("totalCount")):
        symbol = str(row[0]).strip()
        if not COMMON_STOCK_RE.fullmatch(symbol):
            continue
        foreign = _parse_int(row[4])
        if _parse_int(row[10]) != foreign + _parse_int(row[7]):
            raise ProviderSchemaError("TPEx foreign total does not match components")
        trust = _parse_int(row[13])
        dealer = _parse_int(row[22])
        if dealer != _parse_int(row[16]) + _parse_int(row[19]):
            raise ProviderSchemaError("TPEx dealer total does not match components")
        total = _parse_int(row[23])
        if total != foreign + trust + dealer:
            raise ProviderSchemaError("TPEx institutional total does not match components")
        result.append(InstitutionalFlowRow(requested_date, "tpex", symbol, str(row[1]).strip(), foreign, trust, dealer, total))
    return result


def parse_tpex_closes(payload: Mapping[str, Any], requested_date: str) -> list[ClosePriceRow]:
    table = _tpex_table(payload, requested_date)
    fields = table.get("fields")
    if not isinstance(fields, list):
        raise ProviderSchemaError("TPEx close fields missing")
    symbol_idx = _field_index(fields, "代號")
    close_idx = _field_index(fields, "收盤")
    result = []
    for row in _rows(payload, fields, table.get("data"), table.get("totalCount")):
        symbol = str(row[symbol_idx]).strip()
        close_text = str(row[close_idx]).strip()
        if COMMON_STOCK_RE.fullmatch(symbol) and close_text not in {"", "--", "---", "----"}:
            result.append(ClosePriceRow(requested_date, "tpex", symbol, _parse_float(close_text)))
    return result


def parse_tdcc_holdings(payload: Sequence[Mapping[str, Any]]) -> list[HoldingDistributionRow]:
    if not isinstance(payload, list):
        raise ProviderSchemaError("TDCC payload is not a list")
    result: list[HoldingDistributionRow] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise ProviderSchemaError("TDCC row is not an object")
        row = {_header(key): value for key, value in raw.items()}
        required = {"證券代號", "持股分級", "人數", "股數", "占集保庫存數比例%", "資料日期"}
        if not required.issubset(row):
            raise ProviderSchemaError("TDCC fields missing")
        symbol = str(row["證券代號"]).strip()
        if not COMMON_STOCK_RE.fullmatch(symbol):
            continue
        raw_date = str(row["資料日期"]).strip()
        try:
            as_of = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
        except ValueError as exc:
            raise ProviderSchemaError(f"invalid TDCC date: {raw_date!r}") from exc
        result.append(HoldingDistributionRow(as_of, symbol, _parse_int(row["持股分級"]), _parse_int(row["人數"]), _parse_int(row["股數"]), _parse_float(row["占集保庫存數比例%"])))
    if result and len({row.as_of_date for row in result}) != 1:
        raise ProviderSchemaError("TDCC payload contains multiple dates")
    return result


def build_daily_requests(cache_dir: Path, requested_date: str) -> list[SourceRequest]:
    parsed = _validate_iso_date(requested_date)
    compact = parsed.strftime("%Y%m%d")
    slash_date = urllib.parse.quote(parsed.strftime("%Y/%m/%d"), safe="")
    return [
        SourceRequest("twse", "T86", requested_date, f"https://www.twse.com.tw/rwd/zh/fund/T86?date={compact}&selectType=ALLBUT0999&response=json", cache_dir / "twse" / "T86" / requested_date / "market.json"),
        SourceRequest("twse", "MI_INDEX", requested_date, f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={compact}&type=ALLBUT0999&response=json", cache_dir / "twse" / "MI_INDEX" / requested_date / "market.json"),
        SourceRequest("tpex", "institutional", requested_date, f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?date={slash_date}&type=Daily&sect=EW", cache_dir / "tpex" / "institutional" / requested_date / "market.json"),
        SourceRequest("tpex", "daily_close", requested_date, f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash_date}&type=EW", cache_dir / "tpex" / "daily_close" / requested_date / "market.json"),
    ]


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _validate_request_payload(request: SourceRequest, payload: Any) -> int:
    parsers = {
        ("twse", "T86"): parse_twse_institutional,
        ("twse", "MI_INDEX"): parse_twse_closes,
        ("tpex", "institutional"): parse_tpex_institutional,
        ("tpex", "daily_close"): parse_tpex_closes,
    }
    if not isinstance(payload, Mapping) or request.requested_date is None:
        raise ProviderSchemaError("daily payload is not an object")
    return len(parsers[(request.provider, request.dataset)](payload, request.requested_date))


def ingest_sector_flow(
    *, cache_dir: Path, start_date: str, end_date: str, client: JsonHttpClient
) -> dict[str, Any]:
    start = _validate_iso_date(start_date)
    end = _validate_iso_date(end_date)
    if start > end:
        raise ValueError("start date must not exceed end date")
    if (end - start).days > 30:
        raise ValueError("sector-flow range must not exceed 31 calendar days")
    summary: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "fetched": 0, "cached": 0, "no_data": 0, "failed": 0, "sources": []}
    cursor = start
    while cursor <= end:
        for request in build_daily_requests(cache_dir, cursor.isoformat()):
            item = {"provider": request.provider, "dataset": request.dataset, "requested_date": request.requested_date, "cache_path": str(request.cache_path)}
            if request.cache_path.exists():
                try:
                    row_count = _validate_request_payload(request, read_json(request.cache_path))
                except (OSError, ValueError):  # ValueError covers bad JSON, bad UTF-8 and ProviderSchemaError
                    row_count = 0
                if row_count:
                    item.update({"state": "ok", "via": "cache", "row_count": row_count, "embedded_date": request.requested_date})
                    summary["cached"] += 1
                    summary["sources"].append(item)
                    continue
            try:
                payload = client.get_json(request.url)
                row_count = _validate_request_payload(request, payload)
                if row_count == 0:
                    item["state"] = "no_data"
                    summary["no_data"] += 1
                else:
                    _atomic_json(request.cache_path, payload)
                    item.update({"state": "ok", "via": "network", "row_count": row_count, "embedded_date": request.requested_date})
                    summary["fetched"] += 1
            except ProviderNoData as exc:
                item.update({"state": "no_data", "reason": str(exc)})
                summary["no_data"] += 1
            except ProviderSchemaError as exc:
                item.update({"state": "schema_error", "error": str(exc)})
                summary["failed"] += 1
            except Exception as exc:
                item.update({"state": "error", "error": str(exc)})
                summary["failed"] += 1
            summary["sources"].append(item)
        cursor += timedelta(days=1)

    tdcc_url = "https://openapi.tdcc.com.tw/v1/opendata/1-5"
    try:
        tdcc_payload = client.get_json(tdcc_url)
        holdings = parse_tdcc_holdings(tdcc_payload)
        if not holdings:
            summary["sources"].append({"provider": "tdcc", "dataset": "holding_distribution", "state": "no_data"})
            summary["no_data"] += 1
        else:
            as_of = holdings[0].as_of_date
            path = cache_dir / "tdcc" / "holding_distribution" / as_of / "market.json"
            repaired = False
            if path.exists():
                try:
                    cached = parse_tdcc_holdings(read_json(path))
                    valid = bool(cached) and cached[0].as_of_date == as_of
                except (OSError, ValueError):
                    valid = False
                repaired = not valid
            if path.exists() and not repaired:
                summary["cached"] += 1
                via = "cache"
            else:
                _atomic_json(path, tdcc_payload)
                summary["fetched"] += 1
                via = "network"
            item = {"provider": "tdcc", "dataset": "holding_distribution", "state": "ok", "via": via, "as_of_date": as_of, "row_count": len(holdings), "cache_path": str(path)}
            if repaired:
                item["repaired_cache"] = True
            summary["sources"].append(item)
    except Exception as exc:
        summary["failed"] += 1
        summary["sources"].append({"provider": "tdcc", "dataset": "holding_distribution", "state": "schema_error" if isinstance(exc, ProviderSchemaError) else "error", "error": str(exc)})
    summary["status"] = "ok" if summary["failed"] == 0 else "degraded"
    return summary


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
