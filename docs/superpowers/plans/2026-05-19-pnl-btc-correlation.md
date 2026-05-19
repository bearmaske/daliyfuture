# 实盘 P&L vs BTC 指标相关性分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 分析 2026-05-01 ~ 05-18 实盘账户每小时净 P&L 与 BTC 多组市场指标的相关性，输出 markdown 报告 + 图表，定位与「亏损时段」最相关的因子。

**Architecture:** 单文件分析脚本 `scripts/analyze_pnl_vs_btc.py`，模块化函数（数据加载 / 指标计算 / 相关性 / 窗口对比 / 报告 / 绘图）。BTC 数据通过复用 `backtesting/download_data.py:fetch_klines_batched` 拉取增量。

**Tech Stack:** Python 3, pandas, numpy, scipy.stats, matplotlib, python-binance（已有依赖，不引入新包）。

**Reference Spec:** `docs/superpowers/specs/2026-05-19-pnl-btc-correlation-design.md`

## File Structure

| 文件 | 责任 |
|---|---|
| `scripts/analyze_pnl_vs_btc.py` | 主分析脚本：CLI、加载、指标、统计、产出 |
| `tests/test_pnl_btc_analysis.py` | 单元测试：聚合 / 指标 / 相关性 |
| `data/BTCUSDT_1h.csv` | 现有文件，被增量追加 |
| `results/pnl_btc_correlation_report.md` | 输出报告 |
| `results/pnl_btc_chart.png` | 输出图表 |

---

### Task 1: 下载 BTC 1H 增量数据（含 raw smoke test）

**Files:**
- Use: `backtesting/download_data.py:fetch_klines_batched`
- Modify: `data/BTCUSDT_1h.csv`（追加 2026-04-20 之后的数据，去重）

- [ ] **Step 1: 先打印一次原始 API 响应（per CLAUDE.md 规则）**

执行一次性 smoke：
```bash
cd /Users/danny/Desktop/code/daliyfuture && source .venv/bin/activate && python -c "
from binance.client import Client
c = Client()
r = c.futures_klines(symbol='BTCUSDT', interval='1h', limit=3)
import json
print(json.dumps(r, indent=2, default=str))
"
```

Expected: 看到 3 条 K 线数组，每条 12 个字段（open_time, open, high, low, close, volume, close_time, …）

- [ ] **Step 2: 写下载脚本（一次性，直接放在分析脚本里也行；这里独立写）**

新增 `scripts/fetch_btc_recent.py`:
```python
"""一次性增量拉取 BTCUSDT 1H K 线到 data/BTCUSDT_1h.csv。"""
import os, sys
import pandas as pd
from binance.client import Client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backtesting.download_data import fetch_klines_batched, COLUMNS

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "BTCUSDT_1h.csv")

def main():
    client = Client()
    # 读已有数据找到最新 open_time
    existing = pd.read_csv(DATA_FILE)
    last_ms = int(existing["open_time"].max())
    start_ms = last_ms + 1
    # 抓到 "现在" — Binance 会自动到最新已收盘
    import time
    end_ms = int(time.time() * 1000)
    print(f"[fetch] from {pd.to_datetime(start_ms, unit='ms')} to {pd.to_datetime(end_ms, unit='ms')}")
    klines = fetch_klines_batched(client, "BTCUSDT", "1h", start_ms, end_ms)
    if not klines:
        print("[fetch] no new klines, exiting")
        return
    new_df = pd.DataFrame(
        [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in klines],
        columns=COLUMNS,
    )
    combined = pd.concat([existing, new_df]).drop_duplicates(subset=["open_time"]).sort_values("open_time")
    combined.to_csv(DATA_FILE, index=False)
    print(f"[fetch] wrote {len(combined)} rows total ({len(new_df)} new)")

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 运行下载**

```bash
cd /Users/danny/Desktop/code/daliyfuture && source .venv/bin/activate && python scripts/fetch_btc_recent.py
```

Expected: 看到 `from 2026-04-20 ... to 2026-05-19 ...`，并打印写入总行数（应增加 ~700 行）。

- [ ] **Step 4: 校验数据覆盖**

```bash
python -c "
import pandas as pd
df = pd.read_csv('data/BTCUSDT_1h.csv')
df['t'] = pd.to_datetime(df['open_time'], unit='ms')
print('min:', df['t'].min(), 'max:', df['t'].max(), 'rows:', len(df))
"
```

Expected: max 至少到 2026-05-18，rows > 9800。

- [ ] **Step 5: Commit**

```bash
git add scripts/fetch_btc_recent.py data/BTCUSDT_1h.csv
git commit -m "data: 增量拉取 BTCUSDT 1H 至 2026-05-19"
```

---

### Task 2: P&L 小时聚合函数 + 测试

**Files:**
- Create: `scripts/analyze_pnl_vs_btc.py`（先放聚合函数）
- Create: `tests/test_pnl_btc_analysis.py`

- [ ] **Step 1: 写测试**

`tests/test_pnl_btc_analysis.py`:
```python
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
    # 21:00 桶应为 -0.5 + 10.0 = 9.5
    assert out.loc[pd.Timestamp("2026-05-06 21:00:00")] == pytest.approx(9.5)
    # 22:00 桶应为 -0.2 + -3.0 = -3.2
    assert out.loc[pd.Timestamp("2026-05-06 22:00:00")] == pytest.approx(-3.2)


def test_aggregate_hourly_pnl_fills_empty_hours_with_zero():
    raw = pd.DataFrame([
        {"time": "2026-05-06 10:00:00", "symbol": "X", "incomeType": "REALIZED_PNL", "income": 1.0},
        {"time": "2026-05-06 13:00:00", "symbol": "X", "incomeType": "REALIZED_PNL", "income": 2.0},
    ])
    out = aggregate_hourly_pnl(raw)
    assert out.loc[pd.Timestamp("2026-05-06 11:00:00")] == 0.0
    assert out.loc[pd.Timestamp("2026-05-06 12:00:00")] == 0.0
```

- [ ] **Step 2: 跑测试看 ImportError / FAIL**

```bash
cd /Users/danny/Desktop/code/daliyfuture && source .venv/bin/activate && python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: FAIL — `ImportError: cannot import name 'aggregate_hourly_pnl'`

- [ ] **Step 3: 实现函数**

`scripts/analyze_pnl_vs_btc.py`（新建文件）:
```python
"""分析实盘账户每小时净 P&L 与 BTC 指标的相关性。"""
from __future__ import annotations

import pandas as pd


def aggregate_hourly_pnl(income_df: pd.DataFrame) -> pd.Series:
    """把 live_income 逐笔记录聚合成小时净 P&L 序列。

    - 合并所有 incomeType（REALIZED_PNL + COMMISSION + FUNDING_FEE）
    - 按 1H 桶聚合（floor 到整点）
    - 空小时填 0，输出连续的小时索引
    """
    df = income_df.copy()
    df["time"] = pd.to_datetime(df["time"])
    df["bucket"] = df["time"].dt.floor("1h")
    s = df.groupby("bucket")["income"].sum().sort_index()
    full_idx = pd.date_range(s.index.min(), s.index.max(), freq="1h")
    return s.reindex(full_idx, fill_value=0.0)
```

- [ ] **Step 4: 跑测试看 PASS**

```bash
python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_pnl_vs_btc.py tests/test_pnl_btc_analysis.py
git commit -m "feat(analysis): 小时净 P&L 聚合函数 + 测试"
```

---

### Task 3: BTC 指标矩阵 + 测试

**Files:**
- Modify: `scripts/analyze_pnl_vs_btc.py`
- Modify: `tests/test_pnl_btc_analysis.py`

- [ ] **Step 1: 写测试**

追加到 `tests/test_pnl_btc_analysis.py`:
```python
import numpy as np
from scripts.analyze_pnl_vs_btc import build_btc_indicators


def _make_klines(n: int = 100, start="2026-04-25 00:00:00") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1h")
    # 正弦波模拟，便于检验 ROC 与 BB 不全 NaN
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
    # warmup 至少 50（vol z-score 窗口）— 后续应无 NaN
    tail = out.iloc[60:]
    assert not tail.isna().any().any()
```

- [ ] **Step 2: 跑测试看 FAIL**

```bash
python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: 后三个 FAIL — `cannot import name 'build_btc_indicators'`。

- [ ] **Step 3: 实现 build_btc_indicators**

追加到 `scripts/analyze_pnl_vs_btc.py`:
```python
import numpy as np


def build_btc_indicators(klines: pd.DataFrame) -> pd.DataFrame:
    """从 1H K 线构造指标矩阵，索引为整点时间戳。"""
    df = klines.copy().sort_values("open_time").reset_index(drop=True)
    df.index = pd.to_datetime(df["open_time"], unit="ms")

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # --- 波动率 ---
    log_ret = np.log(close / close.shift(1))
    out = pd.DataFrame(index=df.index)
    out["ret_std_24h"] = log_ret.rolling(24).std()

    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr_14"] = tr.rolling(14).mean() / close

    # --- 成交量 ---
    out["vol_ratio_20"] = volume / volume.rolling(20).mean()
    log_vol = np.log(volume.replace(0, np.nan))
    out["vol_zscore_50"] = (log_vol - log_vol.rolling(50).mean()) / log_vol.rolling(50).std()

    # --- 趋势 ---
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    out["sma20_slope"] = (sma20 - sma20.shift(5)) / sma20.shift(5)
    out["sma20_50_dist"] = (sma20 - sma50) / sma50

    # --- 动量 ---
    for n in (6, 12, 24):
        out[f"roc_{n}"] = close.pct_change(n)

    # --- 布林 ---
    bb_std = close.rolling(20).std()
    upper = sma20 + 2 * bb_std
    lower = sma20 - 2 * bb_std
    out["bb_width"] = (upper - lower) / sma20
    out["bb_pctb"] = (close - lower) / (upper - lower)

    # --- 价格行为 ---
    out["hl_range"] = (high - low) / close

    return out
```

- [ ] **Step 4: 跑测试看 PASS**

```bash
python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: 5 passed。

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_pnl_vs_btc.py tests/test_pnl_btc_analysis.py
git commit -m "feat(analysis): BTC 指标矩阵构造 + 测试"
```

---

### Task 4: 相关性 + 窗口对比统计 + 测试

**Files:**
- Modify: `scripts/analyze_pnl_vs_btc.py`
- Modify: `tests/test_pnl_btc_analysis.py`

- [ ] **Step 1: 写测试**

追加到 `tests/test_pnl_btc_analysis.py`:
```python
from scripts.analyze_pnl_vs_btc import compute_correlations, compare_win_loss_windows


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
```

- [ ] **Step 2: 跑测试看 FAIL**

```bash
python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: 后两个 FAIL — 缺函数。

- [ ] **Step 3: 实现两个函数**

追加到 `scripts/analyze_pnl_vs_btc.py`:
```python
from scipy import stats


def compute_correlations(pnl: pd.Series, features: pd.DataFrame) -> pd.DataFrame:
    """对每个特征 vs pnl，返回 Pearson / Spearman 相关系数与 p 值。"""
    common = features.index.intersection(pnl.index)
    pnl_a = pnl.reindex(common)
    rows = []
    for col in features.columns:
        x = features[col].reindex(common)
        mask = x.notna() & pnl_a.notna()
        if mask.sum() < 10:
            rows.append({"feature": col, "pearson_r": np.nan, "pearson_p": np.nan,
                         "spearman_r": np.nan, "spearman_p": np.nan, "n": int(mask.sum())})
            continue
        pr, pp = stats.pearsonr(x[mask], pnl_a[mask])
        sr, sp = stats.spearmanr(x[mask], pnl_a[mask])
        rows.append({"feature": col, "pearson_r": pr, "pearson_p": pp,
                     "spearman_r": sr, "spearman_p": sp, "n": int(mask.sum())})
    return pd.DataFrame(rows).set_index("feature")


def compare_win_loss_windows(pnl: pd.Series, features: pd.DataFrame) -> pd.DataFrame:
    """按 pnl>0 / pnl<0 分组，对每个特征比较分布（mean / median / Mann–Whitney U）。"""
    common = features.index.intersection(pnl.index)
    pnl_a = pnl.reindex(common)
    win_mask = pnl_a > 0
    loss_mask = pnl_a < 0
    rows = []
    for col in features.columns:
        x = features[col].reindex(common)
        win_vals = x[win_mask].dropna()
        loss_vals = x[loss_mask].dropna()
        if len(win_vals) < 5 or len(loss_vals) < 5:
            mwu_p = np.nan
        else:
            mwu_p = stats.mannwhitneyu(loss_vals, win_vals, alternative="two-sided").pvalue
        rows.append({
            "feature": col,
            "loss_mean": loss_vals.mean(),
            "win_mean": win_vals.mean(),
            "loss_median": loss_vals.median(),
            "win_median": win_vals.median(),
            "mwu_p": mwu_p,
            "n_loss": len(loss_vals),
            "n_win": len(win_vals),
        })
    return pd.DataFrame(rows).set_index("feature")
```

- [ ] **Step 4: 跑测试看 PASS**

```bash
python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: 7 passed total。

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_pnl_vs_btc.py tests/test_pnl_btc_analysis.py
git commit -m "feat(analysis): 相关性 + 亏损窗口对比统计 + 测试"
```

---

### Task 5: Markdown 报告 + 图表生成

**Files:**
- Modify: `scripts/analyze_pnl_vs_btc.py`

- [ ] **Step 1: 实现报告函数**

追加到 `scripts/analyze_pnl_vs_btc.py`:
```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def write_markdown_report(
    corr: pd.DataFrame,
    cmp: pd.DataFrame,
    pnl: pd.Series,
    out_path: str,
) -> None:
    """生成 markdown 报告，包含相关性表、窗口对比表、Top 因子解读。"""
    # 排序 Top 因子：|spearman_r| * (1 - p)（缺值视为 0）
    score = corr["spearman_r"].abs() * (1 - corr["spearman_p"].fillna(1.0))
    top = score.sort_values(ascending=False).head(5)

    lines = []
    lines.append("# 实盘 P&L vs BTC 指标相关性报告")
    lines.append("")
    lines.append(f"- 时间范围：{pnl.index.min()} ~ {pnl.index.max()}")
    lines.append(f"- 小时样本数：{len(pnl)}（盈利 {(pnl>0).sum()} / 亏损 {(pnl<0).sum()} / 平 {(pnl==0).sum()}）")
    lines.append(f"- 总净 P&L：{pnl.sum():.2f} USDT")
    lines.append("")
    lines.append("## 1. 相关性（Pearson + Spearman）")
    lines.append("")
    lines.append(corr.round(4).to_markdown())
    lines.append("")
    lines.append("## 2. 盈利 vs 亏损小时的指标分布对比")
    lines.append("")
    lines.append(cmp.round(4).to_markdown())
    lines.append("")
    lines.append("## 3. Top 5 影响因子（按 |Spearman| × (1-p) 排序）")
    lines.append("")
    for feat, sc in top.items():
        row = corr.loc[feat]
        cmp_row = cmp.loc[feat]
        direction = "亏损时偏高" if cmp_row["loss_mean"] > cmp_row["win_mean"] else "亏损时偏低"
        lines.append(f"- **{feat}** (score={sc:.3f})")
        lines.append(f"    - Spearman r={row['spearman_r']:.3f} (p={row['spearman_p']:.3g})")
        lines.append(f"    - {direction}：loss_mean={cmp_row['loss_mean']:.4f} vs win_mean={cmp_row['win_mean']:.4f}, MWU p={cmp_row['mwu_p']:.3g}")
        lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))


def plot_overview(pnl: pd.Series, features: pd.DataFrame, out_path: str) -> None:
    """日累计 P&L 与 BTC 波动率 / BB 带宽双轴叠加图。"""
    daily_pnl = pnl.resample("1D").sum().cumsum()
    daily_vol = features["ret_std_24h"].resample("1D").mean()
    daily_bbw = features["bb_width"].resample("1D").mean()

    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.plot(daily_pnl.index, daily_pnl.values, color="tab:blue", label="cum P&L (USDT)", linewidth=2)
    ax1.set_ylabel("Cumulative P&L (USDT)", color="tab:blue")
    ax1.axhline(0, color="grey", linewidth=0.5)
    ax1.tick_params(axis="y", labelcolor="tab:blue")

    ax2 = ax1.twinx()
    ax2.plot(daily_vol.index, daily_vol.values, color="tab:orange", label="BTC 24H ret std", alpha=0.7)
    ax2.plot(daily_bbw.index, daily_bbw.values, color="tab:green", label="BTC BB width", alpha=0.7)
    ax2.set_ylabel("BTC volatility / BB width", color="tab:gray")

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax1.set_title("Daily cumulative P&L vs BTC volatility regime")
    fig.legend(loc="upper left", bbox_to_anchor=(0.1, 0.95))
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
```

- [ ] **Step 2: Commit（无新测试 — 这些是 I/O，靠端到端 smoke 验证）**

```bash
git add scripts/analyze_pnl_vs_btc.py
git commit -m "feat(analysis): markdown 报告 + matplotlib 双轴图"
```

---

### Task 6: CLI 入口 + 端到端跑通

**Files:**
- Modify: `scripts/analyze_pnl_vs_btc.py`

- [ ] **Step 1: 加 CLI main**

追加到 `scripts/analyze_pnl_vs_btc.py`:
```python
import argparse
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--income", default="results/live_income_20260501_20260518.csv")
    parser.add_argument("--btc", default="data/BTCUSDT_1h.csv")
    parser.add_argument("--report", default="results/pnl_btc_correlation_report.md")
    parser.add_argument("--chart", default="results/pnl_btc_chart.png")
    args = parser.parse_args()

    income = pd.read_csv(args.income)
    klines = pd.read_csv(args.btc)

    pnl = aggregate_hourly_pnl(income)
    features = build_btc_indicators(klines)

    # 只保留 pnl 时间范围内的特征
    features = features.reindex(pnl.index)

    corr = compute_correlations(pnl, features)
    cmp = compare_win_loss_windows(pnl, features)

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    write_markdown_report(corr, cmp, pnl, args.report)
    plot_overview(pnl, features, args.chart)

    print(f"[done] wrote {args.report} and {args.chart}")
    print("\nTop 5 by |Spearman|*(1-p):")
    score = corr["spearman_r"].abs() * (1 - corr["spearman_p"].fillna(1.0))
    print(score.sort_values(ascending=False).head(5).to_string())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 跑全套测试**

```bash
python -m pytest tests/test_pnl_btc_analysis.py -v
```

Expected: 7 passed。

- [ ] **Step 3: 端到端执行**

```bash
cd /Users/danny/Desktop/code/daliyfuture && source .venv/bin/activate && python scripts/analyze_pnl_vs_btc.py
```

Expected:
- 控制台打印 Top 5 因子
- `results/pnl_btc_correlation_report.md` 存在且 > 1KB
- `results/pnl_btc_chart.png` 存在且 > 30KB

校验：
```bash
ls -la results/pnl_btc_correlation_report.md results/pnl_btc_chart.png
```

- [ ] **Step 4: 人工读报告 → 把 Top 因子摘要回报给用户**

打开 `results/pnl_btc_correlation_report.md`，用一两句话点出："亏损时段 BTC 哪些指标显著偏高/偏低"。

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_pnl_vs_btc.py results/pnl_btc_correlation_report.md results/pnl_btc_chart.png
git commit -m "feat(analysis): 端到端跑通 + 首份报告 + 图表"
```

---

## Self-Review

**Spec coverage：**
- 数据来源（live_income + BTC 1H）→ Task 1, 2 ✓
- 时间粒度小时为主 → Task 2 (aggregate to 1h) ✓
- BTC 指标矩阵 12 个字段 → Task 3 ✓
- Pearson + Spearman 相关性 → Task 4 (compute_correlations) ✓
- 亏损 vs 盈利窗口对比 + Mann-Whitney → Task 4 (compare_win_loss_windows) ✓
- Top 5 影响因子排序 → Task 5 (write_markdown_report) ✓
- markdown 报告 + matplotlib 双轴图 → Task 5 ✓
- 交付物路径：scripts/analyze_pnl_vs_btc.py、results/pnl_btc_correlation_report.md、results/pnl_btc_chart.png ✓

**Placeholder scan：** 已检查，无 TBD / "appropriate" / 抽象描述。

**Type consistency：**
- `aggregate_hourly_pnl` 返回 `pd.Series` → Task 4 输入 `pnl: pd.Series` ✓
- `build_btc_indicators` 返回 `pd.DataFrame` 带 DatetimeIndex → Task 4 输入 `features: pd.DataFrame` ✓
- 列名 12 个在 Task 3 实现与 Task 3 测试 expected 集合一一匹配 ✓
- `corr` 列：`pearson_r/pearson_p/spearman_r/spearman_p/n` 在 Task 4 实现与 Task 5 引用一致 ✓
- `cmp` 列：`loss_mean/win_mean/loss_median/win_median/mwu_p/n_loss/n_win` 在 Task 4 实现与 Task 5 引用一致 ✓

**CLAUDE.md 合规：** API 集成前先打 raw response（Task 1 Step 1）✓
