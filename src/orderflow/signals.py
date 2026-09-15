"""Семейства сигналов на кластерах. Каждое — отдельная проверяемая гипотеза.

absorption — агрессия в одну сторону, а цена не пошла (дивергенция дельты и цены).
cluster    — крупный односторонний кластер защищает край диапазона: цена подошла,
             об этот лимитный объём закрылись агрессоры и её отбило.
             Это ближе всего к исходной формулировке "зоны встречных объёмов".
"""

from __future__ import annotations

import polars as pl


def _prep(df: pl.DataFrame, lookback: int) -> pl.DataFrame:
    rng = pl.col("high") - pl.col("low")
    return df.with_columns(
        (pl.col("delta") / pl.col("vol")).alias("delta_ratio"),
        pl.when(rng > 0)
        .then((pl.col("close") - pl.col("low")) / rng)
        .otherwise(0.5)
        .alias("close_loc"),
        pl.col("vol")
        .rolling_median(window_size=lookback, min_samples=lookback // 4)
        .shift(1)
        .alias("vol_med"),
    )


def absorption(
    df: pl.DataFrame, q: float = 0.95, close_loc_max: float = 0.35, lookback: int = 1440
) -> pl.DataFrame:
    df = _prep(df, lookback)
    return df.with_columns(
        pl.col("delta_ratio")
        .rolling_quantile(q, window_size=lookback, min_samples=lookback // 4)
        .shift(1)
        .alias("thr_hi"),
        pl.col("delta_ratio")
        .rolling_quantile(1 - q, window_size=lookback, min_samples=lookback // 4)
        .shift(1)
        .alias("thr_lo"),
    ).with_columns(
        pl.when(
            (pl.col("delta_ratio") > pl.col("thr_hi"))
            & (pl.col("close_loc") < close_loc_max)
            & (pl.col("vol") > pl.col("vol_med"))
        )
        .then(-1)
        .when(
            (pl.col("delta_ratio") < pl.col("thr_lo"))
            & (pl.col("close_loc") > 1 - close_loc_max)
            & (pl.col("vol") > pl.col("vol_med"))
        )
        .then(1)
        .otherwise(0)
        .cast(pl.Int8)
        .alias("signal")
    )


def cluster(
    df: pl.DataFrame, q: float = 0.95, edge_loc: float = 0.70, lookback: int = 1440
) -> pl.DataFrame:
    """Отбой от края, где стоял крупный встречный лимитный объём."""
    span = pl.col("fp_high") - pl.col("fp_low")
    df = _prep(df, lookback).with_columns(
        pl.when(span > 0)
        .then((pl.col("max_sell_bin") - pl.col("fp_low")) / span)
        .otherwise(0.5)
        .alias("sell_cl_loc"),
        pl.when(span > 0)
        .then((pl.col("max_buy_bin") - pl.col("fp_low")) / span)
        .otherwise(0.5)
        .alias("buy_cl_loc"),
        (pl.col("max_sell_cell").abs() / pl.col("vol")).alias("sell_cl_str"),
        (pl.col("max_buy_cell").abs() / pl.col("vol")).alias("buy_cl_str"),
    )
    return df.with_columns(
        pl.col("sell_cl_str")
        .rolling_quantile(q, window_size=lookback, min_samples=lookback // 4)
        .shift(1)
        .alias("thr_sell"),
        pl.col("buy_cl_str")
        .rolling_quantile(q, window_size=lookback, min_samples=lookback // 4)
        .shift(1)
        .alias("thr_buy"),
    ).with_columns(
        pl.when(
            (pl.col("sell_cl_loc") > edge_loc)
            & (pl.col("sell_cl_str") > pl.col("thr_sell"))
            & (pl.col("close") < pl.col("max_sell_bin"))
            & (pl.col("vol") > pl.col("vol_med"))
        )
        .then(-1)
        .when(
            (pl.col("buy_cl_loc") < 1 - edge_loc)
            & (pl.col("buy_cl_str") > pl.col("thr_buy"))
            & (pl.col("close") > pl.col("max_buy_bin"))
            & (pl.col("vol") > pl.col("vol_med"))
        )
        .then(1)
        .otherwise(0)
        .cast(pl.Int8)
        .alias("signal")
    )


FAMILIES = {"absorption": absorption, "cluster": cluster}
