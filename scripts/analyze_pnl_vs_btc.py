"""分析实盘账户每小时净 P&L 与 BTC 指标的相关性。"""
from __future__ import annotations

import numpy as np
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
