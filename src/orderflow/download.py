"""Загрузка архивов сделок Binance (aggTrades) в локальный parquet-кэш.

aggTrades = сделки, склеенные по (цена, направление, таймштамп). Для футпринта
это лучше, чем trades: агрессивный ордер, съевший 5 лимитников на одной цене,
приходит одной записью, что и есть "кластер" в исходном смысле слова.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import polars as pl
import requests

BASE = "https://data.binance.vision/data"
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# В архивах Binance колонки без имён (старые файлы) либо с заголовком (новые).
COLUMNS = [
    "agg_id",
    "price",
    "qty",
    "first_id",
    "last_id",
    "ts",
    "is_buyer_maker",
]


def _url(symbol: str, day: str, market: str) -> str:
    if market == "futures":
        return f"{BASE}/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip"
    return f"{BASE}/spot/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip"


def _parse_csv(raw: bytes) -> pl.DataFrame:
    first_line = raw.split(b"\n", 1)[0]
    has_header = b"agg_trade_id" in first_line or b"price" in first_line

    df = pl.read_csv(
        raw,
        has_header=has_header,
        new_columns=None if has_header else COLUMNS,
        columns=list(range(len(COLUMNS))),
        schema_overrides={"price": pl.Float64, "qty": pl.Float64},
    )
    df.columns = COLUMNS

    # Таймштамп в разных файлах в миллисекундах или микросекундах.
    unit = "us" if df["ts"].max() > 4_000_000_000_000 else "ms"

    return df.select(
        pl.from_epoch("ts", time_unit=unit).alias("ts"),
        pl.col("price"),
        pl.col("qty"),
        # is_buyer_maker=True -> покупатель стоял лимитом -> агрессор продавал.
        pl.when(pl.col("is_buyer_maker").cast(pl.Boolean))
        .then(pl.lit(-1, dtype=pl.Int8))
        .otherwise(pl.lit(1, dtype=pl.Int8))
        .alias("side"),
    ).sort("ts")


def fetch_day(
    symbol: str, day: str, market: str = "futures", force: bool = False
) -> pl.DataFrame:
    """Отдаёт сделки за один день, скачивая только при отсутствии в кэше."""
    cache = DATA_DIR / market / symbol
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{day}.parquet"

    if path.exists() and not force:
        return pl.read_parquet(path)

    resp = requests.get(_url(symbol, day, market), timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        raw = z.read(z.namelist()[0])

    df = _parse_csv(raw)
    df.write_parquet(path, compression="zstd")
    return df


def day_list(start: str, end: str) -> list[str]:
    days = pl.date_range(
        pl.Series([start]).str.to_date().item(),
        pl.Series([end]).str.to_date().item(),
        eager=True,
    )
    return [d.isoformat() for d in days]


def prefetch(
    symbol: str, start: str, end: str, market: str = "futures", workers: int = 8
) -> None:
    """Прогревает кэш параллельно. Дни, которых нет на сервере, пропускаются."""
    from concurrent.futures import ThreadPoolExecutor

    def one(day: str) -> str:
        try:
            n = len(fetch_day(symbol, day, market))
            return f"{day}: {n:,}"
        except requests.HTTPError as exc:
            return f"{day}: НЕТ ({exc.response.status_code})"

    days = day_list(start, end)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(one, days):
            print(line, flush=True)


def load(symbol: str, start: str, end: str, market: str = "futures") -> pl.DataFrame:
    """Склеивает дни [start, end] включительно, пропуская отсутствующие."""
    frames = []
    for day in day_list(start, end):
        try:
            frames.append(fetch_day(symbol, day, market))
        except requests.HTTPError:
            continue
    if not frames:
        raise FileNotFoundError(f"нет данных {symbol} {start}..{end}")
    return pl.concat(frames).sort("ts")


if __name__ == "__main__":
    import sys

    sym, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
    prefetch(sym, start, end)
