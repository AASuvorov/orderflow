"""Автопубликация собственных замеров в Telegram-канал.

Зачем это отдельным модулем. Ценность канала держится на том, что цифры в нём
свои и проверяемые, а не пересказанные. Такие цифры уже считаются пайплайном
проекта — значит, публикация должна быть автоматической, а ручным остаётся только
решение, что именно проверять.

Что здесь принципиально не делается: перепечатка чужих новостей с Investing.com и
подобных агрегаторов. Причина в первую очередь правовая — их условия
использования запрещают копирование и распространение материалов, а канал на
чужом контенте живёт до первой жалобы и не берётся рекламодателями. Плюс
новостная лента — самая дешёвая вертикаль в Telegram (CPM в 4–6 раз ниже
финансовой), и гонка за скорость проигрывается ботам, которые работают годами.

Вопрос «что произошло вчера» при этом закрывается — отчётом morning, который
строится по открытому ISS Московской биржи напрямую. Это первичный источник, а
не чужая статья о нём: биржа публикует свои данные для свободного использования,
и в них есть открытый интерес, которого в новостных сводках не бывает.

Настройка один раз:
  1. @BotFather -> /newbot -> получить токен
  2. добавить бота администратором канала с правом публикации
  3. export TG_BOT_TOKEN=...
     export TG_CHAT_ID=@tradingnadannyh
  4. uv run python tg_post.py check

Запуск:
  uv run python tg_post.py daily --dry-run     # отчёт этого дня недели, без отправки
  uv run python tg_post.py daily               # опубликовать отчёт дня
  uv run python tg_post.py funding             # конкретный отчёт вручную

Расписание — раз в день по будням, режим daily сам выбирает рубрику по дню недели
(см. SCHEDULE). Один пост в день держит регулярность, но не роняет дочитывание:
оно входит в цену рекламы напрямую, поэтому наращивать частоту в ущерб ценности
поста невыгодно даже чисто арифметически.

Разворачивается на сервере рядом со сбором тиков — отчёт о сессии читает те же
файлы, а на ноутбуке они появляются только после sync.sh pull:
  bash deploy/sync.sh push root@IP
  ssh root@IP 'bash /opt/orderflow/install-tg.sh'
"""

from __future__ import annotations

import datetime as dt
import html
import os
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import requests

from funding import CACHE as FUNDING_CACHE
from funding import FAPI, PERIODS_PER_YEAR, fetch_funding
from mm_screen import MAKER_BPS, screen
from moex import CACHE as MOEX_CACHE
from moex import ISS
from moex import _block as _iss_block
from moex import _get as _iss_get
from moex_feasibility import cost_bps
# Каталог тиков берём у сборщика, а не собираем свой путь: он единственный знает,
# куда реально пишет, и уважает ORDERFLOW_DATA.
from moex_ticks import CACHE as TICKS_ROOT

# Базовая мейкерская комиссия Binance, б.п. — порог необходимого условия мейкинга.
MAKER_FEE_BPS = MAKER_BPS["базовый 0.02%"]

# Ликвидные пары для сравнения: на них спред упирается в один тик.
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Запас на выходные и праздники МОЕХ: в понедельник свежайшей будет пятничная сессия.
MAX_SESSION_AGE_DAYS = 4

API = "https://api.telegram.org/bot{token}/{method}"
# Telegram режет подпись к фото на 1024 символах, обычное сообщение — на 4096.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

# Картинки перегенерируются при каждом запуске, поэтому лежат рядом с данными, а
# не в reports/: на сервере каталог кода недоступен для записи.
OUT_DIR = TICKS_ROOT.parent / "tg"

FUNDING_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
# Контракты отобраны по издержкам круга, тот же список, что в deploy/install.sh.
MOEX_CONTRACTS = ("EuU6", "SiU6", "GDU6", "EDU6", "MMU6", "GNU6", "MXU6", "BRV6", "CRU6")

# Долларовая безрисковая ставка для сравнения с carry, % годовых.
# Значение из funding.py; при заметном изменении ставок обновить здесь.
RISK_FREE_USD_PCT = 4.0


# --------------------------------------------------------------------------- #
# Отправка
# --------------------------------------------------------------------------- #


def _credentials() -> tuple[str, str]:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat:
        raise SystemExit(
            "нужны TG_BOT_TOKEN и TG_CHAT_ID.\n"
            "  export TG_BOT_TOKEN=...\n"
            "  export TG_CHAT_ID=@tradingnadannyh"
        )
    return token, chat


def _call(method: str, *, files: dict | None = None, **data) -> dict:
    token, chat = _credentials()
    resp = requests.post(
        API.format(token=token, method=method),
        data={"chat_id": chat, **data},
        files=files,
        timeout=60,
    )
    payload = resp.json()
    if not payload.get("ok"):
        # Описание от Telegram информативнее кода статуса, поэтому показываем его.
        raise RuntimeError(f"{method}: {payload.get('description', resp.text)}")
    return payload["result"]


def check() -> None:
    """Проверка доступа: бот существует, добавлен в канал и вправе публиковать.

    Членство проверяется отдельно от getChat: для публичного канала getChat
    отвечает и постороннему боту, поэтому сам по себе успех этого вызова ничего
    не доказывает и создаёт ложное впечатление готовности.
    """
    token, chat = _credentials()
    me = requests.get(API.format(token=token, method="getMe"), timeout=30).json()
    if not me.get("ok"):
        raise SystemExit(f"токен не принят: {me.get('description')}")
    bot = me["result"]
    print(f"бот:   @{bot['username']}")

    info = _call("getChat")
    print(f"канал: {info.get('title')} ({chat})")

    try:
        member = _call("getChatMember", user_id=bot["id"])
    except RuntimeError as exc:
        raise SystemExit(
            f"бот не в канале: {exc}\n\n"
            f"Откройте канал -> Управление каналом -> Администраторы -> "
            f"Добавить администратора -> @{bot['username']}\n"
            f"Права: «Публикация сообщений» обязательно, «Закрепление» — если нужен "
            f"автозакреп."
        )

    status = member.get("status")
    can_post = member.get("can_post_messages", status == "creator")
    print(f"статус: {status}, публикация: {'да' if can_post else 'НЕТ'}")
    if not can_post:
        raise SystemExit(
            "нет права публиковать. Включите «Публикация сообщений» в правах "
            f"администратора @{bot['username']}."
        )
    print("всё готово:")
    print("  uv run python tg_post.py funding --dry-run   # сначала посмотреть текст")


def stamp_published(report: str) -> None:
    """Отмечает удачную публикацию, чтобы её отсутствие мог заметить сторож.

    Без этой метки автопостинг отказывает молча: таймер отработал, отчёт не
    собрался, канал молчит неделю — и узнать об этом можно только зайдя в него
    глазами. Тот же приём, что с меткой резервной копии: пишет один процесс,
    проверяет другой.
    """
    path = TICKS_ROOT.parent / "meta" / "last_post"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{dt.datetime.now().isoformat(timespec='seconds')} {report}\n", encoding="utf-8"
    )


def publish(text: str, image: Path | None = None, *, dry_run: bool = False) -> None:
    """Публикует пост. Длинный текст уходит отдельным сообщением после фото.

    Подпись к фото ограничена 1024 символами, и молчаливая обрезка потеряла бы
    именно вывод — он всегда в конце. Поэтому длина проверяется явно.
    """
    if dry_run:
        print("=" * 72)
        print(text)
        print("=" * 72)
        print(f"символов: {len(text)}  картинка: {image if image else 'нет'}")
        if image and len(text) > CAPTION_LIMIT:
            print(f"(> {CAPTION_LIMIT}: уйдёт фото, затем текст отдельным сообщением)")
        return

    if len(text) > MESSAGE_LIMIT:
        raise ValueError(f"текст {len(text)} символов, лимит {MESSAGE_LIMIT}")

    if image is None:
        _call("sendMessage", text=text, parse_mode="HTML",
              link_preview_options='{"is_disabled":true}')
        print("опубликовано (текст)")
        return

    with image.open("rb") as fh:
        if len(text) <= CAPTION_LIMIT:
            _call("sendPhoto", files={"photo": fh}, caption=text, parse_mode="HTML")
            print("опубликовано (фото с подписью)")
            return
        _call("sendPhoto", files={"photo": fh})
    _call("sendMessage", text=text, parse_mode="HTML",
          link_preview_options='{"is_disabled":true}')
    print("опубликовано (фото + текст)")


# --------------------------------------------------------------------------- #
# Данные: инкрементальное обновление фандинга
# --------------------------------------------------------------------------- #


def refresh_funding(symbol: str) -> pl.DataFrame:
    """Догружает только новые выплаты вместо перекачки истории с 2019 года.

    Полная выкачка занимает минуты и не нужна: расписание недельное, а фандинг
    начисляется раз в 8 часов. Если кэша нет, разово берём всю историю.
    """
    path = FUNDING_CACHE / f"{symbol}.parquet"
    if not path.exists():
        return fetch_funding(symbol)

    old = pl.read_parquet(path)
    start = int(old["ts"].max().timestamp() * 1000) + 1

    rows: list[dict] = []
    while True:
        resp = requests.get(
            FAPI, params={"symbol": symbol, "startTime": start, "limit": 1000}, timeout=30
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1]["fundingTime"] + 1
        if nxt <= start:
            break
        start = nxt
        time.sleep(0.2)

    if not rows:
        return old

    fresh = pl.DataFrame(rows).select(
        pl.from_epoch("fundingTime", time_unit="ms").alias("ts"),
        pl.col("fundingRate").cast(pl.Float64).alias("rate"),
        pl.col("markPrice").cast(pl.Float64, strict=False).alias("mark"),
    )
    df = pl.concat([old, fresh]).unique("ts").sort("ts")
    df.write_parquet(path, compression="zstd")
    print(f"{symbol}: +{len(fresh)} выплат, всего {len(df):,}")
    return df


def annualized(df: pl.DataFrame, days: int) -> float:
    """Ставка за последние N дней, приведённая к годовой, в процентах."""
    cutoff = df["ts"].max() - pl.duration(days=days)
    tail = df.filter(pl.col("ts") > cutoff)
    if tail.is_empty():
        return float("nan")
    return float(tail["rate"].mean() * PERIODS_PER_YEAR * 100)


def negative_share(df: pl.DataFrame, days: int) -> float:
    cutoff = df["ts"].max() - pl.duration(days=days)
    tail = df.filter(pl.col("ts") > cutoff)
    if tail.is_empty():
        return float("nan")
    return float((tail["rate"] < 0).mean() * 100)


# --------------------------------------------------------------------------- #
# Отчёт 1: фандинг
# --------------------------------------------------------------------------- #


def funding_chart(data: dict[str, pl.DataFrame], months: int = 24) -> Path:
    """Месячная доходность carry за последние два года, по инструментам."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "funding_weekly.png"

    fig, ax = plt.subplots(figsize=(10, 5.2))
    for sym, df in data.items():
        m = (
            df.with_columns(pl.col("ts").dt.truncate("1mo").alias("месяц"))
            .group_by("месяц")
            .agg((pl.col("rate").mean() * PERIODS_PER_YEAR * 100).alias("годовых"))
            .sort("месяц")
            .tail(months)
        )
        ax.plot(m["месяц"].to_list(), m["годовых"].to_numpy(), marker="o", ms=3, label=sym)

    ax.axhline(0, color="black", lw=1)
    ax.axhline(RISK_FREE_USD_PCT, color="firebrick", ls="--", lw=1.3)
    ax.text(
        0.01, 0.94,
        f"долларовая безрисковая ~{RISK_FREE_USD_PCT:.0f}%: ниже неё схема бессмысленна",
        transform=ax.transAxes, color="firebrick", fontsize=9,
    )
    ax.set_ylabel("доходность фандинга, % годовых")
    ax.set_xlabel("месяц")
    ax.set_title(
        "Нейтральный carry на перпетуалах Binance\n"
        f"месячное среднее, приведённое к годовому, последние {months} мес."
    )
    ax.legend()
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def funding_report(*, refresh: bool = True) -> tuple[str, Path]:
    data: dict[str, pl.DataFrame] = {}
    for sym in FUNDING_SYMBOLS:
        data[sym] = refresh_funding(sym) if refresh else fetch_funding(sym)

    lines: list[str] = []
    for sym, df in data.items():
        m30, y365 = annualized(df, 30), annualized(df, 365)
        neg = negative_share(df, 30)
        short = sym.replace("USDT", "")
        lines.append(
            f"<b>{html.escape(short)}</b>: {m30:+.1f}% годовых за 30 дней "
            f"(за год {y365:+.1f}%), отрицательных выплат {neg:.0f}%"
        )

    best = max(annualized(df, 30) for df in data.values())
    if best < RISK_FREE_USD_PCT:
        verdict = (
            f"Ни один инструмент не дотягивает до долларовой безрисковой ставки "
            f"(~{RISK_FREE_USD_PCT:.0f}%). За carry вы берёте риск биржи и риск "
            f"ликвидации фьючерсной ноги — и получаете за это меньше, чем платят "
            f"без всякого риска."
        )
    else:
        verdict = (
            f"Лучший инструмент даёт {best:+.1f}% годовых — выше долларовой "
            f"безрисковой (~{RISK_FREE_USD_PCT:.0f}%). Премия существует, но она "
            f"плата за риск биржи и риск ликвидации фьючерсной ноги, а не бесплатный "
            f"доход."
        )

    text = (
        "<b>Фандинг перпетуалов: сколько платит нейтральная позиция</b>\n\n"
        "Схема без предсказания направления: спот в лонг, вечный фьючерс в шорт. "
        "Доход — фандинг, который лонги платят шортам каждые 8 часов.\n\n"
        + "\n".join(lines)
        + "\n\n"
        + verdict
        + "\n\nСчитается автоматически из истории выплат Binance с 2019 года. "
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#издержки@tradingnadannyh"
    )
    return text, funding_chart(data)


# --------------------------------------------------------------------------- #
# Отчёт 2: издержки круга на МОЕХ
# --------------------------------------------------------------------------- #


def costs_chart(rows: list[dict]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "moex_costs_weekly.png"

    names = [r["secid"] for r in rows]
    vals = [r["издержки_круг_бп"] for r in rows]
    spread = [r["спред_бп"] for r in rows]
    fees = [r["сборы_круг_бп"] for r in rows]

    y = np.arange(len(names))
    fig, ax = plt.subplots(figsize=(10, 0.55 * len(names) + 2.2))
    ax.barh(y, spread, label="спред")
    ax.barh(y, fees, left=spread, label="сборы биржи и брокера")
    for i, v in enumerate(vals):
        ax.text(v + 0.03, i, f"{v:.2f}", va="center", fontsize=9)

    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlabel("издержки полного круга, базисных пунктов")
    ax.set_title(
        "Фьючерсы МОЕХ: во что обходится войти и выйти\n"
        "спред плюс сборы биржи и брокера с двух сторон"
    )
    ax.legend()
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def costs_report() -> tuple[str, Path]:
    rows: list[dict] = []
    for secid in MOEX_CONTRACTS:
        c = cost_bps(secid)
        if c:
            rows.append(c)
        else:
            print(f"{secid}: спецификация недоступна, пропуск")
    if not rows:
        raise RuntimeError("МОЕХ не вернула ни одной спецификации")

    rows.sort(key=lambda r: r["издержки_круг_бп"])
    cheap, dear = rows[0], rows[-1]

    listing = "\n".join(
        f"<b>{html.escape(r['secid'])}</b> — {r['издержки_круг_бп']:.2f} б.п. "
        f"(спред {r['спред_бп']:.2f} + сборы {r['сборы_круг_бп']:.2f})"
        for r in rows
    )

    text = (
        "<b>Сколько стоит один круг на фьючерсах МОЕХ</b>\n\n"
        "Полный круг — это спред плюс сборы биржи и брокера с двух сторон. "
        "Именно эта цифра задаёт минимальную точность, при которой торговля "
        "вообще не убыточна.\n\n"
        + listing
        + f"\n\nРазброс — {dear['издержки_круг_бп'] / cheap['издержки_круг_бп']:.1f}× "
        f"между {html.escape(cheap['secid'])} и {html.escape(dear['secid'])}. "
        "На дорогом контракте та же стратегия требует заметно более высокой "
        "точности при том же движении цены — поэтому инструмент выбирают по "
        "издержкам, а не по оборотам.\n\n"
        "Замер автоматический, по текущим спецификациям МОЕХ. "
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#издержки@tradingnadannyh"
    )
    return text, costs_chart(rows)


# --------------------------------------------------------------------------- #
# Отчёт 3: скрининг спредов для мейкинга
# --------------------------------------------------------------------------- #


def spread_chart(df: pl.DataFrame) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "spread_weekly.png"

    # Топ вместе с мажорами: без них график показывал бы только проходящие порог
    # и терял главное — что на самых ликвидных парах спреда нет вообще.
    top = df.head(9)
    majors = df.filter(pl.col("symbol").is_in(MAJORS))
    rows = list(top.iter_rows(named=True)) + list(majors.iter_rows(named=True))

    names = [r["symbol"].replace("USDT", "") for r in rows]
    half = [r["полспреда_бп"] for r in rows]
    colors = ["tab:blue"] * top.height + ["tab:orange"] * majors.height

    y = np.arange(len(names))
    fig, ax = plt.subplots(figsize=(10, 0.5 * len(names) + 2.2))
    ax.barh(y, half, color=colors)
    ax.axvline(MAKER_FEE_BPS, color="firebrick", ls="--", lw=1.4,
               label=f"мейкерская комиссия {MAKER_FEE_BPS} б.п.")
    for i, v in enumerate(half):
        ax.text(v + 0.05, i, f"{v:.2f}", va="center", fontsize=9)
    ax.text(
        0.98, 0.06,
        "оранжевым — самые ликвидные пары: спреда нет вовсе",
        transform=ax.transAxes, ha="right", color="tab:orange", fontsize=9,
    )

    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlabel("половина спреда, базисных пунктов")
    ax.set_title(
        "Что достаётся мейкеру до вычета adverse selection\n"
        "перпетуалы Binance с оборотом больше 20 млн $ в сутки"
    )
    ax.legend()
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def spread_report(snapshots_n: int = 10) -> tuple[str, Path]:
    """Сколько инструментов проходят необходимое условие мейкинга.

    Условие только необходимое: половина спреда больше комиссии. Достаточным оно
    не становится — adverse selection вычитается уже после и, по замерам проекта,
    съедает спред целиком. Об этом сказано прямо в тексте, иначе пост читался бы
    как приглашение торговать.
    """
    df = screen(n=snapshots_n)
    col = "брутто [базовый 0.02%]"
    viable = df.filter(pl.col(col) > 0)

    majors = df.filter(pl.col("symbol").is_in(MAJORS))
    majors_txt = "\n".join(
        f"<b>{html.escape(r['symbol'].replace('USDT', ''))}</b>: спред "
        f"{r['спред_бп']:.2f} б.п., полспреда {r['полспреда_бп']:.2f} — "
        f"{'выше' if r['полспреда_бп'] > MAKER_FEE_BPS else 'ниже'} комиссии"
        for r in majors.iter_rows(named=True)
    )

    best = viable.head(3)
    best_txt = ", ".join(
        f"{html.escape(r['symbol'].replace('USDT', ''))} ({r['полспреда_бп']:.2f})"
        for r in best.iter_rows(named=True)
    ) or "ни одного"

    text = (
        "<b>Где мейкеру вообще есть что зарабатывать</b>\n\n"
        "Необходимое условие мейкинга простое: половина спреда должна быть больше "
        f"комиссии ({MAKER_FEE_BPS} б.п.). Иначе схема убыточна ещё до всякого "
        "движения цены.\n\n"
        f"Проверил все перпетуалы с оборотом выше 20 млн $ в сутки — их "
        f"{df.height}. Условие проходят <b>{viable.height}</b>. "
        f"Лучшие по полуспреду: {best_txt}.\n\n"
        f"{majors_txt}\n\n"
        "Но условие только необходимое. По моим замерам на 6 млн исполнений "
        "adverse selection съедает спред целиком: нетто по кругу выходит от −3.75 "
        "до −14.78 б.п. То есть широкий спред — это не приглашение, а плата за "
        "риск, который в среднем реализуется.\n\n"
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#издержки@tradingnadannyh"
    )
    return text, spread_chart(df)


# --------------------------------------------------------------------------- #
# Отчёт 4: вчерашняя сессия МОЕХ по собственным тикам
# --------------------------------------------------------------------------- #


def _num(value: int) -> str:
    """Разряды разделяются пробелом, как принято в русском тексте.

    Через str.format с запятой делать нельзя: замена запятых в готовой строке
    затронула бы и знаки препинания самого текста.
    """
    return f"{value:,}".replace(",", "\u00a0")


def latest_session() -> tuple[str, dict[str, pl.DataFrame]]:
    """Последняя завершённая сессия и собранные по ней тики.

    Сегодняшний день исключается намеренно. Публикация идёт в 9:00 МСК, ровно
    когда МОЕХ открывается, и файл за сегодня к этому моменту уже существует с
    десятком сделок. Взяв его, отчёт назвал бы итогами сессии первую минуту
    торгов — формально свежие данные, фактически мусор.
    """
    if not TICKS_ROOT.exists():
        raise RuntimeError(f"нет собранных тиков: {TICKS_ROOT}")

    today = dt.date.today().isoformat()
    files = sorted(p for p in TICKS_ROOT.glob("*/*.parquet") if p.stem < today)
    if not files:
        raise RuntimeError("нет ни одной завершённой сессии в кэше тиков")

    day = files[-1].stem
    age = (dt.date.today() - dt.date.fromisoformat(day)).days
    if age > MAX_SESSION_AGE_DAYS:
        # Данные есть, но старые — а это худший случай: обычная проверка на
        # существование файла его пропустит, и канал начнёт публиковать вчерашнюю
        # сессию как сегодняшнюю. Отказ здесь переводит расписание на другой отчёт.
        raise RuntimeError(
            f"последняя сессия {day}, это {age} дней назад — сбор тиков не идёт"
        )

    data = {p.parent.name: pl.read_parquet(p) for p in files if p.stem == day}
    return day, data


def session_chart(rows: list[dict], day: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "session_daily.png"

    names = [r["контракт"] for r in rows]
    share = [r["дельта_доля_%"] for r in rows]
    colors = ["tab:green" if s > 0 else "tab:red" for s in share]

    y = np.arange(len(names))
    fig, ax = plt.subplots(figsize=(10, 0.5 * len(names) + 2.4))
    ax.barh(y, share, color=colors)
    ax.axvline(0, color="black", lw=1)
    for i, s in enumerate(share):
        ax.text(s + (0.15 if s >= 0 else -0.15), i, f"{s:+.1f}%",
                va="center", ha="left" if s >= 0 else "right", fontsize=9)

    # Запас по краям: подписи выносятся за конец столбца и иначе налезают на ось.
    limit = max(abs(s) for s in share) * 1.22
    ax.set_xlim(-limit, limit)

    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlabel("перевес агрессивных покупок над продажами, % от объёма")
    ax.set_title(
        f"Сессия МОЕХ {day}: куда давил агрессор\n"
        "по собственным тикам со стороной инициатора сделки"
    )
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def session_report() -> tuple[str, Path]:
    day, data = latest_session()

    rows: list[dict] = []
    for contract, df in data.items():
        if df.is_empty():
            continue
        vol = float(df["qty"].sum())
        delta = float((df["qty"] * df["side"]).sum())
        # Самый односторонний час: там, где перевес одной стороны максимален.
        hourly = (
            df.with_columns(pl.col("ts").dt.hour().alias("час"))
            .group_by("час")
            .agg(
                (pl.col("qty") * pl.col("side")).sum().alias("дельта"),
                pl.col("qty").sum().alias("объём"),
            )
            .filter(pl.col("объём") > 0)
            .with_columns((pl.col("дельта") / pl.col("объём") * 100).alias("доля"))
            .sort(pl.col("доля").abs(), descending=True)
        )
        top_hour = hourly.row(0, named=True) if not hourly.is_empty() else None
        rows.append({
            "контракт": contract,
            "объём": vol,
            "дельта_доля_%": round(delta / vol * 100, 1) if vol else 0.0,
            "сделок": df.height,
            "час": top_hour["час"] if top_hour else None,
            "час_доля": round(top_hour["доля"], 1) if top_hour else None,
        })

    if not rows:
        raise RuntimeError(f"сессия {day} пуста")

    rows.sort(key=lambda r: abs(r["дельта_доля_%"]), reverse=True)
    top = rows[0]
    total_trades = sum(r["сделок"] for r in rows)

    listing = "\n".join(
        f"<b>{html.escape(r['контракт'])}</b>: {r['дельта_доля_%']:+.1f}% "
        f"(сделок {_num(r['сделок'])})"
        for r in rows[:6]
    )

    text = (
        f"<b>Сессия МОЕХ {html.escape(day)}: куда давил агрессор</b>\n\n"
        f"Я собираю тики со стороной инициатора сделки — той, кто ударил по цене, "
        f"а не просто стоял в стакане. За сессию накопилось "
        f"{_num(total_trades)} сделок по {len(rows)} контрактам."
        + "\n\nПеревес агрессивных покупок над продажами, % от объёма:\n\n"
        + listing
        + f"\n\nСильнее всего перекошен <b>{html.escape(top['контракт'])}</b>: "
        f"{top['дельта_доля_%']:+.1f}% за сессию"
        + (f", пик в {top['час']}:00 МСК ({top['час_доля']:+.1f}%)"
           if top["час"] is not None else "")
        + ".\n\nВажная оговорка, чтобы это не читалось как сигнал: односторонний "
        "поток сам по себе направление не предсказывает. Я это замерял — знак "
        "эффекта зависит от режима рынка, а не от перекоса потока.\n\n"
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#разбор@tradingnadannyh"
    )
    return text, session_chart(rows, day)


# --------------------------------------------------------------------------- #
# Отчёт 5: утренний брифинг по итогам сессии
# --------------------------------------------------------------------------- #

MARKET_HISTORY = f"{ISS}/history/engines/futures/markets/forts/securities.json"

# Сколько сессий нужно для сравнения объёма с нормой.
BRIEF_SESSIONS = 11
# Минимальный оборот, чтобы актив попал в брифинг, млн рублей за сессию.
MIN_TURNOVER_MRUB = 300.0
# Минимальный открытый интерес в контрактах: на малой базе проценты бессмысленны.
MIN_OPEN_INTEREST = 10_000
# Ниже этого движения цену считаем стоящей на месте, %. Иначе шум в сотых долях
# процента описывался бы как падение или рост, чего в данных нет.
FLAT_BAND_PCT = 0.3


def market_day(date: str) -> pl.DataFrame:
    """Итоги торгов по всем фьючерсам за дату, с кэшем на диске.

    История в ISS неизменна, поэтому кэшируется навсегда: иначе каждый утренний
    запуск заново выкачивал бы десяток страниц на каждую из одиннадцати сессий.
    Возвращает пустой фрейм для выходных и праздников — это нормальный ответ, а
    не ошибка, и вызывающая сторона отличает их по height.
    """
    MOEX_CACHE.mkdir(parents=True, exist_ok=True)
    path = MOEX_CACHE / f"forts_{date}.parquet"
    if path.exists():
        return pl.read_parquet(path)

    frames, start = [], 0
    while True:
        chunk = _iss_block(
            _iss_get(MARKET_HISTORY, date=date, start=start, **{"iss.only": "history"}),
            "history",
        )
        if chunk.is_empty():
            break
        frames.append(chunk)
        start += 100
        if start > 3000:
            break

    schema = {
        "SECID": pl.Utf8, "ASSETCODE": pl.Utf8, "CLOSE": pl.Float64,
        "VALUE": pl.Float64, "VOLUME": pl.Int64, "OPENPOSITION": pl.Int64,
    }
    if not frames:
        df = pl.DataFrame(schema=schema)
    else:
        df = (
            pl.concat(frames, how="vertical_relaxed")
            .select(
                pl.col("SECID"),
                pl.col("ASSETCODE"),
                pl.col("CLOSE").cast(pl.Float64),
                pl.col("VALUE").cast(pl.Float64),
                pl.col("VOLUME").cast(pl.Int64),
                pl.col("OPENPOSITION").cast(pl.Int64),
            )
            .drop_nulls("CLOSE")
        )

    df.write_parquet(path, compression="zstd")
    return df


def trading_sessions(count: int = BRIEF_SESSIONS) -> list[tuple[str, pl.DataFrame]]:
    """Последние торговые сессии, свежая последней. Выходные отсеиваются данными.

    Календарь МОЕХ с праздниками не зашивается в код: пустой ответ ISS сам
    отвечает на вопрос, торговали в этот день или нет.
    """
    sessions: list[tuple[str, pl.DataFrame]] = []
    day = dt.date.today()
    # Запас на новогодние каникулы — самый долгий перерыв в календаре МОЕХ.
    for _ in range(count + 20):
        if len(sessions) >= count:
            break
        df = market_day(day.isoformat())
        if not df.is_empty():
            sessions.append((day.isoformat(), df))
        day -= dt.timedelta(days=1)

    if len(sessions) < 2:
        raise RuntimeError("ISS не отдал даже двух сессий для сравнения")
    return list(reversed(sessions))


def brief_frame(sessions: list[tuple[str, pl.DataFrame]]) -> pl.DataFrame:
    """Свод по базовому активу: цена ближней серии, суммарный интерес и оборот.

    Открытый интерес суммируется по всем сериям одного актива. На ближнем
    контракте перед экспирацией он падает почти до нуля — это перекладка в
    следующую серию, а не уход денег, и по одной серии брифинг сообщал бы
    массовый выход из позиций каждый квартал.
    """
    rows: list[dict] = []
    for date, df in sessions:
        # Цену берём у самой оборотистой серии: она и есть та, по которой актив
        # котируют, тогда как дальние серии могут стоять без сделок.
        front = df.sort("VALUE", descending=True).unique("ASSETCODE", keep="first")
        agg = df.group_by("ASSETCODE").agg(
            pl.col("OPENPOSITION").sum().alias("интерес"),
            (pl.col("VALUE").sum() / 1e6).alias("оборот_млн"),
        )
        rows.append(
            front.select("ASSETCODE", pl.col("CLOSE").alias("цена"), "SECID")
            .join(agg, on="ASSETCODE")
            .with_columns(pl.lit(date).alias("дата"))
        )

    return pl.concat(rows).sort("дата")


def brief_changes(frame: pl.DataFrame) -> pl.DataFrame:
    """Изменения за последнюю сессию против предыдущей и против нормы объёма."""
    dates = frame["дата"].unique().sort().to_list()
    last, prev = dates[-1], dates[-2]

    cur = frame.filter(pl.col("дата") == last)
    old = frame.filter(pl.col("дата") == prev).select(
        "ASSETCODE",
        pl.col("цена").alias("цена_пред"),
        pl.col("интерес").alias("интерес_пред"),
    )
    # Норма считается по сессиям до последней, иначе аномалия размывала бы сама себя.
    norm = (
        frame.filter(pl.col("дата") != last)
        .group_by("ASSETCODE")
        .agg(pl.col("оборот_млн").median().alias("оборот_норма"))
    )

    return (
        cur.join(old, on="ASSETCODE")
        .join(norm, on="ASSETCODE")
        .filter(
            (pl.col("оборот_млн") >= MIN_TURNOVER_MRUB)
            & (pl.col("цена_пред") > 0)
            # Норма тоже должна быть содержательной. Иначе только что запущенный
            # контракт с почти нулевой медианой всегда выигрывает в «разы от нормы»
            # и в процентах прироста интереса: это низкая база, а не событие.
            & (pl.col("оборот_норма") >= MIN_TURNOVER_MRUB)
            & (pl.col("интерес_пред") >= MIN_OPEN_INTEREST)
        )
        .with_columns(
            ((pl.col("цена") / pl.col("цена_пред") - 1) * 100).alias("цена_%"),
            ((pl.col("интерес") / pl.col("интерес_пред") - 1) * 100).alias("интерес_%"),
            (pl.col("оборот_млн") / pl.col("оборот_норма")).alias("оборот_к_норме"),
        )
    )


def brief_chart(df: pl.DataFrame, date: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "morning_brief.png"

    top = df.sort(pl.col("цена_%").abs(), descending=True).head(10)
    names = top["ASSETCODE"].to_list()
    price = top["цена_%"].to_numpy()
    interest = top["интерес_%"].to_numpy()

    y = np.arange(len(names))
    # Две панели со своими шкалами: интерес меняется на десятки процентов, цена на
    # единицы, и на общей оси ценовые столбцы вырождаются в незаметные полоски.
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(11, 0.55 * len(names) + 2.6), sharey=True
    )

    for ax, values, title, color in (
        (left, price, "цена ближней серии", "tab:blue"),
        (right, interest, "открытый интерес по всем сериям", "tab:gray"),
    ):
        ax.barh(y, values, color=color)
        ax.axvline(0, color="black", lw=1)
        span = max(np.abs(values)) * 1.35 or 1.0
        ax.set_xlim(-span, span)
        for i, v in enumerate(values):
            ax.text(
                v + span * 0.03 * (1 if v >= 0 else -1), i, f"{v:+.1f}%",
                va="center", ha="left" if v >= 0 else "right", fontsize=9,
            )
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("изменение за сессию, %")
        ax.grid(axis="x", alpha=0.3)

    left.set_yticks(y)
    left.set_yticklabels(names)
    left.invert_yaxis()
    fig.suptitle(
        f"Итоги сессии МОЕХ {date}\n"
        "интерес просуммирован по всем сериям, чтобы перекладка не читалась как выход",
        fontsize=12,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def morning_report() -> tuple[str, Path]:
    sessions = trading_sessions()
    frame = brief_frame(sessions)
    df = brief_changes(frame)
    if df.is_empty():
        raise RuntimeError("после фильтра по оборотам не осталось активов")

    date = sessions[-1][0]

    movers = df.sort(pl.col("цена_%").abs(), descending=True).head(3)
    movers_txt = "\n".join(
        f"<b>{html.escape(r['ASSETCODE'])}</b> {r['цена_%']:+.2f}%, "
        f"интерес {r['интерес_%']:+.1f}%"
        for r in movers.iter_rows(named=True)
    )

    inflow = df.sort("интерес_%", descending=True).head(1).row(0, named=True)
    outflow = df.sort("интерес_%").head(1).row(0, named=True)
    busiest = df.sort("оборот_к_норме", descending=True).head(1).row(0, named=True)

    # Рост цены вместе с ростом интереса означает приход новых денег, а рост цены
    # при падении интереса — закрытие шортов. Различие в новостях не встречается,
    # хотя данные для него публикует сама биржа.
    move = inflow["цена_%"]
    if abs(move) < FLAT_BAND_PCT:
        inflow_note = "Цена при этом стоит на месте — позиции копятся тихо"
    elif move > 0:
        inflow_note = "Цена растёт вместе с интересом — заходят новые деньги, "\
                      "а не закрываются шорты"
    else:
        inflow_note = "Интерес растёт на падении цены — набирают шорт или ловят дно"

    text = (
        f"<b>Итоги сессии МОЕХ {html.escape(date)}</b>\n\n"
        "Считаю сам по данным биржи: цена ближней серии, открытый интерес по всем "
        "сериям и оборот против нормы за десять сессий.\n\n"
        "<b>Сильнее всего сдвинулись:</b>\n"
        + movers_txt
        + f"\n\n<b>Деньги пришли в {html.escape(inflow['ASSETCODE'])}</b>: интерес "
        f"{inflow['интерес_%']:+.1f}% при цене {move:+.2f}%. {inflow_note}.\n\n"
        f"<b>Ушли из {html.escape(outflow['ASSETCODE'])}</b>: интерес "
        f"{outflow['интерес_%']:+.1f}%.\n\n"
        f"<b>Оборот выше нормы</b> у {html.escape(busiest['ASSETCODE'])}: "
        f"{busiest['оборот_к_норме']:.1f}× от медианы, "
        f"{_num(round(busiest['оборот_млн']))} млн рублей."
        + "\n\nПочему интерес суммируется по сериям: на ближнем контракте перед "
        "экспирацией он падает почти до нуля, но это перекладка в следующую серию, "
        "а не выход из позиций. По одной серии брифинг сообщал бы массовый уход "
        "денег каждый квартал.\n\n"
        "Источник — открытый ISS Московской биржи, без посредников. "
        "Код: github.com/AASuvorov/orderflow\n\n"
        "#сессия@tradingnadannyh"
    )
    return text, brief_chart(df, date)


# --------------------------------------------------------------------------- #

REPORTS = {
    "morning": morning_report,
    "funding": funding_report,
    "costs": costs_report,
    "spread": spread_report,
    "session": session_report,
}

# Утренняя рубрика по дням недели. Понедельник = 0.
# Выходные пропускаются: МОЕХ закрыта, а дочитывание в субботу и воскресенье ниже.
#
# Брифинг стоит трижды в неделю, потому что он единственный отвечает на вопрос
# «что произошло вчера» — тот самый, с которым читатель открывает канал утром.
# Остальные отчёты отвечают на вопрос «как устроен рынок» и не устаревают за день.
SCHEDULE = {0: "morning", 1: "session", 2: "morning", 3: "spread", 4: "morning"}


def daily(*, dry_run: bool = False) -> None:
    """Публикует отчёт этого дня недели, с запасными вариантами.

    Расписание запускается без присмотра, поэтому отказ одного источника не
    должен приводить к молчанию: если сегодняшний отчёт не собрался, берётся
    следующий из списка. Молчаливый пропуск хуже повтора — он ломает
    регулярность, от которой зависит и индексация, и попадание в рекомендации.
    """
    weekday = time.localtime().tm_wday
    if weekday not in SCHEDULE:
        print(f"выходной (день {weekday}) — публикации нет")
        return

    order = [SCHEDULE[weekday]] + [n for n in REPORTS if n != SCHEDULE[weekday]]
    errors: list[str] = []
    for name in order:
        try:
            text, image = REPORTS[name]()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            print(f"{name} не собрался ({exc}), пробую следующий")
            continue
        print(f"отчёт дня: {name}")
        publish(text, image, dry_run=dry_run)
        if not dry_run:
            stamp_published(name)
        return

    raise RuntimeError("ни один отчёт не собрался:\n  " + "\n  ".join(errors))


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv

    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        print(f"команды: daily, check; отчёты: {', '.join(REPORTS)}")
        return

    if args[0] == "check":
        check()
        return

    if args[0] == "daily":
        daily(dry_run=dry)
        return

    name = args[0]
    if name not in REPORTS:
        raise SystemExit(f"неизвестный отчёт: {name}. Доступны: {', '.join(REPORTS)}, check")

    text, image = REPORTS[name]()
    publish(text, image, dry_run=dry)


if __name__ == "__main__":
    main(sys.argv[1:])
