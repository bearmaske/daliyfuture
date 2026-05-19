"""一次性增量拉取 BTCUSDT 1H K 线到 data/BTCUSDT_1h.csv。"""
import os, sys
import pandas as pd
from binance.client import Client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backtesting.download_data import fetch_klines_batched, COLUMNS

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "BTCUSDT_1h.csv")

def main():
    client = Client()
    existing = pd.read_csv(DATA_FILE)
    last_ms = int(existing["open_time"].max())
    start_ms = last_ms + 1
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
