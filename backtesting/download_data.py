"""Download historical klines from Binance mainnet and save as CSV."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from binance.client import Client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "UNIUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "FILUSDT", "LDOUSDT",
    "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT", "JUPUSDT",
    "WLDUSDT", "PENDLEUSDT", "STXUSDT", "FETUSDT", "RUNEUSDT",
]

COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


def fetch_klines_batched(
    client: Client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    batch_size: int = 1500,
) -> list:
    """Fetch klines in batches of `batch_size`, handling Binance's 1500 limit."""
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        try:
            klines = client.futures_klines(
                symbol=symbol,
                interval=interval,
                startTime=current_start,
                endTime=end_ms,
                limit=batch_size,
            )
        except Exception as e:
            print(f"  [WARN] API error for {symbol} {interval}, retrying: {e}")
            time.sleep(5)
            continue

        if not klines:
            break

        all_klines.extend(klines)
        last_open_time = int(klines[-1][0])
        current_start = last_open_time + 1

        if len(klines) < batch_size:
            break

        time.sleep(0.5)

    return all_klines


def klines_to_dataframe(klines: list) -> pd.DataFrame:
    """Convert raw Binance kline list to a clean DataFrame."""
    rows = []
    for k in klines:
        rows.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.drop_duplicates(subset="open_time", inplace=True)
    df.sort_values("open_time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def get_resume_start_ms(filepath: str) -> int | None:
    """If CSV already exists, return the last open_time + 1 for incremental fetch."""
    if not os.path.exists(filepath):
        return None
    try:
        df = pd.read_csv(filepath)
        if df.empty:
            return None
        return int(df["open_time"].iloc[-1]) + 1
    except Exception:
        return None


def download_symbol(
    client: Client, symbol: str, interval: str, start_ms: int, end_ms: int
):
    """Download klines for one symbol+interval, with resume support."""
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = f"{symbol}_{interval}.csv"
    filepath = os.path.join(DATA_DIR, filename)

    resume_ms = get_resume_start_ms(filepath)
    actual_start = resume_ms if resume_ms and resume_ms > start_ms else start_ms

    if actual_start >= end_ms:
        print(f"  {filename}: already up to date, skipping")
        return

    if resume_ms:
        print(f"  {filename}: resuming from {datetime.fromtimestamp(actual_start / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

    klines = fetch_klines_batched(client, symbol, interval, actual_start, end_ms)
    if not klines:
        print(f"  {filename}: no new data")
        return

    new_df = klines_to_dataframe(klines)

    if resume_ms and os.path.exists(filepath):
        existing_df = pd.read_csv(filepath)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined.drop_duplicates(subset="open_time", inplace=True)
        combined.sort_values("open_time", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        new_df = combined

    new_df.to_csv(filepath, index=False)
    print(f"  {filename}: {len(new_df)} bars saved")


def download_all(symbols: list[str] | None = None, days: int = 365):
    """Download 1H and 1D klines for all symbols."""
    symbols = symbols or DEFAULT_SYMBOLS
    client = Client()

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    print(f"Downloading {len(symbols)} symbols x 2 intervals")
    print(f"Period: {datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"Data dir: {os.path.abspath(DATA_DIR)}")
    print()

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}")
        download_symbol(client, symbol, Client.KLINE_INTERVAL_1HOUR, start_ms, end_ms)
        download_symbol(client, symbol, Client.KLINE_INTERVAL_1DAY, start_ms, end_ms)
        print()


if __name__ == "__main__":
    download_all()
