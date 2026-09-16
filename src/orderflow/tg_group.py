"""Оформление канала и группы обсуждений: название, описание, аватар, правила.

Почему это код, а не разовая настройка руками. Описание канала и правила группы —
такой же продукт, как посты: они правятся по мере того, как меняются рубрики, и
руками это делается один раз, а потом расходится с действительностью. Здесь текст
лежит рядом с кодом, который его ставит, поэтому правка описания — это правка файла.

Модуль идемпотентен: сравнивает то, что уже стоит, с тем, что должно стоять, и
трогает только расхождения. Значит его можно вызывать по таймеру и не думать,
запускался он раньше или нет.

Группа настраивается тем же запуском, но только если бот в ней администратор.
Пока прав нет, модуль сообщает об этом и завершается без ошибки: таймер не должен
падать из-за того, что человек ещё не нажал кнопку.

Запуск:
  python tg_group.py            # привести канал и группу к описанному здесь виду
  python tg_group.py --dry-run  # показать, что изменилось бы
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from tg_post import API, TICKS_ROOT, _credentials

AVATAR = Path(__file__).resolve().parents[2] / "content" / "telegram" / "avatar.png"
STATE = TICKS_ROOT.parent / "meta" / "group.json"

CHANNEL_TITLE = "Трейдинг на данных"
# Описание канала: 255 символов у Telegram, поэтому здесь только то, что отвечает
# на вопрос «почему это стоит читать», без перечисления рубрик — они в закрепе.
CHANNEL_ABOUT = (
    "Проверяю утверждения о заработке на рынке так, как проверяют гипотезы: "
    "считаю издержки, замеряю на данных, публикую результат вместе с кодом. "
    "Даже когда результат — «не работает». Фьючерсы МОЕХ и крипта в цифрах. "
    "Архив: aasuvorov.github.io/orderflow"
)

GROUP_TITLE = "Трейдинг на данных — обсуждение"
GROUP_ABOUT = (
    "Обсуждение замеров из канала @tradingnadannyh. Спорить цифрами приветствуется, "
    "сигналы и реклама удаляются. Прогнозов здесь не дают — ни я, ни вы."
)

# Правила закрепляются в группе. Написаны как объяснение, а не как список запретов:
# запреты выполняют, когда понимают, зачем они, и первый абзац отвечает именно на это.
GROUP_RULES = (
    "<b>Зачем эта группа.</b> В канале выходят замеры: сколько стоит круг на "
    "фьючерсах, что платит фандинг, куда давил агрессор, работает ли очередная "
    "схема. Замер можно проверить и можно оспорить — для этого и есть группа. "
    "Лучший разговор здесь звучит так: «у тебя вышло 0.54 базисного пункта, а у "
    "меня 0.8, вот мой расчёт». Такому я рад больше всего: если я ошибся, это "
    "надо найти.\n\n"
    "<b>Что тут не работает.</b>\n"
    "· Просьбы дать прогноз или сигнал. Я не знаю, куда пойдёт цена, и никто в "
    "этой группе не знает. Канал ровно об этом.\n"
    "· Реклама, каналы с «точками входа», приглашения в закрытые чаты — удаляю без "
    "обсуждения.\n"
    "· Спор без цифр. «Это не работает» и «это работает» одинаково бесполезны, пока "
    "нет расчёта или данных.\n\n"
    "<b>Что приветствуется.</b> Возражения с расчётом. Указания на ошибку в коде — "
    "он открыт целиком. Просьбы замерить конкретную гипотезу: если её можно "
    "проверить на доступных данных, я замерю и опубликую результат, каким бы он ни "
    "вышел.\n\n"
    "Код и данные: github.com/AASuvorov/orderflow\n"
    "Архив замеров: aasuvorov.github.io/orderflow"
)


def _call(method: str, token: str, **payload):
    files = payload.pop("files", None)
    r = requests.post(API.format(token=token, method=method), data=payload,
                      files=files, timeout=60)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"{method}: {body.get('description')}")
    return body["result"]


def _try(method: str, token: str, **payload) -> tuple[bool, str]:
    """Вызов, от которого допустим отказ по правам.

    Отсутствие прав — не сбой программы, а состояние настроек, и отличать его от
    настоящей ошибки нужно по коду ответа, а не по тексту: иначе таймер сообщал бы
    о поломке каждый час, пока бота не добавят в группу.
    """
    try:
        _call(method, token, **payload)
        return True, "готово"
    except RuntimeError as exc:
        return False, str(exc)


def channel_state(token: str, chat: str) -> dict:
    return _call("getChat", token, chat_id=chat)


def setup_channel(token: str, chat: str, *, dry_run: bool) -> int | None:
    """Приводит канал к описанному виду. Возвращает id связанной группы."""
    info = channel_state(token, chat)
    changes = []

    if info.get("title") != CHANNEL_TITLE:
        changes.append(("setChatTitle", {"title": CHANNEL_TITLE}))
    if (info.get("description") or "").strip() != CHANNEL_ABOUT:
        changes.append(("setChatDescription", {"description": CHANNEL_ABOUT}))
    if not info.get("photo") and AVATAR.exists():
        changes.append(("setChatPhoto", {"photo": AVATAR}))

    if not changes:
        print("канал: всё уже на месте")
    for method, payload in changes:
        if dry_run:
            print(f"канал: {method} → {str(payload)[:90]}")
            continue
        if "photo" in payload:
            with payload["photo"].open("rb") as fh:
                ok, msg = _try(method, token, chat_id=chat, files={"photo": fh})
        else:
            ok, msg = _try(method, token, chat_id=chat, **payload)
        print(f"канал: {method} — {msg}")

    return info.get("linked_chat_id")


def setup_group(token: str, group_id: int, *, dry_run: bool) -> None:
    """Оформляет группу обсуждений и закрепляет правила.

    Правила публикуются один раз, а дальше правятся редактированием того же
    сообщения: новая публикация при каждом запуске засыпала бы группу копиями, а
    замена закрепа отправляла бы уведомление всем участникам.
    """
    try:
        info = _call("getChat", token, chat_id=group_id)
    except RuntimeError as exc:
        print(f"группа: доступа нет — {exc}")
        print(f"группа: добавьте @{_call('getMe', token)['username']} "
              "администратором в группу обсуждений, дальше настройка пройдёт сама")
        return

    if info.get("title") != GROUP_TITLE and not dry_run:
        print(f"группа: setChatTitle — {_try('setChatTitle', token, chat_id=group_id, title=GROUP_TITLE)[1]}")
    if (info.get("description") or "").strip() != GROUP_ABOUT and not dry_run:
        print(f"группа: setChatDescription — "
              f"{_try('setChatDescription', token, chat_id=group_id, description=GROUP_ABOUT)[1]}")
    if not info.get("photo") and AVATAR.exists() and not dry_run:
        with AVATAR.open("rb") as fh:
            print(f"группа: setChatPhoto — "
                  f"{_try('setChatPhoto', token, chat_id=group_id, files={'photo': fh})[1]}")

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    rules_id = state.get("правила")

    if dry_run:
        print(f"группа: правила {'обновились бы' if rules_id else 'опубликовались бы'}")
        return

    if rules_id:
        ok, msg = _try("editMessageText", token, chat_id=group_id, message_id=rules_id,
                       text=GROUP_RULES, parse_mode="HTML",
                       link_preview_options='{"is_disabled":true}')
        # «not modified» означает, что текст уже совпадает — это успех, а не сбой.
        print(f"группа: правила — {'без изменений' if 'not modified' in msg else msg}")
        return

    msg = _call("sendMessage", token, chat_id=group_id, text=GROUP_RULES,
                parse_mode="HTML", link_preview_options='{"is_disabled":true}')
    _try("pinChatMessage", token, chat_id=group_id, message_id=msg["message_id"],
         disable_notification=True)
    state["правила"] = msg["message_id"]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"группа: правила опубликованы и закреплены (id {msg['message_id']})")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token, chat = _credentials()
    group_id = setup_channel(token, chat, dry_run=dry_run)
    if group_id:
        setup_group(token, group_id, dry_run=dry_run)
    else:
        print("группа обсуждений к каналу не привязана")


if __name__ == "__main__":
    main()
