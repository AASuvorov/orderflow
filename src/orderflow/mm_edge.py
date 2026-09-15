"""Экономика одного мейкерского исполнения: спред минус снос минус комиссия.

Логика. Мейкер всегда исполняется противоположно агрессору: агрессор продал по
биду — мейкер купил по биду. Прибыль такой заливки через время Δ равна

    PnL = полспреда - side_агрессора * (mid[t+Δ] - mid[t]) - комиссия

Второе слагаемое и есть adverse selection: агрессор в среднем прав на коротком
горизонте, и цена уходит против мейкера. Вопрос ровно один: остаётся ли что-то
положительное после вычета сноса и комиссии.

Мид восстанавливается из потока сделок: последняя цена покупки агрессора — это
аск, последняя цена продажи — бид. Для инструментов со спредом в несколько б.п.
такая аппроксимация достаточна.
"""

from __future__ import annotations

import polars as pl
import requests

from download import fetch_day

BOOK = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
MAKER_FEE_BPS = 2.0
MARKOUTS = (1, 5, 10, 60)  # секунды


def live_spread(symbol: str) -> float:
    raw = requests.get(BOOK, params={"symbol": symbol}, timeout=15).json()
    bid, ask = float(raw["bidPrice"]), float(raw["askPrice"])
    return (ask - bid) / ((ask + bid) / 2) * 10_000


def with_mid(trades: pl.DataFrame) -> pl.DataFrame:
    """Восстанавливает бид, аск и мид из потока сделок."""
    return (
        trades.with_columns(
            pl.when(pl.col("side") == 1).then(pl.col("price")).alias("ask"),
            pl.when(pl.col("side") == -1).then(pl.col("price")).alias("bid"),
        )
        .with_columns(pl.col("ask").forward_fill(), pl.col("bid").forward_fill())
        .drop_nulls(["ask", "bid"])
        .with_columns(((pl.col("ask") + pl.col("bid")) / 2).alias("mid"))
    )


def markouts(trades: pl.DataFrame, horizons: tuple[int, ...] = MARKOUTS) -> pl.DataFrame:
    """Средний снос цены против мейкера на каждом горизонте, в б.п."""
    df = with_mid(trades).select("ts", "qty", "side", "mid")
    lookup = df.select("ts", pl.col("mid").alias("mid_fwd"))

    rows = []
    for h in horizons:
        joined = (
            df.with_columns((pl.col("ts") + pl.duration(seconds=h)).alias("t_target"))
            .sort("t_target")
            .join_asof(
                lookup.sort("ts"),
                left_on="t_target",
                right_on="ts",
                strategy="backward",
            )
            .drop_nulls("mid_fwd")
            .with_columns(
                (
                    pl.col("side")
                    * (pl.col("mid_fwd") / pl.col("mid") - 1)
                    * 10_000
                ).alias("adverse")
            )
        )
        # Взвешивание по объёму: крупные заливки токсичнее, а мейкер
        # исполняется на них большим размером.
        w = (
            joined.select(
                (pl.col("adverse") * pl.col("qty")).sum() / pl.col("qty").sum()
            )
            .to_series()
            .item()
        )
        rows.append(
            {
                "Δ_сек": h,
                "снос_бп": round(
                    joined.select(pl.col("adverse").mean()).to_series().item(), 3
                ),
                "снос_взвеш_бп": round(w, 3),
            }
        )
    return pl.DataFrame(rows)


def viability(symbol: str, days: list[str], fee: float = MAKER_FEE_BPS) -> pl.DataFrame:
    frames = []
    for d in days:
        try:
            frames.append(fetch_day(symbol, d))
        except requests.HTTPError:
            continue
    trades = pl.concat(frames).sort("ts")

    half = live_spread(symbol) / 2
    mk = markouts(trades)

    return mk.with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.lit(round(half, 2)).alias("полспреда_бп"),
        pl.lit(len(trades)).alias("сделок"),
        (half - pl.col("снос_взвеш_бп") - fee).round(3).alias("нетто_вход_бп"),
        (half - pl.col("снос_взвеш_бп") - 2 * fee).round(3).alias("нетто_круг_бп"),
    ).select(
        "symbol", "сделок", "полспреда_бп", "Δ_сек",
        "снос_бп", "снос_взвеш_бп", "нетто_вход_бп", "нетто_круг_бп",
    )


def plot(res: pl.DataFrame, path: str, delta: int = 10) -> None:
    """Половина спреда против сноса и комиссии: видно, что съедает заработок."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    d = res.filter(pl.col("Δ_сек") == delta).sort("нетто_вход_бп", descending=True)
    syms = d["symbol"].to_list()
    x = np.arange(len(syms))

    fig, ax = plt.subplots(figsize=(10, 5.6))
    ax.bar(x, d["полспреда_бп"].to_numpy(), 0.55,
           label="получает мейкер: половина спреда", color="seagreen")
    ax.bar(x, -d["снос_взвеш_бп"].to_numpy(), 0.55,
           label=f"теряет: adverse selection ({delta} сек, взвеш. по объёму)",
           color="indianred")
    ax.bar(x, np.full(len(syms), -MAKER_FEE_BPS), 0.55,
           bottom=-d["снос_взвеш_бп"].to_numpy(), label="комиссия мейкера",
           color="darkred")
    ax.plot(x, d["нетто_вход_бп"].to_numpy(), "ko--", lw=1.5, label="итого на заливку")

    for xi, v in zip(x, d["нетто_вход_бп"].to_numpy()):
        ax.annotate(f"{v:.1f}", (xi, v), textcoords="offset points",
                    xytext=(0, -14), ha="center", fontsize=9)

    ax.axhline(0, color="black", lw=1)
    ax.set_xticks(x)
    ax.set_xticklabels(syms, rotation=20)
    ax.set_ylabel("б.п. на одну заливку")
    ax.set_title(
        "Маркет-мейкинг на Binance futures: снос съедает спред целиком\n"
        "инструменты с самым широким спредом, 28 авг – 10 сен 2026"
    )
    ax.legend(loc="lower left", fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


if __name__ == "__main__":
    import sys

    from download import day_list

    symbols = sys.argv[1:] or [
        "SNXXUSDT", "KORUUSDT", "ARKUSDT", "POWERUSDT", "TRUMPUSDT", "KOMAUSDT",
    ]
    days = day_list("2026-08-28", "2026-09-10")

    out = []
    for sym in symbols:
        try:
            out.append(viability(sym, days))
            print(f"{sym}: готово", flush=True)
        except Exception as exc:
            print(f"{sym}: пропуск ({exc})", flush=True)

    res = pl.concat(out)
    with pl.Config(tbl_rows=60, tbl_cols=-1, tbl_width_chars=200):
        print("\n=== Экономика мейкерской заливки ===")
        print(res)
        print("\n=== Сводка по горизонту 10 секунд ===")
        print(res.filter(pl.col("Δ_сек") == 10).sort("нетто_вход_бп", descending=True))

    import os

    os.makedirs("../../reports", exist_ok=True)
    res.write_parquet("../../reports/mm_edge.parquet")
    out = os.path.abspath("../../reports/mm_edge.png")
    plot(res, out)
    print(f"\nграфик: {out}")
