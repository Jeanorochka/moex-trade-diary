from collections.abc import Callable

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.brokers.tbank import TBankBroker

BROKER_FACTORIES: dict[str, Callable[[dict], BrokerAdapter]] = {
    "tbank": TBankBroker,
}


def create_broker(name: str, settings: dict) -> BrokerAdapter:
    normalized = name.strip().lower()
    if normalized not in BROKER_FACTORIES:
        available = ", ".join(sorted(BROKER_FACTORIES))
        raise ValueError(f"Неизвестный брокер '{name}'. Доступны: {available}")
    return BROKER_FACTORIES[normalized](settings)


def available_brokers() -> list[str]:
    return sorted(BROKER_FACTORIES)
