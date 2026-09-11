from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from trade_exporter.domain.models import Account, Fill


class BrokerAdapter(ABC):
    """Контракт между API брокера и бизнес-логикой проекта."""

    name: str

    @abstractmethod
    def list_accounts(self) -> list[Account]:
        raise NotImplementedError

    @abstractmethod
    def get_fills(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[Fill]:
        """Вернуть исполнения BUY/SELL в диапазоне [date_from, date_to)."""
        raise NotImplementedError
