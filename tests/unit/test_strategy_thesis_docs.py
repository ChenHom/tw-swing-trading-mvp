"""P2-T1：每個現役策略須有看結果前寫死的 Strategy Thesis 文件，且含必要章節。"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SECTIONS = ["## Edge 來源", "## 有效 / 失效市場", "## 合格 / 否決標準"]


def _active_strategy_ids():
    trading_yaml = yaml.safe_load((REPO_ROOT / "config" / "trading.yaml").read_text(encoding="utf-8"))
    return trading_yaml["pipeline"]["entry_strategies"]


def test_every_active_strategy_has_a_thesis_doc():
    for strategy_id in _active_strategy_ids():
        doc_path = REPO_ROOT / "docs" / "strategies" / f"{strategy_id}.md"
        assert doc_path.exists(), f"缺少 {strategy_id} 的 Strategy Thesis 文件"


def test_thesis_docs_declare_required_sections():
    for strategy_id in _active_strategy_ids():
        text = (REPO_ROOT / "docs" / "strategies" / f"{strategy_id}.md").read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{strategy_id}.md 缺少章節 {section}"


def test_thesis_docs_pin_regime_gate_threshold_fields():
    required_fields = [
        "max_regime_drawdown", "min_expectancy_ci_lower", "max_bear_underperformance",
        "min_effective_sample_size", "max_profit_concentration",
    ]
    for strategy_id in _active_strategy_ids():
        text = (REPO_ROOT / "docs" / "strategies" / f"{strategy_id}.md").read_text(encoding="utf-8")
        for field in required_fields:
            assert field in text, f"{strategy_id}.md 未寫死 regime gate 欄位 {field}"
