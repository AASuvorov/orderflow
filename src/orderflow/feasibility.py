"""Порог выживания: какая точность нужна, чтобы окупить издержки.

Это расчёт, который надо делать ПЕРВЫМ в любом проекте по трейдингу, до данных
и кода. Если на выбранном горизонте требуемая точность выше 60-65%, задача почти
наверняка нерешаема, и никакое усложнение модели этого не изменит.

Модель простая: сделка захватывает долю k среднего абсолютного движения за
горизонт. При точности p ожидание = (2p - 1) * k * |move| - cost. Отсюда
p_безубыт = 0.5 * (1 + cost / (k * |move|)).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import polars as pl

from download import load
from footprint import bars

# Сценарии издержек round-trip на Binance USDT-M futures, б.п.
COSTS = {
    "тейкер/тейкер (10 б.п.)": 10.0,
    "лимит вход + тейкер выход (7 б.п.)": 7.0,
    "лимит/лимит (4 б.п.)": 4.0,
    # Для сравнения: биржевой фьючерс МОЕХ, где комиссия почти отсутствует.
    "фьючерс МОЕХ (1.5 б.п.)": 1.5,
}
CAPTURE = 0.6  # реалистично захватываемая доля движения


def move_scale(trades: pl.DataFrame, horizons_min: tuple[int, ...]) -> pl.DataFrame:
    """Среднее абсолютное движение цены за каждый горизонт, в б.п."""
    m = bars(trades, "1m").select("ts", "close")
    rows = []
    for h in horizons_min:
        r = (
            m.select(((pl.col("close").shift(-h) / pl.col("close") - 1) * 10_000).abs())
            .drop_nulls()
            .to_series()
            .to_numpy()
        )
        rows.append({"горизонт_мин": h, "средн_|движение|_бп": round(float(r.mean()), 1)})
    return pl.DataFrame(rows)


def breakeven(scale: pl.DataFrame, capture: float = CAPTURE) -> pl.DataFrame:
    out = scale.clone()
    for name, cost in COSTS.items():
        out = out.with_columns(
            (
                0.5
                * (1 + cost / (capture * pl.col("средн_|движение|_бп")))
            ).alias(name)
        )
    return out.with_columns(
        [pl.col(c).round(3) for c in COSTS]
    )


def plot(be: pl.DataFrame, path: str) -> None:
    h = be["горизонт_мин"].to_numpy()
    fig, ax = plt.subplots(figsize=(9, 5.2))
    for name in COSTS:
        p = be[name].to_numpy() * 100
        p = np.where(p > 100, np.nan, p)
        ax.plot(h, p, marker="o", label=name)

    ax.axhline(55, color="grey", ls="--", lw=1)
    ax.text(h[0], 55.6, "55% — уже очень хороший результат", fontsize=9, color="grey")
    ax.axhline(65, color="firebrick", ls="--", lw=1)
    ax.text(h[0], 65.6, "65% — практически недостижимо", fontsize=9, color="firebrick")

    ax.set_xscale("log")
    ax.set_xticks(h)
    ax.set_xticklabels([str(x) for x in h])
    ax.set_xlabel("горизонт удержания, минут")
    ax.set_ylabel("требуемая точность, %")
    ax.set_title(
        "BTCUSDT: какая точность нужна для безубыточности\n"
        f"(захват {int(CAPTURE * 100)}% движения, июнь–сентябрь 2026)"
    )
    ax.set_ylim(45, 100)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


if __name__ == "__main__":
    horizons = (1, 5, 15, 30, 60, 120, 240, 480, 1440)
    trades = load("BTCUSDT", "2026-06-01", "2026-09-10")
    scale = move_scale(trades, horizons)
    be = breakeven(scale)

    with pl.Config(tbl_cols=-1, tbl_width_chars=200):
        print(be)

    out = "../../reports/breakeven.png"
    import os

    os.makedirs("../../reports", exist_ok=True)
    plot(be, out)
    print(f"\nграфик: {os.path.abspath(out)}")
