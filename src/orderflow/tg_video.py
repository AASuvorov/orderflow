"""Короткие видеозамеры для Telegram: график, который строится на глазах.

Зачем вообще видео, если те же цифры уже уходят картинкой. В ленте Telegram
видео проигрывается само, без звука и без нажатия, и забирает внимание там, где
статичный график пролистывают. Это единственный формат, который канал такого
рода может делать без камеры и без диктора: анимируется не лицо, а сам замер.

Отсюда три ограничения, которые определили здесь всё:

Без звука. Ни одного слова нельзя доверить озвучке — весь текст на кадре. Читать
его будут с телефона, поэтому шрифты крупные, а строк мало.

Коротко. Внимание в ленте держится секунд десять-пятнадцать. Клип обязан быть
понятен человеку, который посмотрел его с середины и не читал подпись.

Один вывод на клип. Анимация плоха для сравнения деталей: кадр уже уехал, а
вернуться нельзя. Она хороша для одной мысли, у которой есть развитие во
времени. Всё остальное остаётся картинкам в обычных постах.

Запуск:
  python tg_video.py                      # клип из очереди, по давности выхода
  python tg_video.py breakeven            # собрать и опубликовать конкретный
  python tg_video.py rotation --dry-run   # только собрать файл
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FFMpegWriter

from moex_feasibility import CAPTURE, HORIZONS_MIN, breakeven
from tg_post import OUT_DIR, _call, stamp_published

# Квадрат, а не вертикаль: в ленте канала он занимает столько же места, но не
# режется в предпросмотре пересылки и одинаково смотрится с телефона и с ноутбука.
SIZE = 1080
FPS = 30
DPI = 108  # 1080 / 10 дюймов: размер фигуры в дюймах остаётся круглым

# Цвета те же, что у аватара: канал должен узнаваться в ленте до чтения подписи.
NAVY = "#0F1B2D"
TEAL = "#2ED3C6"
WARN = "#FF6B57"
GREY = "#8A99AD"

# Порог достижимой точности. 65% на ценовом ряду не держит ни одна модель — это
# наблюдение, а не расчёт, поэтому на кадре оно подписано как наблюдение.
LIMIT_PCT = 65.0

SI = "SiU6"
PERIOD = ("2026-06-15", "2026-09-11")
CRYPTO_COST_BPS = 10.0

# Собственный масштаб движения BTCUSDT, б.п. за горизонт: те же числа, что в
# moex_feasibility.plot. Переносить сюда масштаб Si было бы подменой — у крипты
# и цена ходит иначе, сравнение обязано идти по её собственным движениям.
BTC_HORIZONS = np.array([5, 15, 30, 60, 120, 240])
BTC_MOVE_BPS = np.array([8.0, 13.8, 19.6, 27.8, 39.4, 55.7])


def ffmpeg_or_die() -> None:
    """Проверяет ffmpeg до начала рендера, а не в конце.

    matplotlib сообщает об отсутствии писателя только когда первый кадр уже
    посчитан, и на длинном клипе это минуты работы впустую.
    """
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        raise SystemExit(
            "нужен ffmpeg: brew install ffmpeg (macOS) или apt install ffmpeg"
        )


def canvas() -> tuple[plt.Figure, plt.Axes]:
    """Фигура под вертикальный экран телефона: тёмный фон, крупные подписи."""
    fig, ax = plt.subplots(figsize=(SIZE / DPI, SIZE / DPI), dpi=DPI)
    fig.patch.set_facecolor(NAVY)
    ax.set_facecolor(NAVY)
    for side in ax.spines.values():
        side.set_color(GREY)
    ax.tick_params(colors=GREY, labelsize=16)
    ax.grid(alpha=0.15, color=GREY)
    return fig, ax


def ease(x: float) -> float:
    """Плавное начало и конец. Линейное появление кривой читается как рывок."""
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def fade(artist, start: float, now: float, span: float = 0.5) -> None:
    """Проявляет элемент к моменту start за span секунд.

    Обрезка обязательна: matplotlib отвергает прозрачность вне 0..1, а моменты
    сцен считаются из длительностей и легко дают отрицательное значение на
    кадрах до начала проявления.
    """
    artist.set_alpha(ease((now - start) / span))


def breakeven_clip(path: Path) -> tuple[Path, str]:
    """Клип про порог безубыточности: сколько точности требуют издержки.

    Замер выбран не случайно. Это единственный вывод из всей работы, который
    ничего не предполагает о модели: он считается из издержек и размаха цены,
    поэтому спорить с ним можно только цифрами. Для первого клипа это важнее
    зрелищности — канал заявляет, что здесь считают, и клип обязан это показать.

    Развитие во времени, ради которого формат вообще взят: кривая падает слева
    направо, и видно, как требование становится проходимым по мере удлинения
    горизонта. На статичном графике это надо разглядеть, здесь — происходит.
    """
    ffmpeg_or_die()

    be = breakeven(SI, *PERIOD)
    h_si = be["горизонт_мин"].to_numpy().astype(float)
    p_si = be["нужна_точность_%"].to_numpy().astype(float)
    cost_si = float(be["издержки_бп"][0])

    p_btc = 0.5 * (1 + CRYPTO_COST_BPS / (CAPTURE * BTC_MOVE_BPS)) * 100

    # Сцены задаются секундами, а не номерами кадров: править ритм по секундам
    # понятно, по кадрам — нет.
    t_si, t_limit, t_btc, t_hold = 4.0, 2.0, 4.0, 3.5
    total = t_si + t_limit + t_btc + t_hold
    frames = int(total * FPS)

    fig, ax = canvas()
    ax.set_xscale("log")
    ax.set_xticks(HORIZONS_MIN)
    ax.set_xticklabels([str(x) for x in HORIZONS_MIN])
    ax.set_xlim(0.85, 290)
    ax.set_ylim(45, 100)
    ax.set_xlabel("горизонт удержания, минут", color=GREY, fontsize=17)
    ax.set_ylabel("требуемая точность, %", color=GREY, fontsize=17)
    ax.set_title(
        "Какая точность нужна, чтобы просто не потерять",
        color="white", fontsize=23, pad=18, fontweight="bold",
    )

    line_si, = ax.plot([], [], color=TEAL, lw=4, marker="o", ms=9)
    line_btc, = ax.plot([], [], color=WARN, lw=4, marker="x", ms=11, ls="--")
    band = ax.axhspan(LIMIT_PCT, 100, color=WARN, alpha=0.0)
    limit_line = ax.axhline(LIMIT_PCT, color=WARN, ls=":", lw=2, alpha=0.0)

    txt_limit = ax.text(
        0.97, LIMIT_PCT + 1.2, "", color=WARN, fontsize=15,
        ha="right", transform=ax.get_yaxis_transform(), alpha=0.0,
    )
    txt_si = ax.text(0.04, 0.30, "", color=TEAL, fontsize=17,
                     transform=ax.transAxes, alpha=0.0, fontweight="bold")
    txt_btc = ax.text(0.04, 0.86, "", color=WARN, fontsize=17,
                      transform=ax.transAxes, alpha=0.0, fontweight="bold")
    txt_end = ax.text(0.04, 0.16, "", color="white", fontsize=16,
                      transform=ax.transAxes, alpha=0.0)
    ax.text(0.985, 0.02, "@tradingnadannyh", color=GREY, fontsize=13,
            transform=ax.transAxes, ha="right")

    def draw(i: int) -> None:
        t = i / FPS

        # Сцена 1: кривая МОЕХ выезжает слева направо.
        k = ease(t / t_si)
        n = max(2, int(round(k * len(h_si))))
        line_si.set_data(h_si[:n], p_si[:n])
        if n >= len(h_si):
            fade(txt_si, t_si * 0.85, t)
            txt_si.set_text(
                f"{SI} на МОЕХ, издержки круга {cost_si:.2f} б.п.\n"
                f"на 1 минуте нужно {p_si[0]:.0f}%, на 240 — {p_si[-1]:.1f}%"
            )

        # Сцена 2: граница достижимого. До неё кривая МОЕХ уже нарисована, и
        # видно, что она уходит под границу — то есть требование выполнимо.
        if t > t_si:
            a = ease((t - t_si) / t_limit)
            band.set_alpha(0.13 * a)
            limit_line.set_alpha(a)
            txt_limit.set_alpha(a)
            txt_limit.set_text(f"{LIMIT_PCT:.0f}% — выше не держится ни одна модель")

        # Сцена 3: те же издержки, но крипты. Кривая целиком в недостижимой зоне.
        if t > t_si + t_limit:
            k = ease((t - t_si - t_limit) / t_btc)
            n = max(2, int(round(k * len(BTC_HORIZONS))))
            line_btc.set_data(BTC_HORIZONS[:n], p_btc[:n])
            if n >= len(BTC_HORIZONS):
                fade(txt_btc, t_si + t_limit + t_btc * 0.8, t)
                txt_btc.set_text(
                    f"BTCUSDT тейкером, издержки {CRYPTO_COST_BPS:.0f} б.п.\n"
                    f"даже на 240 минутах нужно {p_btc[-1]:.0f}%"
                )

        # Сцена 4: вывод. Держится в кадре, чтобы его успели прочитать.
        if t > t_si + t_limit + t_btc:
            fade(txt_end, t_si + t_limit + t_btc, t, span=1.2)
            txt_end.set_text(
                "Это арифметика, а не качество модели:\n"
                "издержки задают горизонт раньше, чем выбран сигнал"
            )

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    # yuv420p и faststart — требования проигрывателей Telegram: без первого видео
    # не открывается на части устройств, без второго не стартует до полной загрузки.
    writer = FFMpegWriter(
        fps=FPS,
        bitrate=2400,
        extra_args=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    )
    with writer.saving(fig, str(path), dpi=DPI):
        for i in range(frames):
            draw(i)
            writer.grab_frame(facecolor=NAVY)
    plt.close(fig)

    caption = (
        "<b>На 1 минуте нужно "
        f"{p_si[0]:.0f}% верных предсказаний направления, чтобы не потерять.</b> "
        "На 240 минутах — "
        f"{p_si[-1]:.1f}%.\n\n"
        "Считается до всякой модели, из двух вещей: издержек полного круга и "
        "того, сколько цена успевает пройти за горизонт. Сделка захватывает долю "
        f"{CAPTURE:.0%} среднего движения — при точности p ожидание равно "
        "(2p − 1) · захват − издержки, отсюда и порог.\n\n"
        f"У {SI} на МОЕХ круг стоит {cost_si:.2f} б.п., и уже с пятнадцати минут "
        "требование опускается ниже достижимого. У тейкера в крипте круг стоит "
        f"{CRYPTO_COST_BPS:.0f} б.п. — там даже четыре часа требуют "
        f"{p_btc[-1]:.0f}%, чего не показывает никто.\n\n"
        "Отсюда практический вывод, который стоит дороже любого индикатора: "
        "<b>издержки диктуют горизонт удержания.</b> Не наоборот. Считать это "
        "надо до выбора сигнала, а не после первой просадки.\n\n"
        "Архив замеров: aasuvorov.github.io/orderflow\n\n"
        "#МОЕХ #фьючерсы #издержки"
    )
    return path, caption


def publish_video(path: Path, caption: str, *, dry_run: bool = False) -> None:
    """Отправка клипа. Подпись к видео ограничена 1024 символами, как у фото."""
    if len(caption) > 1024:
        raise RuntimeError(f"подпись {len(caption)} символов, лимит 1024")

    print("=" * 72)
    print(caption)
    print("=" * 72)
    print(f"файл: {path}  {path.stat().st_size / 1e6:.1f} МБ")
    if dry_run:
        print("--dry-run: не отправлено")
        return

    with path.open("rb") as f:
        _call(
            "sendVideo",
            files={"video": f},
            caption=caption,
            parse_mode="HTML",
            # supports_streaming позволяет начать проигрывание до загрузки целиком:
            # без него в ленте вместо автозапуска висит превью с кнопкой.
            supports_streaming="true",
        )
    stamp_published("video")
    print("опубликовано")


def rotation_clip(path: Path) -> tuple[Path, str]:
    """Клип про ротацию монет: портфель против вклада, день за днём.

    Здесь формат работает так, как ни на чём другом: у схемы есть развитие во
    времени, и его невозможно оспорить пересказом. Линия вклада растёт ровно и
    скучно, портфель дёргается и уходит вниз — расхождение видно раньше, чем
    человек дочитает подпись.

    Полоса двадцати жеребьёвок рисуется вместе с кривой не для красоты. Одна
    кривая всегда вызывает возражение «просто монеты не те»; полоса показывает,
    что не те они при любом выборе.

    Данные пересчитываются при каждом запуске, поэтому клип не устаревает: набор
    монет берётся по текущему обороту, окно — последний год.
    """
    ffmpeg_or_die()

    from coin_rotation import (SLOTS, STAKE, benchmarks, load_prices, simulate,
                               universe, window)

    print("готовлю данные: набор монет и свечи")
    prices = window(
        load_prices(universe()),
        dt.date.today() - dt.timedelta(days=365),
        dt.date.today(),
    )
    if not prices:
        raise SystemExit("нет данных за последний год")

    length = min(df.height for df in prices.values())
    draws = [simulate(prices, "случайно", seed=s)["кривая"] for s in range(20)]
    hero = simulate(prices, "импульс")
    curve = hero["кривая"]
    marks = benchmarks(prices, length)
    start = SLOTS * STAKE

    low = np.array([min(d[i] for d in draws) for i in range(length)])
    high = np.array([max(d[i] for d in draws) for i in range(length)])
    days = np.arange(length)
    # Линия вклада строится по дням, а не одной чертой на итоге: смысл именно в
    # том, что она растёт всё это время, пока портфель ищет очередную монету.
    deposit = start * (1 + 0.14) ** (days / 365)

    t_curve, t_dep, t_hold = 6.0, 3.5, 4.0
    total = t_curve + t_dep + t_hold
    frames = int(total * FPS)

    fig, ax = canvas()
    ax.set_xlim(0, length)
    ax.set_ylim(min(low.min(), start) * 0.85, max(high.max(), deposit[-1]) * 1.08)
    ax.set_xlabel("дней с начала", color=GREY, fontsize=17)
    # Доллар экранируется во всех подписях кадра: matplotlib принимает пару знаков
    # доллара за формулу и вырезает всё между ними. В подписи «$49 из $100» это
    # съедало и знаки, и пробелы, и на кадре оставалось «49из100».
    ax.set_ylabel("портфель, \\$", color=GREY, fontsize=17)
    ax.set_title(
        "Продавать каждую монету на +10%:\nчто вышло за год",
        color="white", fontsize=23, pad=18, fontweight="bold",
    )
    ax.axhline(start, color=GREY, lw=1.5, alpha=0.5)

    band = ax.fill_between(days, low, high, color=GREY, alpha=0.0)
    line, = ax.plot([], [], color=WARN, lw=4)
    line_dep, = ax.plot([], [], color=TEAL, lw=4)
    txt_dep = ax.text(0.04, 0.90, "", color=TEAL, fontsize=18,
                      transform=ax.transAxes, alpha=0.0, fontweight="bold")
    txt_end = ax.text(0.04, 0.13, "", color="white", fontsize=17,
                      transform=ax.transAxes, alpha=0.0)
    ax.text(0.985, 0.02, "@tradingnadannyh", color=GREY, fontsize=13,
            transform=ax.transAxes, ha="right")

    def draw(i: int) -> None:
        t = i / FPS

        # Сцена 1: портфель и полоса жеребьёвок ползут вправо одновременно.
        k = ease(t / t_curve)
        n = max(2, int(round(k * length)))
        line.set_data(days[:n], curve[:n])
        band.set_alpha(0.18 * k)

        # Сцена 2: линия вклада догоняет и обходит.
        if t > t_curve:
            k2 = ease((t - t_curve) / t_dep)
            m = max(2, int(round(k2 * length)))
            line_dep.set_data(days[:m], deposit[:m])
            if m >= length:
                fade(txt_dep, t_curve + t_dep * 0.75, t)
                txt_dep.set_text(
                    f"вклад под ставку ЦБ: \\${marks['вклад']:.0f}\n"
                    f"схема: \\${hero['итог']:.0f} из \\${start:.0f}"
                )

        # Сцена 3: вывод. Держится в кадре до конца.
        if t > t_curve + t_dep:
            fade(txt_end, t_curve + t_dep, t, span=1.2)
            txt_end.set_text(
                "Правило продаёт только выросшее.\n"
                "Упавшее остаётся в портфеле навсегда."
            )

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = FFMpegWriter(
        fps=FPS,
        bitrate=2400,
        extra_args=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    )
    with writer.saving(fig, str(path), dpi=DPI):
        for i in range(frames):
            draw(i)
            writer.grab_frame(facecolor=NAVY)
    plt.close(fig)

    worst, best = low[-1], high[-1]
    caption = (
        f"<b>Схема «десять монет по ${STAKE:.0f}, продавать каждую на +10%» за год: "
        f"${hero['итог']:.0f} из ${start:.0f}.</b> "
        f"Вклад под ставку ЦБ за то же время дал ${marks['вклад']:.0f}.\n\n"
        f"Серая полоса — двадцать жеребьёвок случайного выбора монет, от "
        f"${worst:.0f} до ${best:.0f}. Ни одна не вышла в плюс. Значит дело не в "
        "том, какие монеты выбрать: осмысленные правила отбора лежат внутри этой "
        "же полосы.\n\n"
        "Причина простая. Правило продаёт только то, что выросло на 10%, и молчит "
        "про упавшее — поэтому прибыль из портфеля уходит, а убыток в нём "
        "остаётся. К концу года свободных денег нет вовсе: всё лежит в позициях, "
        "которые до цели не дошли.\n\n"
        "Условия были щедрыми к схеме: продажа точно по цели, комиссия биржевая, "
        f"монеты только с оборотом от $3 млн. Набор пересчитывается заново при "
        "каждом запуске.\n\n"
        "Это не рекомендация, а замер одного правила. Код открыт.\n\n"
        "#замеры #крипта #ротациямонет"
    )
    return path, caption


CLIPS = {"breakeven": breakeven_clip, "rotation": rotation_clip}

VIDEO_STATE = OUT_DIR.parent / "meta" / "video.json"


def next_clip() -> str:
    """Клип, который не выходил дольше всех.

    По кругу, а не случайно: случайный выбор из двух вариантов регулярно повторяет
    один и тот же дважды подряд, и подписчик видит то же видео второй раз. Очередь
    по давности выхода такого не допускает и не требует ничего, кроме одной даты
    на клип.

    Порядок не зашит в список: новый клип, у которого даты выхода ещё нет, встаёт
    первым в очередь автоматически.
    """
    state = (json.loads(VIDEO_STATE.read_text(encoding="utf-8"))
             if VIDEO_STATE.exists() else {})
    return min(CLIPS, key=lambda name: state.get(name, ""))


def remember_clip(name: str) -> None:
    state = (json.loads(VIDEO_STATE.read_text(encoding="utf-8"))
             if VIDEO_STATE.exists() else {})
    state[name] = dt.datetime.now().isoformat(timespec="seconds")
    VIDEO_STATE.parent.mkdir(parents=True, exist_ok=True)
    VIDEO_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                           encoding="utf-8")


def main() -> None:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    name = argv[0] if argv else "auto"
    if name == "auto":
        name = next_clip()
        print(f"очередь клипов: {name}")
    if name not in CLIPS:
        raise SystemExit(f"клипы: {', '.join(CLIPS)}, либо auto")

    out = Path(os.environ.get("ORDERFLOW_TG_OUT", OUT_DIR)) / f"{name}.mp4"
    path, caption = CLIPS[name](out)
    publish_video(path, caption, dry_run=dry)
    if not dry:
        remember_clip(name)


if __name__ == "__main__":
    main()
