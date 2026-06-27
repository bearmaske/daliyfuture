# Phase Filter Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the daily-BB phase filter, first-trade-per-phase guard, and the two new 1H-close exits (BB-middle cross OR 3.5% retrace from confirmed pre-bar extreme) to the live bot, as a toggleable overlay on the existing Trend Sniper strategy, for paper/testnet forward-observation.

**Architecture:** Entries stay identical to the live bot; the phase gate is an *additive* filter applied after the existing signal fires. Exits switch from the ATR dual-stop to the two new 1H-close exits when `EXIT_MODE="phase_bb"`. The phase timeline is recomputed deterministically from daily klines each scan (no persistence needed); only "have I already traded this phase" is persisted. Exit inputs (1H BB middle + confirmed pre-bar extreme) are recomputed from 1H klines each hourly check (stateless, restart-robust). A wide catastrophe exchange STOP_MARKET is retained for offline/gap protection.

**Tech Stack:** Python 3.12, python-binance, APScheduler, pandas/numpy, pytest. Mirrors the validated reference implementation in `backtesting/phase_filter_backtest.py`.

## Global Constraints

- Timestamps UTC+8 throughout (`state.TZ_CN`); K-line math drops the last unclosed candle (project convention).
- All new log/notification strings in Chinese, matching existing labels (`[阶段]`, `[止损]`, `[平仓]`).
- No future function: phase uses only CLOSED daily bars; exits evaluate only on CLOSED 1H bars.
- Sizing is unchanged from live `atr_dual`: `notional = RISK_PER_TRADE_USD / soft_stop_pct`, capped at `MAX_NOTIONAL_USD`. In `phase_bb` mode `soft_stop_pct` is used for SIZING ONLY, not as a stop.
- Reference behavior to match exactly: `backtesting/phase_filter_backtest.py` — daily BB(20,2) phase rules, first-trade-per-phase, `check_phase_exit` semantics, and the "pre-bar extreme excludes the current bar" rule.
- The 3.5% trigger's "confirmed pre-bar extreme" excludes BOTH the entry bar and the just-closed bar (matches the reference engine's exit-before-entry ordering).
- Default config after this plan activates the NEW behavior (`EXIT_MODE="phase_bb"`, `PHASE_FILTER_ENABLED=True`). Rollback = set `EXIT_MODE="atr_dual"` and `PHASE_FILTER_ENABLED=False`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `config.py` | Strategy constants | Add phase/exit/catastrophe-stop flags |
| `strategy.py` | Entry scan + phase computation + open | Add `compute_phase_state`, `phase_allows`; insert phase gate; widen daily fetch; catastrophe stop in `_open_position` |
| `risk.py` | Exit checks | Add `check_phase_exit` (pure), `compute_phase_exit_inputs` (pure), `_check_phase_exits` (orchestration); dispatch in `check_stop_loss` |
| `state.py` | Persistence | Add `traded_phases` dict + accessors; add `opened_ms` to new positions |
| `tests/test_phase_filter.py` | Tests for the pure functions | Create |

---

### Task 1: Daily phase computation (pure functions)

**Files:**
- Modify: `strategy.py` (add two functions near `check_trend_bb_middle`, ~line 165)
- Test: `tests/test_phase_filter.py`

**Interfaces:**
- Produces:
  - `compute_phase_state(closes: list[float], open_times: list[int], period: int = 20, std_dev: float = 2.0) -> tuple[str | None, int]` — returns `(phase, phase_start_ms)` where `phase ∈ {"UP","DOWN",None}` and `phase_start_ms` is the `open_time` of the daily bar where the current phase began (`0` when `phase is None`). `closes`/`open_times` are CLOSED daily bars (caller drops the unclosed candle).
  - `phase_allows(direction: str, phase: str | None) -> bool` — `True` iff `(direction=="LONG" and phase=="UP")` or `(direction=="SHORT" and phase=="DOWN")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_phase_filter.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_phase_filter.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_phase_state'`

- [ ] **Step 3: Implement the functions**

Add to `strategy.py` immediately after `check_trend_bb_middle` (after line 165):

```python
def compute_phase_state(
    closes: List[float],
    open_times: List[int],
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[Optional[str], int]:
    """Replay daily BB to find the CURRENT phase and when it started.

    UP   starts when close > upper band; ends when close < middle band.
    DOWN starts when close < lower band; ends when close > middle band.
    A phase persists across bars until its middle-band end condition fires.

    `closes`/`open_times` are CLOSED daily bars (caller drops unclosed).
    Returns (phase, phase_start_ms): phase ∈ {"UP","DOWN",None};
    phase_start_ms is open_times[i] of the bar that started the phase (0 if None).
    Mirrors backtesting/phase_filter_backtest.py:compute_phase_timeline.
    """
    phase: Optional[str] = None
    phase_start_ms = 0
    n = len(closes)
    for i in range(n):
        if i + 1 < period:
            continue
        window = closes[i - period + 1: i + 1]
        upper, middle, lower = calculate_bollinger_bands(window, period, std_dev)
        close = closes[i]

        # End check first so same-bar transitions resolve cleanly.
        if phase == "UP" and close < middle:
            phase, phase_start_ms = None, 0
        elif phase == "DOWN" and close > middle:
            phase, phase_start_ms = None, 0

        if phase is None:
            if close > upper:
                phase, phase_start_ms = "UP", int(open_times[i])
            elif close < lower:
                phase, phase_start_ms = "DOWN", int(open_times[i])

    return phase, phase_start_ms


def phase_allows(direction: str, phase: Optional[str]) -> bool:
    """Phase gate: LONG only in UP, SHORT only in DOWN."""
    return (direction == "LONG" and phase == "UP") or (
        direction == "SHORT" and phase == "DOWN"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_phase_filter.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_phase_filter.py
git commit -m "feat(phase): daily BB phase-state computation + direction gate"
```

---

### Task 2: traded_phases persistence

**Files:**
- Modify: `state.py` (`_default_state` ~line 399; add accessors near `set_last_soft_check_hour` ~line 157)
- Test: `tests/test_phase_filter.py` (append)

**Interfaces:**
- Consumes: `StateManager` (existing).
- Produces:
  - `StateManager.get_traded_phase(symbol: str) -> int | None` — last phase_start_ms traded for `symbol`, or None.
  - `StateManager.set_traded_phase(symbol: str, phase_start_ms: int) -> None` — persist it.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_phase_filter.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_phase_filter.py::test_traded_phase_roundtrip_and_persistence -v`
Expected: FAIL with `AttributeError: 'StateManager' object has no attribute 'get_traded_phase'`

- [ ] **Step 3: Implement**

In `state.py` `_default_state` (line ~399), add the key:

```python
    def _default_state(self) -> dict:
        return {
            "balance": self.initial_capital,
            "positions": [],
            "trade_history": [],
            "traded_phases": {},
        }
```

Add accessors after `set_last_soft_check_hour` (line ~157):

```python
    def get_traded_phase(self, symbol: str) -> Optional[int]:
        """phase_start_ms of the phase we last opened a trade in for `symbol`."""
        val = self.state.get("traded_phases", {}).get(symbol)
        return int(val) if val is not None else None

    def set_traded_phase(self, symbol: str, phase_start_ms: int):
        with self._lock:
            self.state.setdefault("traded_phases", {})[symbol] = int(phase_start_ms)
        self.save()
```

Note: existing state.json files load without the key; `.get("traded_phases", {})` handles that (lazy default).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_phase_filter.py::test_traded_phase_roundtrip_and_persistence -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_phase_filter.py
git commit -m "feat(phase): persist first-trade-per-phase markers"
```

---

### Task 3: Config flags

**Files:**
- Modify: `config.py` (after the Trend Filter block, ~line 63)

**Interfaces:**
- Produces: `config.PHASE_FILTER_ENABLED`, `config.EXIT_MODE`, `config.PHASE_DAILY_LOOKBACK`, `config.PHASE_BB_PERIOD`, `config.PHASE_BB_STD`, `config.PHASE_EXIT_TRAILING_PCT`, `config.CATASTROPHE_STOP_ENABLED`, `config.CATASTROPHE_STOP_PCT`.

- [ ] **Step 1: Add the config block**

In `config.py` after line 63 (`SMA_PERIOD`):

```python
    # ── Phase filter overlay (docs/superpowers/plans/2026-06-27-phase-filter-live.md) ──
    # Additive daily-BB phase gate on entries + new 1H-close exits. Forward-test
    # overlay; backtests net-negative (-12.4% over Jun2025–Jun2026). Paper only.
    PHASE_FILTER_ENABLED: bool = True   # gate entries by daily BB phase + first-trade-per-phase
    EXIT_MODE: str = "phase_bb"         # "atr_dual" (legacy stops) | "phase_bb" (1H BB-middle + 3.5% confirmed retrace)
    PHASE_DAILY_LOOKBACK: int = 250     # daily bars fetched to replay the phase timeline
    PHASE_BB_PERIOD: int = 20
    PHASE_BB_STD: float = 2.0
    PHASE_EXIT_TRAILING_PCT: float = 0.035  # 3.5% retrace from confirmed pre-bar extreme
    # Catastrophe stop: in phase_bb mode the two new exits replace the ATR stops,
    # but a wide exchange STOP_MARKET is kept for offline/gap protection. Set wide
    # enough that BB-middle/3.5% exits normally fire first.
    CATASTROPHE_STOP_ENABLED: bool = True
    CATASTROPHE_STOP_PCT: float = 0.08
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from config import config; print(config.EXIT_MODE, config.PHASE_EXIT_TRAILING_PCT, config.CATASTROPHE_STOP_PCT)"`
Expected: `phase_bb 0.035 0.08`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(phase): config flags for phase filter + phase_bb exits"
```

---

### Task 4: New exit logic (pure functions)

**Files:**
- Modify: `risk.py` (add after `check_trailing_tp`, ~line 149)
- Test: `tests/test_phase_filter.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `check_phase_exit(side: str, bar_close: float, bb_middle: float, pre_bar_extreme: float, trailing_pct: float) -> str | None` — returns `"1h_bb_middle"`, `"trailing_3.5pct"`, or `None`. BB-middle takes precedence.
  - `compute_phase_exit_inputs(closed_klines: list, opened_ms: int, entry_price: float, bb_period: int) -> tuple | None` — from CLOSED 1H klines (each `[open_time,open,high,low,close,...]`), returns `(bar_close, bb_middle, pre_bar_extreme_high, pre_bar_extreme_low)` for the just-closed bar, or `None` if not enough bars / still the entry bar (breathe). `pre_bar_extreme_high/low` exclude the entry bar AND the just-closed bar.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_phase_filter.py
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_phase_filter.py -k "phase_exit or exit_inputs" -v`
Expected: FAIL with `ImportError: cannot import name 'check_phase_exit'`

- [ ] **Step 3: Implement**

Add to `risk.py` after `check_trailing_tp` (line ~149). The BB *middle* band is just the SMA, so compute it inline with `np.mean` — do NOT import from `strategy` (that would create a circular import: `strategy.py` already imports from `risk.py`). `numpy as np` is already imported at the top of `risk.py`.

```python
def check_phase_exit(
    side: str,
    bar_close: float,
    bb_middle: float,
    pre_bar_extreme: float,
    trailing_pct: float,
) -> Optional[str]:
    """Phase-mode 1H-close exit. BB-middle cross takes precedence over the
    3.5% confirmed-extreme retrace. Mirrors phase_filter_backtest._check_exits.
    Returns "1h_bb_middle", "trailing_3.5pct", or None.
    """
    if side == "LONG":
        if bar_close < bb_middle:
            return "1h_bb_middle"
        if bar_close <= pre_bar_extreme * (1 - trailing_pct):
            return "trailing_3.5pct"
    else:
        if bar_close > bb_middle:
            return "1h_bb_middle"
        if bar_close >= pre_bar_extreme * (1 + trailing_pct):
            return "trailing_3.5pct"
    return None


def compute_phase_exit_inputs(
    closed_klines: list,
    opened_ms: int,
    entry_price: float,
    bb_period: int = 20,
):
    """From CLOSED 1H klines, derive the inputs for check_phase_exit on the
    just-closed bar. Returns (bar_close, bb_middle, pre_high, pre_low) or None.

    closed_klines: list of [open_time, open, high, low, close, ...] (unclosed
                   candle already dropped by caller), ascending by open_time.
    pre_high/pre_low EXCLUDE the entry bar (open_time == opened_ms) and the
    just-closed bar — matching the reference engine's exit-before-entry order.
    Returns None while only the entry bar has closed (breathe one bar) or when
    there are fewer than bb_period closed bars.
    """
    if len(closed_klines) < bb_period:
        return None
    last = closed_klines[-1]
    last_ot = int(last[0])
    if last_ot <= int(opened_ms):
        return None  # still on/at the entry bar — breathe

    closes = [float(k[4]) for k in closed_klines]
    bb_middle = float(np.mean(closes[-bb_period:]))  # BB middle band == SMA(period)
    bar_close = float(last[4])

    pre_high = entry_price
    pre_low = entry_price
    for k in closed_klines[:-1]:                 # exclude just-closed bar
        ot = int(k[0])
        if ot <= int(opened_ms):                 # exclude entry bar and earlier
            continue
        pre_high = max(pre_high, float(k[2]))
        pre_low = min(pre_low, float(k[3]))
    return bar_close, bb_middle, pre_high, pre_low
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_phase_filter.py -k "phase_exit or exit_inputs" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add risk.py tests/test_phase_filter.py
git commit -m "feat(phase): pure 1H-close exit logic (BB-middle + 3.5% confirmed retrace)"
```

---

### Task 5: Entry-gate integration in strategy.py

**Files:**
- Modify: `strategy.py` — `run_strategy` daily fetch limit (~line 251); `_eval_and_maybe_open` signal block (~line 452); imports.
- Test: smoke run (no unit test — wiring verified by paper run).

**Interfaces:**
- Consumes: `compute_phase_state`, `phase_allows` (Task 1); `state_mgr.get_traded_phase`/`set_traded_phase` (Task 2); `config.PHASE_FILTER_ENABLED`, `config.PHASE_DAILY_LOOKBACK`, `config.PHASE_BB_PERIOD`, `config.PHASE_BB_STD` (Task 3).
- Produces: gated entries — only first signal per (symbol, phase) in the matching direction reaches `_open_position`.

- [ ] **Step 1: Widen the daily fetch so the phase timeline can be replayed**

In `run_strategy`, replace line ~251:

```python
    # +2 for SMA slope comparison, +1 to discard unclosed candle
    daily_kline_limit = config.SMA_PERIOD + 3
```

with:

```python
    # +2 for SMA slope comparison, +1 to discard unclosed candle. Phase filter
    # replays the full daily BB timeline, so fetch enough history to capture the
    # current phase's start.
    daily_kline_limit = config.SMA_PERIOD + 3
    if config.PHASE_FILTER_ENABLED:
        daily_kline_limit = max(daily_kline_limit, config.PHASE_DAILY_LOOKBACK)
```

- [ ] **Step 2: Add a skip counter**

In the `skip_counts` dict (~line 236), add an entry:

```python
        "阶段不符": 0,
```

- [ ] **Step 3: Insert the phase gate before opening**

In `_eval_and_maybe_open`, after `signals_count += 1` (line ~452) and BEFORE the `if opened >= available_slots:` block, insert:

```python
            # ── Phase filter overlay: gate by daily BB phase + first-trade-per-phase ──
            if config.PHASE_FILTER_ENABLED:
                daily_klines_pf = data.get("daily")
                if not daily_klines_pf:
                    logger.info("[阶段] %s 无日线数据,阶段过滤跳过本信号", symbol)
                    skip_counts["阶段不符"] += 1
                    return
                d_closed = daily_klines_pf[:-1]  # drop unclosed
                d_closes_pf = [float(k[4]) for k in d_closed]
                d_open_times = [int(k[0]) for k in d_closed]
                phase, phase_start_ms = compute_phase_state(
                    d_closes_pf, d_open_times,
                    period=config.PHASE_BB_PERIOD, std_dev=config.PHASE_BB_STD,
                )
                if not phase_allows(trend, phase):
                    logger.info("[阶段] %s %s 信号被过滤 | 当前日线阶段: %s",
                                symbol, trend, phase or "无")
                    skip_counts["阶段不符"] += 1
                    return
                if state_mgr.get_traded_phase(symbol) == phase_start_ms:
                    logger.info("[阶段] %s %s 本阶段已交易过(起始 %d),跳过",
                                symbol, trend, phase_start_ms)
                    skip_counts["阶段不符"] += 1
                    return
                # mark this phase as traded; recorded just before opening
                _pf_phase_start = phase_start_ms
            else:
                _pf_phase_start = None
```

Then, immediately after the successful `_open_position(...)` call (line ~463), record the phase:

```python
            _open_position(exchange, state_mgr, symbol, trend, current_price, data["hourly"])
            opened += 1
            if config.PHASE_FILTER_ENABLED and _pf_phase_start is not None:
                state_mgr.set_traded_phase(symbol, _pf_phase_start)
```

- [ ] **Step 4: Add imports**

At the top of `strategy.py`, the new functions are defined in the same module, so no import is needed. Confirm `compute_phase_state`/`phase_allows` are defined above `run_strategy` (Task 1 placed them at ~line 165, before `run_strategy` at ~206). ✓

- [ ] **Step 5: Smoke test — config import + module import**

Run: `python -c "import strategy; from strategy import compute_phase_state, phase_allows; print('ok')"`
Expected: `ok`

Run the full suite to ensure nothing broke:
Run: `python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add strategy.py
git commit -m "feat(phase): gate live entries by daily phase + first-trade-per-phase"
```

---

### Task 6: Catastrophe stop + sizing in `_open_position`

**Files:**
- Modify: `strategy.py` `compute_entry_risk` (~line 492) and `_open_position` (~line 514).

**Interfaces:**
- Consumes: `config.EXIT_MODE`, `config.CATASTROPHE_STOP_ENABLED`, `config.CATASTROPHE_STOP_PCT`.
- Produces: in `phase_bb` mode, positions are opened with atr_dual sizing, `soft_stop_pct=None` (so the legacy soft-stop loop skips them), and a wide catastrophe STOP_MARKET as `hard_stop_pct`.

- [ ] **Step 1: Branch sizing for phase_bb mode**

In `compute_entry_risk` (~line 496), wrap the atr_dual branch so phase_bb keeps atr_dual *sizing* but uses the catastrophe pct as the hard stop and disables the soft stop:

```python
def compute_entry_risk(hourly_klines: list, entry_price: float) -> dict:
    """按 STOP_MODE / EXIT_MODE 计算本笔的止损距离与等风险仓位。
    atr_dual: ATR 自适应软/硬止损 + 名义 = RISK_PER_TRADE_USD/软止损%（封顶 MAX_NOTIONAL_USD）
    phase_bb: 沿用 atr_dual 的等风险名义；软止损=None（出场交给相位逻辑），
              硬止损=CATASTROPHE_STOP_PCT（仅掉线/跳空兜底）
    fixed:    旧逻辑（soft_stop_pct=None）"""
    if config.STOP_MODE == "atr_dual" and hourly_klines:
        closed = hourly_klines[:-1]  # drop unclosed candle
        atr = calculate_atr(
            [float(k[2]) for k in closed],
            [float(k[3]) for k in closed],
            [float(k[4]) for k in closed],
            config.ATR_PERIOD,
        )
        soft_pct, hard_pct = compute_stop_distances(atr, entry_price)
        notional, margin = compute_position_size(soft_pct)
        if config.EXIT_MODE == "phase_bb":
            # sizing keeps the atr_dual notional; stops handed to phase logic
            return {"atr": atr, "soft_stop_pct": None,
                    "hard_stop_pct": (config.CATASTROPHE_STOP_PCT
                                      if config.CATASTROPHE_STOP_ENABLED else None),
                    "notional": notional, "margin": margin}
        return {"atr": atr, "soft_stop_pct": soft_pct, "hard_stop_pct": hard_pct,
                "notional": notional, "margin": margin}
    return {"atr": 0.0, "soft_stop_pct": None,
            "hard_stop_pct": config.FIXED_STOP_LOSS_PCT,
            "notional": config.POSITION_SIZE * config.LEVERAGE,
            "margin": config.POSITION_SIZE}
```

- [ ] **Step 2: Guard the catastrophe-stop placement against None**

In `_open_position`, the STOP_MARKET placement (~line 576) reads `hard_pct`. When `CATASTROPHE_STOP_ENABLED=False`, `hard_pct` is `None`. Wrap the placement in a clean conditional (no exception-as-control-flow). Replace the existing block:

```python
        # 硬止损 — STOP_MARKET at entry ± hard_pct（atr_dual）或 ± FIXED_STOP_LOSS_PCT（fixed）
        try:
            raw_sl = (
                fill_price * (1 - hard_pct) if side == "LONG"
                else fill_price * (1 + hard_pct)
            )
            sl_price = exchange.round_stop_price(symbol, raw_sl, side)
            if sl_price <= 0:
                logger.warning("[开仓] %s 止损价为 0（tick 过大），依赖本地轮询兜底", symbol)
            else:
                sl_order = exchange.place_stop_order(
                    symbol, close_side, executed_qty, sl_price, position_side=side
                )
                state_mgr.set_stop_order_id(pos["id"], sl_order.get("orderId"))
                logger.info("[开仓] %s 止损单 orderId=%s 止损价 %.8f", symbol, sl_order.get("orderId"), sl_price)
        except Exception as e:
            logger.error("[开仓] %s 止损单下单失败: %s | 将由本地轮询兜底", symbol, e)
```

with:

```python
        # 硬止损 — STOP_MARKET（phase_bb: 仅掉线兜底，可关闭；atr_dual/fixed: 主止损）
        if hard_pct is None:
            logger.info("[开仓] %s 灾难止损已关闭(phase_bb),仅依赖相位出场", symbol)
        else:
            try:
                raw_sl = (
                    fill_price * (1 - hard_pct) if side == "LONG"
                    else fill_price * (1 + hard_pct)
                )
                sl_price = exchange.round_stop_price(symbol, raw_sl, side)
                if sl_price <= 0:
                    logger.warning("[开仓] %s 止损价为 0（tick 过大），依赖本地轮询兜底", symbol)
                else:
                    sl_order = exchange.place_stop_order(
                        symbol, close_side, executed_qty, sl_price, position_side=side
                    )
                    state_mgr.set_stop_order_id(pos["id"], sl_order.get("orderId"))
                    logger.info("[开仓] %s 止损单 orderId=%s 止损价 %.8f", symbol, sl_order.get("orderId"), sl_price)
            except Exception as e:
                logger.error("[开仓] %s 止损单下单失败: %s | 将由本地轮询兜底", symbol, e)
```

Also update the notification block (~line 618): compute `sl_line` text conditionally so a `None` `hard_pct` shows `灾难止损: 关闭`. Replace:

```python
        sl_line = (
            fill_price * (1 - hard_pct) if side == "LONG"
            else fill_price * (1 + hard_pct)
        )
```

with:

```python
        if hard_pct is None:
            sl_text = "灾难止损: 关闭"
        else:
            sl_line = (
                fill_price * (1 - hard_pct) if side == "LONG"
                else fill_price * (1 + hard_pct)
            )
            sl_text = f"硬止损: {sl_line:.4f} ({hard_pct*100:.1f}%)"
```

and in the `notify(...)` body that follows, replace the `f"硬止损: {sl_line:.4f} ({hard_pct*100:.1f}%){soft_msg}{funding_msg}"` line with `f"{sl_text}{soft_msg}{funding_msg}"`.

- [ ] **Step 3: Smoke test**

Run: `python -c "from strategy import compute_entry_risk; from config import config; config.EXIT_MODE='phase_bb'; r=compute_entry_risk([[0,1,2,0.5,1,0]]*20,1.0); print(r['soft_stop_pct'], r['hard_stop_pct'], round(r['notional'],1))"`
Expected: `None 0.08 <some notional>`  (soft disabled, catastrophe 8%)

- [ ] **Step 4: Commit**

```bash
git add strategy.py
git commit -m "feat(phase): phase_bb sizing keeps atr_dual notional + wide catastrophe stop"
```

---

### Task 7: Exit orchestration + dispatch in risk.py

**Files:**
- Modify: `risk.py` — add `_check_phase_exits`; dispatch in `check_stop_loss` (~line 340); add `last_phase_exit_hour` accessors to `state.py`.

**Interfaces:**
- Consumes: `check_phase_exit`, `compute_phase_exit_inputs` (Task 4); `config.EXIT_MODE`, `config.PHASE_BB_PERIOD`, `config.PHASE_EXIT_TRAILING_PCT`; `exchange.get_klines`; `_close_position`, `_parse_position_dt` (existing).
- Produces: once-per-hour phase exits that market-close positions (cancelling the catastrophe stop via `_close_position`).

- [ ] **Step 1: Add the hourly dedup accessor to state.py**

After `set_last_soft_check_hour` (~line 157):

```python
    @property
    def last_phase_exit_hour(self):
        return self.state.get("last_phase_exit_hour")

    def set_last_phase_exit_hour(self, hour_key: str):
        with self._lock:
            self.state["last_phase_exit_hour"] = hour_key
        self.save()
```

- [ ] **Step 2: Add `_check_phase_exits` to risk.py**

After `_check_soft_stops` (~line 338):

```python
def _check_phase_exits(exchange: Exchange, state_mgr: StateManager, now: datetime = None):
    """相位出场（EXIT_MODE=phase_bb）：每个整点后的第一个风控 tick，对每个持仓用
    最近一根已收盘 1H 计算 1H 布林中轨 + 入场以来确认最高/最低点。满足
    『收盘越过中轨』或『从确认极值回撤 3.5%』则市价平仓。本小时内开的仓先扛一根 K 线。"""
    if config.EXIT_MODE != "phase_bb":
        return
    now = now or datetime.now(TZ_CN)
    hour_key = now.strftime("%Y-%m-%d %H")
    if state_mgr.last_phase_exit_hour == hour_key:
        return
    prev_key = state_mgr.last_phase_exit_hour
    state_mgr.set_last_phase_exit_hour(hour_key)

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_start_ms = int(hour_start.timestamp() * 1000)
    HOUR_MS = 3_600_000
    stale = False
    bb_period = config.PHASE_BB_PERIOD
    trailing_pct = config.PHASE_EXIT_TRAILING_PCT

    for pos in list(state_mgr.state.get("positions", [])):
        opened_dt = _parse_position_dt(pos.get("opened_at"))
        if opened_dt is None:
            continue
        # entry bar open_time (floor to the hour, ms)
        opened_ms = pos.get("opened_ms")
        if not opened_ms:
            opened_ms = int(opened_dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)

        # need klines from entry through now + a BB warmup margin
        hours_since_entry = max(0, int((hour_start_ms - opened_ms) // HOUR_MS))
        limit = min(1000, hours_since_entry + bb_period + 3)
        try:
            kl = exchange.get_klines(pos["symbol"], "1h", limit)
        except Exception as e:
            logger.warning("[相位出场] %s 拉K线失败,本小时跳过: %s", pos["symbol"], e)
            continue
        if not kl or len(kl) < 2:
            continue
        # confirm the latest CLOSED bar is the one that just closed this hour
        last_closed = kl[-2]
        if int(last_closed[0]) < hour_start_ms - HOUR_MS:
            stale = True  # Binance hasn't rolled the new bar yet; retry this hour
            continue
        closed_klines = kl[:-1]  # drop the in-progress candle

        out = compute_phase_exit_inputs(closed_klines, opened_ms,
                                        pos["entry_price"], bb_period)
        if out is None:
            continue
        bar_close, bb_middle, pre_high, pre_low = out
        pre_extreme = pre_high if pos["side"] == "LONG" else pre_low
        reason = check_phase_exit(pos["side"], bar_close, bb_middle,
                                  pre_extreme, trailing_pct)
        if reason:
            label = "1H中轨" if reason == "1h_bb_middle" else "回撤3.5%"
            logger.info("[相位出场] %s %s | 1H收盘 %.4f | 中轨 %.4f | 确认极值 %.4f | %s | 平仓",
                        pos["symbol"], pos["side"], bar_close, bb_middle, pre_extreme, label)
            _close_position(exchange, state_mgr, pos, bar_close, f"相位出场({label})")

    if stale:
        state_mgr.set_last_phase_exit_hour(prev_key or "")
```

- [ ] **Step 3: Dispatch in `check_stop_loss`**

In `check_stop_loss` (~line 347), after the drawdown check and sync, branch on `EXIT_MODE`:

```python
    if check_drawdown(exchange, state_mgr):
        return

    # Sync local state with exchange first — catches stops triggered while offline
    _sync_positions_with_exchange(exchange, state_mgr)

    if config.EXIT_MODE == "phase_bb":
        _check_phase_exits(exchange, state_mgr)
    else:
        _check_soft_stops(exchange, state_mgr)
```

Leave the rest of `check_stop_loss` (the per-position exchange-order/trailing loop) intact: in `phase_bb` mode positions carry only the catastrophe `stop_order_id` and no `trailing_order_id`, so that loop just keeps the catastrophe STOP_MARKET healthy (re-places if cancelled) and never activates a trailing order (activation needs `soft`-era trailing fields; the +3.5% activation check still runs but its purpose is now covered by the phase exit which fires first on the hourly grid). To avoid double-managing, guard the trailing block: change line ~417 `elif activation_reached:` to `elif activation_reached and config.EXIT_MODE != "phase_bb":`.

- [ ] **Step 4: Store opened_ms on new positions**

In `state.py` `add_position` (~line 78), add `opened_ms` so phase exits have an exact entry-bar timestamp:

```python
            pos = {
                "id": str(uuid.uuid4()),
                ...
                "opened_at": now_cn(),
                "opened_ms": int(datetime.now(TZ_CN).replace(minute=0, second=0, microsecond=0).timestamp() * 1000),
                ...
            }
```

(`datetime` is already imported in state.py.)

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add risk.py state.py
git commit -m "feat(phase): hourly phase-exit orchestration + check_stop_loss dispatch"
```

---

### Task 8: End-to-end paper smoke + docs

**Files:**
- Modify: `CLAUDE.md` (risk-check description), `.env` (confirm `TRADING_MODE=paper`).

- [ ] **Step 1: Dry-run the strategy scan against live data (no orders if no signal)**

Run (paper/testnet, will place orders only if a gated signal fires):
`python -c "from exchange import Exchange; from state import StateManager; from config import config; from strategy import run_strategy; sm=StateManager(config.STATE_FILE, config.STATE_BACKUP_FILE, config.INITIAL_CAPITAL); sm.load(); run_strategy(Exchange(), sm)"`
Expected: scan log shows `过滤模式` and `[阶段]` filter lines; no crash.

- [ ] **Step 2: Update CLAUDE.md risk-check bullet**

Replace the `risk.py:check_stop_loss` description to document the `phase_bb` exit mode (1H BB-middle cross OR 3.5% confirmed-retrace, once per hour; wide catastrophe stop for offline protection; phase gate + first-trade-per-phase on entries).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document phase_bb exit mode + phase filter in CLAUDE.md"
```

---

## Self-Review

**Spec coverage:**
- 日线阶段交易开关 (UP/DOWN start/end, persistence) → Task 1 `compute_phase_state` ✓
- 只看已收盘日K (no future function) → Task 5 drops unclosed daily candle ✓
- 方向限制 (LONG only UP / SHORT only DOWN) → Task 1 `phase_allows` + Task 5 gate ✓
- 同阶段只做第一笔 → Task 2 `traded_phases` + Task 5 dedup on `phase_start_ms` ✓
- 入场沿用原策略 → Task 5 gate is purely additive, original signal logic untouched ✓
- 多/空出场 (1H BB-middle cross) → Task 4 `check_phase_exit` ✓
- 3.5% 回撤 from confirmed pre-bar extreme, current bar excluded → Task 4 `compute_phase_exit_inputs` excludes entry+current bar ✓
- Exits on 1H close → Task 7 once-per-hour orchestration ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `compute_phase_state` returns `(str|None, int)` used identically in Task 5. `compute_phase_exit_inputs` returns 4-tuple consumed in Task 7. `check_phase_exit` reason strings (`"1h_bb_middle"`, `"trailing_3.5pct"`) consistent across Tasks 4 & 7. `traded_phases` keyed by symbol→int across Tasks 2 & 5. ✓

**Open risk flagged for the user:** catastrophe stop default ON at 8% (beyond spec, live-safety). Toggle via `CATASTROPHE_STOP_ENABLED`.
