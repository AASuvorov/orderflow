"""Настройка Telegram-уведомлений для сторожа.

Токен бота вводится не в чат и не в командную строку, а в файл: так секрет не
попадает ни в историю команд, ни в переписку. Идентификатор чата скрипт
определяет сам — по сообщению, которое вы отправили боту.

Порядок:
  1. в Telegram найдите @BotFather, отправьте /newbot и следуйте подсказкам;
  2. полученный токен положите в файл (только токен, одной строкой):
         mkdir -p ~/.config/telegram && chmod 700 ~/.config/telegram
         printf '%s' 'ТОКЕН' > ~/.config/telegram/orderflow
         chmod 600 ~/.config/telegram/orderflow
  3. напишите своему боту любое сообщение, например /start;
  4. запустите этот скрипт — он допишет в файл id чата и пришлёт проверку.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import requests

TOKEN_FILE = Path.home() / ".config" / "telegram" / "orderflow"
REMOTE_FILE = "/root/.config/telegram/orderflow"


def _wait_for_message(api: str, minutes: int) -> dict:
    """Ждёт первое сообщение боту. Пока его нет, id чата узнать невозможно."""
    deadline = time.monotonic() + minutes * 60
    while True:
        upd = requests.get(f"{api}/getUpdates", params={"limit": 20}, timeout=25).json()
        if upd.get("result"):
            return upd
        if time.monotonic() >= deadline:
            return upd
        time.sleep(5)


def _deploy(host: str, token: str, chat_id: int) -> None:
    """Переносит настройки на сервер: сторож живёт там, значит и слать ему.

    Файл создаётся через stdin, чтобы токен не попал в список процессов сервера.
    """
    cmd = (
        f"mkdir -p $(dirname {REMOTE_FILE}) && chmod 700 $(dirname {REMOTE_FILE}) "
        f"&& cat > {REMOTE_FILE} && chmod 600 {REMOTE_FILE}"
    )
    subprocess.run(
        ["ssh", host, cmd],
        input=f"{token}\n{chat_id}\n".encode(),
        check=True,
    )
    print(f"настройки перенесены на {host}")


def main() -> int:
    if not TOKEN_FILE.exists():
        print(f"нет файла {TOKEN_FILE} — сначала положите в него токен бота")
        print(__doc__)
        return 1

    parts = TOKEN_FILE.read_text(encoding="utf-8").split()
    if not parts:
        print(f"файл {TOKEN_FILE} пуст")
        return 1
    token = parts[0]

    api = f"https://api.telegram.org/bot{token}"
    me = requests.get(f"{api}/getMe", timeout=20).json()
    if not me.get("ok"):
        print(f"токен не принят: {me.get('description', me)}")
        return 1
    print(f"бот: @{me['result']['username']}")

    # id чата берём из входящих сообщений: приватный чат появляется здесь
    # только после того, как вы сами написали боту.
    wait = 0
    if "--wait" in sys.argv:
        wait = int(sys.argv[sys.argv.index("--wait") + 1])
        print(f"жду сообщение боту до {wait} мин...", flush=True)

    upd = (
        _wait_for_message(api, wait)
        if wait
        else requests.get(f"{api}/getUpdates", params={"limit": 20}, timeout=20).json()
    )
    chats = {
        m["chat"]["id"]: m["chat"].get("username") or m["chat"].get("first_name", "")
        for u in upd.get("result", [])
        if (m := u.get("message") or u.get("channel_post"))
    }
    if not chats:
        print("бот не получил ни одного сообщения — напишите ему /start и повторите")
        return 1

    chat_id, who = next(iter(chats.items()))
    if len(chats) > 1:
        print(f"найдено чатов: {len(chats)}, беру первый")
    print(f"чат: {who} ({chat_id})")

    TOKEN_FILE.write_text(f"{token}\n{chat_id}\n", encoding="utf-8")
    TOKEN_FILE.chmod(0o600)

    r = requests.post(
        f"{api}/sendMessage",
        json={"chat_id": chat_id, "text": "Сбор тиков МОЕХ: уведомления подключены."},
        timeout=20,
    ).json()
    if not r.get("ok"):
        print(f"сообщение не ушло: {r.get('description', r)}")
        return 1

    print(f"готово, настройки записаны в {TOKEN_FILE} — проверьте Telegram")

    if "--deploy" in sys.argv:
        _deploy(sys.argv[sys.argv.index("--deploy") + 1], token, chat_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
