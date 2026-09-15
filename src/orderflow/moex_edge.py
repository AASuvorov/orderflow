"""Есть ли на Si предсказуемость, достаточная при издержках 0.59 б.п.

Футпринт здесь не нужен: сначала выясняем, достижима ли вообще требуемая
точность 54-56% на горизонтах 15-60 минут, а для этого хватает бесплатной
истории минутных свечей. Если да — тиковые кластеры будут улучшением уже
работающей базы. Если нет — незачем копить тики.

Всё внутри дня: вход и выход в одной сессии, никакого переноса через ночь,
иначе результат определяется гэпами, а не сигналом.
"""

from __future__ import annotations

import itertools

import numpy as np
import polars as pl

from edge import _bootstrap_ci
from moex import candles
from moex_feasibility import cost_bps

TRAIN = ("2026-06-15", "2026-08-15")
HOLDOUT = ("2026-08-16", "2026-09-11")
LOOKBACK = 600  # бары для скользящих порогов, примерно одна сессия


def prepare(secid: str, start: str, end: str) -> pl.DataFrame:
    """Свечи с разметкой по сессиям и базовыми производными."""
    return (
        candles(secid, start, end)
        .with_columns(pl.col("ts").dt.date().alias("day"))
        .with_columns(pl.int_range(pl.len()).over("day").alias("bar_in_day"))
    )


def add_signal(df: pl.DataFrame, family: str, k: int, q: float) -> pl.DataFrame:
    """Сигнал строится только по прошлому: порог сдвинут на бар назад."""
    d = df.with_columns(
        ((pl.col("close") / pl.col("close").shift(k).over("day") - 1) * 10_000)
        .alias("ret_k"),
        pl.col("high").rolling_max(k).over("day").shift(1).alias("hh"),
        pl.col("low").rolling_min(k).over("day").shift(1).alias("ll"),
    ).with_columns(
        pl.col("ret_k")
        .abs()
        .rolling_quantile(q, window_size=LOOKBACK, min_samples=LOOKBACK // 4)
        .shift(1)
        .alias("thr"),
        pl.col("vol")
        .rolling_quantile(q, window_size=LOOKBACK, min_samples=LOOKBACK // 4)
        .shift(1)
        .alias("vol_thr"),
    )

    strong = pl.col("ret_k").abs() > pl.col("thr")
    direction = pl.when(pl.col("ret_k") > 0).then(1).otherwise(-1)

    if family == "momentum":
        sig = pl.when(strong).then(direction).otherwise(0)
    elif family == "reversal":
        sig = pl.when(strong).then(-direction).otherwise(0)
    elif family == "breakout":
        sig = (
            pl.when(pl.col("close") > pl.col("hh"))
            .then(1)
            .when(pl.col("close") < pl.col("ll"))
            .then(-1)
            .otherwise(0)
        )
    elif family == "vol_spike":
        sig = pl.when(pl.col("vol") > pl.col("vol_thr")).then(direction).otherwise(0)
    else:
        raise ValueError(family)

    return d.with_columns(sig.cast(pl.Int8).alias("signal"))


def evaluate(df: pl.DataFrame, horizon: int, cost: float) -> dict | None:
    """Вход по open следующего бара, выход через horizon, только внутри дня."""
    entry = pl.col("open").shift(-1).over("day")
    exit_ = pl.col("open").shift(-(1 + horizon)).over("day")

    ev = (
        df.with_columns(((exit_ / entry - 1) * 10_000).alias("fwd"))
        .filter((pl.col("signal") != 0) & pl.col("fwd").is_not_null())
        .select((pl.col("signal") * pl.col("fwd")).alias("r"), "day")
    )
    if ev.height < 100:
        return None

    r = ev["r"].to_numpy()
    net = r - cost
    return {
        "сделок": len(r),
        "брутто_бп": round(float(r.mean()), 2),
        "нетто_бп": round(float(net.mean()), 2),
        "точность_%": round(float((r > cost).mean()) * 100, 1),
        "_r": net,
        "_days": ev["day"].to_numpy(),
    }


def sweep(df: pl.DataFrame, cost: float) -> list[dict]:
    grid = {
        "family": ["momentum", "reversal", "breakout", "vol_spike"],
        "k": [10, 30],
        "horizon": [15, 60],
        "q": [0.90],
    }
    out = []
    for combo in itertools.product(*grid.values()):
        p = dict(zip(grid, combo))
        sig = add_signal(df, p["family"], p["k"], p["q"])
        res = evaluate(sig, p["horizon"], cost)
        if res:
            out.append({**p, **res})
    return out


def table(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    ).sort("нетто_бп", descending=True)


if __name__ == "__main__":
    import sys

    secid = sys.argv[1] if len(sys.argv) > 1 else "SiU6"
    cost = cost_bps(secid)["издержки_круг_бп"]
    print(f"{secid}: издержки круга {cost} б.п.")

    full = prepare(secid, TRAIN[0], HOLDOUT[1])
    as_str = pl.col("day").cast(pl.Utf8)
    tr = full.filter(as_str.is_between(pl.lit(TRAIN[0]), pl.lit(TRAIN[1])))
    ho = full.filter(as_str.is_between(pl.lit(HOLDOUT[0]), pl.lit(HOLDOUT[1])))
    print(f"train {tr.height:,} баров, holdout {ho.height:,} баров\n")

    rows = sweep(tr, cost)
    print(f"=== TRAIN: проверено {len(rows)} комбинаций ===")
    with pl.Config(tbl_rows=30, tbl_width_chars=160):
        print(table(rows))

    best = max(rows, key=lambda r: r["нетто_бп"])
    lo, hi = _bootstrap_ci(best["_r"], best["_days"])
    print(
        f"\nлучшая: {best['family']} k={best['k']} H={best['horizon']} -> "
        f"нетто {best['нетто_бп']} б.п. (ДИ95 {lo:.2f}..{hi:.2f}), n={best['сделок']}"
    )

    print("\n--- Единственная проверка на holdout ---")
    sig = add_signal(ho, best["family"], best["k"], best["q"])
    res = evaluate(sig, best["horizon"], cost)
    if res is None:
        print("на holdout мало событий")
    else:
        lo, hi = _bootstrap_ci(res["_r"], res["_days"])
        total = float(np.sum(res["_r"]))
        print(
            f"нетто {res['нетто_бп']} б.п. (ДИ95 {lo:.2f}..{hi:.2f}), "
            f"n={res['сделок']}, точность {res['точность_%']}%, "
            f"суммарно {total:.0f} б.п. за период"
        )
