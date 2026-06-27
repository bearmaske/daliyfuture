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
