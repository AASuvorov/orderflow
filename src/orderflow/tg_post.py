"""Автопубликация собственных замеров в Telegram-канал.

Зачем это отдельным модулем. Ценность канала держится на том, что цифры в нём
свои и проверяемые, а не пересказанные. Такие цифры уже считаются пайплайном
проекта — значит, публикация должна быть автоматической, а ручным остаётся только
решение, что именно проверять.

Что здесь принципиально не делается: пересказ чужих новостей. Новостная лента —
самая дешёвая вертикаль в Telegram (CPM в 4–6 раз ниже финансовой), и гонка за
скорость проигрывается ботам, которые работают годами. Здесь публикуются только
измерения, которых нет больше нигде.

Настройка один раз:
  1. @BotFather -> /newbot -> получить токен
  2. добавить бота администратором канала с правом публикации
  3. export TG_BOT_TOKEN=...
     export TG_CHAT_ID=@tradingnadannyh
  4. uv run python tg_post.py check

Запуск:
  uv run python tg_post.py funding --dry-run   # посмотреть текст, ничего не отправляя
  uv run python tg_post.py funding             # опубликовать
  uv run python tg_post.py costs

Расписание — раз в неделю, чтобы цифры успевали измениться. На сервере systemd
по образцу deploy/install.sh, локально проще через cron:
  0 10 * * 1 cd .../src/orderflow && ../../.venv/bin/python tg_post.py funding
"""

from __future__ import annotations

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
from moex_feasibility import cost_bps

API = "https://api.telegram.org/bot{token}/{method}"
# Telegram режет подпись к фото на 1024 символах, обычное сообщение — на 4096.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "tg"

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

REPORTS = {
    "funding": funding_report,
    "costs": costs_report,
}


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv

    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        print(f"отчёты: {', '.join(REPORTS)}, check")
        return

    if args[0] == "check":
        check()
        return

    name = args[0]
    if name not in REPORTS:
        raise SystemExit(f"неизвестный отчёт: {name}. Доступны: {', '.join(REPORTS)}, check")

    text, image = REPORTS[name]()
    publish(text, image, dry_run=dry)


if __name__ == "__main__":
    main(sys.argv[1:])
