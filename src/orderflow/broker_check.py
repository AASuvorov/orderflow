"""Проверка исполнительного слоя: полный жизненный цикл заявки в песочнице.

Прогоняет всё, что понадобится роботу, и печатает измеренные значения:
подключение, счёт, поиск инструмента, лимитная заявка с отменой, рыночная
заявка с исполнением, позиция, закрытие, фактическая комиссия.

Запуск:
    export TINVEST_TOKEN=...
    uv run python broker_check.py SiZ6
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import polars as pl

from broker import ORDER_LOG, TInvest, log_order

STEP = 1


def step(title: str) -> None:
    global STEP
    print(f"\n[{STEP}] {title}")
    STEP += 1


def main(ticker: str) -> None:
    api = TInvest(sandbox=True)

    step("Подключение и счёт")
    account_id = api.ensure_account()
    print(f"    счёт: {account_id}")

    step(f"Поиск фьючерса {ticker}")
    inst = api.find_future(ticker)
    if inst is None:
        print(f"    не найден: {ticker}")
        return
    print(f"    {inst.ticker} — {inst.name}")
    print(f"    uid={inst.uid} лот={inst.lot} шаг={inst.min_increment}"
          f" стоимость_шага={inst.min_increment_amount}")
    print(f"    шорт разрешён: {inst.short_enabled}, торговля через API:"
          f" {inst.api_trade_available}")

    step("Последняя цена")
    px = api.last_price(inst.uid)
    print(f"    {px}")
    if px is None:
        print("    цены нет — вне торговой сессии, остальные шаги пропущены")
        return

    step("Лимитная заявка далеко от рынка, затем отмена")
    # Цена на 10% ниже рынка: заявка гарантированно не исполнится.
    far = round(px * 0.9 / inst.min_increment) * inst.min_increment
    resp = api.post_order(account_id, inst.uid, 1, "buy", price=far)
    order_id = resp.get("orderId", "")
    print(f"    выставлена по {far}, статус {resp.get('executionReportStatus')}")
    log_order(resp, "limit_far")

    time.sleep(1)
    active = api.orders(account_id)
    print(f"    активных заявок: {len(active)}")
    if order_id:
        api.cancel(account_id, order_id)
        print("    отменена")

    step("Рыночная заявка: покупка 1 лота")
    buy = api.post_order(account_id, inst.uid, 1, "buy")
    log_order(buy, "market_buy")
    print(f"    статус {buy.get('executionReportStatus')},"
          f" исполнено лотов {buy.get('lotsExecuted')}")
    print(f"    цена исполнения {from_q(buy.get('executedOrderPrice'))},"
          f" комиссия {from_q(buy.get('executedCommission'))}")

    step("Портфель")
    pf = api.portfolio(account_id)
    for p in pf.get("positions", []):
        print(f"    {p.get('instrumentType')} {p.get('figi')}:"
              f" количество {from_q(p.get('quantity'))},"
              f" средняя {from_q(p.get('averagePositionPrice'))}")

    step("Закрытие позиции")
    sell = api.post_order(account_id, inst.uid, 1, "sell")
    log_order(sell, "market_sell")
    print(f"    статус {sell.get('executionReportStatus')},"
          f" цена {from_q(sell.get('executedOrderPrice'))},"
          f" комиссия {from_q(sell.get('executedCommission'))}")

    step("Операции за сутки")
    now = datetime.now(timezone.utc)
    ops = api.operations(
        account_id,
        (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        now.isoformat().replace("+00:00", "Z"),
    )
    print(f"    операций: {len(ops)}")

    step("Журнал заявок")
    path = ORDER_LOG / f"{now.date().isoformat()}.parquet"
    if path.exists():
        with pl.Config(tbl_cols=-1, tbl_width_chars=200):
            print(pl.read_parquet(path))
    print("\nОбвязка работает. Издержки здесь не измеряются: в песочнице нет"
          "\nвлияния на рынок, проскальзывание меряется на живом счёте.")


def from_q(q: dict | None) -> float | None:
    from broker import from_quotation

    return from_quotation(q)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "SiZ6")
