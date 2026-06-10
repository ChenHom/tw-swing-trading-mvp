import pytest
from pydantic import ValidationError
from src.contracts.models import TrendPullbackParams
from src.strategy.canonicalizer import StrategyParameterCanonicalizer

def test_trend_pullback_params_validation():
    # Valid params
    params = TrendPullbackParams(ma_short=10, ma_long=50)
    assert params.ma_short == 10
    assert params.ma_long == 50
    
    # Defaults
    defaults = TrendPullbackParams()
    assert defaults.ma_short == 20
    assert defaults.ma_long == 60
    assert defaults.stop_loss_bps == 500
    
    # Invalid ma_short
    with pytest.raises(ValidationError):
        TrendPullbackParams(ma_short=1)
        
    # Invalid stop_loss_bps (greater than 5000)
    with pytest.raises(ValidationError):
        TrendPullbackParams(stop_loss_bps=6000)

def test_canonicalizer_hash_stability():
    # Model with explicit values matching defaults
    params_explicit = TrendPullbackParams(
        ma_short=20,
        ma_long=60,
        stop_loss_bps=500,
        take_profit_bps=1200,
        order_budget_twd=20000
    )
    # Model using implicit defaults
    params_implicit = TrendPullbackParams()
    
    hash_explicit = StrategyParameterCanonicalizer.compute_hash(params_explicit)
    hash_implicit = StrategyParameterCanonicalizer.compute_hash(params_implicit)
    
    # Hash must be identical
    assert hash_explicit == hash_implicit
    assert hash_explicit.startswith("sha256:")
    assert len(hash_explicit) == 7 + 64  # "sha256:" prefix + 64 hex chars
