"""Сторож: раз в сутки проверяет, что сбор действительно работает.

Молчаливый отказ — главный риск схемы без присмотра. Сборщик может исправно
запускаться и при этом ничего не собирать: ISS сменил формат, контракт
экспирировался и не подхватился, кончилось место, изменился адрес API. Через
месяц окажется, что данных нет, а восстановить их невозможно.

Проверяется четыре вещи:
  1. по каждому контракту есть файл за последнюю торговую сессию;
  2. число тиков не обвалилось относительно медианы прошлых дней;
  3. в истории нет пропущенных сессий;
  4. на диске остаётся место.

Если что-то не так — сообщение в Telegram. Без настроенного бота сторож всё
равно полезен: он пишет диагноз в журнал systemd и завершается с кодом 1.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import polars as pl
import requests

from moex_ticks import CACHE, DATA_ROOT, MSK, contract_sessions

MIN_RATIO = 0.25  # тиков меньше четверти медианы — повод насторожиться
MIN_FREE_GB = 2.0
TOKEN_FILE = Path.home() / ".config" / "telegram" / "orderflow"
PULL_STAMP = DATA_ROOT / "meta" / "last_pull"
POST_STAMP = DATA_ROOT / "meta" / "last_post"
BACKUP_STALE_DAYS = 14  # ноутбук могут не включать в отпуске — раньше не тревожим


def notify(text: str) -> bool:
    """Отправляет сообщение, если бот настроен. Токен и чат — из файла или окружения.

    Формат файла: две строки — токен бота и id чата.
    """
    token = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT", "")
    if not (token and chat) and TOKEN_FILE.exists():
        parts = TOKEN_FILE.read_text(encoding="utf-8").split()
        if len(parts) >= 2:
            token, chat = parts[0], parts[1]
    if not (token and chat):
        return False

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=20,
        ).raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"уведомление не отправлено: {exc}")
        return False


def check() -> tuple[list[str], list[str]]:
    """Возвращает список проблем и список строк отчёта."""
    problems: list[str] = []
    report: list[str] = []

    if not CACHE.exists():
        return ["каталога с данными нет вообще"], []

    free_gb = shutil.disk_usage(DATA_ROOT).free / 1e9
    if free_gb < MIN_FREE_GB:
        problems.append(f"на диске осталось {free_gb:.1f} ГБ")

    today = datetime.now(MSK).date()
    contracts = sorted(p for p in CACHE.glob("*") if p.is_dir())
    if not contracts:
        return ["ни одного контракта в кэше"], []

    for d in contracts:
        files = sorted(d.glob("*.parquet"))
        if not files:
            problems.append(f"{d.name}: нет файлов")
            continue

        counts = [pl.read_parquet(f, columns=["tradeno"]).height for f in files]
        last_day, last_n = files[-1].stem, counts[-1]

        # Свежесть: в рабочий день данные должны быть за сегодня или вчера.
        age = (today - datetime.fromisoformat(last_day).date()).days
        weekday_gap = age > (3 if today.weekday() == 0 else 1)
        if weekday_gap:
            problems.append(f"{d.name}: последние данные за {last_day} ({age} дн. назад)")

        # Обвал объёма: сравниваем с медианой предыдущих дней.
        if len(counts) >= 4:
            median = sorted(counts[:-1])[len(counts[:-1]) // 2]
            if median and last_n < median * MIN_RATIO:
                problems.append(
                    f"{d.name}: за {last_day} только {last_n:,} тиков "
                    f"против медианы {median:,}"
                )

        # Пропущенные сессии.
        try:
            missing = contract_sessions(d.name, files[0].stem, files[-1].stem) - {
                f.stem for f in files
            }
        except Exception as exc:
            missing = set()
            report.append(f"{d.name}: календарь не проверен ({exc})")
        if missing:
            problems.append(
                f"{d.name}: пропущено сессий {len(missing)}: "
                f"{', '.join(sorted(missing)[:5])}"
            )

        report.append(f"{d.name}: {len(files)} дн., {sum(counts):,} тиков, посл. {last_day}")

    report.append(f"свободно на диске: {free_gb:.1f} ГБ")

    # Свежесть резервной копии. Метку ставит ноутбук после удачной выгрузки, но
    # докладывает о ней сервер: молча испортившаяся копия бесполезна, а
    # проверять её должен тот, кто и так присылает отчёты.
    if PULL_STAMP.exists():
        last = datetime.fromisoformat(PULL_STAMP.read_text(encoding="utf-8").strip())
        days = (datetime.now(MSK) - last).days
        report.append(f"копия на ноутбуке: {days} дн. назад")
        if days > BACKUP_STALE_DAYS:
            problems.append(f"резервная копия не обновлялась {days} дн.")
    else:
        report.append("копия на ноутбуке: выгрузок ещё не было")

    problems.extend(check_posting(today, report))

    return problems, report


def check_posting(today, report: list[str]) -> list[str]:
    """Проверяет, что автопостинг в канал не замолчал.

    Отказ здесь незаметнее, чем отказ сбора: данные продолжают копиться, диск не
    кончается, ошибок в журнале нет — просто канал перестаёт обновляться. По
    выходным пропуск ожидаем, публикация идёт только по будням.
    """
    if not POST_STAMP.exists():
        # До первого автопоста тревожить не о чем: постинг может быть не настроен.
        report.append("автопостинг: публикаций ещё не было")
        return []

    raw = POST_STAMP.read_text(encoding="utf-8").split()
    last = datetime.fromisoformat(raw[0]).date()
    name = raw[1] if len(raw) > 1 else "?"
    age = (today - last).days

    # В субботу свежайшим будет пятничный пост, в воскресенье — двухдневный.
    allowed = 3 if today.weekday() in (5, 6) else 1
    report.append(f"автопостинг: {last} ({name}), {age} дн. назад")
    if age > allowed:
        return [f"канал не обновлялся {age} дн., последний пост {last} ({name})"]
    return []


def pull_ok() -> None:
    """Отмечает удачную выгрузку. Ноутбук сам ни о чём не сообщает — он только
    ставит метку, а докладывает о ней сторож на сервере."""
    PULL_STAMP.parent.mkdir(parents=True, exist_ok=True)
    PULL_STAMP.write_text(datetime.now(MSK).isoformat(), encoding="utf-8")


def send_test() -> bool:
    """Проверка канала связи заметно помеченным сообщением.

    Тестовая тревога, неотличимая от настоящей, обесценивает настоящую: получив
    такую, уже не знаешь, реагировать или нет. Поэтому пометка обязательна.
    """
    ok = notify(
        "ПРОВЕРКА СВЯЗИ — реагировать не нужно\n\n"
        "Это тестовое сообщение. Настоящие тревоги приходят без этой пометки "
        "и начинаются словами «Сбор тиков МОЕХ»."
    )
    print(f"проверочное сообщение отправлено: {ok}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        raise SystemExit(0 if send_test() else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "pull-ok":
        pull_ok()
        raise SystemExit(0)

    problems, report = check()
    stamp = datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК")

    print(f"=== сторож {stamp} ===")
    for line in report:
        print(f"  {line}")

    if problems:
        print("\nПРОБЛЕМЫ:")
        for p in problems:
            print(f"  ! {p}")
        sent = notify("Сбор тиков МОЕХ: проблемы\n\n" + "\n".join(f"• {p}" for p in problems))
        print(f"\nуведомление отправлено: {sent}")
        raise SystemExit(1)

    print("\nвсё в порядке")
    # Недельная сводка по понедельникам. Это единственный сигнал, по которому
    # можно заметить полную смерть сервера: изнутри он о ней сообщить не может,
    # поэтому признаком беды становится не сообщение, а его отсутствие.
    if datetime.now(MSK).weekday() == 0:
        notify(
            "Сбор тиков МОЕХ: всё в порядке\n\n"
            + "\n".join(report)
            + "\n\nЭта сводка приходит по понедельникам. Если её не было — "
            "значит сервер не работает."
        )
