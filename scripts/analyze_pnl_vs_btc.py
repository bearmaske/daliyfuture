"""分析实盘账户每小时净 P&L 与 BTC 指标的相关性。"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


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


def build_btc_indicators(klines: pd.DataFrame) -> pd.DataFrame:
    """从 1H K 线构造指标矩阵，索引为整点时间戳。"""
    df = klines.copy().sort_values("open_time").reset_index(drop=True)
    df.index = pd.to_datetime(df["open_time"], unit="ms")

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

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

    out["vol_ratio_20"] = volume / volume.rolling(20).mean()
    log_vol = np.log(volume.replace(0, np.nan))
    out["vol_zscore_50"] = (log_vol - log_vol.rolling(50).mean()) / log_vol.rolling(50).std()

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    out["sma20_slope"] = (sma20 - sma20.shift(5)) / sma20.shift(5)
    out["sma20_50_dist"] = (sma20 - sma50) / sma50

    for n in (6, 12, 24):
        out[f"roc_{n}"] = close.pct_change(n)

    bb_std = close.rolling(20).std()
    upper = sma20 + 2 * bb_std
    lower = sma20 - 2 * bb_std
    out["bb_width"] = (upper - lower) / sma20
    out["bb_pctb"] = (close - lower) / (upper - lower)

    out["hl_range"] = (high - low) / close

    return out


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


def write_markdown_report(
    corr: pd.DataFrame,
    cmp: pd.DataFrame,
    pnl: pd.Series,
    out_path: str,
) -> None:
    """生成 markdown 报告，包含相关性表、窗口对比表、Top 因子解读。"""
    score = corr["spearman_r"].abs() * (1 - corr["spearman_p"].fillna(1.0))
    top = score.dropna().sort_values(ascending=False).head(5)

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
    ax1.legend(loc="upper left")
    ax2.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
