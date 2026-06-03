"""
验证"亏损尾巴"假设:实盘亏损是否集中在 低流动性 / 高波动 / 新上市 的币?

为每个实盘交易过的 symbol,从 mainnet(无需鉴权)拉真实 1d K 线,计算:
  - liq_usdt   : 交易窗口内 中位 日 quote 成交额(真实市场流动性,≠机器人自己成交额)
  - vol_pct    : 交易窗口内 中位 日 (high-low)/open 波动率
  - age_days   : 可得历史 K 线条数(上市天数代理)
然后把这些盘前可得的特征 与 per-symbol 净 PnL 做关联,看亏损尾巴是否真集中。

只读研究脚本,不下任何单。用法:
  source .venv/bin/activate && python scripts/validate_loss_tail.py
"""
import csv
import os
import sys
import time
import statistics
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exchange import Exchange

SUMMARY = "results/live_summary_20260506_20260602.csv"
OUT = "results/loss_tail_features.csv"

# 交易窗口 (UTC). 实盘 2026-05-06 21:00 UTC+8 ≈ 2026-05-06 13:00 UTC 起.
WIN_START = datetime(2026, 5, 6, tzinfo=timezone.utc)
WIN_END = datetime(2026, 6, 2, tzinfo=timezone.utc)
WIN_START_MS = int(WIN_START.timestamp() * 1000)
WIN_END_MS = int(WIN_END.timestamp() * 1000)


def load_pnl():
    rows = {}
    for r in csv.DictReader(open(SUMMARY)):
        if r["symbol"] == "TOTAL":
            continue
        rows[r["symbol"]] = float(r["net_pnl"])
    return rows


def features(ex, symbol):
    """拉 1d K 线 → (liq_usdt, vol_pct, age_days). 失败返回 None."""
    # futures_klines: [openTime,o,h,l,c,vol,closeTime,quoteVol,trades,...]
    kl = ex._retry(lambda: ex.data_client.futures_klines(
        symbol=symbol, interval="1d", limit=500))
    if not kl:
        return None
    age_days = len(kl)
    win_qv, win_range = [], []
    for k in kl:
        ot = int(k[0])
        if WIN_START_MS <= ot <= WIN_END_MS:
            o, h, l = float(k[1]), float(k[2]), float(k[3])
            qv = float(k[7])
            win_qv.append(qv)
            if o > 0:
                win_range.append((h - l) / o)
    if not win_qv:
        return None
    return (statistics.median(win_qv),
            statistics.median(win_range) if win_range else 0.0,
            age_days)


def bucket_report(rows, key, label, reverse=False):
    """按特征排序,四分位分桶,看每桶净 PnL/胜负."""
    rows = [r for r in rows if r[key] is not None]
    rows.sort(key=lambda r: r[key], reverse=reverse)
    n = len(rows)
    q = max(1, n // 4)
    print(f"\n=== {label}(四分位,低→高{' 已反转为 高→低' if reverse else ''}) ===")
    print(f"{'桶':<10}{'区间':<22}{'symbols':<9}{'净PnL':<10}{'胜/总':<9}")
    for i in range(0, n, q):
        chunk = rows[i:i + q]
        if not chunk:
            continue
        lo, hi = chunk[0][key], chunk[-1][key]
        net = sum(c["net"] for c in chunk)
        wins = sum(1 for c in chunk if c["net"] > 0)
        qn = f"Q{i // q + 1}"
        print(f"{qn:<10}{f'{lo:.4g}~{hi:.4g}':<22}{len(chunk):<9}{net:<10.0f}{f'{wins}/{len(chunk)}':<9}")


def main():
    ex = Exchange()
    pnl = load_pnl()
    print(f"拉取 {len(pnl)} 个 symbol 的 mainnet 1d K 线 ...")
    rows = []
    fails = []
    for i, (sym, net) in enumerate(pnl.items(), 1):
        try:
            f = features(ex, sym)
        except Exception as e:
            f = None
        if f is None:
            fails.append(sym)
            continue
        liq, volp, age = f
        rows.append(dict(symbol=sym, net=net, liq_usdt=liq, vol_pct=volp, age_days=age))
        if i % 20 == 0:
            print(f"  ...{i}/{len(pnl)}")
        time.sleep(0.06)  # 温柔限速

    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol", "net", "liq_usdt", "vol_pct", "age_days"])
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["net"]):
            w.writerow(r)
    print(f"\n写出 {OUT}({len(rows)} symbols,失败 {len(fails)}: {fails[:10]})")

    # 关联:相关系数
    def corr(key):
        xs = [r[key] for r in rows]
        ys = [r["net"] for r in rows]
        return statistics.correlation(xs, ys) if len(xs) > 2 else float("nan")

    print("\n=== Pearson 相关 (特征 vs 净PnL) ===")
    for k, lab in [("liq_usdt", "流动性"), ("vol_pct", "波动率"), ("age_days", "上市天数")]:
        print(f"  {lab:<8}({k}): r = {corr(k):+.3f}")

    bucket_report(rows, "liq_usdt", "流动性 liq_usdt")
    bucket_report(rows, "vol_pct", "波动率 vol_pct")
    bucket_report(rows, "age_days", "上市天数 age_days")

    # 新上市单独看 (< 30 / < 60 天)
    for thr in (30, 60):
        young = [r for r in rows if r["age_days"] < thr]
        if young:
            print(f"\n上市 <{thr}天: {len(young)} symbols, 净 {sum(r['net'] for r in young):+.0f}, "
                  f"胜 {sum(1 for r in young if r['net']>0)}/{len(young)}")


if __name__ == "__main__":
    main()
