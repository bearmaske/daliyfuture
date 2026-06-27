from strategy import compute_phase_state, phase_allows


def _series(values):
    """Helper: build (closes, open_times) with 1-day spacing in ms."""
    return list(map(float, values)), [i * 86_400_000 for i in range(len(values))]


def test_phase_none_before_any_breakout():
    closes, ots = _series([100.0] * 25)  # flat → bands hug price, no break
    phase, start = compute_phase_state(closes, ots, period=20, std_dev=2.0)
    assert phase is None
    assert start == 0


def test_up_phase_starts_on_upper_break_and_persists_above_middle():
    # 20 flat bars build the band, then a spike breaks the upper band,
    # then a mild pullback that stays above the middle keeps UP active.
    closes, ots = _series([100.0] * 20 + [130.0, 122.0, 121.0])
    phase, start = compute_phase_state(closes, ots, period=20, std_dev=2.0)
    assert phase == "UP"
    assert start == 20 * 86_400_000  # the +130 bar (index 20) started the phase


def test_up_phase_ends_on_middle_cross_down():
    closes, ots = _series([100.0] * 20 + [130.0, 100.0])  # break up, then back below middle
    phase, start = compute_phase_state(closes, ots, period=20, std_dev=2.0)
    assert phase is None


def test_down_phase_starts_on_lower_break():
    closes, ots = _series([100.0] * 20 + [70.0, 78.0])
    phase, start = compute_phase_state(closes, ots, period=20, std_dev=2.0)
    assert phase == "DOWN"
    assert start == 20 * 86_400_000


def test_phase_allows_direction_gate():
    assert phase_allows("LONG", "UP") is True
    assert phase_allows("LONG", "DOWN") is False
    assert phase_allows("LONG", None) is False
    assert phase_allows("SHORT", "DOWN") is True
    assert phase_allows("SHORT", "UP") is False


import os
import tempfile
from state import StateManager


def test_traded_phase_roundtrip_and_persistence():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, "s.json")
        bf = os.path.join(d, "s.backup.json")
        sm = StateManager(sf, bf, initial_capital=1000.0)
        sm.load()
        assert sm.get_traded_phase("BTCUSDT") is None
        sm.set_traded_phase("BTCUSDT", 1700000000000)
        assert sm.get_traded_phase("BTCUSDT") == 1700000000000
        # survives reload
        sm2 = StateManager(sf, bf, initial_capital=1000.0)
        sm2.load()
        assert sm2.get_traded_phase("BTCUSDT") == 1700000000000


from risk import check_phase_exit, compute_phase_exit_inputs


def test_check_phase_exit_long_bb_middle_precedence():
    # close below middle -> bb_middle exit even if 3.5% not hit
    assert check_phase_exit("LONG", bar_close=95.0, bb_middle=96.0,
                            pre_bar_extreme=100.0, trailing_pct=0.035) == "1h_bb_middle"


def test_check_phase_exit_long_trailing():
    # close above middle, but retraced >=3.5% from pre-bar high (100*0.965=96.5)
    assert check_phase_exit("LONG", bar_close=96.0, bb_middle=90.0,
                            pre_bar_extreme=100.0, trailing_pct=0.035) == "trailing_3.5pct"


def test_check_phase_exit_long_hold():
    assert check_phase_exit("LONG", bar_close=99.0, bb_middle=90.0,
                            pre_bar_extreme=100.0, trailing_pct=0.035) is None


def test_check_phase_exit_short_mirror():
    assert check_phase_exit("SHORT", bar_close=105.0, bb_middle=104.0,
                            pre_bar_extreme=100.0, trailing_pct=0.035) == "1h_bb_middle"
    assert check_phase_exit("SHORT", bar_close=104.0, bb_middle=110.0,
                            pre_bar_extreme=100.0, trailing_pct=0.035) == "trailing_3.5pct"


def _kl(ot, close, high, low):
    return [ot, close, high, low, close, 0.0]  # open unused; idx: 0 ot,2 high,3 low,4 close


def test_compute_exit_inputs_breathe_on_entry_bar():
    # only the entry bar has closed -> None (let it breathe)
    ots = [i * 3_600_000 for i in range(25)]
    klines = [[ot, 100, 101, 99, 100, 0] for ot in ots]  # [ot,open,high,low,close,vol]
    opened_ms = ots[-1]
    assert compute_phase_exit_inputs(klines, opened_ms, 100.0, bb_period=20) is None


def test_compute_exit_inputs_excludes_entry_and_current_bar():
    # 22 bars; entry at index 20. Bar 21 is the just-closed bar.
    # Pre-bar extreme must exclude entry bar (20) and current bar (21) -> falls
    # back to entry_price since no bar strictly between them.
    ots = [i * 3_600_000 for i in range(22)]
    klines = []
    for i, ot in enumerate(ots):
        klines.append([ot, 100, 100 + i, 100 - i, 100, 0])  # [ot,open,high,low,close,vol]
    opened_ms = ots[20]
    out = compute_phase_exit_inputs(klines, opened_ms, entry_price=100.0, bb_period=20)
    assert out is not None
    bar_close, bb_middle, pre_high, pre_low = out
    assert bar_close == 100
    assert pre_high == 100.0  # entry bar (idx20 high=120) and current (idx21) excluded
