"""Сборка статического сайта из архива публикаций.

Зачем сайт, если есть канал. Telegram не индексируется поисковиками: пост,
написанный сегодня, завтра находится только теми, кто уже подписан. У статической
страницы противоположное свойство — она живёт в выдаче Google и Яндекса годами и
приводит людей, которые о канале не слышали. Это единственный источник роста,
который не требует ни бюджета, ни чужой аудитории, ни моего участия.

Второе назначение — постоянный адрес. В канале пост тонет через день, а в архиве
он остаётся ссылкой, которую можно дать в споре или в статье.

Почему генератор, а не готовый движок: содержимое уже есть в архиве, который
ведёт tg_post.archive(), а сайту нужны только заголовок, дата, текст и картинка.
Движок принёс бы базу, шаблоны и обновления безопасности ради того, чего здесь
пятнадцать строк.

Запуск:
  python site_build.py                # собрать в каталог сайта
  python site_build.py --publish      # собрать и выложить на GitHub Pages
  python site_build.py --merge КАТАЛОГ  # влить чужой архив постов перед сборкой
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tg_post import TICKS_ROOT

ARCHIVE = TICKS_ROOT.parent / "archive"
OUT = Path(os.environ.get("ORDERFLOW_SITE", TICKS_ROOT.parent / "site"))

SITE_TITLE = "Трейдинг на данных"
SITE_TAGLINE = "Замеры вместо прогнозов: фьючерсы МОЕХ и крипта в цифрах"
CHANNEL = "https://t.me/tradingnadannyh"
REPO = "https://github.com/AASuvorov/orderflow"

# Цвета те же, что у аватара и видеоклипов: узнаваемость важнее оригинальности.
CSS = """
:root{--bg:#0F1B2D;--card:#16243a;--ink:#E8EEF7;--dim:#8A99AD;--teal:#2ED3C6;--warn:#FF6B57}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:17px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--teal);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:760px;margin:0 auto;padding:0 20px 80px}
header{padding:56px 0 28px;border-bottom:1px solid #24344f;margin-bottom:32px}
h1{margin:0 0 8px;font-size:34px;letter-spacing:-.4px}
h1 a{color:var(--ink)}
.tagline{color:var(--dim);margin:0 0 20px}
.links a{margin-right:18px;font-weight:600}
.board{background:var(--card);border:1px solid #24344f;border-radius:12px;
  padding:18px 20px;margin:26px 0 0}
.board h2{margin:0 0 12px;font-size:14px;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--dim);font-weight:700}
.board .row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;
  border-bottom:1px solid #1e2c44;font-variant-numeric:tabular-nums}
.board .row:last-child{border-bottom:0}
.board .row span:last-child{font-weight:700}
.up{color:var(--teal)}.down{color:var(--warn)}
article{background:var(--card);border:1px solid #24344f;border-radius:12px;
  padding:22px 24px;margin-bottom:22px}
article h2{margin:0 0 4px;font-size:21px;line-height:1.35}
article h2 a{color:var(--ink)}
time{color:var(--dim);font-size:14px}
article img{width:100%;height:auto;border-radius:8px;margin:16px 0;background:#fff}
.body{margin-top:14px}
.body b{color:#fff}
.tags{margin-top:14px}
.tags span{display:inline-block;background:#1e2c44;color:var(--dim);
  border-radius:999px;padding:3px 11px;font-size:13px;margin:0 6px 6px 0}
footer{color:var(--dim);font-size:14px;border-top:1px solid #24344f;
  margin-top:40px;padding-top:22px}
.empty{color:var(--dim);text-align:center;padding:60px 0}
"""

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")


def ru_date(iso: str) -> str:
    import datetime as dt

    d = dt.datetime.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}, {d:%H:%M}"


def split_post(raw: str) -> tuple[str, str, list[str]]:
    """Делит текст поста на заголовок, тело и хештеги.

    В Telegram заголовок — это первая строка в <b>, а хештеги — последняя строка.
    Для страницы они нужны отдельно: заголовок идёт в <h2> и в <title>, потому что
    именно его показывает поисковик, а хештеги превращаются в метки.
    """
    lines = [l for l in raw.strip().split("\n")]
    tags: list[str] = []
    if lines and lines[-1].strip().startswith("#"):
        tags = lines.pop().split()
        while lines and not lines[-1].strip():
            lines.pop()

    head = lines[0] if lines else ""
    rest = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    # Заголовок — только то, что в <b>, а не вся первая строка. У постов-событий
    # за жирной фразой на той же строке идёт продолжение мысли, и без этого
    # разделения в <h2> уезжал целый абзац: в ленте он выглядел как сбой вёрстки,
    # а в выдаче поисковика обрезался на середине слова.
    m = re.match(r"\s*<b>(.+?)</b>(.*)", head, flags=re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        tail = m.group(2).strip()
    else:
        title = re.sub(r"<[^>]+>", "", head).strip()
        tail = ""

    title = title.rstrip(" .")  # точка в конце заголовка не ставится
    body = "\n\n".join(x for x in (tail, rest) if x)
    return title, body, tags


def paragraphs(body: str) -> str:
    """Пустая строка — абзац, одиночный перевод строки — <br>.

    Посты писались под Telegram, где перенос значим сам по себе: списки замеров
    идут строками без пустых строк между ними, и склеив их в один абзац мы
    получили бы кашу из цифр.
    """
    out = []
    for block in re.split(r"\n\s*\n", body):
        if block.strip():
            out.append("<p>" + block.strip().replace("\n", "<br>") + "</p>")
    return "\n".join(out)


def page(title: str, inner: str, *, board: str = "") -> str:
    desc = html_mod.escape(SITE_TAGLINE, quote=True)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title)}</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{html_mod.escape(title)}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="website">
<style>{CSS}</style>
</head>
<body><div class="wrap">
<header>
<h1><a href="/orderflow/">{SITE_TITLE}</a></h1>
<p class="tagline">{SITE_TAGLINE}</p>
<p class="links"><a href="{CHANNEL}">Канал в Telegram</a><a href="{REPO}">Код и данные</a></p>
{board}
</header>
{inner}
<footer>
<p>Все цифры считаются автоматически из первичных источников: открытый ISS
Московской биржи, API Binance, сервисы Банка России. Пересказа новостей здесь нет.</p>
<p>Это не инвестиционная рекомендация и не предложение совершать сделки.
Замер описывает то, что уже произошло, и ничего не утверждает о будущем.</p>
</footer>
</div></body></html>
"""


def board_html() -> str:
    """Живые цифры на главной. Те же, что в закрепе канала."""
    try:
        from tg_board import cbr_rates, crypto_snapshot

        c = cbr_rates()
        rows = [
            ("Ключевая ставка ЦБ", f"{c['ставка']:.2f}%", ""),
            ("Доллар ЦБ", f"{c['USD']:.2f} ₽", ""),
            ("Юань ЦБ", f"{c['CNY']:.2f} ₽", ""),
        ]
        for r in crypto_snapshot():
            cls = "up" if r["сутки_%"] >= 0 else "down"
            rows.append((
                f"{r['тикер']} · фандинг {r['фандинг_год_%']:+.1f}% годовых",
                f"{r['цена']:,.2f} $ ({r['сутки_%']:+.1f}%)".replace(",", " "),
                cls,
            ))
    except Exception as exc:
        print(f"живые цифры недоступны ({exc}) — главная соберётся без них")
        return ""

    body = "\n".join(
        f'<div class="row"><span>{html_mod.escape(k)}</span>'
        f'<span class="{cls}">{html_mod.escape(v)}</span></div>'
        for k, v, cls in rows
    )
    return f'<div class="board"><h2>Живые цифры</h2>{body}</div>'


def load_posts() -> list[dict]:
    index = ARCHIVE / "posts.json"
    if not index.exists():
        return []
    posts = json.loads(index.read_text(encoding="utf-8"))
    return sorted(posts, key=lambda p: p["время"], reverse=True)


def build() -> int:
    posts = load_posts()
    OUT.mkdir(parents=True, exist_ok=True)
    img_out = OUT / "img"
    img_out.mkdir(exist_ok=True)

    cards = []
    used: set[str] = set()
    for p in posts:
        title, body, tags = split_post(p["html"])
        # Страховка на случай совпавших slug в старых записях архива: имя страницы
        # разводится здесь же. Без неё два поста писали один файл, и в ленте обе
        # ссылки открывали один и тот же текст — сборка при этом молчала.
        if p["slug"] in used:
            base, n = p["slug"], 2
            while p["slug"] in used:
                p["slug"] = f"{base}-{n}"
                n += 1
        used.add(p["slug"])
        img = ""
        if p.get("картинка"):
            src = ARCHIVE / "img" / p["картинка"]
            if src.exists():
                shutil.copy2(src, img_out / p["картинка"])
                img = (f'<img src="img/{p["картинка"]}" alt="{html_mod.escape(title)}" '
                       f'loading="lazy">')
        tag_html = ""
        if tags:
            tag_html = '<div class="tags">' + "".join(
                f"<span>{html_mod.escape(t)}</span>" for t in tags
            ) + "</div>"

        page_name = f"{p['slug']}.html"
        cards.append(
            f'<article>\n<h2><a href="{page_name}">{title}</a></h2>\n'
            f'<time>{ru_date(p["время"])}</time>\n{img}\n'
            f'<div class="body">{paragraphs(body)}</div>\n{tag_html}\n</article>'
        )
        # Отдельная страница на пост: именно она попадает в выдачу поисковика, у
        # ленты для этого слишком общий заголовок.
        (OUT / page_name).write_text(
            page(
                f"{title} — {SITE_TITLE}",
                f'<article>\n<h2>{title}</h2>\n<time>{ru_date(p["время"])}</time>\n'
                f'{img}\n<div class="body">{paragraphs(body)}</div>\n{tag_html}\n'
                f'<p><a href="./">← Все замеры</a></p></article>',
            ),
            encoding="utf-8",
        )

    inner = "\n".join(cards) if cards else (
        '<p class="empty">Замеры появятся здесь сразу после первой публикации.</p>'
    )
    (OUT / "index.html").write_text(
        page(f"{SITE_TITLE} — {SITE_TAGLINE}", inner, board=board_html()),
        encoding="utf-8",
    )
    # Отключает обработку Jekyll на GitHub Pages: иначе он игнорирует файлы и
    # каталоги, начинающиеся с подчёркивания, и молча ломает часть сайта.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"собрано: {len(posts)} публикаций в {OUT}")
    return len(posts)


def merge_archive(incoming: Path) -> int:
    """Вливает чужой архив постов в местный, сводя записи по slug.

    Публиковать может и сервер по расписанию, и ноутбук вручную, а архив у каждого
    свой: Bot API не отдаёт историю канала, так что восстановить пропущенное потом
    неоткуда. Без сведения тот, кто соберёт сайт последним, затирал бы чужие посты —
    сборка идёт из архива целиком, а выкладка перезаписывает ветку.

    Сводится по самому тексту, а не по slug. slug — отметка времени публикации, и
    один запуск успевает отправить несколько отчётов в одну секунду; на таких
    записях сведение по slug выбросило бы разные посты как повтор. Текст же у двух
    разных постов не совпадает никогда, а у одного и того же совпадает всегда,
    сколько бы раз архивы ни сводили.
    """
    src = incoming / "posts.json"
    if not src.exists():
        print(f"нечего вливать: {src} не найден")
        return 0

    index = ARCHIVE / "posts.json"
    posts = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    known = {p["html"] for p in posts}
    slugs = {p["slug"] for p in posts}

    added = 0
    for p in json.loads(src.read_text(encoding="utf-8")):
        if p["html"] in known:
            continue
        p = dict(p)
        # Чужой slug мог совпасть с местным — тогда обе стороны писали бы одну
        # страницу сайта и одну картинку. Разводим, сохраняя отметку времени.
        if p["slug"] in slugs:
            base, n = p["slug"], 2
            while p["slug"] in slugs:
                p["slug"] = f"{base}-{n}"
                n += 1
        known.add(p["html"])
        slugs.add(p["slug"])
        posts.append(p)
        added += 1
        source = incoming / "img" / (p.get("картинка") or "")
        if p.get("картинка") and source.exists():
            (ARCHIVE / "img").mkdir(parents=True, exist_ok=True)
            # Имя картинки построено из прежнего slug, и в местном архиве такой файл
            # может уже лежать — с другим содержимым. Кладём под новым именем, иначе
            # один график встал бы к двум разным постам.
            if (ARCHIVE / "img" / p["картинка"]).exists():
                p["картинка"] = p["slug"] + source.suffix
            shutil.copy2(source, ARCHIVE / "img" / p["картинка"])

    posts.sort(key=lambda p: p["время"])
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"влито постов: {added}, всего в архиве: {len(posts)}")
    return added


def publish() -> None:
    """Выкладывает собранное на GitHub Pages в отдельную ветку.

    Отдельная ветка, а не каталог в main: сайт перегенерируется целиком при каждом
    запуске, и его коммиты в основной ветке смешивались бы с историей кода, ломая
    её читаемость и создавая конфликты при любой правке с ноутбука.
    """
    if not (OUT / ".git").exists():
        raise SystemExit(
            f"каталог {OUT} не подключён к git.\n"
            "Настройка описана в deploy/install-site.sh"
        )
    run = lambda *a: subprocess.run(a, cwd=OUT, check=False, capture_output=True, text=True)
    run("git", "add", "-A")
    if run("git", "status", "--porcelain").stdout.strip():
        run("git", "commit", "-m", "Обновление сайта")
    # Отправка идёт всегда, даже когда коммитить нечего. Иначе одна сорванная сеть
    # выключала публикацию навсегда: коммит оставался локальным, следующий запуск
    # видел чистый каталог и докладывал «нечего публиковать», а сайт тихо замирал
    # на старой версии. Пустая отправка ничего не стоит и завершается успехом.
    # Перезапись ветки, а не слияние. Ветка целиком машинная: её содержимое каждый
    # раз собирается заново из архива, поэтому расхождение с удалённой копией не
    # означает потерю работы — там нечего сохранять, кроме предыдущей сборки. Без
    # этого публикация встаёт насмерть, как только сайт собран и с сервера, и с
    # ноутбука: истории у двух сборок разные, а сливать сгенерированный HTML
    # бессмысленно. Ветка защищена тем, что кода в ней нет вообще.
    push = run("git", "push", "--force", "origin", "HEAD:gh-pages")
    if push.returncode != 0:
        raise RuntimeError(f"git push: {push.stderr.strip()[:400]}")
    print("сайт выложен")


if __name__ == "__main__":
    if "--merge" in sys.argv:
        merge_archive(Path(sys.argv[sys.argv.index("--merge") + 1]))
    build()
    if "--publish" in sys.argv:
        publish()
