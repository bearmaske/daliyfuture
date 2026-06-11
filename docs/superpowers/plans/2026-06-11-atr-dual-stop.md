# 双层 ATR 自适应止损 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把固定 2% 止损改造为双层结构——软止损（ATR 自适应距离，仅 1H 收盘确认，本地判断）+ 硬止损（2× 软，交易所常驻 STOP_MARKET 单），仓位按软止损距离等风险缩放（$40/笔）。

**Architecture:** 纯计算函数（ATR、止损距离、等风险仓位）放 `risk.py` 供实盘和回测共用；`strategy.py` 开仓时算距离和仓位、把硬止损挂到交易所、新字段写入仓位记录；`risk.py` 风控循环改为按仓位字段取硬止损/保证金，并新增每整点一次的软止损收盘确认；`backtesting/engine.py` 加 `STOP_MODE` 分支镜像同样语义。`STOP_MODE="fixed"` 保留旧逻辑可一键回滚。

**Tech Stack:** Python 3, pytest, pandas (回测), python-binance。

**Spec:** `docs/superpowers/specs/2026-06-11-atr-dual-stop-design.md`

**项目约定（执行者必读）：**
- 测试命令：`source .venv/bin/activate` 后 `python -m pytest tests/ -v`（或用 `.venv/bin/python -m pytest`）
- K 线为 Binance 原始列表格式：`[open_time, open, high, low, close, volume, close_time, quote_volume, ...]`，索引 2=high, 3=low, 4=close，元素是字符串
- 计算前丢掉最后一根未收盘 K 线（项目惯例），由**调用方**负责
- 日志/通知用中文标签
- git 提交信息结尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: config.py 新增双层止损配置块

**Files:**
- Modify: `config.py` (在 `# Trailing TP + Fixed SL` 块之后、`# Global Drawdown Circuit Breaker` 之前插入)

- [ ] **Step 1: 添加配置常量**

在 `config.py` 中 `FIXED_STOP_LOSS_PCT: float = 0.02` 这一行之后插入：

```python
    # Dual-layer ATR-adaptive stop (see docs/superpowers/specs/2026-06-11-atr-dual-stop-design.md)
    # "fixed"    = 旧逻辑：交易所 STOP_MARKET 挂在 entry ± FIXED_STOP_LOSS_PCT
    # "atr_dual" = 新逻辑：软止损(ATR 自适应, 1H 收盘确认, 本地) + 硬止损(2×软, 交易所挂单)
    STOP_MODE: str = "atr_dual"
    ATR_PERIOD: int = 14               # 1H K线 Wilder ATR 周期
    SOFT_STOP_ATR_MULT: float = 1.5    # 软止损 = 1.5 × ATR / 入场价
    SOFT_STOP_FLOOR_PCT: float = 0.02  # 软止损下限 2%
    HARD_STOP_MULT: float = 2.0        # 硬止损 = 2 × 软止损
    HARD_STOP_CAP_PCT: float = 0.06    # 硬止损上限 6%（软止损也以此封顶，保证 软 ≤ 硬）
    RISK_PER_TRADE_USD: float = 40.0   # 单笔风险额：名义 = 40 / 软止损%
    MAX_NOTIONAL_USD: float = 2000.0   # 名义上限 = 现状 POSITION_SIZE × LEVERAGE，只缩不放大
```

- [ ] **Step 2: 跑全量测试确认无回归**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS（纯增配置，不应影响任何现有行为）

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): 双层 ATR 止损配置块 (STOP_MODE/ATR/风险额)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: risk.calculate_atr — Wilder ATR

**Files:**
- Modify: `risk.py` (在 `check_fixed_sl` 之前添加函数)
- Test: `tests/test_risk.py` (追加)

- [ ] **Step 1: 写失败测试**

在 `tests/test_risk.py` 末尾追加：

```python
# ---------- calculate_atr ----------

from risk import calculate_atr


def test_atr_constant_range():
    # 每根 K 线 high-low=2、无跳空 → TR 恒为 2 → ATR=2
    n = 16
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    assert calculate_atr(highs, lows, closes, period=14) == pytest.approx(2.0)


def test_atr_uses_prev_close_for_gaps():
    # period=2: TR1 = max(1, |12-9.5|, |11-9.5|) = 2.5; TR2 = max(1, |20-11.5|, |19-11.5|) = 8.5
    # 初始 ATR = (2.5+8.5)/2 = 5.5
    highs = [10.0, 12.0, 20.0]
    lows = [9.0, 11.0, 19.0]
    closes = [9.5, 11.5, 19.5]
    assert calculate_atr(highs, lows, closes, period=2) == pytest.approx(5.5)


def test_atr_wilder_smoothing():
    # 在上例后追加一根: TR3 = max(1, |21-19.5|, |20-19.5|) = 1.5
    # ATR = (5.5×(2-1) + 1.5)/2 = 3.5
    highs = [10.0, 12.0, 20.0, 21.0]
    lows = [9.0, 11.0, 19.0, 20.0]
    closes = [9.5, 11.5, 19.5, 20.5]
    assert calculate_atr(highs, lows, closes, period=2) == pytest.approx(3.5)


def test_atr_insufficient_data_returns_zero():
    # 需要 period+1 根，14 根不够
    assert calculate_atr([1.0] * 14, [1.0] * 14, [1.0] * 14, period=14) == 0.0


def test_atr_mismatched_lengths_returns_zero():
    assert calculate_atr([1.0] * 16, [1.0] * 15, [1.0] * 16, period=14) == 0.0
```

注意：若 `tests/test_risk.py` 文件头部没有 `import pytest`，需补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k atr`
Expected: FAIL，`ImportError: cannot import name 'calculate_atr'`

- [ ] **Step 3: 实现 calculate_atr**

在 `risk.py` 的 `check_fixed_sl` 函数之前添加：

```python
def calculate_atr(highs: List[float], lows: List[float], closes: List[float],
                  period: int = 14) -> float:
    """Wilder ATR。数据不足（< period+1 根）或长度不一致时返回 0.0。
    调用方负责丢掉最后一根未收盘 K 线（项目惯例）。"""
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return 0.0
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr
```

`risk.py` 顶部已有 `from typing import List`，无需新增 import。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k atr`
Expected: 5 个测试全部 PASS

- [ ] **Step 5: Commit**

```bash
git add risk.py tests/test_risk.py
git commit -m "feat(risk): Wilder ATR 计算函数 (实盘/回测共用)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: risk.compute_stop_distances + compute_position_size

**Files:**
- Modify: `risk.py` (紧跟 `calculate_atr` 之后)
- Test: `tests/test_risk.py` (追加)

- [ ] **Step 1: 写失败测试**

在 `tests/test_risk.py` 末尾追加（依赖 config 默认值：floor 2% / mult 1.5 / hard 2× / cap 6% / 风险 $40 / 名义上限 $2000 / 杠杆 5）：

```python
# ---------- compute_stop_distances / compute_position_size ----------

from risk import compute_stop_distances, compute_position_size


def test_stop_distances_zero_atr_falls_back_to_floor():
    soft, hard = compute_stop_distances(0.0, 100.0)
    assert soft == pytest.approx(0.02)
    assert hard == pytest.approx(0.04)


def test_stop_distances_calm_coin_floor_binds():
    # 1.5×1/100 = 1.5% < 2% floor → (2%, 4%)
    soft, hard = compute_stop_distances(1.0, 100.0)
    assert soft == pytest.approx(0.02)
    assert hard == pytest.approx(0.04)


def test_stop_distances_volatile_coin_scales():
    # 1.5×2/100 = 3% → (3%, 6%)
    soft, hard = compute_stop_distances(2.0, 100.0)
    assert soft == pytest.approx(0.03)
    assert hard == pytest.approx(0.06)


def test_stop_distances_hard_cap_binds_first():
    # 1.5×2.4/100 = 3.6% → hard = min(7.2%, 6%) = 6%
    soft, hard = compute_stop_distances(2.4, 100.0)
    assert soft == pytest.approx(0.036)
    assert hard == pytest.approx(0.06)


def test_stop_distances_extreme_atr_soft_capped_no_inversion():
    # 1.5×5/100 = 7.5% → soft 封顶 6%，hard=6%；软 ≤ 硬 恒成立
    soft, hard = compute_stop_distances(5.0, 100.0)
    assert soft == pytest.approx(0.06)
    assert hard == pytest.approx(0.06)
    assert soft <= hard


def test_position_size_baseline_matches_status_quo():
    # 软 2% → 名义 min(40/0.02, 2000)=2000，保证金 400 —— 与现状完全一致
    notional, margin = compute_position_size(0.02)
    assert notional == pytest.approx(2000.0)
    assert margin == pytest.approx(400.0)


def test_position_size_scales_down_with_wider_stop():
    # 软 4% → 名义 1000，保证金 200
    notional, margin = compute_position_size(0.04)
    assert notional == pytest.approx(1000.0)
    assert margin == pytest.approx(200.0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k "stop_distances or position_size"`
Expected: FAIL，`ImportError: cannot import name 'compute_stop_distances'`

- [ ] **Step 3: 实现两个函数**

在 `risk.py` 的 `calculate_atr` 之后添加：

```python
def compute_stop_distances(atr: float, entry_price: float) -> tuple[float, float]:
    """软/硬止损距离（占入场价的比例）。
    软 = clamp(SOFT_STOP_ATR_MULT × ATR / 价格, floor=SOFT_STOP_FLOOR_PCT, cap=HARD_STOP_CAP_PCT)
    硬 = min(HARD_STOP_MULT × 软, HARD_STOP_CAP_PCT)。软 ≤ 硬 恒成立。
    ATR 缺失（=0）时退化为 floor。"""
    if atr <= 0 or entry_price <= 0:
        soft = config.SOFT_STOP_FLOOR_PCT
    else:
        soft = max(config.SOFT_STOP_FLOOR_PCT,
                   config.SOFT_STOP_ATR_MULT * atr / entry_price)
    soft = min(soft, config.HARD_STOP_CAP_PCT)
    hard = min(config.HARD_STOP_MULT * soft, config.HARD_STOP_CAP_PCT)
    return soft, hard


def compute_position_size(soft_stop_pct: float) -> tuple[float, float]:
    """等风险仓位：名义 = RISK_PER_TRADE_USD / 软止损%，封顶 MAX_NOTIONAL_USD。
    返回 (名义, 保证金)。"""
    notional = min(config.RISK_PER_TRADE_USD / soft_stop_pct, config.MAX_NOTIONAL_USD)
    return notional, notional / config.LEVERAGE
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k "stop_distances or position_size"`
Expected: 7 个测试全部 PASS

- [ ] **Step 5: Commit**

```bash
git add risk.py tests/test_risk.py
git commit -m "feat(risk): 软/硬止损距离与等风险仓位计算

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: state.py — 仓位新字段 + last_soft_check_hour

**Files:**
- Modify: `state.py:71-92` (`add_position`)，类内另加两个访问器
- Test: `tests/test_state.py` (追加)

- [ ] **Step 1: 写失败测试**

在 `tests/test_state.py` 末尾追加：

```python
def test_add_position_with_stop_fields(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="ETHUSDT", side="SHORT", entry_price=3000.0, quantity=0.5,
        soft_stop_pct=0.03, hard_stop_pct=0.06, position_size=222.0, atr_at_entry=60.0,
    )
    assert pos["soft_stop_pct"] == 0.03
    assert pos["hard_stop_pct"] == 0.06
    assert pos["position_size"] == 222.0
    assert pos["atr_at_entry"] == 60.0


def test_add_position_stop_fields_default_none(state_mgr):
    # 不传新参数（旧调用方式）→ 字段存在且为 None（存量兼容由 risk.py 回退处理）
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.01,
    )
    assert pos["soft_stop_pct"] is None
    assert pos["hard_stop_pct"] is None
    assert pos["position_size"] is None


def test_last_soft_check_hour_roundtrip(state_mgr):
    state_mgr.load()
    assert state_mgr.last_soft_check_hour is None
    state_mgr.set_last_soft_check_hour("2026-06-11 14")
    assert state_mgr.last_soft_check_hour == "2026-06-11 14"
    # 持久化验证：重新 load 后仍在
    state_mgr.load()
    assert state_mgr.last_soft_check_hour == "2026-06-11 14"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_state.py -v -k "stop_fields or soft_check_hour"`
Expected: FAIL（`add_position() got an unexpected keyword argument 'soft_stop_pct'` / `AttributeError: last_soft_check_hour`）

- [ ] **Step 3: 实现**

修改 `state.py` 的 `add_position`（替换现有签名与 pos 字典）：

```python
    def add_position(
        self, symbol: str, side: str, entry_price: float, quantity: float,
        open_order_id: int = None,
        soft_stop_pct: float = None, hard_stop_pct: float = None,
        position_size: float = None, atr_at_entry: float = None,
    ) -> dict:
        with self._lock:
            pos = {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "quantity": quantity,
                "highest_price": entry_price,
                "lowest_price": entry_price,
                "trailing_activated": False,
                "stop_order_id": None,
                "trailing_order_id": None,
                "open_order_id": open_order_id,
                "opened_at": now_cn(),
                "soft_stop_pct": soft_stop_pct,
                "hard_stop_pct": hard_stop_pct,
                "position_size": position_size,
                "atr_at_entry": atr_at_entry,
            }
            self.state["positions"].append(pos)
        self.save()
        return pos
```

在 `set_trailing_activated` 方法之后添加：

```python
    @property
    def last_soft_check_hour(self):
        """软止损收盘确认的去重键：'YYYY-MM-DD HH'，每小时只跑一次。"""
        return self.state.get("last_soft_check_hour")

    def set_last_soft_check_hour(self, hour_key: str):
        with self._lock:
            self.state["last_soft_check_hour"] = hour_key
        self.save()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_state.py -v`
Expected: 全部 PASS（含原有测试——`add_position` 新参数全部有默认值，向后兼容）

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat(state): 仓位记录软/硬止损字段 + 软止损小时去重键

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: strategy.py — 开仓接入 ATR 仓位与硬止损

**Files:**
- Modify: `strategy.py` (`_open_position` 函数、调用点 `strategy.py:462`、顶部 import)
- Test: `tests/test_strategy.py` (追加)

- [ ] **Step 1: 写失败测试（纯函数 compute_entry_risk）**

在 `tests/test_strategy.py` 末尾追加：

```python
# ---------- compute_entry_risk ----------

from strategy import compute_entry_risk
from config import config as _cfg


def _mk_klines(n, high, low, close):
    """Binance 原始 K 线格式（字符串数值），最后一根视为未收盘。"""
    return [[i, str(close), str(high), str(low), str(close), "0", 0, "0"]
            for i in range(n)]


def test_compute_entry_risk_atr_dual_scales_position(monkeypatch):
    monkeypatch.setattr(_cfg, "STOP_MODE", "atr_dual")
    # 30 根，high-low=2.4 恒定 → ATR=2.4 → 软=1.5×2.4/100=3.6%，硬=min(7.2%,6%)=6%
    kl = _mk_klines(30, high=101.2, low=98.8, close=100.0)
    r = compute_entry_risk(kl, 100.0)
    assert r["soft_stop_pct"] == pytest.approx(0.036)
    assert r["hard_stop_pct"] == pytest.approx(0.06)
    assert r["notional"] == pytest.approx(40.0 / 0.036)
    assert r["margin"] == pytest.approx(40.0 / 0.036 / 5)
    assert r["atr"] == pytest.approx(2.4)


def test_compute_entry_risk_fixed_mode_matches_legacy(monkeypatch):
    monkeypatch.setattr(_cfg, "STOP_MODE", "fixed")
    kl = _mk_klines(30, high=101.2, low=98.8, close=100.0)
    r = compute_entry_risk(kl, 100.0)
    assert r["soft_stop_pct"] is None
    assert r["hard_stop_pct"] == _cfg.FIXED_STOP_LOSS_PCT
    assert r["notional"] == pytest.approx(_cfg.POSITION_SIZE * _cfg.LEVERAGE)
    assert r["margin"] == pytest.approx(_cfg.POSITION_SIZE)


def test_compute_entry_risk_insufficient_klines_falls_back_to_floor(monkeypatch):
    monkeypatch.setattr(_cfg, "STOP_MODE", "atr_dual")
    kl = _mk_klines(5, high=101.0, low=99.0, close=100.0)  # 不足 ATR_PERIOD+1
    r = compute_entry_risk(kl, 100.0)
    # ATR=0 → 软 2% / 硬 4%，名义回到 $2000
    assert r["soft_stop_pct"] == pytest.approx(0.02)
    assert r["hard_stop_pct"] == pytest.approx(0.04)
    assert r["notional"] == pytest.approx(2000.0)
```

注意：若 `tests/test_strategy.py` 头部没有 `import pytest`，需补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy.py -v -k entry_risk`
Expected: FAIL，`ImportError: cannot import name 'compute_entry_risk'`

- [ ] **Step 3: 实现 compute_entry_risk**

`strategy.py` 顶部 import 区追加：

```python
from risk import calculate_atr, compute_stop_distances, compute_position_size
```

（`risk.py` 不 import `strategy`，无循环依赖。）

在 `_open_position` 函数定义之前添加：

```python
def compute_entry_risk(hourly_klines: list, entry_price: float) -> dict:
    """按 STOP_MODE 计算本笔的止损距离与等风险仓位。
    atr_dual: ATR 自适应软/硬止损 + 名义 = RISK_PER_TRADE_USD/软止损%（封顶 MAX_NOTIONAL_USD）
    fixed:    旧逻辑（soft_stop_pct=None → 风控循环跳过软止损检查）"""
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
        return {"atr": atr, "soft_stop_pct": soft_pct, "hard_stop_pct": hard_pct,
                "notional": notional, "margin": margin}
    return {"atr": 0.0, "soft_stop_pct": None,
            "hard_stop_pct": config.FIXED_STOP_LOSS_PCT,
            "notional": config.POSITION_SIZE * config.LEVERAGE,
            "margin": config.POSITION_SIZE}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy.py -v -k entry_risk`
Expected: 3 个测试 PASS

- [ ] **Step 5: 接线 _open_position**

5a. 修改 `_open_position` 签名（`strategy.py:491-497`）：

```python
def _open_position(
    exchange: Exchange,
    state_mgr: StateManager,
    symbol: str,
    side: str,
    current_price: float,
    hourly_klines: list = None,
):
```

5b. 替换 sizing 块（原 `notional = config.POSITION_SIZE * config.LEVERAGE` 至 `quantity = ...` 三行）：

```python
    risk_info = compute_entry_risk(hourly_klines, current_price)
    soft_pct = risk_info["soft_stop_pct"]
    hard_pct = risk_info["hard_stop_pct"]
    margin = risk_info["margin"]
    raw_qty = risk_info["notional"] / current_price
    quantity = exchange.round_quantity(symbol, raw_qty)
```

5c. `add_position` 调用与扣余额（原 `pos = state_mgr.add_position(...)` 与 `state_mgr.update_balance(-config.POSITION_SIZE)`）替换为：

```python
        pos = state_mgr.add_position(
            symbol=symbol,
            side=side,
            entry_price=fill_price,
            quantity=executed_qty,
            open_order_id=open_order_id,
            soft_stop_pct=soft_pct,
            hard_stop_pct=hard_pct,
            position_size=margin,
            atr_at_entry=risk_info["atr"],
        )
        state_mgr.update_balance(-margin)
```

5d. 止损挂单块（`# Fixed stop loss — STOP_MARKET at entry ± 2%` 注释及其 `raw_sl` 计算）改为挂**硬止损价**：

```python
        # 硬止损 — STOP_MARKET at entry ± hard_pct（atr_dual）或 ± FIXED_STOP_LOSS_PCT（fixed）
        try:
            raw_sl = (
                fill_price * (1 - hard_pct) if side == "LONG"
                else fill_price * (1 + hard_pct)
            )
```

（块内后续 `sl_price = exchange.round_stop_price(...)` 等行不变。）

5e. 开仓日志行（`实际名义 $%.2f | 保证金 $%.2f` 那条 `logger.info`）中 `config.POSITION_SIZE` 参数改为 `margin`。

5f. 通知块（原 `sl_price = fill_price * (1 - config.FIXED_STOP_LOSS_PCT) ...` 与 `notify(...)`）替换为：

```python
        sl_line = (
            fill_price * (1 - hard_pct) if side == "LONG"
            else fill_price * (1 + hard_pct)
        )
        soft_msg = ""
        if soft_pct:
            soft_line = (
                fill_price * (1 - soft_pct) if side == "LONG"
                else fill_price * (1 + soft_pct)
            )
            soft_msg = f"\n软止损(1H收盘): {soft_line:.4f} ({soft_pct*100:.1f}%)"
        notify(
            f"开仓 {side}",
            f"{symbol} | 成交价 {fill_price:.4f} | 数量 {executed_qty:g} | "
            f"保证金 ${margin:.0f}\n"
            f"硬止损: {sl_line:.4f} ({hard_pct*100:.1f}%){soft_msg}{funding_msg}",
        )
```

5g. 调用点 `strategy.py:462` 改为传入 K 线：

```python
            _open_position(exchange, state_mgr, symbol, trend, current_price, data["hourly"])
```

说明：余额闸门（`state_mgr.balance < config.POSITION_SIZE`，两处）**不改**——`margin ≤ MAX_NOTIONAL_USD/LEVERAGE = $400 = POSITION_SIZE`，现有检查是保守上界。`quantity <= 0` 跳过逻辑已覆盖"缩仓后数量过小"的放弃路径（stepSize 取整为 0）。

- [ ] **Step 6: 全量回归**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat(strategy): 开仓接入 ATR 双层止损与等风险缩仓

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: risk.py — 风控循环按仓位字段取硬止损/保证金

**Files:**
- Modify: `risk.py` (`calculate_pnl`、`check_stop_loss`、`_replace_stop_order`、`_record_position_close`)
- Test: `tests/test_risk.py` (追加)

- [ ] **Step 1: 写失败测试**

在 `tests/test_risk.py` 末尾追加：

```python
# ---------- 仓位字段回退助手 ----------

from risk import _pos_hard_stop_pct, _pos_margin


def test_pos_helpers_use_position_fields():
    pos = {"hard_stop_pct": 0.05, "position_size": 250.0}
    assert _pos_hard_stop_pct(pos) == 0.05
    assert _pos_margin(pos) == 250.0


def test_pos_helpers_fall_back_for_legacy_positions():
    # 存量仓位无新字段（或为 None）→ 回退 config
    from config import config
    assert _pos_hard_stop_pct({}) == config.FIXED_STOP_LOSS_PCT
    assert _pos_hard_stop_pct({"hard_stop_pct": None}) == config.FIXED_STOP_LOSS_PCT
    assert _pos_margin({}) == config.POSITION_SIZE
    assert _pos_margin({"position_size": None}) == config.POSITION_SIZE


def test_calculate_pnl_fallback_uses_position_size_param():
    from risk import calculate_pnl
    # 无 quantity 时用名义公式：1% × margin × LEVERAGE(5)
    pnl = calculate_pnl("LONG", 100.0, 101.0, quantity=None, position_size=200.0)
    assert pnl == pytest.approx(0.01 * 200.0 * 5)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k "pos_helpers or pnl_fallback"`
Expected: FAIL，`ImportError: cannot import name '_pos_hard_stop_pct'`

- [ ] **Step 3: 实现助手 + 改 calculate_pnl**

在 `risk.py` 的 `calculate_pnl` 之前添加：

```python
def _pos_hard_stop_pct(pos: dict) -> float:
    """硬止损距离：仓位字段优先，存量仓位回退 FIXED_STOP_LOSS_PCT。"""
    return pos.get("hard_stop_pct") or config.FIXED_STOP_LOSS_PCT


def _pos_margin(pos: dict) -> float:
    """本笔保证金：仓位字段优先，存量仓位回退 POSITION_SIZE。"""
    return pos.get("position_size") or config.POSITION_SIZE
```

修改 `calculate_pnl`（签名加 `position_size` 参数，fallback 分支用它）：

```python
def calculate_pnl(side: str, entry_price: float, exit_price: float,
                  quantity: float = None, position_size: float = None) -> float:
    """Calculate PnL. When quantity is given, uses qty × (exit − entry) which
    matches Binance's exchange-side accounting. Without quantity, falls back to
    the notional formula (position_size × leverage; 等风险缩仓后各笔不同)."""
    if quantity is not None and quantity > 0:
        if side == "LONG":
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity
    size = position_size or config.POSITION_SIZE
    if side == "LONG":
        return (exit_price - entry_price) / entry_price * size * config.LEVERAGE
    return (entry_price - exit_price) / entry_price * size * config.LEVERAGE
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k "pos_helpers or pnl_fallback"`
Expected: 3 个测试 PASS

- [ ] **Step 5: 替换 check_stop_loss / _replace_stop_order / _record_position_close 中的常量引用**

5a. `check_stop_loss` 中 `fixed_sl_price` 计算（原 `risk.py:271-275`）：

```python
            hard_pct = _pos_hard_stop_pct(pos)
            fixed_sl_price = (
                pos["entry_price"] * (1 - hard_pct)
                if pos["side"] == "LONG"
                else pos["entry_price"] * (1 + hard_pct)
            )
```

5b. 本地兜底检查（原 `risk.py:289-290`）：

```python
                if check_fixed_sl(pos["side"], pos["entry_price"], current_price,
                                  hard_pct):
```

5c. 浮动盈亏行（原 `risk.py:346-347`）：

```python
            unrealized = calculate_pnl(pos["side"], pos["entry_price"], current_price,
                                       pos.get("quantity"), position_size=_pos_margin(pos))
            unrealized_pct = unrealized / _pos_margin(pos) * 100
```

5d. `_replace_stop_order` 中（原 `risk.py:370-373`）：

```python
        hard_pct = _pos_hard_stop_pct(pos)
        raw_sl = (
            pos["entry_price"] * (1 - hard_pct) if pos["side"] == "LONG"
            else pos["entry_price"] * (1 + hard_pct)
        )
```

5e. `_record_position_close` 中（原 `risk.py:445-447`）：

```python
    state_mgr.update_balance(_pos_margin(pos) + raw_pnl)

    pnl_pct = raw_pnl / _pos_margin(pos) * 100
```

说明：交易所止损单的中文标签保持 `"固定止损"` 不变（`_check_exchange_order` 与 `_sync_positions_with_exchange` 按这个字符串做分支匹配），其含义在 atr_dual 模式下=硬止损。本轮不改标签，避免无关 diff。

- [ ] **Step 6: 全量回归**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add risk.py tests/test_risk.py
git commit -m "refactor(risk): 硬止损/保证金按仓位字段取值, 存量仓位回退旧常量

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: risk.py — 软止损（1H 收盘确认）

**Files:**
- Modify: `risk.py` (`_position_age` 重构出 `_parse_position_dt`；新增 `_check_soft_stops`；`check_stop_loss` 接入)
- Test: `tests/test_risk.py` (追加)

- [ ] **Step 1: 写失败测试**

在 `tests/test_risk.py` 末尾追加：

```python
# ---------- 软止损 (1H 收盘确认) ----------

from datetime import datetime
from unittest.mock import MagicMock
import risk
from risk import _check_soft_stops, TZ_CN
from state import StateManager


def _mk_state(tmp_path):
    sm = StateManager(str(tmp_path / "s.json"), str(tmp_path / "b.json"),
                      initial_capital=10000.0)
    sm.load()
    return sm


def _mk_exchange(closed_bar_close):
    """MagicMock Exchange：get_klines 返回 [已收盘bar, 未收盘bar]。"""
    ex = MagicMock()
    ex.get_klines.return_value = [
        [0, "0", "0", "0", str(closed_bar_close), "0", 0, "0"],
        [1, "0", "0", "0", "0", "0", 0, "0"],
    ]
    ex.get_order_fill.return_value = (closed_bar_close, 10.0)
    return ex


def _add_soft_pos(sm, opened_at, side="LONG", entry=100.0, soft=0.03):
    pos = sm.add_position(symbol="AAAUSDT", side=side, entry_price=entry,
                          quantity=10.0, soft_stop_pct=soft, hard_stop_pct=0.06,
                          position_size=266.0)
    pos["opened_at"] = opened_at
    sm.save()
    return pos


NOW = datetime(2026, 6, 11, 14, 1, 0, tzinfo=TZ_CN)


def test_soft_stop_closes_position_on_breached_close(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")          # 上一小时开仓
    ex = _mk_exchange(96.5)                            # 收盘 96.5 < 软止损线 97
    _check_soft_stops(ex, sm, now=NOW)
    assert sm.state["positions"] == []
    ex.place_order.assert_called_once()


def test_soft_stop_no_trigger_when_close_above_line(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    ex = _mk_exchange(97.5)                            # 收盘 97.5 > 97，盘中无所谓
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1
    ex.place_order.assert_not_called()


def test_soft_stop_skips_position_opened_this_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 14:00:30")           # 本小时刚开 → 等下个整点
    ex = _mk_exchange(90.0)
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1


def test_soft_stop_skips_legacy_position_without_field(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    sm.add_position(symbol="OLDUSDT", side="LONG", entry_price=100.0, quantity=10.0)
    ex = _mk_exchange(50.0)
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1
    ex.get_klines.assert_not_called()


def test_soft_stop_runs_once_per_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    ex = _mk_exchange(97.5)
    _check_soft_stops(ex, sm, now=NOW)
    _check_soft_stops(ex, sm, now=NOW.replace(minute=2))  # 同一小时第二次 tick
    assert ex.get_klines.call_count == 1


def test_soft_stop_disabled_in_fixed_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "fixed")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    ex = _mk_exchange(50.0)
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1


def test_soft_stop_short_side(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00", side="SHORT", entry=100.0, soft=0.03)
    ex = _mk_exchange(103.5)                           # 收盘 103.5 > 103 → SHORT 触发
    _check_soft_stops(ex, sm, now=NOW)
    assert sm.state["positions"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k soft_stop`
Expected: FAIL，`ImportError: cannot import name '_check_soft_stops'`

- [ ] **Step 3: 实现**

3a. 在 `risk.py` 的 `_position_age` 之前添加解析助手，并让 `_position_age` 复用：

```python
def _parse_position_dt(value: str):
    """Parse opened_at in any historical format. Returns aware datetime or None."""
    if not value:
        return None
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
    for f in fmts:
        try:
            dt = datetime.strptime(value, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_CN)
            return dt
        except ValueError:
            continue
    return None
```

`_position_age` 函数体头部（原 fmts 循环，到 `if dt is None` 为止）替换为：

```python
    dt = _parse_position_dt(opened_at)
    if dt is None:
        return "?"
```

3b. 在 `check_stop_loss` 之前添加：

```python
def _check_soft_stops(exchange: Exchange, state_mgr: StateManager, now: datetime = None):
    """软止损：每个整点后的第一个风控 tick，用最近一根已收盘 1H 的收盘价确认。
    收盘价越过软止损线 → 市价平仓。仅 atr_dual 模式、仅带 soft_stop_pct 的仓位。
    本小时内开的仓跳过（第一次确认等下个整点 → 至少扛过第一根 K 线）。"""
    if config.STOP_MODE != "atr_dual":
        return
    now = now or datetime.now(TZ_CN)
    hour_key = now.strftime("%Y-%m-%d %H")
    if state_mgr.last_soft_check_hour == hour_key:
        return
    state_mgr.set_last_soft_check_hour(hour_key)

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    for pos in list(state_mgr.state.get("positions", [])):
        soft_pct = pos.get("soft_stop_pct")
        if not soft_pct:
            continue  # 存量仓位（旧逻辑）
        opened = _parse_position_dt(pos.get("opened_at"))
        if opened is None or opened >= hour_start:
            continue
        try:
            kl = exchange.get_klines(pos["symbol"], "1h", 2)
            bar_close = float(kl[-2][4])  # 最近一根已收盘 1H 的收盘价
        except Exception as e:
            logger.warning("[软止损] %s 拉K线失败,本小时跳过: %s", pos["symbol"], e)
            continue
        if check_fixed_sl(pos["side"], pos["entry_price"], bar_close, soft_pct):
            logger.info("[软止损] %s %s | 1H收盘 %.4f 越过软止损线 (入场 %.4f, %.2f%%) | 平仓",
                        pos["symbol"], pos["side"], bar_close,
                        pos["entry_price"], soft_pct * 100)
            _close_position(exchange, state_mgr, pos, bar_close, "软止损(1H收盘)")
```

3c. `check_stop_loss` 中 `_sync_positions_with_exchange(exchange, state_mgr)` 之后插入一行：

```python
    _check_soft_stops(exchange, state_mgr)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v -k soft_stop`
Expected: 7 个测试 PASS

- [ ] **Step 5: 全量回归**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add risk.py tests/test_risk.py
git commit -m "feat(risk): 软止损 1H 收盘确认 (每整点一次, 至少扛过第一根K线)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: backtesting/engine.py — STOP_MODE 分支

**Files:**
- Modify: `backtesting/engine.py` (`BacktestPosition`、`__init__`、`_check_signal`、新方法 `_entry_sizing`、`_check_stops_minute`、`_check_stops_hour`、`_close_position`、`_calc_equity`、import)
- Test: `tests/test_backtest_engine.py` (追加)

- [ ] **Step 1: 写失败测试**

在 `tests/test_backtest_engine.py` 末尾追加：

```python
# ---------- 双层 ATR 止损 (STOP_MODE=atr_dual) ----------

import pandas as pd
import pytest
from backtesting.engine import BacktestEngine, BacktestPosition

HOUR_MS = 3600_000


def _mk_engine(stop_mode):
    return BacktestEngine(initial_capital=10000.0, position_size=400.0,
                          leverage=5, stop_mode=stop_mode)


def _mk_hourly_df(rows):
    """rows: list of (open_time, open, high, low, close)"""
    return pd.DataFrame(
        [{"open_time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}
         for t, o, h, l, c in rows]
    )


def _flat_df(n, high, low, close):
    return _mk_hourly_df([(i * HOUR_MS, close, high, low, close) for i in range(n)])


def test_entry_sizing_atr_dual():
    eng = _mk_engine("atr_dual")
    # TR 恒为 1.6 → ATR=1.6 → 软 = 1.5×1.6/100 = 2.4%，硬 = 4.8%
    df = _flat_df(30, high=100.8, low=99.2, close=100.0)
    soft, hard, notional, margin, atr = eng._entry_sizing(df, 100.0)
    assert soft == pytest.approx(0.024)
    assert hard == pytest.approx(0.048)
    assert notional == pytest.approx(40.0 / 0.024)
    assert margin == pytest.approx(40.0 / 0.024 / 5)


def test_entry_sizing_fixed_matches_legacy():
    eng = _mk_engine("fixed")
    df = _flat_df(30, high=100.8, low=99.2, close=100.0)
    soft, hard, notional, margin, atr = eng._entry_sizing(df, 100.0)
    assert soft == 0.0
    assert hard == eng.fixed_stop_loss_pct
    assert notional == pytest.approx(400.0 * 5)
    assert margin == pytest.approx(400.0)


def _soft_pos(opened_ms=0):
    return BacktestPosition(
        symbol="X", side="LONG", entry_price=100.0, quantity=10.0,
        opened_at="2026-01-01 08:00:00", opened_ms=opened_ms,
        soft_stop_pct=0.03, hard_stop_pct=0.06, notional=1000.0, margin=200.0,
    )


def test_soft_stop_fires_on_entry_bar_close():
    # 入场 bar (open_time=0) 收盘 96.5 < 软止损线 97 → 在 ts=HOUR_MS 检查刚收盘的 bar 0 → soft_sl
    eng = _mk_engine("atr_dual")
    eng.positions.append(_soft_pos(opened_ms=0))
    df = _mk_hourly_df([(0, 100.0, 101.0, 95.0, 96.5),
                        (HOUR_MS, 96.0, 97.0, 95.5, 96.8)])
    eng._check_stops_hour(HOUR_MS, {"X": (df, None)})
    assert len(eng.trades) == 1
    assert eng.trades[0].exit_reason == "soft_sl"


def test_soft_stop_survives_intrabar_dip():
    # bar 0 盘中 low=95（旧逻辑会打掉），收盘 98 > 97 → 不触发
    eng = _mk_engine("atr_dual")
    eng.positions.append(_soft_pos(opened_ms=0))
    df = _mk_hourly_df([(0, 100.0, 101.0, 95.0, 98.0),
                        (HOUR_MS, 98.0, 99.0, 97.5, 98.5)])
    eng._check_stops_hour(HOUR_MS, {"X": (df, None)})
    assert eng.trades == []
    assert len(eng.positions) == 1


def test_hard_stop_fires_intrabar_on_minute_grid():
    eng = _mk_engine("atr_dual")
    pos = _soft_pos(opened_ms=0)
    eng.positions.append(pos)
    minute_df = pd.DataFrame([
        {"open_time": 60_000, "open": 100.0, "high": 100.0, "low": 93.0,
         "close": 93.5, "volume": 1.0},
    ])
    eng._minute_data = {"X": minute_df}
    eng._check_stops_minute(60_000)
    # 93.5 < 硬止损线 94 → 盘中触发
    assert len(eng.trades) == 1
    assert eng.trades[0].exit_reason == "hard_sl"


def test_close_position_uses_pos_margin_and_notional():
    eng = _mk_engine("atr_dual")
    pos = _soft_pos(opened_ms=0)
    eng.positions.append(pos)
    balance_before = eng.balance
    eng._close_position(pos, 99.0, "soft_sl", HOUR_MS)
    # pnl = -1% × notional(1000) = -10；exit fee = 1000×0.0004 = 0.4
    # balance += margin(200) + (-10.4)
    assert eng.balance == pytest.approx(balance_before + 200.0 - 10.4)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_backtest_engine.py -v -k "entry_sizing or soft_stop or hard_stop_fires or pos_margin"`
Expected: FAIL（`__init__() got an unexpected keyword argument 'stop_mode'`）

- [ ] **Step 3: 实现引擎改造**

3a. import 行（`from risk import check_fixed_sl, check_trailing_tp`）扩展为：

```python
from risk import check_fixed_sl, check_trailing_tp, calculate_atr, compute_stop_distances
```

3b. `BacktestPosition` 增加字段（`trailing_activated: bool = False` 之后）：

```python
    soft_stop_pct: float = 0.0
    hard_stop_pct: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
```

3c. `__init__` 参数表 `trend_timeframe_hours: int = 24,` 之后加 `stop_mode: str = None,`；函数体加：

```python
        self.stop_mode = stop_mode or config.STOP_MODE
        self.atr_period = config.ATR_PERIOD
```

3d. 新方法 `_entry_sizing`（放在 `_check_signal` 之前）：

```python
    def _entry_sizing(self, h_closed: pd.DataFrame, exec_price: float):
        """按 stop_mode 计算 (软止损%, 硬止损%, 名义, 保证金, ATR)。
        atr_dual 的距离/风险参数读 config（与实盘同源）；杠杆用引擎参数。"""
        if self.stop_mode == "atr_dual":
            atr = calculate_atr(
                h_closed["high"].astype(float).tolist(),
                h_closed["low"].astype(float).tolist(),
                h_closed["close"].astype(float).tolist(),
                self.atr_period,
            )
            soft, hard = compute_stop_distances(atr, exec_price)
            notional = min(config.RISK_PER_TRADE_USD / soft, config.MAX_NOTIONAL_USD)
            return soft, hard, notional, notional / self.leverage, atr
        notional = self.position_size * self.leverage
        return 0.0, self.fixed_stop_loss_pct, notional, self.position_size, 0.0
```

3e. `_check_signal` 末段（原 `notional = self.position_size * self.leverage` 到 `self.balance -= fee`）替换为：

```python
        soft_pct, hard_pct, notional, margin, _atr = self._entry_sizing(h_closed, exec_price)
        quantity = notional / exec_price
        fee = calculate_fee(notional)

        ts_str = datetime.fromtimestamp(current_ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        pos = BacktestPosition(
            symbol=symbol,
            side=trend,
            entry_price=exec_price,
            quantity=quantity,
            opened_at=ts_str,
            opened_ms=current_ts,
            soft_stop_pct=soft_pct,
            hard_stop_pct=hard_pct,
            notional=notional,
            margin=margin,
        )
        self.positions.append(pos)
        self.balance -= margin
        self.balance -= fee
```

3f. `_check_stops_minute` 中 fixed SL 判断改为：

```python
            hard_pct = pos.hard_stop_pct or self.fixed_stop_loss_pct
            if check_fixed_sl(pos.side, pos.entry_price, current_price, hard_pct):
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                reason = "hard_sl" if self.stop_mode == "atr_dual" else "fixed_sl"
                to_close.append((pos, exit_price, reason, minute_ts))
                continue
```

3g. `_check_stops_hour` 整体替换为（软止损用**刚收盘的上一根 bar**，与实盘"整点检查刚收盘 K 线"对齐；硬止损/移动止盈的小时级回退路径维持原语义）：

```python
    def _check_stops_hour(self, current_ts: int, data: dict):
        """Hourly checks: soft stop (atr_dual, close-confirmed on the just-closed
        bar, ALL positions) + hard/trailing close-only fallback for symbols
        without minute data."""
        HOUR_MS = 3600_000
        to_close = []
        for pos in self.positions:
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]

            # --- 软止损：检查刚收盘的那根 1H（入场 bar 收盘即第一次检查）---
            if (self.stop_mode == "atr_dual" and pos.soft_stop_pct > 0
                    and pos.opened_ms <= current_ts - HOUR_MS):
                closed_bar = hourly_df[hourly_df["open_time"] == current_ts - HOUR_MS]
                if not closed_bar.empty:
                    bar_close = float(closed_bar.iloc[0]["close"])
                    if check_fixed_sl(pos.side, pos.entry_price, bar_close, pos.soft_stop_pct):
                        exit_price = apply_slippage(bar_close, pos.side, is_entry=False)
                        to_close.append((pos, exit_price, "soft_sl", current_ts))
                        continue

            if pos.symbol in self._minute_data:
                continue  # 硬止损/移动止盈在分钟网格处理
            bar = hourly_df[hourly_df["open_time"] == current_ts]
            if bar.empty:
                continue
            current_price = float(bar.iloc[0]["close"])
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

            extreme_price = pos.highest_price if pos.side == "LONG" else pos.lowest_price

            hard_pct = pos.hard_stop_pct or self.fixed_stop_loss_pct
            if check_fixed_sl(pos.side, pos.entry_price, current_price, hard_pct):
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                reason = "hard_sl" if self.stop_mode == "atr_dual" else "fixed_sl"
                to_close.append((pos, exit_price, reason, current_ts))
                continue

            trail_triggered, newly_activated = check_trailing_tp(
                side=pos.side,
                entry_price=pos.entry_price,
                extreme_price=extreme_price,
                current_price=current_price,
                trailing_activated=pos.trailing_activated,
                activation_pct=self.trailing_activation_pct,
                drawdown_pct=self.trailing_drawdown_pct,
            )
            if newly_activated:
                pos.trailing_activated = True
            if trail_triggered:
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                to_close.append((pos, exit_price, "trailing_tp", current_ts))

        for pos, exit_price, reason, ts in to_close:
            self._close_position(pos, exit_price, reason, ts)
```

3h. `_close_position` 整体替换为：

```python
    def _close_position(self, pos: BacktestPosition, exit_price: float, reason: str, ts: int):
        """Close a position and record the trade."""
        notional = pos.notional or self.position_size * self.leverage
        margin = pos.margin or self.position_size

        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) / pos.entry_price * notional
        else:
            pnl = (pos.entry_price - exit_price) / pos.entry_price * notional

        exit_fee = calculate_fee(notional)
        net_pnl = pnl - exit_fee

        ts_str = datetime.fromtimestamp(ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        trade = BacktestTrade(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            pnl=net_pnl,
            fee=calculate_fee(notional) * 2,
            opened_at=pos.opened_at,
            closed_at=ts_str,
            exit_reason=reason,
        )
        self.trades.append(trade)
        self.positions.remove(pos)
        self.balance += margin + net_pnl
```

3i. `_calc_equity` 改为按仓位字段：

```python
    def _calc_equity(self, current_ts: int, data: dict) -> float:
        """Calculate total equity = balance + margins + unrealized PnL."""
        equity = self.balance
        for pos in self.positions:
            equity += pos.margin or self.position_size
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]
            bar = hourly_df[hourly_df["open_time"] == current_ts]
            if bar.empty:
                continue
            price = float(bar.iloc[0]["close"])
            notional = pos.notional or self.position_size * self.leverage

            if pos.side == "LONG":
                unrealized = (price - pos.entry_price) / pos.entry_price * notional
            else:
                unrealized = (pos.entry_price - price) / pos.entry_price * notional
            equity += unrealized
        return equity
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_backtest_engine.py -v`
Expected: 新增 6 个测试 PASS，原有引擎测试 PASS（fixed 模式行为不变；若原有测试构造 `BacktestEngine` 未传 `stop_mode` 且依赖旧默认行为，需在该测试的 setup 传 `stop_mode="fixed"`——逐个检查失败原因再改测试，不改引擎语义）

- [ ] **Step 5: 全量回归**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add backtesting/engine.py tests/test_backtest_engine.py
git commit -m "feat(backtest): 引擎 STOP_MODE 分支 — 软止损收盘确认 + 硬止损盘中 + 等风险缩仓

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: backtest.py — --stop-mode CLI 参数

**Files:**
- Modify: `backtesting/backtest.py`

- [ ] **Step 1: 加参数与接线**

`main()` 中 `--max-positions` 参数之后添加：

```python
    parser.add_argument("--stop-mode", type=str, default=config.STOP_MODE,
                        choices=["fixed", "atr_dual"],
                        help="止损模式: fixed=旧2%%固定止损 | atr_dual=双层ATR止损")
```

打印块 `print(f"  Max Positions: {args.max_positions}")` 之后加：

```python
    print(f"  Stop Mode:     {args.stop_mode}")
```

`BacktestEngine(...)` 构造加一行参数：

```python
        stop_mode=args.stop_mode,
```

- [ ] **Step 2: 冒烟验证 CLI**

Run: `.venv/bin/python -m backtesting.backtest --symbols BTCUSDT --stop-mode fixed 2>&1 | head -20`
Expected: 打印 `Stop Mode:     fixed` 且正常跑完（或因数据缺失提示运行 download_data.py——只要参数被接受即可）

- [ ] **Step 3: Commit**

```bash
git add backtesting/backtest.py
git commit -m "feat(backtest): --stop-mode CLI 参数

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: 回测验证 — fixed vs atr_dual 对比（切实盘的门槛）

**Files:**
- Modify: `data/*_1h.csv`, `data/*_1d.csv` (重新下载补到 6 月)
- Modify: `docs/superpowers/specs/2026-06-11-atr-dual-stop-design.md` (追加结果)

- [ ] **Step 1: 补数据到当前日期**

`download_data.py` 的 resume 是"跳过已存在文件"，不会延长旧文件——先移走 1h/1d 旧档再下载（**保留 `_1m.csv`，体积大且本次对比不用分钟数据**）：

```bash
mkdir -p data/_archive_20260611
mv data/*_1h.csv data/*_1d.csv data/_archive_20260611/
.venv/bin/python backtesting/download_data.py
```

Expected: 30 个默认 symbol 的 `_1h.csv`/`_1d.csv` 重新生成，K 线覆盖到 2026-06-11 附近。用 `tail -1 data/BTCUSDT_1h.csv` 确认最后一行日期在 6 月。

- [ ] **Step 2: 双模式回测**

```bash
.venv/bin/python -m backtesting.backtest --stop-mode fixed    > results/bt_fixed.txt 2>&1
cp results/trades.csv results/bt_fixed_trades.csv
.venv/bin/python -m backtesting.backtest --stop-mode atr_dual > results/bt_atr_dual.txt 2>&1
cp results/trades.csv results/bt_atr_dual_trades.csv
```

Expected: 两份报告正常生成。两次回测除 stop-mode 外参数完全相同（注意 `report.py` 会覆盖 `results/trades.csv`，故每跑一次立即 cp 快照）。

- [ ] **Step 3: 对比关键指标**

对比 `bt_fixed.txt` vs `bt_atr_dual.txt` 的：净 PnL、总手续费、交易笔数、胜率、最大回撤、Sharpe。另用 trades 快照统计"1 根 K 线内死亡"占比（`opened_at == closed_at` 的小时数差 ≤1）：

```bash
.venv/bin/python - << 'EOF'
import pandas as pd
for tag in ["fixed", "atr_dual"]:
    df = pd.read_csv(f"results/bt_{tag}_trades.csv")
    o = pd.to_datetime(df["opened_at"]); c = pd.to_datetime(df["closed_at"])
    hold_h = (c - o).dt.total_seconds() / 3600
    fast = (hold_h <= 1).mean() * 100
    print(f"{tag:>8}: {len(df)} trades | net {df['pnl'].sum():+.2f} | fee {df['fee'].sum():.2f} "
          f"| <=1h死亡 {fast:.0f}% | by reason: {df['exit_reason'].value_counts().to_dict()}")
EOF
```

- [ ] **Step 4: 记录结果到 spec 并判定**

把 Step 3 的数字追加到 `docs/superpowers/specs/2026-06-11-atr-dual-stop-design.md` 新章节 `## 回测验证结果 (2026-06-11)`，并写明判定。

**通过标准（spec）**：atr_dual 净 PnL 显著优于 fixed，且回合数明显下降。
**若不通过**：不切实盘。把 `config.STOP_MODE` 默认值改回 `"fixed"`，结果与原因记入 spec，停在这里向用户汇报（参数调优是新一轮决策，不在本计划内）。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-06-11-atr-dual-stop-design.md
git commit -m "docs(spec): 回测验证结果 — fixed vs atr_dual

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

（`results/` 与 `data/` 不入库，遵循现状 gitignore。）

---

### Task 11: CLAUDE.md 更新 + 收尾回归

**Files:**
- Modify: `CLAUDE.md` (Architecture 一节的 Risk check 描述)

- [ ] **Step 1: 修正过时的风控描述**

把 CLAUDE.md 中这段：

```
- **Risk check** (`risk.py:check_stop_loss`) — runs every 2 minutes. Exit via ATR-based dynamic trailing stop: stop line = extreme price ± ATR × multiplier (only tightens, never loosens), with a hard 6% max drawdown cap.
```

替换为：

```
- **Risk check** (`risk.py:check_stop_loss`) — runs every minute. Dual-layer stops (`STOP_MODE="atr_dual"`): soft stop = ATR-adaptive distance (max(2%, 1.5×ATR14(1H)/price)), confirmed only on 1H close (local check, once per hour); hard stop = min(2×soft, 6%) as a resting exchange STOP_MARKET order (gap/offline protection). Trailing TP activates at +3.5% profit (exchange TRAILING_STOP_MARKET, 1.5% callback). Position notional = $40 risk / soft-stop pct, capped at $2,000 (equal-risk sizing). `STOP_MODE="fixed"` rolls back to the legacy flat 2% exchange stop.
```

- [ ] **Step 2: 全量回归**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 风控描述更新为双层 ATR 止损

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 执行后注意事项（不在本计划自动执行）

1. **任何真实下单的验证一律走模拟盘**（用户要求）：Task 10 回测通过后，先以 `TRADING_MODE=paper`（testnet）运行观察，验证软止损触发时机、硬止损挂单位置、缩仓保证金数额符合预期；确认无误后由用户决定是否切实盘。部署时存量持仓按旧逻辑自然换血（risk.py 已做回退），无需手动迁移。
2. 实盘观察期重点看通知里的 `软止损(1H收盘)` 平仓事件与缩仓后的保证金数字是否符合预期。
3. 5/6 月归因脚本 `scripts/analyze_may_june.py` 可在实盘运行 2 周后复跑，对比 <1h 死亡占比是否下降。
