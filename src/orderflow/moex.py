"""Данные МОЕХ через открытый ISS: свечи, ликвидность, спецификации контрактов.

Зачем сюда переезжаем: издержки на фьючерсах МОЕХ на порядок ниже крипты, а
именно издержки, а не качество модели, закрыли все проверенные схемы в крипте.
Плюс ISS отдаёт в потоке сделок поле BUYSELL — сторону агрессора, без которой
футпринт построить нельзя. Исторических тиков в ISS нет, только текущая сессия,
поэтому тиковую базу придётся накапливать самим.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import polars as pl
import requests

ISS = "https://iss.moex.com/iss"
FORTS = f"{ISS}/engines/futures/markets/forts"
DATA_ROOT = Path(
    os.environ.get("ORDERFLOW_DATA", Path(__file__).resolve().parents[2] / "data")
)
CACHE = DATA_ROOT / "moex"


RETRIES = 4


def _get(url: str, **params) -> dict:
    """GET с повторами: ISS обрывает соединение на длинной пагинации, а этим
    модулем пользуется сборщик на сервере — одиночный сбой не должен его ронять."""
    params.setdefault("iss.meta", "off")
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, timeout=40)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} от ISS")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"ISS недоступен после {RETRIES} попыток: {last}")


def _block(payload: dict, name: str) -> pl.DataFrame:
    blk = payload[name]
    if not blk["data"]:
        return pl.DataFrame(schema={c: pl.Utf8 for c in blk["columns"]})
    return pl.DataFrame(blk["data"], schema=blk["columns"], orient="row")


def liquid_futures(date: str, min_trades: int = 500) -> pl.DataFrame:
    """Все фьючерсы с торгами за дату. ISS отдаёт по 100 штук, поэтому пагинация."""
    frames, start = [], 0
    while True:
        payload = _get(
            f"{ISS}/history/engines/futures/markets/forts/securities.json",
            date=date,
            start=start,
            **{"iss.only": "history"},
        )
        chunk = _block(payload, "history")
        if chunk.is_empty():
            break
        frames.append(chunk)
        start += 100
        if start > 3000:
            break

    if not frames:  # выходной или праздник
        return pl.DataFrame(
            schema={"SECID": pl.Utf8, "CLOSE": pl.Float64,
                    "контрактов": pl.Int64, "сделок": pl.Int64}
        )

    df = pl.concat(frames, how="vertical_relaxed")
    return (
        df.select(
            pl.col("SECID"),
            pl.col("CLOSE").cast(pl.Float64),
            pl.col("VOLUME").cast(pl.Int64).alias("контрактов"),
            pl.col("NUMTRADES").cast(pl.Int64).alias("сделок"),
        )
        .filter(pl.col("сделок") >= min_trades)
        .sort("сделок", descending=True)
    )


def spec(secid: str) -> dict:
    """Шаг цены, стоимость шага и биржевые сборы в рублях за контракт.

    МОЕХ публикует сборы прямо в спецификации: BUYSELLFEE за обычную сделку и
    SCALPERFEE за внутридневной оборот. Это избавляет от угадывания издержек.
    """
    payload = _get(
        f"{FORTS}/securities/{secid}.json", **{"iss.only": "securities"}
    )
    df = _block(payload, "securities")
    if df.is_empty():
        return {}
    row = df.row(0, named=True)
    price = float(row["LASTSETTLEPRICE"] or row["PREVSETTLEPRICE"] or 0)
    minstep = float(row["MINSTEP"] or 0)
    stepprice = float(row["STEPPRICE"] or 0)
    if not (price and minstep and stepprice):
        return {}

    value_rub = price / minstep * stepprice
    return {
        "secid": secid,
        "name": row["SHORTNAME"],
        "price": price,
        "minstep": minstep,
        "stepprice": stepprice,
        "стоимость_контракта_руб": round(value_rub),
        "сбор_биржи_руб": float(row["BUYSELLFEE"] or 0),
        "сбор_скальпера_руб": float(row["SCALPERFEE"] or 0),
        # Спред в один шаг цены — норма для ликвидных контрактов МОЕХ.
        "спред_бп": round(minstep / price * 10_000, 3),
    }


def candles(secid: str, start: str, end: str, interval: int = 1) -> pl.DataFrame:
    """Минутные свечи за период. ISS отдаёт порциями по 500, поэтому пагинация."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{secid}_{interval}m_{start}_{end}.parquet"
    if path.exists():
        return pl.read_parquet(path)

    frames, cursor = [], 0
    while True:
        payload = _get(
            f"{FORTS}/securities/{secid}/candles.json",
            interval=interval,
            **{"from": start, "till": end, "start": cursor},
        )
        chunk = _block(payload, "candles")
        if chunk.is_empty():
            break
        frames.append(chunk)
        cursor += chunk.height
        if chunk.height < 500:
            break

    if not frames:
        raise FileNotFoundError(f"нет свечей {secid} {start}..{end}")

    df = (
        pl.concat(frames, how="vertical_relaxed")
        .select(
            pl.col("begin").str.to_datetime("%Y-%m-%d %H:%M:%S").alias("ts"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64).alias("vol"),
        )
        .unique("ts")
        .sort("ts")
    )
    df.write_parquet(path, compression="zstd")
    return df


if __name__ == "__main__":
    import sys

    date = sys.argv[1] if len(sys.argv) > 1 else "2026-09-11"
    df = liquid_futures(date)
    with pl.Config(tbl_rows=30):
        print(f"=== Фьючерсы МОЕХ с торгами за {date} ===")
        print(df.head(25))
    print(f"\nвсего инструментов с >=500 сделок: {df.height}")
