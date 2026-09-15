"""Можно ли восстановить сторону агрессора из цены и объёма.

Зачем. Бесплатная историческая тиковая база по МОЕХ (экспорт Finam и подобные)
даёт время, цену и объём, но не даёт направление инициатора. ISS даёт направление,
но только за текущую сессию. Если сторону удаётся восстановить по правилу тиков
достаточно точно, то историю за годы можно использовать сразу, вместо того чтобы
месяц накапливать сессии.

Проверка честная: сегодняшняя сессия с настоящим BUYSELL служит эталоном.
Важна не точность на отдельной сделке, а точность агрегата — дельты в баре,
потому что именно она входит во все кластерные сигналы.
"""

from __future__ import annotations

import numpy as np
import polars as pl

BUY, SELL = 1, -1


def infer_side(trades: pl.DataFrame) -> pl.DataFrame:
    """Правило тиков: рост цены — инициатор покупал, падение — продавал.

    При неизменной цене направление наследуется от предыдущей сделки: это
    классическая схема Lee-Ready в варианте без стакана.
    """
    return trades.with_columns(
        pl.when(pl.col("price") > pl.col("price").shift(1))
        .then(pl.lit(BUY, dtype=pl.Int8))
        .when(pl.col("price") < pl.col("price").shift(1))
        .then(pl.lit(SELL, dtype=pl.Int8))
        .otherwise(None)
        .forward_fill()
        .fill_null(BUY)
        .alias("side_hat")
    )


def per_trade_quality(df: pl.DataFrame) -> dict:
    ok = (pl.col("side") == pl.col("side_hat")).cast(pl.Float64)
    res = df.select(
        ok.mean().alias("точность"),
        (ok * pl.col("qty")).sum().alias("объём_верно"),
        pl.col("qty").sum().alias("объём_всего"),
        (pl.col("price") == pl.col("price").shift(1)).cast(pl.Float64).mean()
        .alias("доля_без_изменения_цены"),
    ).row(0, named=True)
    return {
        "точность_по_сделкам_%": round(res["точность"] * 100, 1),
        "точность_по_объёму_%": round(
            res["объём_верно"] / res["объём_всего"] * 100, 1
        ),
        "сделок_без_движения_цены_%": round(res["доля_без_изменения_цены"] * 100, 1),
    }


def per_bar_quality(df: pl.DataFrame, every: str = "1m") -> dict:
    """Главная метрика: насколько совпадает дельта бара — истинная и восстановленная."""
    bars = (
        df.sort("ts")
        .group_by_dynamic("ts", every=every, label="left", closed="left")
        .agg(
            (pl.col("qty") * pl.col("side")).sum().alias("delta"),
            (pl.col("qty") * pl.col("side_hat")).sum().alias("delta_hat"),
            pl.col("qty").sum().alias("vol"),
        )
        .filter(pl.col("vol") > 0)
    )

    d = bars["delta"].to_numpy()
    dh = bars["delta_hat"].to_numpy()
    return {
        "бар": every,
        "баров": len(d),
        "корреляция_дельты": round(float(np.corrcoef(d, dh)[0, 1]), 3),
        "совпадение_знака_%": round(float((np.sign(d) == np.sign(dh)).mean()) * 100, 1),
        # Смещение важно: если прокси систематически завышает покупки,
        # сигналы будут перекошены в одну сторону.
        "смещение_бп_объёма": round(
            float((dh - d).mean() / bars["vol"].mean() * 100), 2
        ),
    }


if __name__ == "__main__":
    import sys

    from moex_ticks import load

    secid = sys.argv[1] if len(sys.argv) > 1 else "SiU6"
    trades = load(secid)
    df = infer_side(trades)

    print(f"=== {secid}: {len(df):,} сделок с эталонной стороной ===\n")

    q = per_trade_quality(df)
    for k, v in q.items():
        print(f"{k:<32} {v}")

    print("\n=== Качество агрегата (то, что реально идёт в сигналы) ===")
    rows = [per_bar_quality(df, e) for e in ("1m", "5m", "15m")]
    with pl.Config(tbl_width_chars=160):
        print(pl.DataFrame(rows))

    true_buy = df.filter(pl.col("side") == BUY)["qty"].sum() / df["qty"].sum()
    hat_buy = df.filter(pl.col("side_hat") == BUY)["qty"].sum() / df["qty"].sum()
    print(
        f"\nдоля покупок: истинная {true_buy * 100:.1f}%, "
        f"восстановленная {hat_buy * 100:.1f}%"
    )
