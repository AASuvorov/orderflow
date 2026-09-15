"""Исполнительный слой: заявки на фьючерсы МОЕХ через T-Invest API.

Зачем отдельным модулем и заранее. Весь наш расчёт издержек стоит на двух
допущениях: спред в один шаг цены и комиссия брокера в рубль за контракт.
Разница между 0.59 и 1.5 б.п. — это разница между порогом 56% и 63% на
пятнадцати минутах, то есть между "возможно" и "нет". Проверить допущения можно
только реальными заявками, поэтому обвязка нужна раньше, чем сигнал.

Песочница проверяет жизненный цикл заявки, но НЕ измеряет издержки: в ней нет
влияния на рынок и нет расчёта ГО, рыночные заявки исполняются по последней
цене. Проскальзывание меряется потом, на живом счёте минимальным размером.

Один и тот же код работает в обоих контурах: отличается только базовый адрес.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import requests

LIVE = "https://invest-public-api.tbank.ru/rest"
SANDBOX = "https://sandbox-invest-public-api.tbank.ru/rest"
NS = "tinkoff.public.invest.api.contract.v1"

DATA_ROOT = Path(
    os.environ.get("ORDERFLOW_DATA", Path(__file__).resolve().parents[2] / "data")
)
ORDER_LOG = DATA_ROOT / "orders"


def to_quotation(value: float) -> dict:
    """Цена в формате API: целая часть и наноединицы."""
    units = int(value)
    nano = round((value - units) * 1e9)
    return {"units": str(units), "nano": nano}


def from_quotation(q: dict | None) -> float | None:
    if not q:
        return None
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9


@dataclass
class Instrument:
    uid: str
    figi: str
    ticker: str
    name: str
    lot: int
    min_increment: float
    min_increment_amount: float | None
    short_enabled: bool
    api_trade_available: bool


class TInvest:
    """Тонкий клиент REST-контура T-Invest API.

    Токен берётся из переменной TINVEST_TOKEN и никогда не пишется в логи.
    """

    TOKEN_FILE = Path.home() / ".config" / "tinvest" / "token"

    def __init__(self, token: str | None = None, sandbox: bool = True):
        self.token = token or os.environ.get("TINVEST_TOKEN", "") or self._from_file()
        if not self.token:
            raise RuntimeError(
                "нужен токен: положите его в "
                f"{self.TOKEN_FILE} либо export TINVEST_TOKEN=..."
            )
        self.base = SANDBOX if sandbox else LIVE
        self.sandbox = sandbox
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "x-app-name": "orderflow.research",
            }
        )

    @classmethod
    def _from_file(cls) -> str:
        """Токен из файла: секрет не попадает ни в историю команд, ни в переписку."""
        if not cls.TOKEN_FILE.exists():
            return ""
        return cls.TOKEN_FILE.read_text(encoding="utf-8").strip()

    def call(self, service: str, method: str, payload: dict | None = None) -> dict:
        r = self.session.post(
            f"{self.base}/{NS}.{service}/{method}", json=payload or {}, timeout=30
        )
        if r.status_code >= 400:
            # Сообщение API информативно, а токен в него не попадает.
            raise RuntimeError(f"{service}/{method} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # --- счёт ---

    def accounts(self) -> list[dict]:
        return self.call("UsersService", "GetAccounts").get("accounts", [])

    def ensure_account(self, initial_rub: float = 500_000) -> str:
        """В песочнице открывает счёт и пополняет его, если счетов ещё нет."""
        acc = self.accounts()
        if acc:
            return acc[0]["id"]
        if not self.sandbox:
            raise RuntimeError("на живом контуре счёт открывается в приложении")

        new = self.call("SandboxService", "OpenSandboxAccount")
        account_id = new["accountId"]
        self.call(
            "SandboxService",
            "SandboxPayIn",
            {
                "accountId": account_id,
                "amount": {"currency": "rub", **to_quotation(initial_rub)},
            },
        )
        return account_id

    # --- инструменты ---

    def find_future(self, query: str) -> Instrument | None:
        """Ищет фьючерс по тикеру, например SiZ6."""
        res = self.call(
            "InstrumentsService",
            "FindInstrument",
            {"query": query, "instrumentKind": "INSTRUMENT_TYPE_FUTURES", "apiTradeAvailableFlag": True},
        )
        items = res.get("instruments", [])
        exact = [i for i in items if i.get("ticker", "").upper() == query.upper()]
        pick = (exact or items or [None])[0]
        if pick is None:
            return None

        full = self.call(
            "InstrumentsService", "FutureBy",
            {"idType": "INSTRUMENT_ID_TYPE_UID", "id": pick["uid"]},
        ).get("instrument", {})

        return Instrument(
            uid=full.get("uid", pick["uid"]),
            figi=full.get("figi", pick.get("figi", "")),
            ticker=full.get("ticker", pick.get("ticker", "")),
            name=full.get("name", pick.get("name", "")),
            lot=int(full.get("lot", 1)),
            min_increment=from_quotation(full.get("minPriceIncrement")) or 0.0,
            min_increment_amount=from_quotation(full.get("minPriceIncrementAmount")),
            short_enabled=bool(full.get("shortEnabledFlag", False)),
            api_trade_available=bool(full.get("apiTradeAvailableFlag", False)),
        )

    def last_price(self, uid: str) -> float | None:
        res = self.call("MarketDataService", "GetLastPrices", {"instrumentId": [uid]})
        prices = res.get("lastPrices", [])
        return from_quotation(prices[0].get("price")) if prices else None

    # --- заявки ---

    def post_order(
        self,
        account_id: str,
        uid: str,
        lots: int,
        direction: str,
        price: float | None = None,
    ) -> dict:
        """direction: buy | sell. Без price — рыночная заявка."""
        body = {
            "accountId": account_id,
            "instrumentId": uid,
            "quantity": str(lots),
            "direction": f"ORDER_DIRECTION_{direction.upper()}",
            "orderType": "ORDER_TYPE_MARKET" if price is None else "ORDER_TYPE_LIMIT",
            "orderId": str(uuid.uuid4()),
        }
        if price is not None:
            body["price"] = to_quotation(price)
        return self.call("OrdersService", "PostOrder", body)

    def orders(self, account_id: str) -> list[dict]:
        return self.call("OrdersService", "GetOrders", {"accountId": account_id}).get(
            "orders", []
        )

    def cancel(self, account_id: str, order_id: str) -> dict:
        return self.call(
            "OrdersService", "CancelOrder",
            {"accountId": account_id, "orderId": order_id},
        )

    def portfolio(self, account_id: str) -> dict:
        return self.call(
            "OperationsService", "GetPortfolio", {"accountId": account_id}
        )

    def operations(self, account_id: str, since: str, to: str) -> list[dict]:
        return self.call(
            "OperationsService",
            "GetOperations",
            {"accountId": account_id, "from": since, "to": to},
        ).get("operations", [])


def log_order(resp: dict, tag: str) -> None:
    """Пишет факт исполнения в тот же формат, что используется в анализе.

    Именно из этого журнала потом считаются реальные издержки: разница между
    ценой решения и ценой исполнения плюс фактическая комиссия.
    """
    ORDER_LOG.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).replace(tzinfo=None),
        "tag": tag,
        "order_id": resp.get("orderId", ""),
        "status": resp.get("executionReportStatus", ""),
        "direction": resp.get("direction", ""),
        "lots_requested": int(resp.get("lotsRequested", 0) or 0),
        "lots_executed": int(resp.get("lotsExecuted", 0) or 0),
        "price_requested": from_quotation(resp.get("initialSecurityPrice")),
        "price_executed": from_quotation(resp.get("executedOrderPrice")),
        "commission": from_quotation(resp.get("executedCommission")),
        "total": from_quotation(resp.get("totalOrderAmount")),
    }
    path = ORDER_LOG / f"{datetime.now(timezone.utc).date().isoformat()}.parquet"
    df = pl.DataFrame([row])
    if path.exists():
        df = pl.concat([pl.read_parquet(path), df], how="vertical_relaxed")
    df.write_parquet(path, compression="zstd")
