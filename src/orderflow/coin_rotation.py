"""Ротация монет с фиксацией на +10%: замер схемы, а не мнение о ней.

Проверяемое правило целиком: держим десять монет по десять долларов, любая
выросшая на 10% продаётся, выручка сразу уходит в следующую монету, и так по кругу
без остановки. Убыточные позиции не закрываются — правило про них молчит.

Почему это проверяется, а не отвергается с порога. Схема выглядит убедительно
именно потому, что каждая её сделка кажется выигрышной: продают только в плюс, а
значит «в среднем» плюс. Ошибка не в арифметике одной сделки, а в том, что
результат портфеля определяют сделки, которые не совершаются. Это видно только на
данных, поэтому здесь данные, а не рассуждение.

Что именно замеряется:
  · итог портфеля против трёх планок — рублёвого вклада под ставку ЦБ, простого
    удержания биткоина и равновзвешенной покупки того же набора монет;
  · сколько денег к концу заморожено в непроданных позициях и насколько они в
    минусе — это и есть цена отсутствующего правила выхода;
  · чувствительность к способу отбора монет: импульс, откат и случайный выбор.
    Если результат от отбора почти не зависит, дело не в выборе монет.

Условия намеренно щедрые к схеме: продажа исполняется точно по цели, как только
дневной максимум её коснулся, комиссия берётся биржевая, проскальзывание в базовом
прогоне не учитывается вовсе. Всё сомнительное трактуется в пользу правила: цель
показать, что схема проигрывает даже в условиях лучше реальных.

Запуск:
  python coin_rotation.py            # прогон на кэше, догрузка недостающего
  python coin_rotation.py --refresh  # перекачать свечи
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl
import requests

SPOT = "https://api.binance.com/api/v3"
DATA_ROOT = Path(
    os.environ.get("ORDERFLOW_DATA", Path(__file__).resolve().parents[2] / "data")
)
CACHE = DATA_ROOT / "spot_daily"
OUT_DIR = DATA_ROOT / "tg"

SLOTS = 10               # монет в портфеле
STAKE = 10.0             # долларов на монету
TARGET = 0.10            # цель фиксации
FEE = 0.001              # комиссия Binance за сторону, спот
HISTORY = 1000           # свечей на монету: предел одного запроса Binance
CBR_RATE = 0.14          # планка: ключевая ставка, годовых

# Стейблкоины и обёртки исключаются: их цена привязана к доллару, роста на 10% там
# не бывает, и в наборе они работали бы как холостые слоты, улучшая результат
# схемы просто за счёт того, что не падают.
STABLE = {
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "USDD", "PYUSD",
    "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "ARS", "RUB", "UAH", "ZAR",
    "IDRT", "NGN", "PLN", "RON", "CZK", "JPY", "MXN", "COP", "XUSD", "USD1",
}


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #

def universe(min_volume_usd: float = 3_000_000) -> list[str]:
    """Монеты, которыми вообще можно торговать на десять долларов.

    Порог по обороту не про «качество» монеты, а про исполнимость: в паре с
    оборотом в десятки тысяч долларов заявка на десять долларов двигает цену сама,
    и замер превратился бы в измерение собственного проскальзывания.
    """
    info = requests.get(f"{SPOT}/exchangeInfo", timeout=60).json()
    tradable = {
        s["symbol"] for s in info["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
        and s["baseAsset"] not in STABLE
        # Токены с плечом и индексные обёртки — не монеты, а производные с
        # собственным распадом стоимости; в наборе CoinMarketCap их тоже нет.
        and not s["baseAsset"].endswith(("UP", "DOWN", "BULL", "BEAR"))
    }
    tickers = requests.get(f"{SPOT}/ticker/24hr", timeout=60).json()
    liquid = [
        t["symbol"] for t in tickers
        if t["symbol"] in tradable and float(t["quoteVolume"]) >= min_volume_usd
    ]
    return sorted(liquid)


def klines(symbol: str, days: int = HISTORY, refresh: bool = False) -> pl.DataFrame | None:
    """Дневные свечи одной монеты, с кэшем на диске.

    Берётся вся доступная за один запрос история, а не только проверяемое окно:
    вывод о схеме, сделанный на одном отрезке, ничего не стоит, а падающий и
    растущий рынок должны сравниваться на одних и тех же монетах.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}.parquet"
    if path.exists() and not refresh:
        return pl.read_parquet(path)

    try:
        raw = requests.get(
            f"{SPOT}/klines",
            params={"symbol": symbol, "interval": "1d", "limit": days},
            timeout=60,
        ).json()
    except Exception as exc:
        print(f"  {symbol}: {exc}")
        return None
    if not isinstance(raw, list) or len(raw) < 200:
        return None

    df = pl.DataFrame(
        {
            "дата": [dt.date.fromtimestamp(r[0] / 1000) for r in raw],
            "открытие": [float(r[1]) for r in raw],
            "максимум": [float(r[2]) for r in raw],
            "минимум": [float(r[3]) for r in raw],
            "закрытие": [float(r[4]) for r in raw],
        }
    )
    df.write_parquet(path)
    return df


def load_prices(symbols: list[str], refresh: bool = False) -> dict[str, pl.DataFrame]:
    out: dict[str, pl.DataFrame] = {}
    for i, s in enumerate(symbols, 1):
        df = klines(s, refresh=refresh)
        if df is not None:
            out[s] = df
        if i % 50 == 0:
            print(f"  свечей загружено: {i}/{len(symbols)}")
        if refresh or not (CACHE / f"{s}.parquet").exists():
            time.sleep(0.05)
    return out


def window(prices: dict[str, pl.DataFrame], start: dt.date, end: dt.date,
           ) -> dict[str, pl.DataFrame]:
    """Отрезок истории по датам, только монеты с полным покрытием отрезка.

    Неполные отбрасываются, а не дополняются: монета, появившаяся в середине
    растущего рынка, попала бы в набор уже после роста и завысила бы результат.
    """
    out = {}
    for s, df in prices.items():
        cut = df.filter((pl.col("дата") >= start) & (pl.col("дата") <= end))
        if cut.height and cut["дата"][0] <= start + dt.timedelta(days=3):
            out[s] = cut
    if not out:
        return {}
    full = max(df.height for df in out.values())
    return {s: df for s, df in out.items() if df.height >= full - 3}


# --------------------------------------------------------------------------- #
# Симуляция
# --------------------------------------------------------------------------- #

def pick(candidates: list[str], mode: str, day: int, prices: dict, rng: random.Random) -> str:
    """Выбор следующей монеты. Три способа, чтобы проверить их роль.

    Отбор «потенциально прибыльных» — самое расплывчатое место схемы, и его нельзя
    ни подтвердить, ни опровергнуть, пока он не назван правилом. Поэтому берутся
    три несовместимых прочтения: покупать выросшее, покупать упавшее и покупать
    наугад. Если исход схемы от этого почти не меняется, спор об отборе теряет
    смысл — дело не в нём.
    """
    if mode == "случайно":
        return rng.choice(candidates)

    scored = []
    for s in candidates:
        df = prices[s]
        if day < 1 or day >= df.height:
            continue
        prev, now = df["закрытие"][day - 1], df["закрытие"][day]
        if prev > 0:
            scored.append((now / prev - 1, s))
    if not scored:
        return rng.choice(candidates)
    scored.sort(reverse=(mode == "импульс"))
    return scored[0][1]


def simulate(prices: dict[str, pl.DataFrame], mode: str, seed: int = 7,
             slippage: float = 0.0) -> dict:
    """Прогон правила по дням. Возвращает итог и то, из чего он сложился."""
    rng = random.Random(seed)
    symbols = sorted(prices)
    length = min(df.height for df in prices.values())

    free = SLOTS * STAKE
    positions: list[dict] = []
    trades = 0
    equity: list[float] = []

    def buy(symbol: str, day: int, money: float) -> dict:
        price = prices[symbol]["закрытие"][day]
        # Комиссия и проскальзывание вычитаются из количества, а не из цены: так же
        # это происходит на бирже, и итог не зависит от того, чем считать.
        qty = money * (1 - FEE - slippage) / price
        return {"монета": symbol, "цена": price, "кол": qty, "день": day}

    for day in range(length):
        # Сначала продажи: выручка того же дня должна успеть уйти в новую монету,
        # иначе схема получила бы искусственный простой капитала.
        held = []
        for p in positions:
            high = prices[p["монета"]]["максимум"][day]
            target = p["цена"] * (1 + TARGET)
            if day > p["день"] and high >= target:
                free += p["кол"] * target * (1 - FEE - slippage)
                trades += 1
            else:
                held.append(p)
        positions = held

        while len(positions) < SLOTS and free >= STAKE * 0.5:
            busy = {p["монета"] for p in positions}
            candidates = [s for s in symbols if s not in busy]
            if not candidates:
                break
            money = min(free, max(STAKE, free / max(1, SLOTS - len(positions))))
            positions.append(buy(pick(candidates, mode, day, prices, rng), day, money))
            free -= money

        value = free + sum(
            p["кол"] * prices[p["монета"]]["закрытие"][day] for p in positions
        )
        equity.append(value)

    last = length - 1
    stuck = []
    for p in positions:
        now = prices[p["монета"]]["закрытие"][last]
        stuck.append({
            "монета": p["монета"],
            "просадка_%": (now / p["цена"] - 1) * 100,
            "стоимость": p["кол"] * now,
        })
    stuck.sort(key=lambda x: x["просадка_%"])

    return {
        "отбор": mode,
        "итог": equity[-1],
        "старт": SLOTS * STAKE,
        "кривая": equity,
        "сделок": trades,
        "заморожено": sum(s["стоимость"] for s in stuck),
        "позиции": stuck,
        "дней": length,
    }


def benchmarks(prices: dict[str, pl.DataFrame], length: int) -> dict:
    """Планки, с которыми схему нужно сравнивать.

    Сравнение с нулём ничего не значит: деньги всегда можно положить под ставку, и
    любая схема обязана побить именно её. Удержание биткоина и равные доли по всему
    набору отвечают на второй вопрос — а не проще ли было ничего не делать.
    """
    start = SLOTS * STAKE
    out = {"вклад": start * (1 + CBR_RATE) ** (length / 365)}

    btc = prices.get("BTCUSDT")
    if btc is not None:
        first, last = btc["закрытие"][0], btc["закрытие"][length - 1]
        out["биткоин"] = start * (last / first)

    ratios = [
        df["закрытие"][length - 1] / df["закрытие"][0]
        for df in prices.values() if df["закрытие"][0] > 0
    ]
    if ratios:
        out["равные доли"] = start * (sum(ratios) / len(ratios))
    return out


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #

def chart(runs: list[dict], marks: dict, draws: list[list[float]], path: Path) -> Path:
    """Итог схемы против планок и полоса случайности вокруг него.

    Доллар пишется как \\$: matplotlib принимает пару знаков доллара в строке за
    формулу и вырезает всё между ними. В подписи «$77 из $77» это съедало half
    строки, и подпись читалась как «77из77».
    """
    fig, (ax, bx) = plt.subplots(
        2, 1, figsize=(11, 9), gridspec_kw={"height_ratios": [3, 2]}
    )

    if draws:
        length = min(len(d) for d in draws)
        low = [min(d[i] for d in draws) for i in range(length)]
        high = [max(d[i] for d in draws) for i in range(length)]
        ax.fill_between(range(length), low, high, color="#888888", alpha=0.25,
                        label="20 жеребьёвок случайного отбора")
    for r in runs:
        ax.plot(r["кривая"], lw=1.8, label=f"схема, отбор «{r['отбор']}»")
    for name, value in marks.items():
        ax.axhline(value, ls="--", lw=1.2, alpha=0.7)
        ax.text(len(runs[0]["кривая"]) * 0.995, value, f" {name}: \\${value:.0f}",
                va="bottom", ha="right", fontsize=9)
    ax.axhline(SLOTS * STAKE, color="black", lw=1, alpha=0.5)
    ax.set_title(f"Ротация монет с фиксацией на +{TARGET:.0%}: "
                 f"{SLOTS} монет по \\${STAKE:.0f}, {runs[0]['дней']} дней")
    ax.set_ylabel("стоимость портфеля, \\$")
    ax.set_xlabel("день")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)

    base = runs[0]
    rows = base["позиции"][:10]
    bx.barh(range(len(rows)), [r["просадка_%"] for r in rows], color="#d62728", alpha=0.85)
    bx.set_yticks(range(len(rows)))
    bx.set_yticklabels([r["монета"].replace("USDT", "") for r in rows], fontsize=10)
    bx.axvline(0, color="black", lw=1)
    bx.set_xlabel("просадка непроданных позиций к концу, %")
    bx.set_title(f"Чего правило не продаёт: в позициях \\${base['заморожено']:.0f} "
                 f"из \\${base['итог']:.0f}, свободных денег нет")
    bx.grid(alpha=0.3, axis="x")

    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def report(prices: dict[str, pl.DataFrame], label: str) -> dict:
    """Прогон схемы на одном отрезке и сравнение с планками."""
    length = min(df.height for df in prices.values())
    print(f"\n=== {label}: монет {len(prices)}, дней {length} ===")

    runs = [simulate(prices, mode) for mode in ("импульс", "откат", "случайно")]
    marks = benchmarks(prices, length)

    for r in runs:
        pct = (r["итог"] / r["старт"] - 1) * 100
        print(f"отбор «{r['отбор']}»: ${r['итог']:.2f} ({pct:+.1f}%), "
              f"сделок {r['сделок']}, заморожено ${r['заморожено']:.2f}")
    for name, value in marks.items():
        print(f"{name}: ${value:.2f} ({(value / (SLOTS * STAKE) - 1) * 100:+.1f}%)")

    slip = simulate(prices, "импульс", slippage=0.003)
    print(f"импульс с проскальзыванием 0.3%: ${slip['итог']:.2f} "
          f"({(slip['итог'] / slip['старт'] - 1) * 100:+.1f}%)")

    # Двадцать жеребьёвок случайного отбора. Один прогон схемы — это одна
    # реализация случая, и по нему нельзя отличить работающее правило от везения.
    # Разброс отвечает на вопрос прямо: если он шире расстояния до планки, итог
    # решает не правило, а то, какие монеты достались.
    draws = [simulate(prices, "случайно", seed=s) for s in range(20)]
    spread = sorted(d["итог"] for d in draws)
    mid = spread[len(spread) // 2]
    print(f"случайный отбор, 20 жеребьёвок: от ${spread[0]:.0f} до ${spread[-1]:.0f}, "
          f"середина ${mid:.0f}")

    return {"отрезок": label, "дней": length, "монет": len(prices),
            "планки": marks, "прогоны": runs,
            "кривые_жеребьёвок": [d["кривая"] for d in draws],
            "разброс": {"мин": spread[0], "медиана": mid, "макс": spread[-1]}}


def main() -> None:
    refresh = "--refresh" in sys.argv
    print(">>> отбор монет с оборотом от $3 млн")
    symbols = universe()
    print(f"монет в наборе: {len(symbols)}")

    print(">>> свечи")
    prices = load_prices(symbols, refresh=refresh)
    print(f"монет с историей: {len(prices)}")

    today = dt.date.today()
    # Два отрезка одинаковой длины на одних и тех же монетах: падающий рынок и
    # растущий. Без второго любой вывод о схеме сводился бы к «крипта упала», а
    # проверять надо не рынок, а правило.
    periods = [
        ("падающий рынок", today - dt.timedelta(days=365), today),
        ("растущий рынок", today - dt.timedelta(days=730), today - dt.timedelta(days=366)),
    ]

    results = []
    for label, start, end in periods:
        cut = window(prices, start, end)
        if not cut:
            print(f"\n=== {label}: данных нет ===")
            continue
        results.append(report(cut, f"{label}, {start:%m.%Y}–{end:%m.%Y}"))

    if results:
        img = chart(results[0]["прогоны"], results[0]["планки"],
                    results[0]["кривые_жеребьёвок"], OUT_DIR / "rotation.png")
        print(f"\nграфик: {img}")

    (DATA_ROOT / "meta").mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "meta" / "rotation.json").write_text(
        json.dumps(
            [
                {
                    **{k: v for k, v in res.items()
                       if k not in ("прогоны", "кривые_жеребьёвок")},
                    "прогоны": [
                        {k: v for k, v in r.items() if k != "кривая"}
                        for r in res["прогоны"]
                    ],
                }
                for res in results
            ],
            ensure_ascii=False, indent=1, default=float,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
