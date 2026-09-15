"""Как долго живёт информация в потоке ордеров.

Пробел, который закрывает этот модуль. Мы посчитали издержки (1.2 б.п. на Si),
посчитали мощность проверки (20 сессий хватит) и вывели оптимальный горизонт
h* = (2c/ρσ₁)², из которого следует: чем слабее сигнал, тем длиннее удержание,
и для реалистичной силы это десятки минут — часы. Но сила сигнала ρ входила туда
как предположение, а не как измеренная величина.

Между двумя выводами проекта есть противоречие, и оно ключевое. Издержки гонят
нас на горизонт 30-240 минут. А поток ордеров — самый короткоживущий класс
информации, который существует: агрессия в стакане разрешается за секунды и
минуты. Если ρ потока ордеров затухает до нуля раньше, чем издержки перестают
съедать движение, то пятая гипотеза мертва структурно — так же, как четыре
предыдущие, и это можно узнать до октября, а не после.

Замер идёт на крипте, и это осознанный выбор. Нам нужна не торгуемость BTCUSDT
(она уже закрыта комиссией), а форма кривой затухания — свойство микроструктуры.
По Si есть одна сессия, по BTCUSDT — 102 дня с настоящим BUYSELL. Форма
переносится качественно, уровень — нет; поэтому выводы формулируются про форму.

Что именно измеряется: ρ(w, h) — корреляция между дисбалансом потока за
последние w минут и последующим движением цены за h минут. Порог выживания
ρ_треб(h) = c / σ_h, где c — круговые издержки. Сигнал имеет право на жизнь
только там, где измеренная ρ выше требуемой.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from footprint import bars

# Окна накопления сигнала и горизонты удержания, минуты.
WINDOWS = (1, 5, 15, 60)
HORIZONS = (1, 5, 15, 30, 60, 120, 240)

# Круговые издержки для порога выживания, б.п.
COSTS = {"Si МОЕХ": 1.2, "крипта тейкер": 10.0}


def minute_bars(symbol: str, start: str, end: str) -> pl.DataFrame:
    """Минутные бары с дельтой за длинный период, по одному дню за раз.

    Тики целиком в память не влезают (102 дня это 165 млн сделок), а бары
    занимают пустяки: около 147 тысяч строк.

    Кэш читается с диска напрямую, без обращения к сети: замер должен быть
    воспроизводимым и не зависеть от доступности Binance.
    """
    from download import DATA_DIR, day_list

    cache = DATA_DIR / "futures" / symbol
    frames = []
    for day in day_list(start, end):
        path = cache / f"{day}.parquet"
        if not path.exists():
            continue
        trades = pl.read_parquet(path)
        frames.append(bars(trades, "1m").select("ts", "open", "close", "vol", "delta"))

    if not frames:
        raise FileNotFoundError(f"нет тиков {symbol} {start}..{end}")
    return pl.concat(frames).unique("ts").sort("ts")


def add_signals(df: pl.DataFrame, windows: tuple[int, ...] = WINDOWS) -> pl.DataFrame:
    """Дисбаланс потока за последние w минут: только прошлое, включая текущий бар.

    Нормировка на объём того же окна обязательна: без неё признак меряет
    активность рынка, а не перекос, и корреляция с движением появляется из-за
    связи волатильности с объёмом, а не из-за направления.
    """
    return df.with_columns(
        [
            (
                pl.col("delta").rolling_sum(w)
                / pl.col("vol").rolling_sum(w).clip(lower_bound=1e-9)
            ).alias(f"ofi_{w}")
            for w in windows
        ]
    )


def add_forward(df: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> pl.DataFrame:
    """Движение от входа по open(t+1) до выхода по open(t+1+h), в б.п.

    Вход сдвинут на бар вперёд намеренно: сигнал использует close текущего бара,
    поэтому исполниться раньше следующего открытия он не может.
    """
    entry = pl.col("open").shift(-1)
    return df.with_columns(
        [
            ((pl.col("open").shift(-(1 + h)) / entry - 1) * 10_000).alias(f"fwd_{h}")
            for h in horizons
        ]
    )


def _rho_ci(
    x: np.ndarray, y: np.ndarray, days: np.ndarray, iters: int = 400, seed: int = 0
) -> tuple[float, float]:
    """ДИ95 для корреляции с ресемплингом целых дней.

    Горизонты перекрываются, соседние наблюдения почти одинаковы, поэтому
    обычная формула для corr дала бы интервал в разы уже настоящего.
    """
    rng = np.random.default_rng(seed)
    uniq = np.unique(days)
    idx_by_day = {d: np.flatnonzero(days == d) for d in uniq}
    out = np.empty(iters)
    for i in range(iters):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([idx_by_day[d] for d in pick])
        xs, ys = x[idx], y[idx]
        if xs.std() < 1e-12 or ys.std() < 1e-12:
            out[i] = 0.0
        else:
            out[i] = np.corrcoef(xs, ys)[0, 1]
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def surface(
    df: pl.DataFrame,
    windows: tuple[int, ...] = WINDOWS,
    horizons: tuple[int, ...] = HORIZONS,
) -> pl.DataFrame:
    """Таблица ρ по всем парам (окно сигнала, горизонт удержания)."""
    d = add_forward(add_signals(df, windows), horizons)
    d = d.with_columns(pl.col("ts").dt.date().alias("day"))

    rows = []
    for h in horizons:
        col = f"fwd_{h}"
        for w in windows:
            sig = f"ofi_{w}"
            sub = d.select(sig, col, "day").drop_nulls()
            if sub.height < 1000:
                continue
            x = sub[sig].to_numpy()
            y = sub[col].to_numpy()
            rho = float(np.corrcoef(x, y)[0, 1])
            lo, hi = _rho_ci(x, y, sub["day"].to_numpy())
            rows.append(
                {
                    "окно_мин": w,
                    "горизонт_мин": h,
                    "ρ": round(rho, 4),
                    "ДИ95_низ": round(lo, 4),
                    "ДИ95_верх": round(hi, 4),
                    "σ_гор_бп": round(float(y.std()), 1),
                    "наблюдений": sub.height,
                }
            )
    return pl.DataFrame(rows)


def thresholds(surf: pl.DataFrame, costs: dict[str, float] = COSTS) -> pl.DataFrame:
    """Требуемая ρ для безубыточности: ρ_треб = c / σ_h.

    Смысл: валовое на сделку примерно ρ·σ_h, издержки постоянны, значит сигнал
    окупается только при ρ выше этого отношения.
    """
    sigma = (
        surf.group_by("горизонт_мин")
        .agg(pl.col("σ_гор_бп").first())
        .sort("горизонт_мин")
    )
    best = (
        surf.group_by("горизонт_мин")
        .agg(
            pl.col("ρ").abs().max().alias("ρ_лучшая"),
            pl.col("окно_мин").sort_by(pl.col("ρ").abs()).last().alias("окно_лучшее"),
        )
        .sort("горизонт_мин")
    )
    out = sigma.join(best, on="горизонт_мин")
    for name, c in costs.items():
        out = out.with_columns(
            (pl.lit(c) / pl.col("σ_гор_бп")).round(4).alias(f"ρ_треб_{name}")
        )
    return out


def _mean_ci(
    values: np.ndarray, days: np.ndarray, iters: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """ДИ95 для среднего с ресемплингом целых дней."""
    rng = np.random.default_rng(seed)
    uniq = np.unique(days)
    by_day = {d: values[days == d] for d in uniq}
    means = np.empty(iters)
    for i in range(iters):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        means[i] = np.concatenate([by_day[d] for d in pick]).mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def conditional(
    df: pl.DataFrame,
    windows: tuple[int, ...] = WINDOWS,
    horizons: tuple[int, ...] = HORIZONS,
    quantiles: tuple[float, ...] = (0.90, 0.99),
    cost: float = COSTS["Si МОЕХ"],
) -> pl.DataFrame:
    """Экономика отбора крайних значений потока, а не среднего по всем барам.

    Корреляция по всей выборке — это среднее по 99% шума, и она обязана быть
    крошечной. Гипотеза проекта другая: торговать только редкие события, где
    поток аномально односторонний. Здесь и проверяется, помогает ли отбор.

    Направление сделки берётся против агрессора, потому что именно такой знак
    показал безусловный замер: после агрессивных покупок цена в среднем идёт
    вниз. Это ровно то, что описывал Денис словами «покупаешь — рынок идёт вниз».

    Порог квантиля скользящий и сдвинут на бар назад: иначе отбор событий
    использовал бы будущее распределение.
    """
    d = add_forward(add_signals(df, windows), horizons)
    d = d.with_columns(pl.col("ts").dt.date().alias("day"))

    lookback = 1440  # сутки минутных баров
    for w in windows:
        for q in quantiles:
            d = d.with_columns(
                pl.col(f"ofi_{w}")
                .abs()
                .rolling_quantile(q, window_size=lookback, min_samples=lookback // 2)
                .shift(1)
                .alias(f"thr_{w}_{q}")
            )

    rows = []
    for w in windows:
        for q in quantiles:
            for h in horizons:
                col = f"fwd_{h}"
                sub = (
                    d.filter(
                        (pl.col(f"ofi_{w}").abs() > pl.col(f"thr_{w}_{q}"))
                        & pl.col(col).is_not_null()
                    )
                    # Против агрессора: знак потока с минусом.
                    .select(
                        (-pl.col(f"ofi_{w}").sign() * pl.col(col)).alias("r"), "day"
                    )
                )
                if sub.height < 200:
                    continue
                r = sub["r"].to_numpy()
                lo, hi = _mean_ci(r, sub["day"].to_numpy())
                rows.append(
                    {
                        "окно_мин": w,
                        "квантиль": q,
                        "горизонт_мин": h,
                        "событий": sub.height,
                        "валовое_бп": round(float(r.mean()), 2),
                        "нетто_бп": round(float(r.mean()) - cost, 2),
                        "ДИ95_низ": round(lo, 2),
                        "ДИ95_верх": round(hi, 2),
                    }
                )
    return pl.DataFrame(rows).sort("нетто_бп", descending=True)


def stability(
    df: pl.DataFrame,
    windows: tuple[int, ...] = (15, 60),
    horizons: tuple[int, ...] = (60, 120),
    q: float = 0.90,
) -> pl.DataFrame:
    """Устойчивость эффекта по месяцам рядом с режимом рынка.

    Это главная проверка, и она важнее величины эффекта. Средняя по всей
    выборке отвечает на вопрос «работало ли это в прошлом в среднем», а
    торговать придётся в одном конкретном режиме. Если знак эффекта меняется
    вместе с режимом, то накопление данных внутри одного месяца не приближает
    к ответу: проверка на 20 сессиях измерит один режим и выдаст уверенный
    результат, который развернётся при смене обстановки.

    Порог квантиля здесь берётся по всей выборке, а не скользящий: цель —
    сравнить месяцы между собой на одинаковом определении события.
    """
    d = add_forward(add_signals(df, windows), horizons)
    d = d.with_columns(
        pl.col("ts").dt.strftime("%Y-%m").alias("месяц"),
        pl.col("ts").dt.date().alias("day"),
    )

    regime = (
        d.group_by("месяц")
        .agg(
            ((pl.col("close").last() / pl.col("close").first() - 1) * 100)
            .round(1)
            .alias("изменение_%"),
            (pl.col("close").std() / pl.col("close").mean() * 100)
            .round(2)
            .alias("разброс_%"),
            pl.col("day").n_unique().alias("дней"),
        )
        .sort("месяц")
    )

    rows = []
    for w in windows:
        thr = d.select(pl.col(f"ofi_{w}").abs().quantile(q)).item()
        for h in horizons:
            sub = d.filter(
                (pl.col(f"ofi_{w}").abs() > thr) & pl.col(f"fwd_{h}").is_not_null()
            ).select(
                "месяц", (-pl.col(f"ofi_{w}").sign() * pl.col(f"fwd_{h}")).alias("r")
            )
            g = sub.group_by("месяц").agg(
                pl.col("r").mean().round(2).alias("бп"), pl.len().alias("событий")
            )
            for row in g.iter_rows(named=True):
                rows.append({"окно_мин": w, "горизонт_мин": h, **row})

    return pl.DataFrame(rows).join(regime, on="месяц").sort(
        "окно_мин", "горизонт_мин", "месяц"
    )


def plot_stability(stab: pl.DataFrame, path: str, cost: float = COSTS["Si МОЕХ"]) -> None:
    """Эффект по месяцам против режима рынка: видно, что знак не постоянен."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    months = sorted(stab["месяц"].unique().to_list())
    configs = (
        stab.select("окно_мин", "горизонт_мин").unique().sort("окно_мин", "горизонт_мин")
    )
    x = np.arange(len(months))
    width = 0.8 / configs.height

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(10, 7.5), sharex=True, height_ratios=[2, 1]
    )

    for i, (w, h) in enumerate(configs.iter_rows()):
        vals = [
            stab.filter(
                (pl.col("окно_мин") == w)
                & (pl.col("горизонт_мин") == h)
                & (pl.col("месяц") == m)
            )["бп"].to_list()
            for m in months
        ]
        vals = [v[0] if v else 0.0 for v in vals]
        ax.bar(x + i * width - 0.4 + width / 2, vals, width,
               label=f"поток {w} мин, удержание {h} мин")

    ax.axhline(0, color="black", lw=1)
    ax.axhline(cost, color="firebrick", ls="--", lw=1.2)
    ax.text(-0.45, cost + 0.15, f"издержки круга Si {cost} б.п.",
            fontsize=9, color="firebrick")
    ax.set_ylabel("валовое на сделку, б.п.")
    ax.set_title(
        "Торговля против агрессивного потока: знак эффекта зависит от режима\n"
        "BTCUSDT, отбор 10% баров с самым односторонним потоком"
    )
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    reg = stab.group_by("месяц").agg(
        pl.col("изменение_%").first(), pl.col("разброс_%").first()
    ).sort("месяц")
    ax2.bar(x, reg["изменение_%"].to_numpy(), 0.5, color="steelblue",
            label="изменение цены за месяц, %")
    ax2.plot(x, reg["разброс_%"].to_numpy(), "o-", color="darkorange",
             label="внутримесячный разброс, %")
    ax2.axhline(0, color="black", lw=1)
    ax2.set_xticks(x)
    ax2.set_xticklabels(months)
    ax2.set_ylabel("режим рынка")
    ax2.legend(fontsize=9)
    ax2.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=150)


def plot(surf: pl.DataFrame, thr: pl.DataFrame, path: str) -> None:
    """Кривые затухания против порогов выживания: видно, есть ли пересечение."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))

    for w in sorted(surf["окно_мин"].unique().to_list()):
        s = surf.filter(pl.col("окно_мин") == w).sort("горизонт_мин")
        ax.plot(
            s["горизонт_мин"].to_numpy(),
            s["ρ"].abs().to_numpy(),
            marker="o",
            label=f"поток за {w} мин",
        )

    styles = {"Si МОЕХ": ("firebrick", "--"), "крипта тейкер": ("black", ":")}
    for name in COSTS:
        col = f"ρ_треб_{name}"
        color, ls = styles.get(name, ("grey", "--"))
        ax.plot(
            thr["горизонт_мин"].to_numpy(),
            thr[col].to_numpy(),
            color=color,
            ls=ls,
            lw=2,
            label=f"порог выживания, {name}",
        )

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks(HORIZONS)
    ax.set_xticklabels([str(x) for x in HORIZONS])
    ax.set_xlabel("горизонт удержания, минут")
    ax.set_ylabel("сила сигнала ρ (log)")
    ax.set_title(
        "Затухание информации в потоке ордеров против порога издержек\n"
        "BTCUSDT, 102 дня, настоящая сторона агрессора"
    )
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(path, dpi=150)


if __name__ == "__main__":
    import os
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    start = sys.argv[2] if len(sys.argv) > 2 else "2026-06-01"
    end = sys.argv[3] if len(sys.argv) > 3 else "2026-09-10"

    print(f"=== {symbol}: минутные бары {start}..{end} ===")
    df = minute_bars(symbol, start, end)
    print(f"{df.height:,} баров, {df['ts'].dt.date().n_unique()} дней\n")

    surf = surface(df)
    print("=== Сила сигнала ρ по окнам и горизонтам ===")
    with pl.Config(tbl_rows=60, tbl_width_chars=180):
        print(surf.pivot(on="окно_мин", index="горизонт_мин", values="ρ"))

    print("\n=== С доверительными интервалами ===")
    with pl.Config(tbl_rows=60, tbl_width_chars=180):
        print(surf)

    thr = thresholds(surf)
    print("\n=== Порог выживания против лучшей измеренной ρ ===")
    with pl.Config(tbl_cols=-1, tbl_width_chars=200):
        print(thr)

    cond = conditional(df)
    print("\n=== Отбор крайних значений потока: экономика против издержек Si ===")
    print(f"(издержки круга {COSTS['Si МОЕХ']} б.п. вычтены)")
    with pl.Config(tbl_rows=60, tbl_width_chars=180):
        print(cond)

    print(f"\nпроверено комбинаций: {cond.height} — поправка на перебор обязательна")

    stab = stability(df)
    print("\n=== Устойчивость по месяцам против режима рынка ===")
    with pl.Config(tbl_rows=40, tbl_width_chars=180):
        print(stab)

    pos = stab.filter(pl.col("бп") > 0).height
    print(f"\nмесяцев с положительным эффектом: {pos} из {stab.height}")

    os.makedirs("../../reports", exist_ok=True)
    out = os.path.abspath("../../reports/decay.png")
    plot(surf, thr, out)
    out2 = os.path.abspath("../../reports/decay_stability.png")
    plot_stability(stab, out2)
    print(f"\nграфики: {out}\n          {out2}")
