"""ensure_stock_name 的自我檢查：已知不查、未知補檔、查無不寫。"""
import pytest

from src.contracts import stock_names as sn


@pytest.fixture
def isolated_auto(monkeypatch, tmp_path):
    """把 auto 檔導向 tmp，並強制查表重建（避開既有 mtime 快取）。"""
    monkeypatch.setattr(sn, "_AUTO_PATH", tmp_path / "auto.yaml")
    monkeypatch.setattr(sn, "_cached_mtimes", (-1.0, -1.0))
    return tmp_path / "auto.yaml"


def test_known_symbol_skips_resolver(isolated_auto):
    def boom(_symbol):
        raise AssertionError("resolver 不該被呼叫")

    # 2330 在 _BUILTIN_NAMES → 直接回，不查、不寫檔
    assert sn.ensure_stock_name("2330", boom) == "台積電"
    assert not isolated_auto.exists()


def test_unknown_symbol_resolved_and_persisted(isolated_auto):
    assert sn.ensure_stock_name("9999", lambda s: "測試股") == "測試股"
    # 寫進 auto 檔，且查表（經 mtime 重載）後可見
    assert isolated_auto.exists()
    assert sn.stock_name("9999") == "測試股"


def test_unresolvable_symbol_stays_blank(isolated_auto):
    assert sn.ensure_stock_name("8888", lambda s: "") == ""
    assert sn.stock_name("8888") == ""
    # 查無不應建檔/寫入
    if isolated_auto.exists():
        assert "8888" not in isolated_auto.read_text(encoding="utf-8")
