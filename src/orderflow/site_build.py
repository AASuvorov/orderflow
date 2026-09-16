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
#
# Шрифт подключается с запасом из системных: если Google Fonts недоступен — а в
# России это обычное дело, — страница обязана остаться читаемой, а не поехать.
# Цифры набираются табличными глифами: в замерах они стоят столбиками, и
# пропорциональные знаки заставляли бы глаз выравнивать их заново на каждой строке.
CSS = """
:root{
  --bg:#0B1524;--bg2:#0F1B2D;--card:#141F33;--card2:#18243B;
  --line:#22314C;--ink:#EDF2FA;--dim:#8798AF;--teal:#2ED3C6;--warn:#FF6B57;
  --radius:16px;--maxw:820px
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;
  font:400 17px/1.7 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  "Helvetica Neue",Arial,sans-serif;
  background-image:radial-gradient(1200px 600px at 50% -240px,#16305180,transparent)}
.num,time,.stat b,.board b{font-variant-numeric:tabular-nums}
a{color:var(--teal);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:3px}
.wrap{max-width:var(--maxw);margin:0 auto;padding:0 22px 96px}

header{padding:56px 0 32px}
.brand{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.brand img{width:52px;height:52px;border-radius:14px;box-shadow:0 6px 24px #0006}
h1{margin:0;font-size:clamp(28px,5vw,40px);line-height:1.1;letter-spacing:-.9px;
  font-weight:700}
h1 a{color:var(--ink)}
.tagline{color:var(--dim);margin:0 0 22px;font-size:18px;max-width:56ch}
.links{display:flex;flex-wrap:wrap;gap:10px;margin:0}
.links a{background:var(--card);border:1px solid var(--line);border-radius:999px;
  padding:8px 16px;font-size:15px;font-weight:600;color:var(--ink);
  transition:border-color .15s,transform .15s}
.links a:hover{text-decoration:none;border-color:var(--teal);transform:translateY(-1px)}
.links a.accent{background:linear-gradient(135deg,#2ED3C6,#1FA9C2);color:#06131f;
  border-color:transparent}

.board{margin:34px 0 8px}
/* Подпись раздела отдельным классом, а не правилом для h2 внутри секции: такое
   правило перебивало заголовки самих замеров — они набирались капсом в размер
   подписи, потому что класс в селекторе весит больше, чем имя тега. */
.eyebrow{margin:0 0 14px;font-size:13px;text-transform:uppercase;
  letter-spacing:1.6px;color:var(--dim);font-weight:700}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.stat{background:linear-gradient(180deg,var(--card2),var(--card));
  border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;
  display:flex;flex-direction:column;gap:5px}
.stat span{color:var(--dim);font-size:13px}
/* Значение и изменение не переносятся внутри себя: без этого знак валюты и минус
   отрывались на следующую строку и повисали там отдельно от числа. */
.stat b,.stat i{white-space:nowrap}
.stat b{font-size:24px;font-weight:700;letter-spacing:-.5px}
.stat i{font-style:normal;font-size:13px;font-weight:600}
.stat i.note{color:var(--dim);font-weight:400;white-space:normal}
.stat b{margin-top:auto}
.up{color:var(--teal)}.down{color:var(--warn)}

.section{margin-top:44px;padding-top:8px}
article{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:26px 28px;margin-bottom:20px;transition:border-color .15s,transform .15s}
.feed article:hover{border-color:#2f4568;transform:translateY(-2px)}
article h2{margin:0 0 6px;font-size:23px;line-height:1.3;letter-spacing:-.4px;
  font-weight:700}
article h2 a{color:var(--ink)}
article h2 a:hover{color:var(--teal);text-decoration:none}
time{color:var(--dim);font-size:14px}
article img{width:100%;height:auto;border-radius:10px;margin:18px 0 4px;
  background:#fff;border:1px solid var(--line)}
.body{margin-top:16px}
.body p{margin:0 0 15px}
.body p:last-child{margin-bottom:0}
.body b{color:#fff;font-weight:600}
.excerpt{color:#C7D3E4;margin-top:14px}
.more{display:inline-block;margin-top:14px;font-weight:600}
.tags{margin-top:18px;display:flex;flex-wrap:wrap;gap:7px}
.tags span{background:#1B2942;color:#9FB0C7;border:1px solid var(--line);
  border-radius:999px;padding:4px 12px;font-size:13px}
.nav{display:flex;justify-content:space-between;gap:14px;margin-top:26px;
  font-weight:600}
footer{color:var(--dim);font-size:14px;border-top:1px solid var(--line);
  margin-top:56px;padding-top:24px}
footer p{margin:0 0 12px}
.empty{color:var(--dim);text-align:center;padding:72px 0}
@media(max-width:600px){
  article{padding:20px 18px}
  body{font-size:16px}
}
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


def page(title: str, inner: str, *, board: str = "", description: str = "") -> str:
    desc = html_mod.escape(description or SITE_TAGLINE, quote=True)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title)}</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#0B1524">
<link rel="icon" href="avatar.png">
<meta property="og:title" content="{html_mod.escape(title)}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="avatar.png">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap">
<style>{CSS}</style>
</head>
<body><div class="wrap">
<header>
<div class="brand">
<a href="./"><img src="avatar.png" alt="{html_mod.escape(SITE_TITLE)}" width="52" height="52"></a>
<h1><a href="./">{SITE_TITLE}</a></h1>
</div>
<p class="tagline">{SITE_TAGLINE}</p>
<p class="links"><a class="accent" href="{CHANNEL}">Читать в Telegram</a><a href="{REPO}">Код и данные</a></p>
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
    """Живые цифры на главной плитками. Те же, что в закрепе канала.

    Плитками, а не таблицей: у показателей разная природа — ставка, курс, цена с
    суточным изменением, — и в двух колонках они выглядели бы однородным списком,
    хотя ставка здесь главная, это планка для всех замеров.
    """
    try:
        from tg_board import cbr_rates, crypto_snapshot, imoex

        c = cbr_rates()
        # Подписи короткие и в одну строку у всех плиток: длинная подпись переносилась
        # только у части из них, и плитки одного ряда получались разной высоты.
        tiles = [
            ("Ставка ЦБ", f"{c['ставка']:.2f}%", "", "", "планка доходности"),
            ("Доллар ЦБ", f"{c['USD']:.2f} ₽", "", "", ""),
            ("Юань ЦБ", f"{c['CNY']:.2f} ₽", "", "", ""),
        ]
        index = imoex()
        if index:
            tiles.append((
                "Индекс МОЕХ", f"{index['значение']:.2f}",
                f"{index['изм_%']:+.2f}%",
                "up" if index["изм_%"] >= 0 else "down", "к закрытию",
            ))
        for r in crypto_snapshot():
            # Знак фандинга и суточное изменение раскрашены по-разному не случайно:
            # покрасив фандинг цветом цены, мы утверждали бы, что они об одном, тогда
            # как фандинг говорит, кто платит за удержание позиции, а не куда идёт цена.
            tiles.append((
                r["тикер"],
                f"{r['цена']:,.2f} $".replace(",", " "),
                f"{r['сутки_%']:+.1f}% за сутки",
                "up" if r["сутки_%"] >= 0 else "down",
                f"фандинг {r['фандинг_год_%']:+.1f}% годовых",
            ))
    except Exception as exc:
        print(f"живые цифры недоступны ({exc}) — главная соберётся без них")
        return ""

    cells = "\n".join(
        f'<div class="stat"><span>{html_mod.escape(label)}</span>'
        f'<b>{html_mod.escape(value)}</b>'
        + (f'<i class="{cls}">{html_mod.escape(delta)}</i>' if delta else "")
        + (f'<i class="note">{html_mod.escape(note)}</i>' if note else "")
        + "</div>"
        for label, value, delta, cls, note in tiles
    )
    return (f'<div class="board"><p class="eyebrow">Живые цифры, обновляются сами</p>'
            f'<div class="grid">{cells}</div></div>')


def excerpt(body: str, limit: int = 260) -> str:
    """Начало поста для ленты, обрезанное по границе слова.

    В ленте нужен анонс, а не весь текст: замеры длинные, и десяток полных постов
    подряд превращал бы главную в свиток, по которому нельзя выбрать интересное.
    Обрезка по пробелу, а не по символу, чтобы строка не рвалась посреди слова.
    """
    plain = re.sub(r"<[^>]+>", "", body).replace("\n", " ")
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= limit:
        return html_mod.escape(plain)
    cut = plain[:limit].rsplit(" ", 1)[0]
    return html_mod.escape(cut.rstrip(" ,.;:—-")) + "…"


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
    pages: list[dict] = []
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
            f'<p class="excerpt">{excerpt(body)}</p>\n'
            f'<a class="more" href="{page_name}">Читать замер →</a>\n'
            f'{tag_html}\n</article>'
        )
        pages.append({"файл": page_name, "заголовок": title, "тело": body,
                      "картинка": img, "метки": tag_html, "время": p["время"]})

    # Страницы пишутся после ленты: ссылки «предыдущий/следующий» требуют знать
    # соседей, а на момент сборки карточки соседа ещё нет.
    for i, pg in enumerate(pages):
        links = []
        if i + 1 < len(pages):
            links.append(f'<a href="{pages[i + 1]["файл"]}">← {pages[i + 1]["заголовок"]}</a>')
        else:
            links.append('<a href="./">← Все замеры</a>')
        if i > 0:
            links.append(f'<a href="{pages[i - 1]["файл"]}">{pages[i - 1]["заголовок"]} →</a>')
        # Отдельная страница на пост: именно она попадает в выдачу поисковика, у
        # ленты для этого слишком общий заголовок.
        (OUT / pg["файл"]).write_text(
            page(
                f"{pg['заголовок']} — {SITE_TITLE}",
                f'<article>\n<h2>{pg["заголовок"]}</h2>\n'
                f'<time>{ru_date(pg["время"])}</time>\n{pg["картинка"]}\n'
                f'<div class="body">{paragraphs(pg["тело"])}</div>\n{pg["метки"]}\n'
                f'<div class="nav">{"".join(links)}</div></article>',
                description=re.sub(r"<[^>]+>", "", pg["заголовок"]),
            ),
            encoding="utf-8",
        )

    inner = (f'<div class="section feed"><p class="eyebrow">Замеры</p>\n'
             + "\n".join(cards) + "</div>") if cards else (
        '<p class="empty">Замеры появятся здесь сразу после первой публикации.</p>'
    )
    (OUT / "index.html").write_text(
        page(f"{SITE_TITLE} — {SITE_TAGLINE}", inner, board=board_html()),
        encoding="utf-8",
    )

    # Аватар канала служит и favicon, и картинкой для соцсетей: ссылка на сайт
    # должна узнаваться так же, как канал в ленте Telegram.
    #
    # Ищется в двух местах, потому что на сервере лежит только код, без репозитория:
    # путь от файла модуля там уводит в корень диска, и аватар молча не находился —
    # сайт собирался, а иконка отдавала 404.
    for avatar in (
        ARCHIVE.parent / "assets" / "avatar.png",
        Path(__file__).resolve().parents[2] / "content" / "telegram" / "avatar.png",
    ):
        if avatar.exists():
            shutil.copy2(avatar, OUT / "avatar.png")
            break
    else:
        print("аватар не найден — favicon и картинка для соцсетей не появятся")
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
