"""Скрининг инструментов для мейкинга: где спред шире комиссии.

Мейкер получает половину спреда, платит комиссию и теряет на adverse selection.
Первое необходимое условие — половина спреда должна быть больше комиссии, иначе
схема убыточна ещё до всякого движения цены. Это отсекает большинство пар за
секунды, включая BTC, где спред равен одному тику.

Считаем по нескольким снимкам стакана, а не по одному, чтобы не поймать
случайный момент расширения спреда.
"""

from __future__ import annotations

import time

import polars as pl
import requests

BOOK = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
STATS = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# Мейкерская комиссия Binance USDT-M futures, б.п.
MAKER_BPS = {"базовый 0.02%": 2.0, "с BNB 0.018%": 1.8, "VIP1 0.016%": 1.6}


def snapshots(n: int = 30, pause: float = 2.0) -> pl.DataFrame:
    """Средний спред в б.п. по всем инструментам за n снимков."""
    frames = []
    for i in range(n):
        raw = requests.get(BOOK, timeout=20).json()
        frames.append(
            pl.DataFrame(raw).select(
                pl.col("symbol"),
                pl.col("bidPrice").cast(pl.Float64).alias("bid"),
                pl.col("askPrice").cast(pl.Float64).alias("ask"),
            )
        )
        if i < n - 1:
            time.sleep(pause)

    return (
        pl.concat(frames)
        .filter((pl.col("bid") > 0) & (pl.col("ask") > pl.col("bid")))
        .with_columns(
            (
                (pl.col("ask") - pl.col("bid"))
                / ((pl.col("ask") + pl.col("bid")) / 2)
                * 10_000
            ).alias("spread_bps")
        )
        .group_by("symbol")
        .agg(
            pl.col("spread_bps").median().alias("спред_бп"),
            pl.col("spread_bps").quantile(0.9).alias("спред_p90"),
            pl.len().alias("снимков"),
        )
    )


def volumes() -> pl.DataFrame:
    raw = requests.get(STATS, timeout=30).json()
    return pl.DataFrame(raw).select(
        pl.col("symbol"),
        (pl.col("quoteVolume").cast(pl.Float64) / 1e6).alias("оборот_млн_$"),
        pl.col("count").cast(pl.Int64).alias("сделок_сутки"),
    )


def screen(min_volume_musd: float = 20.0, n: int = 30) -> pl.DataFrame:
    df = snapshots(n).join(volumes(), on="symbol")
    return (
        df.filter(pl.col("оборот_млн_$") > min_volume_musd)
        .with_columns(
            (pl.col("спред_бп") / 2).alias("полспреда_бп"),
            *[
                (pl.col("спред_бп") / 2 - fee).alias(f"брутто [{name}]")
                for name, fee in MAKER_BPS.items()
            ],
        )
        .sort("брутто [базовый 0.02%]", descending=True)
        .with_columns(pl.col(pl.Float64).round(2))
    )


if __name__ == "__main__":
    df = screen()
    total = df.height
    viable = df.filter(pl.col("брутто [базовый 0.02%]") > 0)

    with pl.Config(tbl_rows=25, tbl_cols=-1, tbl_width_chars=220):
        print("\n=== Топ по брутто-марже мейкера ===")
        print(
            df.select(
                "symbol", "спред_бп", "полспреда_бп", "оборот_млн_$",
                "сделок_сутки", "брутто [базовый 0.02%]", "брутто [VIP1 0.016%]",
            ).head(20)
        )

        print("\n=== Мажоры для сравнения ===")
        print(
            df.filter(pl.col("symbol").is_in(["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]))
            .select("symbol", "спред_бп", "полспреда_бп", "оборот_млн_$",
                    "брутто [базовый 0.02%]")
        )

    print(
        f"\nинструментов с оборотом >20 млн $: {total}; "
        f"из них полспреда > комиссии: {viable.height}"
    )
