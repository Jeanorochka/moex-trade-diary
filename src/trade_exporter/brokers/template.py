"""Шаблон подключения следующего брокера."""

from datetime import datetime

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account, Fill


class NewBrokerAdapter(BrokerAdapter):
    name = "newbroker"

    def __init__(self, settings: dict):
        self.settings = settings

    def list_accounts(self) -> list[Account]:
        # Запросить счета API и привести их к Account.
        raise NotImplementedError

    def get_fills(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[Fill]:
        # Запросить историю и привести BUY/SELL к Fill.
        raise NotImplementedError
