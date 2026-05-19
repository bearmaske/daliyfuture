import pandas as pd
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.analyze_pnl_vs_btc import aggregate_hourly_pnl


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
