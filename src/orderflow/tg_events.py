"""Новости как замер на событие, а не пересказ ленты.

Обычный новостной поток каналу такого рода противопоказан. Он состоит из чужого
текста под копирайтом, его делает тысяча каналов одновременно, и он не добавляет
ни одной цифры, которой у нас бы не было — читатель, пришедший за замерами,
получает ленту, неотличимую от любой другой. Ценность появляется только там, где
у события есть наше собственное число.

Поэтому здесь не события пересказываются, а считаются последствия. Событие лишь
задаёт повод: ЦБ поменял ключевую ставку — пересчитываем планку, которую обязана
побить любая рублёвая схема; биржа поменяла сборы — пересчитываем стоимость
круга, на которой держится половина содержания канала; истекает серия —
показываем, где на самом деле находится ликвидность.

Два разных вида событий, и разница между ними определила устройство модуля.
Календарные (экспирация) считаются из первичных данных на любом запуске: чтобы
узнать дату последнего торга, помнить прошлое не нужно. Событие-изменение (ставка,
сборы) существует только относительно сохранённого снимка, и на первом запуске
объявлять его нельзя — сравнивать не с чем. Поэтому первый запуск такие события
только запоминает: иначе канал выдал бы залпом «новости» о переменах, случившихся
неизвестно когда.

Запуск:
  python tg_events.py            # опубликовать созревшее событие, если оно есть
  python tg_events.py --dry-run  # только показать
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import polars as pl
import requests

from moex import ROLL_BUFFER_DAYS, _block, _get, FORTS
from moex_feasibility import cost_bps
from tg_post import (
    MOEX_ASSETS,
    OUT_DIR,
    TICKS_ROOT,
    _num,
    active_series,
    publish,
    trading_sessions,
)

# Что уже объявлено. Файл лежит рядом с данными, то есть у каждой машины свой, и
# это ловушка: публикация с ноутбука не видна серверу, поэтому назавтра таймер
# объявит то же событие второй раз. Публиковать надо с сервера; если разово вышло
# с ноутбука — скопировать файл туда:
#   scp data/meta/events.json HOST:/var/lib/orderflow/meta/events.json
STATE = TICKS_ROOT.parent / "meta" / "events.json"

CBR_SOAP = "http://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
FAPI = "https://fapi.binance.com/fapi/v1"

# Порог «экстремального» фандинга. Не выдуман: замерено распределение по 80
# контрактам с оборотом свыше 50 млн $ — медиана +1.1% годовых, 95-й процентиль
# +59%. Полсотни процентов годовых — это уже хвост, а не рабочий диапазон.
FUNDING_EXTREME_PCT = 50.0
# Оборот, ниже которого перекос не обсуждаем: на тонком контракте фандинг двигают
# несколько участников, и «толпа» там ничего не значит.
MIN_TURNOVER_USD = 150e6
# Один контракт не обсуждаем повторно, пока не пройдёт столько дней: перекос
# держится сутками, и без этого канал писал бы о нём каждый запуск.
CROWDING_COOLDOWN_DAYS = 5
# Сколько постов-событий канал имеет право выпустить за сутки. Событий в крипте
# сколько угодно, а охват делится между постами: третий пост за день отбирает
# читателей у первых двух, а не добавляет новых.
MAX_EVENTS_PER_DAY = 2
# Ставку объявляем только пока она новость. Решение ЦБ разбирают все, и приходить
# с ним через две недели незачем; замер при этом остаётся в силе и без повода.
RATE_FRESH_DAYS = 7
# За сколько дней до последнего торга предупреждаем об экспирации. Больше буфера
# ротации серий: сначала предупредить, потом самим переехать.
EXPIRY_WARN_DAYS = ROLL_BUFFER_DAYS + 2


def load_state() -> dict:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Ключевая ставка
# --------------------------------------------------------------------------- #


def key_rate_history(days: int = 700) -> list[tuple[dt.date, float]]:
    """История ключевой ставки из официального SOAP-сервиса ЦБ, по возрастанию даты.

    Берём у первоисточника, а не из пересказов: ЦБ отдаёт ставку сам, и это
    избавляет и от копирайта, и от чужих опечаток.
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><KeyRateXML xmlns="http://web.cbr.ru/">'
        f"<fromDate>{start}</fromDate><ToDate>{end}</ToDate>"
        "</KeyRateXML></soap:Body></soap:Envelope>"
    )
    r = requests.post(
        CBR_SOAP,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://web.cbr.ru/KeyRateXML",
        },
        timeout=40,
    )
    r.raise_for_status()
    rows = [
        (dt.date.fromisoformat(d[:10]), float(v))
        for d, v in re.findall(r"<DT>(.*?)</DT>\s*<Rate>(.*?)</Rate>", r.text)
    ]
    if not rows:
        raise RuntimeError("ЦБ не вернул историю ключевой ставки")
    return sorted(rows)


def rate_changes(hist: list[tuple[dt.date, float]]) -> list[tuple[dt.date, float]]:
    """Только даты, когда значение сменилось: ЦБ отдаёт ставку на каждый день."""
    out = [hist[0]]
    for (_, prev), (day, cur) in zip(hist, hist[1:]):
        if cur != prev:
            out.append((day, cur))
    return out


def rate_chart(hist: list[tuple[dt.date, float]], path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5))
    days = [d for d, _ in hist]
    vals = [v for _, v in hist]
    # Ступенями, а не линией: ставка не движется между решениями, и линейная
    # интерполяция изображала бы плавный спуск, которого не было.
    ax.step(days, vals, where="post", color="#1f77b4", lw=2.4)
    ax.set_ylabel("ключевая ставка, % годовых")
    ax.set_title("Планка, которую обязана побить любая рублёвая схема")
    ax.grid(alpha=0.3)
    ax.annotate(
        f"{vals[-1]:.2f}%",
        xy=(days[-1], vals[-1]),
        xytext=(-10, 10),
        textcoords="offset points",
        fontsize=13,
        fontweight="bold",
        color="#1f77b4",
        ha="right",
    )
    fig.autofmt_xdate()
    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def rate_event(state: dict) -> tuple[str, Path] | None:
    hist = key_rate_history()
    changes = rate_changes(hist)
    day, rate = changes[-1]
    prev = changes[-2][1] if len(changes) > 1 else None
    key = f"{day.isoformat()}:{rate}"

    if "rate" not in state:
        # Первый запуск: запоминаем и молчим, объявлять нечего.
        state["rate"] = key
        return None
    if state["rate"] == key or (dt.date.today() - day).days > RATE_FRESH_DAYS:
        return None

    state["rate"] = key
    delta = "" if prev is None else f" (было {prev:.2f}%)"
    direction = "снизил" if prev is not None and rate < prev else "поднял"
    path = rate_chart(hist, OUT_DIR / "key_rate.png")
    text = (
        f"<b>Ключевая ставка — {rate:.2f}%.</b> ЦБ {direction} её {day.strftime('%d.%m')}"
        f"{delta}.\n\n"
        "Для канала это не новость, а смена планки. Ставка — доходность, которую "
        "рублёвый капитал получает без риска и без работы, поэтому любая схема "
        f"обязана побить {rate:.2f}% годовых, чтобы вообще иметь смысл. Всё, что "
        "ниже, проигрывает вкладу, даже когда показывает прибыль.\n\n"
        "И сразу оговорка, потому что здесь ошибаются чаще всего: сравнивать эту "
        "ставку с доходностью в долларах напрямую нельзя. Нейтральная позиция на "
        "перпетуалах платит проценты в долларах, и разница ставок между валютами "
        "уже сидит в форварде — рублёвая и долларовая доходности сопоставимы только "
        "после приведения к одной валюте, вместе со стоимостью этого приведения.\n\n"
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#ставкаЦБ #МОЕХ #издержки"
    )
    return text, path


# --------------------------------------------------------------------------- #
# Экспирация серий
# --------------------------------------------------------------------------- #


def expiry_calendar() -> pl.DataFrame:
    """Ближайший последний торг по каждому отслеживаемому активу."""
    df = _block(
        _get(f"{FORTS}/securities.json", **{"iss.only": "securities"}), "securities"
    )
    today = dt.date.today().isoformat()
    return (
        df.select("SECID", "ASSETCODE", "SHORTNAME", "LASTTRADEDATE")
        .filter(pl.col("ASSETCODE").is_in(list(MOEX_ASSETS)))
        .filter(pl.col("LASTTRADEDATE") >= today)
        .sort(["ASSETCODE", "LASTTRADEDATE"])
    )


def roll_state(cal: pl.DataFrame, assets: set[str]) -> tuple[pl.DataFrame, str]:
    """Где сейчас деньги: доля открытого интереса, уже уехавшая в дальнюю серию.

    Открытый интерес, а не оборот: он показывает, где позиции держат, тогда как
    оборот показывает, где ими торгуют. Перед экспирацией эти две вещи расходятся,
    и расхождение — самое содержательное, что можно сказать про перекладку.

    Считается только по активам, серия которых действительно истекает. Без этого
    фильтра в «отстающих» оказывается контракт с экспирацией через месяц: он не
    переложился не потому, что запаздывает, а потому, что ему пока некуда.
    """
    day, hist = trading_sessions(2)[-1]
    oi = {r["SECID"]: r for r in hist.select("SECID", "OPENPOSITION", "VALUE").iter_rows(named=True)}

    rows = []
    for asset, grp in cal.group_by("ASSETCODE", maintain_order=True):
        code = asset[0] if isinstance(asset, tuple) else asset
        if code not in assets:
            continue
        series = grp.sort("LASTTRADEDATE").head(2)
        if series.height < 2:
            continue
        near, far = series.row(0, named=True), series.row(1, named=True)
        n, f = oi.get(near["SECID"]), oi.get(far["SECID"])
        if not n or not f:
            continue
        total_oi = n["OPENPOSITION"] + f["OPENPOSITION"]
        total_val = n["VALUE"] + f["VALUE"]
        if total_oi <= 0 or total_val <= 0:
            continue
        rows.append({
            "актив": code,
            "ближняя": near["SECID"],
            "дальняя": far["SECID"],
            "последний_торг": near["LASTTRADEDATE"],
            "интерес_в_дальней_%": round(f["OPENPOSITION"] / total_oi * 100, 1),
            "оборот_в_дальней_%": round(f["VALUE"] / total_val * 100, 1),
        })
    return pl.DataFrame(rows).sort("интерес_в_дальней_%", descending=True), day


def expiry_chart(roll: pl.DataFrame, day: str, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5.6))
    y = range(roll.height)
    ax.barh(
        [i + 0.2 for i in y], roll["интерес_в_дальней_%"], height=0.38,
        color="#2ca02c", label="открытый интерес",
    )
    ax.barh(
        [i - 0.2 for i in y], roll["оборот_в_дальней_%"], height=0.38,
        color="#8c8c8c", label="оборот",
    )
    ax.axvline(50, color="firebrick", ls=":", lw=1.5)
    ax.text(51, roll.height - 0.6, "половина", color="firebrick", fontsize=9)
    ax.set_yticks(list(y))
    ax.set_yticklabels(roll["актив"])
    ax.set_xlabel("доля, уехавшая в дальнюю серию, %")
    ax.set_title(f"Перекладка перед экспирацией: где позиции, а где торговля\n(сессия {day})")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def expiry_event(state: dict) -> tuple[str, Path] | None:
    cal = expiry_calendar()
    today = dt.date.today()
    soon = [
        r for r in cal.iter_rows(named=True)
        if 0 <= (dt.date.fromisoformat(r["LASTTRADEDATE"]) - today).days
        <= EXPIRY_WARN_DAYS
    ]
    if not soon:
        return None

    # Ключ по дате, а не по тикеру: серии истекают пачкой в один-два дня, и
    # предупреждать о каждой отдельным постом значило бы завалить канал.
    key = min(r["LASTTRADEDATE"] for r in soon)
    if state.get("expiry") == key:
        return None

    soon_assets = {r["ASSETCODE"] for r in soon}
    roll, day = roll_state(cal, soon_assets)
    if roll.is_empty():
        return None
    state["expiry"] = key

    moved = roll.filter(pl.col("интерес_в_дальней_%") >= 50)
    lagging = roll.filter(pl.col("интерес_в_дальней_%") < 50)
    lead = roll.row(0, named=True)
    names = ", ".join(sorted(soon_assets))

    # Серии истекают пачкой, но не всегда в один день: золото на день позже валют.
    # Одна дата в тексте была бы неверной для части перечисленных контрактов.
    days = sorted({dt.date.fromisoformat(r["LASTTRADEDATE"]) for r in soon})
    when = (
        days[0].strftime("%d.%m")
        if len(days) == 1
        else f"{days[0].strftime('%d.%m')} и {days[-1].strftime('%d.%m')}"
    )

    # Стоимость перекладки: выход из ближней плюс вход в дальнюю, то есть по
    # половине круга на каждой ноге — в сумме примерно один полный круг.
    costs = []
    for r in roll.iter_rows(named=True):
        a, b = cost_bps(r["ближняя"]), cost_bps(r["дальняя"])
        if a and b:
            costs.append((a["издержки_круг_бп"] + b["издержки_круг_бп"]) / 2)
    roll_cost = f"{min(costs):.2f}–{max(costs):.2f}" if costs else "—"

    text = (
        f"<b>{lead['актив']}: {lead['интерес_в_дальней_%']:.0f}% открытого интереса "
        f"уже в дальней серии</b>, хотя торгуют всё ещё ближней — там "
        f"{100 - lead['оборот_в_дальней_%']:.0f}% оборота.\n\n"
        f"{when} — последние дни торгов серии по {len(soon)} активам: "
        f"{html.escape(names)}. Дальше расчёт, и позиция закрывается сама, хочет "
        "того держатель или нет.\n\n"
        "Интересно здесь расхождение двух чисел. Открытый интерес показывает, где "
        "позиции держат, оборот — где ими торгуют, и перед экспирацией они "
        f"расходятся: деньги уже переложились, активность ещё нет. Перекладку "
        f"прошли {moved.height} актива из {roll.height}"
        + (
            f", отстают {html.escape(', '.join(lagging['актив'].to_list()))}"
            if not lagging.is_empty() else ""
        )
        + ".\n\n"
        f"Стоит перекладка {roll_cost} б.п. — выход из ближней плюс вход в дальнюю, "
        "по половине круга на ноге. Это не бесплатная техническая операция, а "
        "четыре раза в год списываемые издержки, которые редко закладывают в "
        "расчёт доходности.\n\n"
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#МОЕХ #фьючерсы #экспирация"
    )
    return text, expiry_chart(roll, day, OUT_DIR / "expiry_roll.png")


# --------------------------------------------------------------------------- #
# Крипта: перекос фандинга и новые контракты
# --------------------------------------------------------------------------- #


def perp_universe() -> list[dict]:
    """Перпетуалы к USDT с ценой, суточным движением, фандингом и оборотом."""
    tick = {x["symbol"]: x for x in requests.get(f"{FAPI}/ticker/24hr", timeout=40).json()}
    prem = requests.get(f"{FAPI}/premiumIndex", timeout=40).json()

    rows = []
    for p in prem:
        sym = p["symbol"]
        t = tick.get(sym)
        if not t or not sym.endswith("USDT"):
            continue
        turnover = float(t["quoteVolume"])
        if turnover < MIN_TURNOVER_USD:
            continue
        rows.append({
            "тикер": sym,
            "имя": sym.replace("USDT", ""),
            "цена": float(t["lastPrice"]),
            "сутки_%": float(t["priceChangePercent"]),
            "фандинг_год_%": float(p["lastFundingRate"]) * 3 * 365 * 100,
            "оборот_млн": turnover / 1e6,
        })
    return rows


SUSTAINED_PAYMENTS = 6  # шесть выплат по 8 часов = двое суток


def sustained_funding(symbol: str) -> float | None:
    """Средняя фактическая ставка фандинга за последние двое суток, % годовых.

    Именно фактическая, из состоявшихся выплат, а не прогноз следующей: разница
    между ними и есть разница между «перекос держится» и «на минуту дёрнуло».
    """
    r = requests.get(
        f"{FAPI}/fundingRate",
        params={"symbol": symbol, "limit": SUSTAINED_PAYMENTS},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    if len(rows) < SUSTAINED_PAYMENTS:
        # Меньше двух суток истории — контракт слишком свежий, чтобы говорить об
        # устойчивости чего-либо.
        return None
    rates = [float(x["fundingRate"]) for x in rows]
    return sum(rates) / len(rates) * 3 * 365 * 100


def crowding_chart(rows: list[dict], hero: dict, path: Path) -> Path:
    """Облако «фандинг против движения цены» с выделенным героем поста.

    Облако и герой обязаны считаться по одной ставке. Пока облако строилось по
    прогнозу выплаты, а герой по фактическим, он попадал на график дважды — серым
    в одной точке и красным в другой, и это выглядело как ошибка данных, потому
    что ошибкой и было.
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    others = [r for r in rows if r["тикер"] != hero["тикер"]]
    ax.scatter([r["сутки_%"] for r in others], [r["фандинг_год_%"] for r in others],
               s=28, color="#8c8c8c", alpha=0.55, label="перпетуалы Binance")
    ax.scatter([hero["сутки_%"]], [hero["фандинг_год_%"]], s=170, color="firebrick",
               zorder=5, label=hero["имя"])
    ax.annotate(
        hero["имя"],
        xy=(hero["сутки_%"], hero["фандинг_год_%"]),
        xytext=(12, 12), textcoords="offset points",
        fontsize=13, fontweight="bold", color="firebrick",
    )

    xs = [r["сутки_%"] for r in rows]
    ys = [r["фандинг_год_%"] for r in rows]
    pad_x = (max(xs) - min(xs)) * 0.08 or 1
    pad_y = (max(ys) - min(ys)) * 0.08 or 1
    ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
    ax.axhline(0, color="black", lw=1)
    ax.axvline(0, color="black", lw=1)

    # Четверти, где фандинг спорит с ценой, — единственное, что здесь интересно.
    # Граница считается по нулю в долях оси: xmin=0.5 делило бы картинку по
    # середине рамки, а ноль цены почти никогда не стоит в её середине.
    lo, hi = ax.get_xlim()
    zero = (0 - lo) / (hi - lo)
    ax.axhspan(0, ax.get_ylim()[1], xmin=0, xmax=zero, color="#d62728", alpha=0.06)
    ax.axhspan(ax.get_ylim()[0], 0, xmin=zero, xmax=1, color="#2ca02c", alpha=0.06)

    ax.set_xlabel("движение цены за сутки, %")
    ax.set_ylabel("фандинг, % годовых (средний по фактическим выплатам за двое суток)")
    ax.set_title(
        "Кто платит за удержание позиции\n"
        f"(перпетуалы с оборотом свыше {MIN_TURNOVER_USD / 1e6:.0f} млн $ в сутки)"
    )
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def crowding_event(state: dict) -> tuple[str, Path] | None:
    """Фандинг спорит с ценой: толпа платит за позицию, которая идёт против неё.

    Почему именно это считаем новостью, а не движение цены. Цену показывает любой
    источник, и сказать о ней нечего. Фандинг говорит, кто кому платит за право
    держать позицию, и когда его знак противоречит движению цены, видно ровно то,
    чего в цене не видно: сторона, которая платит, ещё и проигрывает.
    """
    rows = perp_universe()
    if not rows:
        return None

    # Устойчивая ставка считается сразу по всей выборке, а не только по кандидатам:
    # иначе облако на графике и герой в тексте измерены разными линейками.
    # Это стоит по одному запросу на контракт, зато сравнение честное.
    measured = []
    for r in rows:
        paid = sustained_funding(r["тикер"])
        if paid is not None:
            measured.append({**r, "фандинг_год_%": paid})
    if not measured:
        return None
    rows = measured

    today = dt.date.today()
    seen = state.setdefault("crowding", {})

    def fresh(sym: str) -> bool:
        last = seen.get(sym)
        if not last:
            return True
        return (today - dt.date.fromisoformat(last)).days >= CROWDING_COOLDOWN_DAYS

    # Противоречие: платят лонги, а цена падает, либо платят шорты, а цена растёт.
    candidates = [
        r for r in rows
        if abs(r["фандинг_год_%"]) >= FUNDING_EXTREME_PCT
        and r["фандинг_год_%"] * r["сутки_%"] < 0
        and fresh(r["тикер"])
    ]
    if not candidates:
        return None

    hero = max(candidates, key=lambda r: abs(r["фандинг_год_%"]))
    seen[hero["тикер"]] = today.isoformat()

    longs_pay = hero["фандинг_год_%"] > 0
    who = "лонги" if longs_pay else "шорты"
    against = "падает" if longs_pay else "растёт"
    # Стоимость упрямства в понятных деньгах: сколько съест позиция за неделю,
    # если фандинг останется таким же.
    weekly = abs(hero["фандинг_год_%"]) / 52

    text = (
        f"<b>{html.escape(hero['имя'])}: {who} платят "
        f"{abs(hero['фандинг_год_%']):.0f}% годовых за позицию, которая идёт против "
        f"них.</b> Цена за сутки {hero['сутки_%']:+.1f}%.\n\n"
        f"Фандинг — плата за право держать вечный фьючерс, её раз в 8 часов "
        f"переводит одна сторона другой. Здесь платят {who}, а цена {against}: "
        "сторона, которая платит, ещё и проигрывает. Так выглядит перекос — "
        "слишком много желающих в одну сторону.\n\n"
        f"В деньгах это {weekly:.1f}% от позиции за неделю. Ставка взята не с "
        "прогноза следующей выплаты, который дёргается внутри интервала, а как "
        f"средняя по {SUSTAINED_PAYMENTS} фактически состоявшимся выплатам за двое "
        f"суток — перекос держится, а не мигнул. Оборот "
        f"{_num(round(hero['оборот_млн']))} млн $ в сутки, то есть цифра не с "
        "тонкого контракта, где ставку двигают несколько участников.\n\n"
        "Порог отбора не выдуман: медиана фандинга по этой выборке около 1% "
        f"годовых, {FUNDING_EXTREME_PCT:.0f}% — уже хвост. На графике видно, где "
        "остальные.\n\n"
        "И чего здесь нет: это не сигнал. Перекос говорит, что позиция дорого "
        "обходится, а не что цена развернётся — знак эффекта зависит от режима "
        "рынка, я это замерял.\n\n"
        "#фандинг #крипта #Binance"
    )
    return text, crowding_chart(rows, hero, OUT_DIR / "crowding.png")


def listing_event(state: dict) -> tuple[str, None] | None:
    """Новые перпетуалы на Binance: событие, а не мнение о нём.

    Первичный источник — сама биржа, поле onboardDate в exchangeInfo. Пересказ
    анонсов тут не нужен: факт листинга биржа публикует сама, машинно.
    """
    ei = requests.get(f"{FAPI}/exchangeInfo", timeout=40).json()
    live = [s for s in ei.get("symbols", []) if s.get("status") == "TRADING"]
    if not live:
        return None

    today = dt.date.today()
    fresh = []
    for s in live:
        onboard = s.get("onboardDate")
        if not onboard:
            continue
        day = dt.datetime.fromtimestamp(onboard / 1000).date()
        if 0 <= (today - day).days <= 1:
            fresh.append((s["symbol"], day))

    if not fresh:
        return None
    key = ",".join(sorted(s for s, _ in fresh))
    if state.get("listings") == key:
        return None
    state["listings"] = key

    names = ", ".join(html.escape(s.replace("USDT", "")) for s, _ in sorted(fresh))
    text = (
        f"<b>Binance добавила {len(fresh)} новых перпетуалов:</b> {names}.\n\n"
        f"Всего на бессрочных фьючерсах теперь {len(live)} контрактов. Цифра сама по "
        "себе показательна: инструментов больше, чем кто-либо способен отслеживать, "
        "и именно поэтому отбирать их надо по издержкам, а не по интересности.\n\n"
        "Что стоит посмотреть в новом контракте до всякой торговли, в этом порядке: "
        "спред относительно цены, потому что на свежих парах он в разы шире, чем на "
        "мажорах, и съедает результат раньше модели; глубину стакана, потому что "
        "заявка крупнее верхнего уровня двигает цену сама против себя; и фандинг, "
        "который на новых контрактах регулярно улетает в десятки процентов годовых "
        "из-за перекоса в одну сторону.\n\n"
        "Свежий листинг — это не возможность и не угроза, это инструмент с ещё "
        "неизмеренными издержками. Пока они не измерены, сказать о нём нечего.\n\n"
        "#Binance #крипта #издержки"
    )
    return text, None


# --------------------------------------------------------------------------- #
# Сборы биржи
# --------------------------------------------------------------------------- #


def fees_event(state: dict) -> tuple[str, Path] | None:
    """Смена биржевых сборов или шага цены: пересчитываем стоимость круга.

    Это самое редкое из событий и самое неприятное, если его пропустить: на
    издержках круга держатся все пороги, которые канал публикует, и молчаливое
    изменение сборов сделало бы старые цифры неверными без всякого признака.
    """
    snap: dict[str, dict] = {}
    for secid in active_series(MOEX_ASSETS).values():
        c = cost_bps(secid)
        if c:
            snap[secid] = {
                "сбор": c["сбор_биржи_руб"],
                "скальпер": c["сбор_скальпера_руб"],
                "круг": c["издержки_круг_бп"],
            }
    if not snap:
        return None

    old = state.get("fees")
    state["fees"] = snap
    if not old:
        return None

    # Сравниваем только по тикерам, которые были в снимке: смена серии меняет
    # тикер целиком, и без этого фильтра каждая экспирация выглядела бы как
    # изменение тарифов.
    changed = [
        (s, old[s], snap[s]) for s in snap
        if s in old and (old[s]["сбор"], old[s]["скальпер"]) != (snap[s]["сбор"], snap[s]["скальпер"])
    ]
    if not changed:
        return None

    lines = [
        f"<b>{html.escape(s)}</b>: круг {o['круг']:.2f} → {n['круг']:.2f} б.п."
        for s, o, n in changed
    ]
    text = (
        f"<b>МОЕХ изменила сборы по {len(changed)} контрактам.</b>\n\n"
        + "\n".join(lines)
        + "\n\nЗачем об этом пост. Издержки полного круга — не деталь, а величина, "
        "которая задаёт минимальную точность и минимальный горизонт для любой "
        "стратегии. Меняются сборы — сдвигаются все пороги, которые здесь "
        "публиковались раньше, и старые цифры перестают быть верными.\n\n"
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#МОЕХ #фьючерсы #издержки"
    )
    return text, None


# Порядок задаёт приоритет: за один запуск выходит одно событие. Впереди то, что
# случается редко и меняет цифры в канале (ставка, сборы, экспирация), затем крипта,
# где поводов много и они не так значимы.
EVENTS = (rate_event, fees_event, expiry_event, listing_event, crowding_event)


def posted_today(state: dict) -> int:
    return state.get("posted", {}).get(dt.date.today().isoformat(), 0)


def count_post(state: dict) -> None:
    """Считает выпущенные события по дням, чтобы соблюдать суточный предел.

    Старые дни вычищаются: файл состояния иначе растёт вечно, а нужен только
    сегодняшний счёт.
    """
    today = dt.date.today().isoformat()
    state["posted"] = {today: posted_today(state) + 1}


def pending(state: dict) -> tuple[str, Path | None] | None:
    if posted_today(state) >= MAX_EVENTS_PER_DAY:
        print(f"за сегодня уже {posted_today(state)} события — предел исчерпан")
        return None

    for check in EVENTS:
        try:
            found = check(state)
        except Exception as exc:
            # Отказ одного источника не должен глушить остальные события.
            print(f"{check.__name__}: пропуск ({exc})")
            continue
        if found:
            print(f"событие: {check.__name__}")
            count_post(state)
            return found
    return None


def main() -> None:
    dry = "--dry-run" in sys.argv
    state = load_state()
    found = pending(state)
    if not found:
        print("созревших событий нет")
        if not dry:
            save_state(state)
        return

    text, image = found
    publish(text, image, dry_run=dry)
    if not dry:
        save_state(state)


if __name__ == "__main__":
    main()
