from collections.abc import Callable

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.brokers.bcs import BcsBroker
from trade_exporter.brokers.finam import FinamBroker
from trade_exporter.brokers.tbank import TBankBroker

BROKER_FACTORIES: dict[str, Callable[[dict], BrokerAdapter]] = {
    "finam": FinamBroker,
    "tbank": TBankBroker,
    "bcs": BcsBroker,
}

BROKER_NAMES = {"finam": "Финам", "tbank": "Т-Банк", "bcs": "БКС"}
TOKEN_ENVS = {"finam": "F.INVEST_TOKEN", "tbank": "T.INVEST_TOKEN", "bcs": "B.INVEST_TOKEN"}


def create_broker(name: str, settings: dict) -> BrokerAdapter:
    normalized = name.strip().lower()
    if normalized not in BROKER_FACTORIES:
        available = ", ".join(sorted(BROKER_FACTORIES))
        raise ValueError(f"Неизвестный брокер '{name}'. Доступны: {available}")
    return BROKER_FACTORIES[normalized](settings)


def available_brokers() -> list[str]:
    return list(BROKER_FACTORIES)
