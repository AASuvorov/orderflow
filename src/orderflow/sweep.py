"""Перебор гипотез на train + однократная проверка лучшей на holdout.

Дисциплина, без которой перебор бессмыслен:
  * holdout не участвует в отборе и смотрится ОДИН раз;
  * число проверенных комбинаций печатается, чтобы было видно масштаб
    проблемы множественных сравнений (при 60 тестах лучший результат почти
    наверняка случаен, если он не сильно выше порога);
  * издержки вычитаются везде.
"""

from __future__ import annotations

import itertools

import numpy as np
import polars as pl

from download import load
from edge import ROUND_TRIP_BPS, _bootstrap_ci, add_forward_returns
from footprint import build
from signals import FAMILIES

TRAIN = ("2026-06-01", "2026-07-31")
HOLDOUT = ("2026-08-01", "2026-09-10")

BARS_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96}
GRID = {
    "every": ["1m", "5m", "15m"],
    "family": ["absorption", "cluster"],
    "q": [0.95, 0.99],
    "horizon": [4, 12, 24],  # в барах, а не в минутах
}


def evaluate(
    df: pl.DataFrame, family: str, q: float, horizon: int, every: str
) -> dict | None:
    sig = FAMILIES[family](df, q=q, lookback=BARS_PER_DAY[every])
    sig = add_forward_returns(sig, (horizon,))
    col = f"fwd_{horizon}"

    ev = sig.filter((pl.col("signal") != 0) & pl.col(col).is_not_null()).select(
        (pl.col("signal") * pl.col(col)).alias("r"),
        pl.col("ts").dt.date().alias("day"),
    )
    if ev.height < 150:
        return None

    r = ev["r"].to_numpy()
    base = sig.select(pl.col(col).drop_nulls()).to_series().to_numpy()
    return {
        "every": every,
        "family": family,
        "q": q,
        "H_бар": horizon,
        "сделок": len(r),
        "средн_бп": round(float(r.mean()), 2),
        "чистыми_бп": round(float(r.mean()) - ROUND_TRIP_BPS, 2),
        "доля_плюс": round(float((r > 0).mean()), 3),
        "базовая_|бп|": round(float(np.abs(base).mean()), 1),
        "_r": r,
        "_days": ev["day"].to_numpy(),
    }


def run(period: tuple[str, str], label: str) -> list[dict]:
    trades = load("BTCUSDT", *period)
    cache = {e: build(trades, every=e, price_step=10.0) for e in GRID["every"]}
    del trades

    results = []
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, combo))
        res = evaluate(cache[p["every"]], p["family"], p["q"], p["horizon"], p["every"])
        if res:
            results.append(res)

    print(f"\n=== {label}: {period[0]}..{period[1]}, проверено {len(results)} комбинаций")
    return results


def table(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    ).sort("чистыми_бп", descending=True)


if __name__ == "__main__":
    train = run(TRAIN, "TRAIN")
    with pl.Config(tbl_cols=-1, tbl_rows=40, tbl_width_chars=200):
        print(table(train))

    best = max(train, key=lambda r: r["средн_бп"])
    lo, hi = _bootstrap_ci(best["_r"], best["_days"])
    print(
        f"\nЛучшая на train: {best['family']} {best['every']} q={best['q']} "
        f"H={best['H_бар']} -> {best['средн_бп']} б.п. (ДИ95 {lo:.2f}..{hi:.2f}), "
        f"n={best['сделок']}"
    )

    print("\n--- Единственная проверка на holdout ---")
    ho_trades = load("BTCUSDT", *HOLDOUT)
    ho_df = build(ho_trades, every=best["every"], price_step=10.0)
    ho = evaluate(ho_df, best["family"], best["q"], best["H_бар"], best["every"])
    if ho is None:
        print("на holdout слишком мало событий")
    else:
        lo, hi = _bootstrap_ci(ho["_r"], ho["_days"])
        print(
            f"holdout: {ho['средн_бп']} б.п. (ДИ95 {lo:.2f}..{hi:.2f}), "
            f"n={ho['сделок']}, доля_плюс={ho['доля_плюс']}, "
            f"чистыми {ho['чистыми_бп']} б.п."
        )
