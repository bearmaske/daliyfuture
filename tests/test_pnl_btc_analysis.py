import numpy as np
import pandas as pd
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.analyze_pnl_vs_btc import aggregate_hourly_pnl, build_btc_indicators, compute_correlations, compare_win_loss_windows


def test_aggregate_hourly_pnl_sums_all_income_types_per_hour():
    raw = pd.DataFrame([
        {"time": "2026-05-06 21:01:01", "symbol": "BTC", "incomeType": "COMMISSION",  "income": -0.5},
        {"time": "2026-05-06 21:30:00", "symbol": "BTC", "incomeType": "REALIZED_PNL", "income": 10.0},
        {"time": "2026-05-06 22:05:00", "symbol": "ETH", "incomeType": "FUNDING_FEE",  "income": -0.2},
        {"time": "2026-05-06 22:10:00", "symbol": "ETH", "incomeType": "REALIZED_PNL", "income": -3.0},
    ])
    out = aggregate_hourly_pnl(raw)
    assert out.loc[pd.Timestamp("2026-05-06 21:00:00")] == pytest.approx(9.5)
    assert out.loc[pd.Timestamp("2026-05-06 22:00:00")] == pytest.approx(-3.2)


def test_aggregate_hourly_pnl_fills_empty_hours_with_zero():
    raw = pd.DataFrame([
        {"time": "2026-05-06 10:00:00", "symbol": "X", "incomeType": "REALIZED_PNL", "income": 1.0},
        {"time": "2026-05-06 13:00:00", "symbol": "X", "incomeType": "REALIZED_PNL", "income": 2.0},
    ])
    out = aggregate_hourly_pnl(raw)
    assert out.loc[pd.Timestamp("2026-05-06 11:00:00")] == 0.0
    assert out.loc[pd.Timestamp("2026-05-06 12:00:00")] == 0.0


def _make_klines(n: int = 100, start="2026-04-25 00:00:00") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1h")
    close = 80000 + 2000 * np.sin(np.linspace(0, 4 * np.pi, n))
    high = close + 50
    low = close - 50
    open_ = close
    volume = np.linspace(100, 200, n)
    return pd.DataFrame({
        "open_time": (idx.view("int64") // 10**6),
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    })


def test_build_btc_indicators_has_all_expected_columns():
    klines = _make_klines(200)
    out = build_btc_indicators(klines)
    expected = {
        "ret_std_24h", "atr_14", "vol_ratio_20", "vol_zscore_50",
        "sma20_slope", "sma20_50_dist",
        "roc_6", "roc_12", "roc_24",
        "bb_width", "bb_pctb", "hl_range",
    }
    assert expected.issubset(set(out.columns))


def test_build_btc_indicators_index_is_hourly_timestamps():
    klines = _make_klines(200)
    out = build_btc_indicators(klines)
    assert isinstance(out.index, pd.DatetimeIndex)
    diffs = out.index.to_series().diff().dropna().unique()
    assert len(diffs) == 1 and diffs[0] == pd.Timedelta(hours=1)


def test_build_btc_indicators_no_nan_after_warmup():
    klines = _make_klines(200)
    out = build_btc_indicators(klines)
    tail = out.iloc[60:]
    assert not tail.isna().any().any()


def test_compute_correlations_returns_pearson_and_spearman_per_column():
    idx = pd.date_range("2026-05-01", periods=100, freq="1h")
    pnl = pd.Series(np.linspace(-10, 10, 100), index=idx)
    feats = pd.DataFrame({
        "perfectly_correlated": np.linspace(-10, 10, 100),
        "noise": np.random.RandomState(0).randn(100),
    }, index=idx)
    out = compute_correlations(pnl, feats)
    assert {"pearson_r", "pearson_p", "spearman_r", "spearman_p"}.issubset(out.columns)
    assert out.loc["perfectly_correlated", "pearson_r"] == pytest.approx(1.0, abs=1e-9)
    assert abs(out.loc["noise", "pearson_r"]) < 0.5


def test_compare_win_loss_windows_reports_means_and_mwu_p():
    idx = pd.date_range("2026-05-01", periods=100, freq="1h")
    pnl = pd.Series([1.0] * 50 + [-1.0] * 50, index=idx)
    feats = pd.DataFrame({
        "feat_high_on_loss": [0.0] * 50 + [5.0] * 50,
    }, index=idx)
    out = compare_win_loss_windows(pnl, feats)
    assert "loss_mean" in out.columns and "win_mean" in out.columns and "mwu_p" in out.columns
    assert out.loc["feat_high_on_loss", "loss_mean"] == pytest.approx(5.0)
    assert out.loc["feat_high_on_loss", "win_mean"] == pytest.approx(0.0)
    assert out.loc["feat_high_on_loss", "mwu_p"] < 0.01
