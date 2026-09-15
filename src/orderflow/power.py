"""Сколько данных нужно, чтобы отличить настоящее преимущество от случайности.

Это следовало посчитать до начала сбора, а не после. Расчёт отвечает на два
вопроса, от которых зависит весь план: какое преимущество мы вообще способны
заметить на имеющемся объёме данных и сколько торговых дней нужно накопить,
чтобы проверка имела смысл.

Логика. Стратегия делает k сделок в день, каждая приносит чистыми δ базисных
пунктов при разбросе доходности σ. Тогда за D дней:
    сигнал  = δ · k · D
    шум     = σ · sqrt(k · D)
    t-статистика = δ · sqrt(k · D) / σ
Чтобы уверенно увидеть преимущество, нужно t около 2.8 — это 5% уровень
значимости при 80% мощности. Отсюда:
    D = (2.8 · σ / δ)² / k

Важная оговорка про σ. Внутри дня доходности зависимы: новости, режим
волатильности, тренд действуют на все сделки сессии сразу. Поэтому честная
оценка шума берётся не по отдельным сделкам, а по дневным итогам — так же, как
в bootstrap по дневным блокам в edge.py. Иначе мощность оказывается завышена в
разы, и проверка «находит» преимущество там, где его нет.
"""

from __future__ import annotations

import polars as pl

from moex import candles

HORIZONS = [1, 5, 15]  # минуты удержания позиции
COST_BP = 1.5  # круговые издержки на ликвидных фьючерсах МОЕХ
EDGES_BP = [0.5, 1.0, 2.0, 5.0]  # чистое преимущество на сделку
TRADES_PER_DAY = [5, 20, 50]
T_NEEDED = 2.8  # 5% значимость при 80% мощности


def minute_vol(secid: str, start: str, end: str) -> pl.DataFrame:
    """Разброс доходности на разных горизонтах удержания, в базисных пунктах."""
    df = candles(secid, start, end, interval=1)
    df = df.with_columns(pl.col("ts").dt.date().alias("день"))

    rows = []
    for h in HORIZONS:
        # Сдвиг внутри дня: перенос через ночь дал бы гэп, а не результат сделки.
        r = (
            df.with_columns(
                (
                    pl.col("close").shift(-h).over("день") / pl.col("close") - 1
                ).alias("ret")
            )
            .drop_nulls("ret")
            .with_columns((pl.col("ret") * 10_000).alias("bp"))
        )
        rows.append(
            {
                "горизонт_мин": h,
                "σ_бп": round(r["bp"].std(), 2),
                "наблюдений": r.height,
                "дней": r["день"].n_unique(),
            }
        )
    return pl.DataFrame(rows)


def dependence(secid: str, start: str, end: str, h: int = 5) -> dict:
    """Во сколько раз реальный шум дневного результата больше теоретического.

    Формула D = (2.8·σ/δ)²/k молча предполагает, что сделки внутри дня
    независимы. Это неправда: режим волатильности, тренд и новости действуют на
    все сделки сессии сразу, поэтому их результаты складываются не как случайные
    величины, а с положительной корреляцией. Меряем поправку на реальных данных:
    берём механическую стратегию, считаем её дневные итоги и сравниваем их
    разброс с тем, что предсказывает формула.
    """
    df = candles(secid, start, end, interval=1).with_columns(
        pl.col("ts").dt.date().alias("день")
    )

    # Неперекрывающиеся блоки по h минут внутри дня: одна сделка на блок.
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(h).over("день") - 1).alias("прошлое"),
        (pl.col("close").shift(-h).over("день") / pl.col("close") - 1).alias("будущее"),
        (pl.col("ts").dt.hour() * 60 + pl.col("ts").dt.minute()).alias("минута"),
    ).filter((pl.col("минута") % h == 0))

    # Инерция как пример: направление прошлого движения переносим на следующее.
    trades = (
        df.drop_nulls(["прошлое", "будущее"])
        .with_columns(
            (
                pl.col("прошлое").sign() * pl.col("будущее") * 10_000
            ).alias("сделка_бп")
        )
    )

    per_trade_sd = float(trades["сделка_бп"].std())
    daily = trades.group_by("день").agg(
        pl.col("сделка_бп").sum().alias("день_бп"),
        pl.len().alias("сделок"),
    )
    k = float(daily["сделок"].mean())
    observed = float(daily["день_бп"].std())
    theoretical = per_trade_sd * k**0.5

    return {
        "горизонт_мин": h,
        "сделок_в_день": round(k),
        "σ_сделки_бп": round(per_trade_sd, 2),
        "шум_дня_теория_бп": round(theoretical, 1),
        "шум_дня_факт_бп": round(observed, 1),
        "во_сколько_раз_хуже": round(observed / theoretical, 2),
        "дней": daily.height,
    }


def optimal_horizon(sigma_1min: float, cost_bp: float = COST_BP) -> pl.DataFrame:
    """На каком горизонте удержания издержки мешают меньше всего.

    Ключевое соотношение: разброс цены растёт как корень из времени, а издержки
    на сделку постоянны. Значит на коротком горизонте комиссия съедает почти всё
    движение, а на длинном становится незаметной — но и сделок в день меньше.

    Пусть сигнал предсказывает долю ρ от стандартного отклонения (величина
    качества сигнала, для реальных сигналов это 0.05–0.2). Тогда за день:
        результат = (T / h) · (ρ · σ₁ · √h − c)
    Максимум по h даёт h* = (2c / (ρ · σ₁))². Отсюда видно главное: чем слабее
    сигнал, тем длиннее должен быть горизонт.
    """
    session_min = 840  # утренняя плюс вечерняя сессия FORTS
    rows = []
    for rho in (0.05, 0.10, 0.15, 0.20):
        h_opt = (2 * cost_bp / (rho * sigma_1min)) ** 2
        h_opt = max(1.0, h_opt)
        sigma_h = sigma_1min * h_opt**0.5
        k = session_min / h_opt
        gross = rho * sigma_h
        net_day = k * (gross - cost_bp)
        rows.append(
            {
                "качество_сигнала_ρ": rho,
                "горизонт_мин": round(h_opt),
                "σ_на_горизонте_бп": round(sigma_h, 1),
                "валовое_на_сделку_бп": round(gross, 2),
                "издержки_доля_σ": round(cost_bp / sigma_h, 2),
                "сделок_в_день": round(k, 1),
                "результат_дня_бп": round(net_day, 1),
            }
        )
    return pl.DataFrame(rows)


def required_days_daily(daily_sd_bp: float, k: int) -> pl.DataFrame:
    """Честный расчёт: шум берём из фактического разброса дневных итогов."""
    rows = []
    for edge in EDGES_BP:
        per_day = edge * k
        rows.append(
            {
                "чистое_преимущество_бп": edge,
                "результат_дня_бп": round(per_day, 1),
                "нужно_дней": round((T_NEEDED * daily_sd_bp / per_day) ** 2),
            }
        )
    return pl.DataFrame(rows)


def required_days(sigma_bp: float) -> pl.DataFrame:
    """Сколько торговых дней нужно для надёжного вывода при разных предпосылках."""
    rows = []
    for edge in EDGES_BP:
        row = {"чистое_преимущество_бп": edge}
        for k in TRADES_PER_DAY:
            days = (T_NEEDED * sigma_bp / edge) ** 2 / k
            row[f"сделок_в_день_{k}"] = round(days)
        rows.append(row)
    return pl.DataFrame(rows)


def detectable_edge(sigma_bp: float, days: int, k: int) -> float:
    """Обратная задача: какое минимальное преимущество различимо на объёме data."""
    return T_NEEDED * sigma_bp / (days * k) ** 0.5


if __name__ == "__main__":
    secid = "SiU6"
    start, end = "2026-05-01", "2026-09-11"

    print(f"=== Разброс доходности {secid}, {start}..{end} ===")
    vol = minute_vol(secid, start, end)
    print(vol)

    sigma = float(vol.filter(pl.col("горизонт_мин") == 1)["σ_бп"][0])
    print(f"\nБерём σ = {sigma:.2f} бп на горизонте 1 минута")
    print(f"Круговые издержки на МОЕХ: {COST_BP} бп")
    print(
        f"Значит валовое преимущество должно превышать {COST_BP} бп, "
        "иначе чистое отрицательно"
    )

    print("\n=== Сколько торговых дней нужно для вывода ===")
    with pl.Config(tbl_rows=20):
        print(required_days(sigma))

    print("\n=== Поправка на зависимость сделок внутри дня ===")
    dep = dependence(secid, start, end, h=5)
    for k_, v in dep.items():
        print(f"  {k_}: {v}")

    print("\n=== Честный расчёт по фактическому разбросу дневных итогов ===")
    print(required_days_daily(dep["шум_дня_факт_бп"], dep["сделок_в_день"]))

    print("\n=== Издержки против горизонта удержания ===")
    print(
        "Разброс цены растёт как корень из времени, издержки на сделку постоянны.\n"
        "ρ — какую долю стандартного отклонения предсказывает сигнал."
    )
    with pl.Config(tbl_cols=10):
        print(optimal_horizon(sigma))

    print("\n=== Что различимо на реально доступном объёме ===")
    for days, label in [(20, "20 сессий, середина октября"),
                        (60, "60 сессий, декабрь"),
                        (250, "250 сессий, год")]:
        for k in (20,):
            e = detectable_edge(sigma, days, k)
            print(
                f"{label}: различимо преимущество от {e:.2f} бп "
                f"при {k} сделках в день (нужно валовых {e + COST_BP:.2f} бп)"
            )
