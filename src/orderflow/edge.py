"""Замер преимущества: есть ли в поглощении объёма предсказательная сила.

Здесь нет ни сделок, ни стопов, ни тейков — только вопрос "куда в среднем идёт
цена после события и отличается ли это от случайного момента времени". Пока
ответ не получен, писать торговую логику бессмысленно.

Методические правила, которые здесь соблюдаются:
  * вход по open следующего бара, а не по close сигнального (иначе смотрим в будущее);
  * пороги считаются по скользящему окну прошлого, а не по всей выборке;
  * доверительный интервал через bootstrap по дням, потому что перекрывающиеся
    горизонты делают обычный t-stat завышенным в разы;
  * издержки вычитаются сразу, иначе цифры не значат ничего.
"""

from __future__ import annotations

import numpy as np
import polars as pl

# Binance USDT-M futures: taker 0.05% = 5 б.п. за сторону.
TAKER_BPS = 5.0
ROUND_TRIP_BPS = 2 * TAKER_BPS
HORIZONS = (5, 15, 30, 60)
LOOKBACK = 1440  # бары для скользящих порогов = сутки при 1m


def add_signals(
    df: pl.DataFrame,
    q: float = 0.90,
    close_loc_max: float = 0.40,
    lookback: int = LOOKBACK,
) -> pl.DataFrame:
    """Поглощение: агрессия в одну сторону, а цена не пошла.

    Сильная положительная дельта при закрытии в нижней части диапазона означает,
    что покупателей залили лимитными продажами — ожидаем движение вниз (short).
    Зеркально для покупок.
    """
    rng = pl.col("high") - pl.col("low")
    return (
        df.with_columns(
            (pl.col("delta") / pl.col("vol")).alias("delta_ratio"),
            pl.when(rng > 0)
            .then((pl.col("close") - pl.col("low")) / rng)
            .otherwise(0.5)
            .alias("close_loc"),
        )
        .with_columns(
            pl.col("delta_ratio")
            .rolling_quantile(q, window_size=lookback, min_samples=lookback // 2)
            .shift(1)
            .alias("thr_hi"),
            pl.col("delta_ratio")
            .rolling_quantile(1 - q, window_size=lookback, min_samples=lookback // 2)
            .shift(1)
            .alias("thr_lo"),
            pl.col("vol")
            .rolling_median(window_size=lookback, min_samples=lookback // 2)
            .shift(1)
            .alias("vol_med"),
        )
        .with_columns(
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
    )


def add_forward_returns(
    df: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS
) -> pl.DataFrame:
    """Форвардная доходность в б.п. от входа по open(t+1) до open(t+1+H)."""
    entry = pl.col("open").shift(-1)
    return df.with_columns(
        [
            ((pl.col("open").shift(-(1 + h)) / entry - 1) * 10_000).alias(f"fwd_{h}")
            for h in horizons
        ]
    )


def _bootstrap_ci(
    values: np.ndarray, days: np.ndarray, iters: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% ДИ для среднего с ресемплингом целых дней (учитывает автокорреляцию)."""
    rng = np.random.default_rng(seed)
    uniq = np.unique(days)
    by_day = {d: values[days == d] for d in uniq}
    means = np.empty(iters)
    for i in range(iters):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        means[i] = np.concatenate([by_day[d] for d in pick]).mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def study(
    df: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS, cost: float = ROUND_TRIP_BPS
) -> pl.DataFrame:
    """Сравнивает доходность после сигнала с базовой линией всех баров."""
    df = add_forward_returns(add_signals(df), horizons)
    rows = []

    for h in horizons:
        col = f"fwd_{h}"
        # Базовая линия: тот же горизонт, но вход в каждый бар подряд, без условия.
        base = df.select(pl.col(col).drop_nulls()).to_series().to_numpy()

        ev = df.filter((pl.col("signal") != 0) & pl.col(col).is_not_null()).select(
            (pl.col("signal") * pl.col(col)).alias("r"),
            pl.col("ts").dt.date().alias("day"),
        )
        if ev.is_empty():
            continue

        r = ev["r"].to_numpy()
        days = ev["day"].to_numpy()
        lo, hi = _bootstrap_ci(r, days)

        rows.append(
            {
                "горизонт_мин": h,
                "сделок": len(r),
                "средн_бп": round(float(r.mean()), 2),
                "ДИ95_низ": round(lo, 2),
                "ДИ95_верх": round(hi, 2),
                "доля_плюс": round(float((r > 0).mean()), 3),
                "медиана_бп": round(float(np.median(r)), 2),
                "базовая_|бп|": round(float(np.abs(base).mean()), 2),
                "чистыми_бп": round(float(r.mean()) - cost, 2),
            }
        )

    return pl.DataFrame(rows)


if __name__ == "__main__":
    import sys

    from download import load
    from footprint import build

    start = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-10"

    trades = load("BTCUSDT", start, end)
    print(f"{len(trades):,} сделок, {start}..{end}")

    df = build(trades, every="1m", price_step=10.0)
    print(f"{len(df):,} баров\n")

    sig = add_signals(df)
    n_sig = sig.filter(pl.col("signal") != 0).height
    print(f"сигналов: {n_sig} ({100 * n_sig / len(sig):.2f}% баров)\n")

    with pl.Config(tbl_cols=-1, tbl_width_chars=200):
        print(study(df))
    print(f"\nиздержки round-trip taker: {ROUND_TRIP_BPS} б.п.")
