"""Фандинг перпетуалов: доход, не требующий предсказания направления.

Схема: покупаем спот, продаём вечный фьючерс на тот же объём. Направление цены
не важно — ноги компенсируют друг друга. Доход = фандинг, который лонги платят
шортам каждые 8 часов, пока рынок в контанго.

Это единственный источник дохода в крипте с полностью прозрачной экономикой:
ставка известна заранее, а не оценивается моделью. Риски здесь не рыночные, а
инфраструктурные — уход фандинга в минус, ликвидация фьючерсной ноги, биржа.
"""

from __future__ import annotations

import time
from pathlib import Path

import polars as pl
import requests

FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"
CACHE = Path(__file__).resolve().parents[2] / "data" / "funding"
PERIODS_PER_YEAR = 365 * 3  # фандинг каждые 8 часов


def fetch_funding(symbol: str, force: bool = False) -> pl.DataFrame:
    """Вся история ставок фандинга по инструменту, с постраничной выкачкой."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}.parquet"
    if path.exists() and not force:
        return pl.read_parquet(path)

    rows: list[dict] = []
    start = 1568102400000  # сентябрь 2019, запуск USDT-M фьючерсов
    while True:
        resp = requests.get(
            FAPI,
            params={"symbol": symbol, "startTime": start, "limit": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1]["fundingTime"] + 1
        if nxt <= start:
            break
        start = nxt
        time.sleep(0.2)

    df = (
        pl.DataFrame(rows)
        .select(
            pl.from_epoch("fundingTime", time_unit="ms").alias("ts"),
            pl.col("fundingRate").cast(pl.Float64).alias("rate"),
            # В ранних записях markPrice пустой, поэтому cast нестрогий.
            pl.col("markPrice").cast(pl.Float64, strict=False).alias("mark"),
        )
        .unique("ts")
        .sort("ts")
    )
    df.write_parquet(path, compression="zstd")
    return df


def yearly(df: pl.DataFrame) -> pl.DataFrame:
    """Годовая доходность нейтральной позиции до и после издержек."""
    return (
        df.with_columns(pl.col("ts").dt.year().alias("год"))
        .group_by("год")
        .agg(
            pl.len().alias("выплат"),
            (pl.col("rate").mean() * PERIODS_PER_YEAR * 100).alias("годовых_%"),
            ((pl.col("rate") > 0).mean() * 100).alias("доля_положит_%"),
            (pl.col("rate").std() * PERIODS_PER_YEAR * 100).alias("разброс_%"),
            (pl.col("rate").sum() * 100).alias("собрано_за_год_%"),
        )
        .sort("год")
        .with_columns(pl.exclude("год", "выплат").round(2))
    )


def equity(df: pl.DataFrame, fee_bps: float = 20.0) -> pl.DataFrame:
    """Кривая накопленного фандинга, минус разовые издержки на постройку позиции.

    fee_bps — вход и выход по двум ногам сразу: тейкер 5 б.п. * 2 ноги * 2 раза.
    """
    return df.with_columns(
        ((pl.col("rate").cum_sum() - fee_bps / 10_000) * 100).alias("накоплено_%")
    ).select("ts", "rate", "накоплено_%")


def plot_yearly(data: dict[str, pl.DataFrame], path: str) -> None:
    """Сравнение carry с безрисковой ставкой: если ниже — схема не имеет смысла."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    years = sorted({y for df in data.values() for y in df["год"].to_list()})
    width = 0.8 / len(data)
    fig, ax = plt.subplots(figsize=(10, 5.4))

    for i, (sym, df) in enumerate(data.items()):
        lookup = dict(zip(df["год"].to_list(), df["годовых_%"].to_list()))
        vals = [lookup.get(y, np.nan) for y in years]
        ax.bar(np.arange(len(years)) + i * width, vals, width, label=sym)

    ax.axhline(0, color="black", lw=1)
    ax.axhline(4, color="firebrick", ls="--", lw=1.4)
    ax.text(
        -0.35, 5.0, "безрисковая ставка ~4%: ниже неё смысла нет",
        color="firebrick", fontsize=9,
    )
    ax.set_xticks(np.arange(len(years)) + 0.4 - width / 2)
    ax.set_xticklabels([str(y) for y in years])
    ax.set_ylabel("доходность фандинга, % годовых")
    ax.set_title("Нейтральный carry на перпетуалах Binance: преимущество сжалось")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


if __name__ == "__main__":
    import sys

    symbols = sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    stats = {}
    for sym in symbols:
        df = fetch_funding(sym)
        stats[sym] = yearly(df)
        print(f"\n===== {sym}: {len(df):,} выплат, "
              f"{df['ts'].min():%Y-%m-%d}..{df['ts'].max():%Y-%m-%d}")
        with pl.Config(tbl_rows=20, tbl_width_chars=140):
            print(stats[sym])

        last = df.filter(pl.col("ts") > pl.col("ts").max() - pl.duration(days=90))
        print(
            f"последние 90 дней: {last['rate'].mean() * PERIODS_PER_YEAR * 100:.2f}% "
            f"годовых, положительных выплат {(last['rate'] > 0).mean() * 100:.0f}%"
        )

    import os

    os.makedirs("../../reports", exist_ok=True)
    out = os.path.abspath("../../reports/funding.png")
    plot_yearly(stats, out)
    print(f"\nграфик: {out}")
