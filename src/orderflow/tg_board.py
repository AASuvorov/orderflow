"""Живая сводка в закрепе: ставка ЦБ, официальные курсы, крипта.

Зачем это в закреп, а не отдельным постом. Закреплённое сообщение — первое, что
видит новый человек, и единственный текст канала, который можно менять сколько
угодно раз без уведомления подписчикам. Значит это единственное место, где цифры
могут быть свежими всегда, не требуя ни одного поста. Отдельный пост с курсами
пришлось бы публиковать ежедневно, он вытеснял бы содержательные замеры и через
неделю превратил бы канал в табло.

Второй смысл — доказательство. Канал обещает, что здесь считают по первичным
источникам; сводка, которая обновляется сама и сходится с сайтом ЦБ, показывает
это раньше, чем человек дочитает описание.

Источники только первоисточники: ставка и курсы — у ЦБ, крипта — у Binance.
Никаких агрегаторов, чтобы не пересказывать чужие опечатки.

Запуск:
  python tg_board.py            # обновить закреп
  python tg_board.py --dry-run  # показать текст
"""

from __future__ import annotations

import datetime as dt
import re
import sys

import requests

from tg_post import API, _credentials

CBR_DAILY = "https://www.cbr.ru/scripts/XML_daily.asp"
FAPI_BASE = "https://fapi.binance.com/fapi/v1"

# Сообщение канала, в котором живёт закреп. Публиковать новое нельзя: у бота нет
# права закреплять, а замена закрепа стоила бы уведомления всем подписчикам.
PIN_MESSAGE_ID = 2

BOARD_ASSETS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
PERIODS_PER_YEAR = 3 * 365  # фандинг каждые 8 часов

# Постоянная часть закрепа. Держится здесь, а не читается из Telegram, потому что
# getChat отдаёт текст без разметки — прочитав его и записав обратно, мы потеряли
# бы всё оформление. Правится этот текст здесь и только здесь.
MANIFESTO = (
    "<b>Здесь считают, а не верят.</b>\n\n"
    "Я проверяю утверждения о заработке на рынке так, как проверяют гипотезы: "
    "считаю издержки, считаю требуемую точность, замеряю на данных и публикую "
    "результат вместе с кодом. Даже когда результат — «не работает». Особенно "
    "когда «не работает»: об этом почти никто не пишет, потому что на этом нечего "
    "продать.\n\n"
    "За плечами пять собственных гипотез о микроструктуре рынка. Все пять закрыты: "
    "четыре на измеренных данных, одна структурно. Код открыт целиком, цифры "
    "перепроверяются: github.com/AASuvorov/orderflow\n\n"
    "<b>Чем этот канал отличается.</b> Здесь нет пересказа новостей. Цифры "
    "считаются автоматически из первичных источников — открытого ISS Московской "
    "биржи и API Binance — и часть из них не существует больше нигде: я собираю "
    "тики МОЕХ со стороной инициатора сделки, а ISS отдаёт это поле только за "
    "текущую сессию, истории нет, и базу приходится накапливать самому.\n\n"
    "<b>Что выходит и когда</b>\n\n"
    "#итогисессии — понедельник, среда, пятница. Итоги сессии МОЕХ: цена ближней "
    "серии, открытый интерес по всем сериям, оборот против нормы.\n\n"
    "#объёмы — вторник. Куда давил агрессор: перекос потока заявок по контрактам.\n\n"
    "#фандинг — четверг. Сколько платит нейтральная позиция на перпетуалах.\n\n"
    "#итогинедели — суббота. Что изменилось за неделю в замерах, а не в новостях.\n\n"
    "Отдельно, по поводу: #ставкаЦБ, #экспирация, #издержки, #маркетмейкинг — "
    "когда происходит событие, которое меняет цифры.\n\n"
    "<b>Чего здесь не будет:</b> сигналов, прогнозов, «иксов», рекламы брокеров и "
    "пересказа чужих новостей.\n\n"
    "Спорить приветствуется — с цифрами, в комментариях."
)


def _num(x: float, digits: int = 2) -> str:
    """Разряды пробелом, как принято в русском тексте."""
    return f"{x:,.{digits}f}".replace(",", " ")


def cbr_rates() -> dict:
    """Официальные курсы ЦБ на сегодня плюс ключевая ставка.

    Курс ЦБ, а не биржевой: он один и тот же для всех, публикуется официально и
    не требует оговорок про то, у какого брокера и в какой момент он снят.
    """
    r = requests.get(CBR_DAILY, timeout=30)
    r.encoding = "windows-1251"
    out: dict = {"дата": re.search(r'Date="([^"]+)"', r.text).group(1)}
    for code in ("USD", "EUR", "CNY"):
        m = re.search(
            rf"<CharCode>{code}</CharCode>.*?<Nominal>(\d+)</Nominal>"
            rf".*?<Value>([\d,]+)</Value>",
            r.text,
            re.S,
        )
        if m:
            out[code] = float(m.group(2).replace(",", ".")) / int(m.group(1))

    from tg_events import key_rate_history

    out["ставка"] = key_rate_history(days=40)[-1][1]
    return out


def crypto_snapshot() -> list[dict]:
    """Цена, суточное изменение и текущий фандинг по основным перпетуалам."""
    tick = {
        x["symbol"]: x
        for x in requests.get(f"{FAPI_BASE}/ticker/24hr", timeout=30).json()
    }
    prem = {
        x["symbol"]: x
        for x in requests.get(f"{FAPI_BASE}/premiumIndex", timeout=30).json()
    }
    rows = []
    for sym in BOARD_ASSETS:
        t, p = tick.get(sym), prem.get(sym)
        if not t or not p:
            continue
        rows.append({
            "тикер": sym.replace("USDT", ""),
            "цена": float(t["lastPrice"]),
            "сутки_%": float(t["priceChangePercent"]),
            "фандинг_год_%": float(p["lastFundingRate"]) * PERIODS_PER_YEAR * 100,
        })
    return rows


CALENDAR_LINES = 4


def calendar_block() -> list[str]:
    """Что из макростатистики выходит сегодня и что уже вышло.

    Именно в закрепе, а не постом: календарь интересен ровно один день и обновляется
    несколько раз за него. Пост с ним пришлось бы публиковать ежедневно, вытесняя
    замеры, а к вечеру он всё равно устаревал бы. Отдельным постом выходит только
    расхождение факта с прогнозом — там уже есть что сказать.
    """
    try:
        from tg_events import calendar_today, ru_country, ru_indicator
    except Exception:
        return []

    try:
        events = [
            e for e in calendar_today()
            if e.get("importance") in ("high", "medium")
        ]
    except Exception as exc:
        print(f"календарь недоступен ({exc}) — сводка без него")
        return []
    if not events:
        return []

    ahead, done = [], []
    for e in sorted(events, key=lambda x: x["time"]):
        when = dt.datetime.fromisoformat(e["time"].replace("Z", "+00:00"))
        msk = (when + dt.timedelta(hours=3)).strftime("%H:%M")
        label = f"{ru_country(e['countryCode'])}: {ru_indicator(e['name'])}"
        a, f = e.get("actual"), e.get("forecast")
        if a is None:
            hint = f" (ждут {f:g})" if isinstance(f, (int, float)) else ""
            ahead.append(f"{msk} — {label}{hint}")
        elif isinstance(a, (int, float)) and isinstance(f, (int, float)):
            done.append((abs(a - f), f"{label}: <b>{a:g}</b> против {f:g}"))
    # Из вышедшего показываем не последнее по времени, а самое расходящееся с
    # прогнозом: попадание в прогноз не новость, промах — новость.
    done.sort(key=lambda x: -x[0])

    lines = ["", "<b>Сегодня в календаре</b>"]
    # Впереди — важнее: вышедшее уже можно посмотреть где угодно, а ближайшее
    # объясняет, почему рынок может дёрнуться в следующий час.
    if ahead:
        lines += [f"· {x}" for x in ahead[:CALENDAR_LINES]]
        if len(ahead) > CALENDAR_LINES:
            lines.append(f"· и ещё {len(ahead) - CALENDAR_LINES} публикаций")
    if done:
        room = max(1, CALENDAR_LINES - len(ahead[:CALENDAR_LINES]))
        lines.append("Уже вышло, сильнее всего мимо прогноза:")
        lines += [f"· {x}" for _, x in done[:room]]
    return lines


def board() -> str:
    """Блок живых цифр, который встаёт над постоянной частью закрепа."""
    c = cbr_rates()
    crypto = crypto_snapshot()
    now = dt.datetime.now().strftime("%H:%M")

    lines = [
        f"<b>Живые цифры · {c['дата']}, {now} МСК</b>",
        "",
        f"Ключевая ставка ЦБ: <b>{c['ставка']:.2f}%</b> — это и есть планка, "
        "которую обязана побить любая рублёвая схема.",
        f"Курсы ЦБ: доллар {_num(c['USD'])} ₽, юань {_num(c['CNY'])} ₽, "
        f"евро {_num(c['EUR'])} ₽.",
        "",
    ]
    for r in crypto:
        # Фандинг важнее цены: цену покажет любой источник, а знак фандинга
        # говорит, кто кому платит за удержание позиции, и это уже вывод.
        who = "лонги платят шортам" if r["фандинг_год_%"] > 0 else "шорты платят лонгам"
        lines.append(
            f"<b>{r['тикер']}</b> {_num(r['цена'])} $ ({r['сутки_%']:+.1f}% за сутки), "
            f"фандинг {r['фандинг_год_%']:+.1f}% годовых — {who}."
        )
    lines += calendar_block()
    lines += [
        "",
        "Сводка обновляется сама, из ЦБ, Binance и календаря напрямую.",
        "———",
        "",
    ]
    return "\n".join(lines)


def update(*, dry_run: bool = False) -> None:
    text = board() + MANIFESTO
    print(text)
    print(f"\nсимволов: {len(text)}")
    if len(text) > 4096:
        raise RuntimeError(f"закреп {len(text)} символов, лимит 4096")
    if dry_run:
        print("--dry-run: не отправлено")
        return

    token, chat = _credentials()
    r = requests.post(
        API.format(token=token, method="editMessageText"),
        data={
            "chat_id": chat,
            "message_id": PIN_MESSAGE_ID,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": '{"is_disabled":true}',
        },
        timeout=60,
    ).json()
    if not r.get("ok"):
        desc = r.get("description", "")
        # Telegram отвергает правку, если текст совпал с прежним. Это не ошибка:
        # значит цифры не изменились с прошлого запуска.
        if "not modified" in desc:
            print("цифры не изменились — правка не требуется")
            return
        raise RuntimeError(f"editMessageText: {desc}")
    print("закреп обновлён")


if __name__ == "__main__":
    update(dry_run="--dry-run" in sys.argv)
