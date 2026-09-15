"""Предохранитель: механические ограничения риска перед каждой заявкой.

Зачем это отдельным слоем. Ограничения, существующие только в намерениях,
отменяются ровно в тот момент, когда их надо соблюдать: после серии убытков
хочется отыграться, после серии прибылей — увеличить объём. Оба желания
естественны и оба разоряют. Поэтому лимиты записываются один раз в файл, а код
исполнения обязан спрашивать разрешение и не имеет способа его обойти.

Что ограничивается и почему именно это:
  * число контрактов — прямой ограничитель плеча, главной причины разорения
    при работающей стратегии;
  * дневной убыток — останавливает торговлю до конца дня, отсекая попытку
    отыграться, самую дорогую ошибку в трейдинге;
  * число заявок за день — защита от программной ошибки, а не от эмоций:
    цикл, потерявший условие выхода, способен разорить счёт за минуты;
  * торговые часы — заявка вне сессии либо не исполнится, либо исполнится по
    неожиданной цене на низкой ликвидности.

Лимиты намеренно заданы в рублях и контрактах, а не в процентах: проценты
незаметно растут вместе со счётом, а разорение случается в рублях.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
STATE = DATA_ROOT / "risk_state.json"
LIMITS_FILE = DATA_ROOT / "risk_limits.json"


class RiskViolation(RuntimeError):
    """Заявка отклонена предохранителем. Не перехватывать в торговом цикле."""


@dataclass
class Limits:
    """Границы, за которые торговый код не может выйти.

    Значения по умолчанию рассчитаны на капитал 200 000 руб и контракт Si:
    7 контрактов — это около 30% капитала в обеспечении, дневной убыток
    5 000 руб — примерно одно стандартное отклонение дневного результата,
    то есть срабатывание ожидается несколько раз в месяц и не является ЧП.
    """

    макс_контрактов: int = 7
    макс_убыток_день_руб: float = 5_000
    макс_заявок_день: int = 200
    часы_торговли: tuple[int, int] = (10, 22)

    @classmethod
    def load(cls) -> "Limits":
        if LIMITS_FILE.exists():
            raw = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
            raw["часы_торговли"] = tuple(raw["часы_торговли"])
            return cls(**raw)
        return cls()

    def save(self) -> None:
        LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIMITS_FILE.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )


class RiskGate:
    """Хранит состояние дня и решает, пропускать ли заявку.

    Состояние лежит на диске: перезапуск процесса не должен обнулять счётчик
    убытков, иначе дневной лимит обходится простым рестартом.
    """

    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits.load()
        self.state = self._load_state()

    def _load_state(self) -> dict:
        today = datetime.now(MSK).date().isoformat()
        if STATE.exists():
            state = json.loads(STATE.read_text(encoding="utf-8"))
            if state.get("день") == today:
                return state
        return {"день": today, "пнл_руб": 0.0, "заявок": 0, "позиция": 0,
                "остановлен": False, "причина": ""}

    def _save_state(self) -> None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def check(self, лотов: int, направление: int) -> None:
        """Проверяет заявку. Молча возвращается, если всё в порядке.

        направление: +1 покупка, -1 продажа. Проверяется итоговая позиция, а не
        размер заявки: закрытие позиции разрешено всегда, даже после остановки.
        """
        итог = self.state["позиция"] + направление * лотов
        закрываемся = abs(итог) < abs(self.state["позиция"])

        if self.state["остановлен"] and not закрываемся:
            raise RiskViolation(
                f"торговля остановлена на сегодня: {self.state['причина']}. "
                "Разрешено только закрытие позиции"
            )

        час = datetime.now(MSK).hour
        начало, конец = self.limits.часы_торговли
        if not (начало <= час < конец) and not закрываемся:
            raise RiskViolation(
                f"вне торговых часов {начало}:00–{конец}:00 МСК (сейчас {час}:00)"
            )

        if abs(итог) > self.limits.макс_контрактов:
            raise RiskViolation(
                f"позиция {итог} превысит лимит {self.limits.макс_контрактов} "
                "контрактов"
            )

        if self.state["заявок"] >= self.limits.макс_заявок_день:
            raise RiskViolation(
                f"исчерпан дневной лимит заявок ({self.limits.макс_заявок_день}) — "
                "похоже на программную ошибку, а не на торговлю"
            )

    def register(self, лотов: int, направление: int, пнл_руб: float = 0.0) -> None:
        """Учитывает исполненную заявку и при необходимости останавливает день."""
        self.state["позиция"] += направление * лотов
        self.state["заявок"] += 1
        self.state["пнл_руб"] += пнл_руб

        if self.state["пнл_руб"] <= -self.limits.макс_убыток_день_руб:
            self.state["остановлен"] = True
            self.state["причина"] = (
                f"дневной убыток {self.state['пнл_руб']:.0f} руб достиг лимита "
                f"{self.limits.макс_убыток_день_руб:.0f} руб"
            )
        self._save_state()

    def status(self) -> str:
        s, l = self.state, self.limits
        строки = [
            f"день {s['день']}",
            f"позиция {s['позиция']} из {l.макс_контрактов} контрактов",
            f"результат {s['пнл_руб']:+.0f} руб, лимит убытка "
            f"{-l.макс_убыток_день_руб:.0f} руб",
            f"заявок {s['заявок']} из {l.макс_заявок_день}",
        ]
        if s["остановлен"]:
            строки.append(f"ОСТАНОВЛЕН: {s['причина']}")
        return "\n".join(строки)


if __name__ == "__main__":
    limits = Limits.load()
    limits.save()
    gate = RiskGate(limits)

    print("=== Лимиты ===")
    for k, v in asdict(limits).items():
        print(f"  {k}: {v}")
    print(f"\n(изменить: {LIMITS_FILE})")

    print("\n=== Состояние ===")
    print(gate.status())

    print("\n=== Проверка предохранителя ===")
    for лотов, направление, описание in [
        (3, 1, "покупка 3 контрактов"),
        (10, 1, "покупка 10 контрактов — должна быть отклонена"),
    ]:
        try:
            gate.check(лотов, направление)
            print(f"  разрешено: {описание}")
        except RiskViolation as exc:
            print(f"  отклонено: {описание}\n      причина: {exc}")
