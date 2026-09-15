"""Футпринт и барные метрики order flow.

Ячейка футпринта = (интервал времени, ценовая корзина) с раздельным объёмом
агрессивных покупок и продаж. Всё остальное — дельта, CVD, POC, дисбалансы —
производные от этой сетки. Никаких предположений о рынке здесь нет, это чистое
переупаковывание сделок.
"""

from __future__ import annotations

import polars as pl

BUY, SELL = 1, -1


def bars(trades: pl.DataFrame, every: str = "1m") -> pl.DataFrame:
    """OHLC + объёмы по сторонам агрессора + дельта и CVD."""
    return (
        trades.sort("ts")
        .group_by_dynamic("ts", every=every, label="left", closed="left")
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("qty").sum().alias("vol"),
            pl.col("qty").filter(pl.col("side") == BUY).sum().alias("buy_vol"),
            pl.col("qty").filter(pl.col("side") == SELL).sum().alias("sell_vol"),
            pl.len().alias("n_trades"),
        )
        .with_columns(
            pl.col("buy_vol").fill_null(0.0),
            pl.col("sell_vol").fill_null(0.0),
        )
        .with_columns((pl.col("buy_vol") - pl.col("sell_vol")).alias("delta"))
        .with_columns(pl.col("delta").cum_sum().alias("cvd"))
    )


def cells(
    trades: pl.DataFrame, every: str = "1m", price_step: float = 10.0
) -> pl.DataFrame:
    """Сетка футпринта: одна строка на (бар, ценовая корзина)."""
    return (
        trades.with_columns(
            pl.col("ts").dt.truncate(every).alias("bar_ts"),
            ((pl.col("price") / price_step).floor() * price_step).alias("bin"),
        )
        .group_by("bar_ts", "bin")
        .agg(
            pl.col("qty").filter(pl.col("side") == BUY).sum().alias("buy_vol"),
            pl.col("qty").filter(pl.col("side") == SELL).sum().alias("sell_vol"),
            pl.len().alias("n_trades"),
        )
        .with_columns(
            pl.col("buy_vol").fill_null(0.0),
            pl.col("sell_vol").fill_null(0.0),
        )
        .with_columns(
            (pl.col("buy_vol") + pl.col("sell_vol")).alias("vol"),
            (pl.col("buy_vol") - pl.col("sell_vol")).alias("delta"),
        )
        .sort("bar_ts", "bin")
    )


def cluster_features(fp: pl.DataFrame) -> pl.DataFrame:
    """Сворачивает сетку футпринта в признаки уровня бара.

    poc          — ценовая корзина с максимальным объёмом (точка контроля).
    poc_loc      — где POC внутри диапазона бара: 0 = у минимума, 1 = у максимума.
                   Высокий POC при развороте вниз = продавцы стояли лимитом сверху.
    top_cell_*   — крупнейший односторонний кластер бара, кандидат в "зону
                   встречных объёмов", о которую закрывались агрессоры.
    """
    return (
        fp.group_by("bar_ts")
        .agg(
            pl.col("bin").sort_by("vol").last().alias("poc"),
            pl.col("bin").min().alias("fp_low"),
            pl.col("bin").max().alias("fp_high"),
            pl.col("vol").max().alias("poc_vol"),
            pl.col("vol").sum().alias("fp_vol"),
            pl.col("bin").sort_by("delta").last().alias("max_buy_bin"),
            pl.col("bin").sort_by("delta").first().alias("max_sell_bin"),
            pl.col("delta").max().alias("max_buy_cell"),
            pl.col("delta").min().alias("max_sell_cell"),
            pl.len().alias("n_bins"),
        )
        .with_columns(
            pl.when(pl.col("fp_high") > pl.col("fp_low"))
            .then(
                (pl.col("poc") - pl.col("fp_low"))
                / (pl.col("fp_high") - pl.col("fp_low"))
            )
            .otherwise(0.5)
            .alias("poc_loc"),
            (pl.col("poc_vol") / pl.col("fp_vol")).alias("poc_share"),
        )
        .sort("bar_ts")
    )


def build(
    trades: pl.DataFrame, every: str = "1m", price_step: float = 10.0
) -> pl.DataFrame:
    """Бары, обогащённые кластерными признаками футпринта."""
    b = bars(trades, every)
    f = cluster_features(cells(trades, every, price_step))
    return b.join(f, left_on="ts", right_on="bar_ts", how="left").sort("ts")


if __name__ == "__main__":
    from download import fetch_day

    trades = fetch_day("BTCUSDT", "2026-09-01")
    df = build(trades, every="1m", price_step=10.0)

    print(df.select(
        "ts", "close", "vol", "delta", "cvd", "poc", "poc_loc", "max_sell_cell"
    ).head(10))
    print(f"\n{len(df)} баров, ожидается 1440")

    fp = cells(trades, every="1m", price_step=10.0)
    hour = fp.filter(pl.col("bar_ts") == pl.col("bar_ts").min())
    print("\nФутпринт первой минуты (цена | покупки | продажи | дельта):")
    print(hour.select("bin", "buy_vol", "sell_vol", "delta").sort("bin", descending=True))
