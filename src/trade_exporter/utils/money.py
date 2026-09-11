from decimal import Decimal

NANO = Decimal("1000000000")


def quotation_to_decimal(value: dict | None) -> Decimal:
    if not value:
        return Decimal("0")
    units = Decimal(str(value.get("units", 0)))
    nano = Decimal(str(value.get("nano", 0)))
    return units + nano / NANO


def money_currency(value: dict | None) -> str:
    if not value:
        return ""
    return str(value.get("currency", "") or "")
