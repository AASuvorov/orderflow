"""Порог безубыточности на фьючерсах МОЕХ против крипты.

Тот же расчёт, что для Binance, но на реальных биржевых сборах МОЕХ и реальном
шаге цены. Задача — понять, на каких контрактах и горизонтах издержки перестают
съедать движение, то есть где кластерный подход вообще имеет право на жизнь.
"""

from __future__ import annotations

import polars as pl

from moex import candles, spec

# Комиссия брокера сверх биржевого сбора, руб за контракт за сторону.
BROKER_FEE_RUB = 1.0
CAPTURE = 0.6
HORIZONS_MIN = (1, 5, 15, 30, 60, 120, 240)


def cost_bps(secid: str, broker_rub: float = BROKER_FEE_RUB) -> dict | None:
    """Издержки полного круга: спред плюс сборы биржи и брокера с двух сторон."""
    s = spec(secid)
    if not s:
        return None

    value = s["стоимость_контракта_руб"]
    # Скальперский сбор берётся за внутридневной оборот целиком, а не за сторону.
    exch_round = s["сбор_скальпера_руб"] or 2 * s["сбор_биржи_руб"]
    fees_rub = exch_round + 2 * broker_rub

    return {
        **s,
        "сборы_круг_бп": round(fees_rub / value * 10_000, 3),
        "издержки_круг_бп": round(s["спред_бп"] + fees_rub / value * 10_000, 3),
    }


def move_scale(secid: str, start: str, end: str) -> pl.DataFrame:
    m = candles(secid, start, end).select("ts", "close")
    rows = []
    for h in HORIZONS_MIN:
        r = (
            m.select(((pl.col("close").shift(-h) / pl.col("close") - 1) * 10_000).abs())
            .drop_nulls()
            .to_series()
            .to_numpy()
        )
        rows.append({"горизонт_мин": h, "движение_бп": round(float(r.mean()), 1)})
    return pl.DataFrame(rows)


def breakeven(secid: str, start: str, end: str, capture: float = CAPTURE) -> pl.DataFrame:
    c = cost_bps(secid)
    if c is None:
        raise ValueError(f"нет спецификации {secid}")
    return (
        move_scale(secid, start, end)
        .with_columns(
            pl.lit(secid).alias("secid"),
            pl.lit(c["издержки_круг_бп"]).alias("издержки_бп"),
        )
        .with_columns(
            (
                0.5 * (1 + pl.col("издержки_бп") / (capture * pl.col("движение_бп")))
                * 100
            )
            .round(1)
            .alias("нужна_точность_%")
        )
        .select("secid", "горизонт_мин", "движение_бп", "издержки_бп", "нужна_точность_%")
    )


def plot(tables: dict[str, pl.DataFrame], path: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(10, 5.8))
    for secid, t in tables.items():
        h = t["горизонт_мин"].to_numpy()
        p = t["нужна_точность_%"].to_numpy().astype(float)
        p = np.where(p > 100, np.nan, p)
        ax.plot(h, p, marker="o", label=f"{secid}, издержки {t['издержки_бп'][0]} б.п.")

    # Крипта для сравнения: BTCUSDT с тейкерскими комиссиями 10 б.п.
    btc_h = np.array([5, 15, 30, 60, 120, 240])
    btc_move = np.array([8.0, 13.8, 19.6, 27.8, 39.4, 55.7])
    btc = 0.5 * (1 + 10.0 / (CAPTURE * btc_move)) * 100
    ax.plot(btc_h, np.where(btc > 100, np.nan, btc), "k--", marker="x",
            label="BTCUSDT Binance, издержки 10 б.п.")

    ax.axhline(55, color="grey", ls=":", lw=1.2)
    ax.text(1.05, 55.4, "55% — реально достижимо", fontsize=9, color="grey")
    ax.axhline(65, color="firebrick", ls=":", lw=1.2)
    ax.text(1.05, 65.4, "65% — практически недостижимо", fontsize=9, color="firebrick")

    ax.set_xscale("log")
    ax.set_xticks(HORIZONS_MIN)
    ax.set_xticklabels([str(x) for x in HORIZONS_MIN])
    ax.set_xlabel("горизонт удержания, минут")
    ax.set_ylabel("требуемая точность, %")
    ax.set_title(
        "Фьючерсы МОЕХ против крипты: где издержки перестают съедать движение\n"
        f"(захват {int(CAPTURE * 100)}% движения, июнь–сентябрь 2026)"
    )
    ax.set_ylim(45, 100)
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


if __name__ == "__main__":
    import os
    import sys

    symbols = sys.argv[1:] or ["SiU6", "CRU6", "BRV6", "MXU6", "GDU6", "MMU6"]
    start, end = "2026-06-15", "2026-09-11"

    print("=== Издержки полного круга ===")
    costs = []
    for s in symbols:
        c = cost_bps(s)
        if c:
            costs.append(c)
    with pl.Config(tbl_cols=-1, tbl_width_chars=200):
        print(
            pl.DataFrame(costs).select(
                "secid", "name", "стоимость_контракта_руб", "спред_бп",
                "сбор_скальпера_руб", "сборы_круг_бп", "издержки_круг_бп",
            )
        )

    tables = {}
    for s in symbols:
        try:
            tables[s] = breakeven(s, start, end)
            print(f"{s}: свечи загружены")
        except Exception as exc:
            print(f"{s}: пропуск ({exc})")

    print("\n=== Требуемая точность по горизонтам ===")
    allt = pl.concat(tables.values())
    with pl.Config(tbl_rows=60, tbl_width_chars=160):
        print(allt.pivot(on="secid", index="горизонт_мин", values="нужна_точность_%"))

    os.makedirs("../../reports", exist_ok=True)
    out = os.path.abspath("../../reports/moex_breakeven.png")
    plot(tables, out)
    print(f"\nграфик: {out}")
