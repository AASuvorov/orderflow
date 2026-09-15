"""Сборщик тиков МОЕХ со стороной агрессора.

ISS отдаёт сделки только за текущую сессию, поэтому историю приходится
накапливать самим: скрипт запускается по расписанию и догружает новые сделки по
курсору TRADENO. Формат на выходе совпадает с крипто-загрузчиком
(ts, price, qty, side), поэтому footprint.py и signals.py работают без изменений.

Поле BUYSELL в потоке МОЕХ — направление инициатора сделки, то есть агрессора.
Именно оно и делает построение футпринта возможным.

Решения, продиктованные требованием работать без присмотра:
  * файл называется по дате самих сделок, а не по системной дате: утренний
    запуск до открытия сессии иначе положил бы прошлую сессию в файл нового дня;
  * список контрактов вычисляется один раз в день и кэшируется — иначе каждый
    проход тратит сотни запросов на постраничный обход справочника;
  * собираются два ближайших контракта по каждому активу, поэтому экспирация
    проходит без разрыва: история по следующему контракту есть заранее;
  * сетевые сбои переживаются повторами, а не падением прохода.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import requests

FORTS = "https://iss.moex.com/iss/engines/futures/markets/forts"
DATA_ROOT = Path(
    os.environ.get("ORDERFLOW_DATA", Path(__file__).resolve().parents[2] / "data")
)
CACHE = DATA_ROOT / "moex_ticks"
META = DATA_ROOT / "meta"
PAGE = 5000
MSK = timezone(timedelta(hours=3))
RETRIES = 4
SCHEMA = {
    "tradeno": pl.Int64,
    "ts": pl.Datetime,
    "price": pl.Float64,
    "qty": pl.Float64,
    "side": pl.Int8,
}
DEFAULT_ASSETS = ["Eu", "Si", "GD", "ED", "MM", "GN", "MX", "BR", "CR"]


def session_open(now: datetime | None = None) -> bool:
    """Сессия FORTS: утренняя с 07:00 МСК, вечерняя до 23:50. Праздники не учтены —
    в такой день проход просто ничего не найдёт."""
    now = now or datetime.now(MSK)
    if now.weekday() >= 5:
        return False
    return 6 <= now.hour <= 23


def _request(url: str, params: dict) -> dict:
    """GET с повторами: одиночный сбой ISS не должен ронять проход."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, timeout=45)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} от ISS")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"ISS недоступен после {RETRIES} попыток: {last}")


def front_contracts(
    assets: list[str], top: int = 2, refresh: bool = False
) -> dict[str, list[str]]:
    """Ближайшие контракты по каждому активу, с кэшем на сутки.

    Берём top штук по числу сделок: первый — текущий ближний, второй — следующий,
    который станет ближним после экспирации. Так история появляется заранее.
    """
    from moex import liquid_futures

    META.mkdir(parents=True, exist_ok=True)
    today = datetime.now(MSK).date().isoformat()
    path = META / f"contracts_{today}.json"

    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if all(a in cached for a in assets):
            return cached

    # Справочник за последний торговый день: ищем назад, минуя выходные.
    df = None
    for back in range(1, 8):
        day = (datetime.now(MSK) - timedelta(days=back)).date().isoformat()
        candidate = liquid_futures(day, min_trades=50)
        if not candidate.is_empty():
            df = candidate
            break
    if df is None:
        raise RuntimeError("не удалось получить справочник инструментов")

    out: dict[str, list[str]] = {}
    for asset in assets:
        # Код контракта = актив + буква месяца + цифра года.
        cand = df.filter(
            pl.col("SECID").str.starts_with(asset)
            & (pl.col("SECID").str.len_chars() == len(asset) + 2)
        ).sort("сделок", descending=True)
        out[asset] = cand["SECID"].to_list()[:top]

    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    for old in META.glob("contracts_*.json"):
        if old.name != path.name:
            old.unlink(missing_ok=True)
    return out


def _page(secid: str, tradeno: int | None, start: int) -> pl.DataFrame:
    params = {"iss.meta": "off", "iss.only": "trades", "limit": PAGE}
    if tradeno is not None:
        params["tradeno"] = tradeno
    else:
        params["start"] = start

    blk = _request(f"{FORTS}/securities/{secid}/trades.json", params)["trades"]
    if not blk["data"]:
        return pl.DataFrame(schema=SCHEMA)

    return pl.DataFrame(blk["data"], schema=blk["columns"], orient="row").select(
        pl.col("TRADENO").cast(pl.Int64).alias("tradeno"),
        (pl.col("TRADEDATE") + " " + pl.col("TRADETIME"))
        .str.to_datetime("%Y-%m-%d %H:%M:%S")
        .alias("ts"),
        pl.col("PRICE").cast(pl.Float64).alias("price"),
        pl.col("QUANTITY").cast(pl.Float64).alias("qty"),
        pl.when(pl.col("BUYSELL") == "B")
        .then(pl.lit(1, dtype=pl.Int8))
        .otherwise(pl.lit(-1, dtype=pl.Int8))
        .alias("side"),
    )


def _last_tradeno(secid: str) -> int | None:
    """Максимальный номер сделки среди уже сохранённых файлов контракта."""
    files = sorted((CACHE / secid).glob("*.parquet"))
    if not files:
        return None
    tail = pl.read_parquet(files[-1], columns=["tradeno"])
    return int(tail["tradeno"].max()) + 1 if tail.height else None


def fetch_session(secid: str, since: int | None = None) -> pl.DataFrame:
    """Сделки: с нуля постранично либо только новые от курсора since."""
    frames, cursor, offset = [], since, 0
    while True:
        chunk = _page(secid, cursor, offset)
        if chunk.is_empty():
            break
        frames.append(chunk)
        got = chunk.height
        if cursor is not None:
            cursor = int(chunk["tradeno"].max()) + 1
        else:
            offset += got
        if got < PAGE:
            break

    if not frames:
        return pl.DataFrame(schema=SCHEMA)
    return pl.concat(frames).unique("tradeno").sort("tradeno")


def record(secid: str) -> int:
    """Догружает сделки и раскладывает их по файлам согласно дате самих сделок."""
    out = CACHE / secid
    out.mkdir(parents=True, exist_ok=True)

    fresh = fetch_session(secid, _last_tradeno(secid))
    if fresh.is_empty():
        return 0

    added = 0
    # Группировка по дате сделки, а не по системной дате: защищает от записи
    # прошлой сессии в файл нового дня при раннем утреннем запуске.
    for (day,), part in fresh.group_by([pl.col("ts").dt.date()], maintain_order=True):
        path = out / f"{day.isoformat()}.parquet"
        if path.exists():
            before = pl.read_parquet(path)
            merged = pl.concat([before, part]).unique("tradeno").sort("ts")
            gained = merged.height - before.height
        else:
            merged = part.sort("ts")
            gained = merged.height
        merged.write_parquet(path, compression="zstd")
        added += gained

    total = sum(
        pl.read_parquet(f, columns=["tradeno"]).height for f in out.glob("*.parquet")
    )
    buy = fresh.filter(pl.col("side") == 1)["qty"].sum()
    print(
        f"{secid}: +{added:,} новых (всего {total:,}), "
        f"покупок в свежих {100 * buy / max(fresh['qty'].sum(), 1):.0f}%"
    )
    return added


def load(secid: str, start: str | None = None, end: str | None = None) -> pl.DataFrame:
    """Читает накопленные тики тем же интерфейсом, что крипто-загрузчик."""
    files = sorted((CACHE / secid).glob("*.parquet"))
    if start:
        files = [f for f in files if f.stem >= start]
    if end:
        files = [f for f in files if f.stem <= end]
    if not files:
        raise FileNotFoundError(f"нет тиков {secid} {start}..{end}")
    return (
        pl.concat([pl.read_parquet(f) for f in files]).drop("tradeno").sort("ts")
    )


def merge_dir(incoming: str | Path) -> int:
    """Вливает выгрузку с сервера, объединяя по номеру сделки.

    Простой rsync затёр бы локальные файлы серверными, а они могут содержать
    разные части одной сессии. Объединение делает синхронизацию безопасной.
    """
    incoming = Path(incoming)
    added = 0
    for src in sorted(incoming.rglob("*.parquet")):
        dst = CACHE / src.parent.name / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        new = pl.read_parquet(src)

        if dst.exists():
            before = pl.read_parquet(dst)
            merged = pl.concat([before, new]).unique("tradeno").sort("ts")
            gained = merged.height - before.height
        else:
            merged = new.sort("ts")
            gained = merged.height

        merged.write_parquet(dst, compression="zstd")
        if gained:
            print(f"  {src.parent.name}/{src.stem}: +{gained:,} тиков")
        added += gained

    print(f"влито {added:,} новых тиков")
    return added


def contract_sessions(secid: str, start: str, end: str) -> set[str]:
    """Дни, когда контракт реально торговался, по итоговой истории ISS.

    Выходные и праздники исключаются сами собой. Итоги текущего дня публикуются
    вечером, поэтому сегодняшняя дата в этот список ещё не попадает — и не
    считается пропуском.
    """
    payload = _request(
        f"https://iss.moex.com/iss/history/engines/futures/markets/forts"
        f"/securities/{secid}.json",
        {
            "iss.meta": "off",
            "iss.only": "history",
            "history.columns": "TRADEDATE,NUMTRADES",
            "from": start,
            "till": end,
        },
    )
    blk = payload.get("history", {})
    return {
        row[0]
        for row in blk.get("data", [])
        if row[1]  # дни без сделок не считаем сессиями
    }


def status(check_calendar: bool = True) -> pl.DataFrame:
    """Что накоплено и какие торговые дни пропущены.

    Пропуск критичен: ISS не отдаёт историю, восстановить сессию нельзя.
    """
    rows = []
    for d in sorted(CACHE.glob("*")):
        files = sorted(d.glob("*.parquet"))
        if not files:
            continue

        have = {f.stem for f in files}
        expected = (
            contract_sessions(d.name, files[0].stem, files[-1].stem)
            if check_calendar
            else set(have)
        )
        missing = sorted(expected - have)

        rows.append(
            {
                "контракт": d.name,
                "дней": len(files),
                "тиков": sum(
                    pl.read_parquet(f, columns=["tradeno"]).height for f in files
                ),
                "с": files[0].stem,
                "по": files[-1].stem,
                "пропущено": len(missing),
                "мб": round(sum(f.stat().st_size for f in files) / 1e6, 1),
            }
        )
    return pl.DataFrame(rows)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "status":
        print(status())
        return 0

    if argv and argv[0] == "merge":
        merge_dir(argv[1])
        print(status())
        return 0

    forced = bool(argv) and argv[0] == "--force"
    if forced:
        argv = argv[1:]
    if not forced and not session_open():
        print("сессия закрыта, нечего собирать")
        return 0

    assets = argv or DEFAULT_ASSETS
    plan = front_contracts(assets)

    failures = []
    for asset, contracts in plan.items():
        if not contracts:
            failures.append(f"{asset}: контракт не определён")
            continue
        for secid in contracts:
            try:
                record(secid)
            except Exception as exc:
                # Один сбойный контракт не должен ронять остальные.
                failures.append(f"{secid}: {exc}")
                print(f"{secid}: ОШИБКА {exc}")

    if failures:
        print(f"сбоев: {len(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
