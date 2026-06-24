"""策略選股宇宙抽象（R-T4b Track 2）。

策略以往吃固定 `universe_symbols: list[str]`；接 PIT 流動性 universe 後改吃一個
`UniverseProvider.symbols_as_of(as_of)`——固定清單與 policy 驅動各一實作。

`coerce_universe` 讓既有「以 list 建構策略」的呼叫端/測試**零改動**（list 自動包成
FixedUniverseProvider；已是 provider 則原樣）。
"""
from datetime import date


class FixedUniverseProvider:
    """固定清單：不論日期回同一批（live/sim 與既有測試的行為）。"""

    def __init__(self, symbols):
        self._symbols = list(symbols)

    def symbols_as_of(self, as_of: date) -> list[str]:
        return self._symbols


class PolicyUniverseProvider:
    """PIT policy 驅動：回某 policy_version 在 as_of 當日的成分股（known_at<=as_of）。"""

    def __init__(self, conn, policy_version: str):
        from src.market_data.universe_policy import UniversePolicy
        self._policy = UniversePolicy(conn)
        self._policy_version = policy_version

    def symbols_as_of(self, as_of: date) -> list[str]:
        return self._policy.constituents_as_of(self._policy_version, as_of)


def coerce_universe(universe):
    """list[str] → FixedUniverseProvider；已是 provider（有 symbols_as_of）則原樣。"""
    if hasattr(universe, "symbols_as_of"):
        return universe
    return FixedUniverseProvider(universe)
