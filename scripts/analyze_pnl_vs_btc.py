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
