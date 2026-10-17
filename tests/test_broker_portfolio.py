from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_exporter.brokers.portfolio import load_cash_flows, load_portfolio
from trade_exporter.domain.models import Account


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=4)


def account(broker="finam", opened=START):
    return Account("123", "test", broker, opened_at=opened)


def finam_money(value, currency="RUB"):
    number = Decimal(str(value))
    units = int(number)
    return {"currency_code": currency, "units": str(units), "nanos": int((number - units) * 1_000_000_000)}


def money(value, currency="rub"):
    raw = finam_money(value, currency)
    return {"currency": raw["currency_code"], "units": raw["units"], "nano": raw["nanos"]}


def quotation(value):
    raw = money(value)
    return {"units": raw["units"], "nano": raw["nano"]}


def finam_position(symbol="SBER@MISX", **changes):
    return {
        "symbol": symbol, "quantity": {"value": "2"}, "average_price": {"value": "100"},
        "current_price": {"value": "120"}, "current_price_currency": "RUB",
        "average_price_currency": "RUB", "unrealized_pnl": {"value": "40"}, **changes,
    }


def finam_broker(*, data=None, transactions=None, orders=None, settings=None):
    value = SimpleNamespace(
        name="finam", settings=settings or {}, calls=[], transactions=transactions or [],
        data=data if data is not None else {
            "account_id": "123", "equity": {"value": "1100.01"},
            "unrealized_profit": {"value": "99999"}, "portfolio_mc": {},
            "cash": [finam_money("860.01")], "positions": [finam_position()],
            "open_account_date": START.isoformat(), "first_non_trade_date": START.isoformat(),
        },
        orders=orders if orders is not None else [],
    )

    def request(method, path, **options):
        value.calls.append((method, path, options))
        assert method == "GET"
        if path.endswith("/orders"):
            if isinstance(value.orders, Exception):
                raise value.orders
            return {"orders": value.orders}
        if path.endswith("/transactions"):
            params = options["params"]
            start = datetime.fromisoformat(params["interval.start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(params["interval.end_time"].replace("Z", "+00:00"))
            rows = [item for item in value.transactions if start <= datetime.fromisoformat(item["timestamp"]) < end]
            return {"transactions": rows[:params["limit"]]}
        assert path == "accounts/123"
        return value.data

    def asset(account_id, symbol):
        assert account_id == "123"
        return {"ticker": symbol.split("@")[0], "type": "FUTURES" if "@RTSX" in symbol else "EQUITIES", "quote_currency": "RUB"}

    value._request, value._asset = request, asset
    return value


def tbank_position(**changes):
    return {
        "instrumentUid": "stock-uid", "ticker": "SBER", "instrumentType": "share",
        "quantity": quotation("2"), "quantityLots": quotation("999"),
        "currentPrice": money("120"), "averagePositionPrice": money("101"),
        "averagePositionPriceFifo": money("100"), "expectedYield": quotation("38"),
        "expectedYieldFifo": quotation("40"), **changes,
    }


def tbank_broker(*, data=None, balances=None, pages=None, settings=None):
    value = SimpleNamespace(
        name="tbank", settings=settings or {}, calls=[],
        data=data if data is not None else {
            "accountId": "123", "totalAmountPortfolio": money("1100.01"),
            "expectedYield": quotation("987"), "positions": [tbank_position()],
            "virtualPositions": [tbank_position(instrumentUid="virtual", quantity=quotation("99"))],
        },
        balances=balances if balances is not None else {
            "accountId": "123", "money": [money("860.01")], "blocked": [money("200")],
            "limitsLoadingInProgress": False,
        },
        pages=pages if pages is not None else [{"items": [], "hasNext": False}],
    )

    def post(method, payload):
        value.calls.append((method, payload))
        if method.endswith("/GetPortfolio"):
            return value.data
        if method.endswith("/GetPositions"):
            return value.balances
        assert method.endswith("/GetOperationsByCursor")
        return value.pages.pop(0)

    value._post = post
    return value


def transaction(identity, day=0, category="DEPOSIT", amount="100", **changes):
    return {
        "id": identity, "timestamp": (START + timedelta(days=day)).isoformat(),
        "transaction_category": category, "change": finam_money(amount), **changes,
    }


def operation(identity, day=0, kind="INPUT", amount="100", **changes):
    return {
        "id": identity, "date": (START + timedelta(days=day)).isoformat(),
        "type": "OPERATION_TYPE_" + kind, "payment": money(amount),
        "state": "OPERATION_STATE_EXECUTED", "brokerAccountId": "123", **changes,
    }


def test_finam_snapshot_keeps_authoritative_equity_and_brackets_orders():
    broker = finam_broker()
    snapshot = load_portfolio(broker, account(), captured_at=END)
    assert snapshot.equity == Decimal("1100.01")
    assert snapshot.cash[0].amount == Decimal("860.01")
    assert snapshot.captured_at == END
    assert snapshot.base_currency == "RUB"
    assert not snapshot.positions_include_pending_orders
    assert snapshot.positions[0].average_price == Decimal("100")
    assert snapshot.positions[0].current_price == Decimal("120")
    assert snapshot.positions[0].unrealized_pnl == Decimal("40")
    assert snapshot.positions[0].quantity == Decimal("2")
    assert [call[1] for call in broker.calls] == ["accounts/123/orders", "accounts/123", "accounts/123/orders"]
    assert "по умолчанию" in snapshot.source


@pytest.mark.parametrize("status", ["ORDER_STATUS_NEW", "ORDER_STATUS_DONE_FOR_DAY", "UNKNOWN", 0, 4, None])
def test_finam_active_or_unknown_order_status_marks_theoretical_positions(status):
    broker = finam_broker(orders=[{"status": status}])
    snapshot = load_portfolio(broker, account())
    assert snapshot.positions_include_pending_orders
    assert snapshot.positions[0].market_value is None
    assert snapshot.positions[0].cost_basis is None


@pytest.mark.parametrize("status", ["ORDER_STATUS_FILLED", "ORDER_STATUS_CANCELED", "ORDER_STATUS_TP_EXECUTED", 3, 5, 31])
def test_finam_terminal_orders_do_not_block_actual_position_valuation(status):
    snapshot = load_portfolio(finam_broker(orders=[{"status": status}]), account())
    assert not snapshot.positions_include_pending_orders


def test_finam_unavailable_order_history_keeps_snapshot_and_marks_pending():
    snapshot = load_portfolio(finam_broker(orders=RuntimeError("unavailable")), account())
    assert snapshot.equity == Decimal("1100.01")
    assert snapshot.positions_include_pending_orders


def test_finam_pending_future_order_does_not_hide_unrelated_stock_valuation():
    broker = finam_broker(orders=[{
        "status": "ORDER_STATUS_NEW", "order": {"symbol": "SiH6@RTSX"},
        "remaining_quantity": {"value": "5"},
    }])
    broker.data["positions"].append(finam_position("SiH6@RTSX", average_price=None))
    snapshot = load_portfolio(broker, account())
    assert snapshot.positions_include_pending_orders
    assert snapshot.positions[0].quantity_includes_pending_orders is False
    assert snapshot.positions[1].quantity_includes_pending_orders is True
    assert snapshot.positions[1].quantity == 2


def test_finam_futures_do_not_turn_notional_or_margin_into_equity():
    broker = finam_broker()
    broker.data["positions"] = [finam_position(
        "SiH6@RTSX", average_price=None, current_price={"value": "100000"},
        unrealized_pnl={"value": "12.34"}, maintenance_margin={"value": "10000"},
    )]
    snapshot = load_portfolio(broker, account())
    position = snapshot.positions[0]
    assert position.market_value == 0
    assert position.cost_basis is None
    assert position.average_price is None
    assert position.unrealized_pnl == Decimal("12.34")
    assert snapshot.equity == Decimal("1100.01")


def test_finam_different_price_currencies_are_preserved_without_false_ruble_pnl():
    broker = finam_broker()
    broker.data["positions"] = [finam_position(current_price_currency="USD", average_price_currency="EUR")]
    position = load_portfolio(broker, account()).positions[0]
    assert position.price_currency == "USD"
    assert position.average_price_currency == "EUR"
    assert position.unrealized_pnl is None
    assert position.market_value is None


def test_finam_mct_requires_explicit_equity_currency():
    broker = finam_broker()
    broker.data["portfolio_mct"] = {}
    with pytest.raises(RuntimeError, match="base_currency"):
        load_portfolio(broker, account())
    broker.settings["base_currency"] = "USD"
    assert load_portfolio(broker, account()).base_currency == "USD"


def test_finam_moex_base_currency_cannot_relabel_ruble_equity_as_dollars():
    with pytest.raises(RuntimeError, match="не выполняет пересчёт"):
        load_portfolio(finam_broker(settings={"base_currency": "USD"}), account())


def test_finam_different_average_currency_does_not_invent_ruble_unrealized_pnl():
    broker = finam_broker()
    broker.data["positions"] = [finam_position(average_price_currency="USD")]
    assert load_portfolio(broker, account()).positions[0].unrealized_pnl is None


@pytest.mark.parametrize("field", ["equity", "quantity", "current_price", "unrealized_pnl"])
def test_finam_nonfinite_values_are_rejected(field):
    broker = finam_broker()
    target = broker.data if field == "equity" else broker.data["positions"][0]
    target[field] = {"value": "NaN"}
    with pytest.raises(RuntimeError, match=field):
        load_portfolio(broker, account())


def test_tbank_quantity_is_pieces_money_excludes_repeated_blocked_and_virtuals():
    broker = tbank_broker()
    broker.data["positions"].append(tbank_position(instrumentUid="cash", instrumentType="currency"))
    snapshot = load_portfolio(broker, account("tbank"), captured_at=END)
    assert snapshot.equity == Decimal("1100.01")
    assert snapshot.cash[0].amount == Decimal("860.01")
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.quantity == Decimal("2")
    assert position.average_price == Decimal("100")
    assert position.cost_basis == Decimal("200")
    assert position.market_value == Decimal("240")
    assert position.unrealized_pnl == Decimal("40")
    assert broker.calls[0][1] == {"accountId": "123", "currency": "RUB"}
    assert not snapshot.positions_include_pending_orders


def test_tbank_futures_value_is_zero_but_broker_monetary_pnl_is_preserved():
    broker = tbank_broker()
    broker.data["positions"] = [tbank_position(
        instrumentType="futures", currentPrice=money("100000"),
        expectedYieldFifo=quotation("-25.01"), varMargin=money("90000"),
    )]
    snapshot = load_portfolio(broker, account("tbank"))
    assert snapshot.positions[0].market_value == 0
    assert snapshot.positions[0].cost_basis is None
    assert snapshot.positions[0].unrealized_pnl == Decimal("-25.01")
    assert snapshot.equity == Decimal("1100.01")


def test_tbank_price_and_average_currencies_are_not_combined():
    broker = tbank_broker()
    broker.data["positions"] = [tbank_position(averagePositionPriceFifo=money("100", "usd"))]
    position = load_portfolio(broker, account("tbank")).positions[0]
    assert position.price_currency == "RUB"
    assert position.average_price_currency == "USD"
    assert position.cost_basis is None


@pytest.mark.parametrize("change", ["loading", "wrong_account", "wrong_currency", "missing_equity"])
def test_tbank_incomplete_or_inconsistent_snapshot_fails(change):
    broker = tbank_broker()
    if change == "loading":
        broker.balances["limitsLoadingInProgress"] = True
    elif change == "wrong_account":
        broker.data["accountId"] = "other"
    elif change == "wrong_currency":
        broker.data["totalAmountPortfolio"]["currency"] = "USD"
    else:
        del broker.data["totalAmountPortfolio"]
    with pytest.raises(RuntimeError):
        load_portfolio(broker, account("tbank"))


def test_finam_full_flow_history_splits_saturated_windows_and_preserves_distinct_ids():
    rows = [transaction("a"), transaction("b", 1), transaction("c", 2), transaction("d", 3, "WITHDRAW", "-10")]
    broker = finam_broker(transactions=rows, settings={"transaction_limit": 2})
    history = load_cash_flows(broker, account(), START, END)
    assert history.complete
    assert [flow.id for flow in history.flows] == ["a", "b", "c", "d"]
    assert sum(flow.amount for flow in history.flows) == Decimal("290")
    assert history.started_at == START
    assert len([call for call in broker.calls if call[1].endswith("/transactions")]) > 1


def test_finam_exact_id_duplicates_are_removed_but_equal_economics_remain():
    row = transaction("a")
    broker = finam_broker(transactions=[row, deepcopy(row), transaction("b")])
    history = load_cash_flows(broker, account(), START, END)
    assert history.complete
    assert [flow.id for flow in history.flows] == ["a", "b"]


def test_finam_ordinary_trades_income_fees_and_tax_do_not_invalidate_funding():
    rows = [
        transaction("deposit"), transaction("trade", category="OTHERS", amount="-20", trade={"size": {"value": "1"}}),
        transaction("income", category="INCOME", amount="2"),
        transaction("fee", category="COMMISSION", amount="-1"),
        transaction("tax", category="TAX", amount="-0.5"),
        transaction("margin", category="OTHERS", amount="4", transaction_name="Вариационная маржа"),
    ]
    history = load_cash_flows(finam_broker(transactions=rows), account(), START, END)
    assert history.complete
    assert [flow.kind for flow in history.flows] == ["external", "other", "income", "fee", "tax", "other"]
    assert sum(flow.amount for flow in history.flows if flow.kind == "external") == Decimal("100")


@pytest.mark.parametrize("row", [
    transaction("x", category="TRANSFER", amount="0", change_qty={"value": "2"}),
    transaction("x", category="TRANSFER", amount="50"),
    transaction("x", category="TRANSFER", amount="0"),
    transaction("x", category="INHERITANCE", amount="50"),
    transaction("x", category="UNKNOWN", amount="50"),
    transaction("x", category="OTHERS", amount="50", transaction_name="Неизвестное зачисление"),
    transaction("x", change=finam_money("50", "USD")),
])
def test_finam_ambiguous_external_value_makes_history_incomplete(row):
    history = load_cash_flows(finam_broker(transactions=[row]), account(), START, END)
    assert not history.complete
    assert history.reasons


def test_missing_open_date_returns_empty_incomplete_history_even_with_none_start():
    broker = finam_broker()
    history = load_cash_flows(broker, account(opened=None), None, END)
    assert not history.complete
    assert history.flows == ()
    assert history.started_at is None
    assert broker.calls == []


def test_first_trade_date_cannot_prove_complete_historical_funding():
    broker = finam_broker(transactions=[transaction("a", 2)])
    del broker.data["open_account_date"]
    broker.data["first_trade_date"] = (START + timedelta(days=2)).isoformat()
    history = load_cash_flows(broker, account(opened=START + timedelta(days=2)), START + timedelta(days=2), END)
    assert not history.complete
    assert any("первой сделки" in reason for reason in history.reasons)
    assert any("первые неторговые" in reason for reason in history.reasons)


def test_finam_missing_prefirst_trade_funding_is_not_hidden_by_later_deposit():
    broker = finam_broker(transactions=[transaction("later", day=3)])
    broker.data["first_trade_date"] = (START + timedelta(days=2)).isoformat()
    history = load_cash_flows(broker, account(), START, END)
    assert not history.complete
    assert len(history.flows) == 1
    assert history.flows[0].kind == "external"
    assert any("начальное финансирование" in reason for reason in history.reasons)


def test_finam_returned_prefirst_trade_funding_confirms_start_coverage():
    broker = finam_broker(transactions=[transaction("initial"), transaction("later", day=3)])
    broker.data["first_trade_date"] = (START + timedelta(days=2)).isoformat()
    history = load_cash_flows(broker, account(), START, END)
    assert history.complete
    assert [flow.id for flow in history.flows] == ["initial", "later"]


def test_finam_missing_first_non_trade_metadata_cannot_confirm_history_before_first_trade():
    broker = finam_broker(transactions=[transaction("later", day=3)])
    del broker.data["first_non_trade_date"]
    broker.data["first_trade_date"] = (START + timedelta(days=2)).isoformat()
    history = load_cash_flows(broker, account(), START, END)
    assert not history.complete
    assert any("Transactions может начинаться" in reason for reason in history.reasons)


def test_finam_observed_window_can_be_complete_after_missing_inception_funding():
    broker = finam_broker(transactions=[transaction("later", day=3)])
    broker.data["first_trade_date"] = (START + timedelta(days=1)).isoformat()
    lifetime = load_cash_flows(broker, account(), START, END)
    observed = load_cash_flows(
        broker, account(), START + timedelta(days=2), END, require_inception=False,
    )
    assert not lifetime.complete
    assert observed.complete
    assert observed.started_at == START + timedelta(days=2)
    assert [flow.id for flow in observed.flows] == ["later"]


def test_finam_observed_window_does_not_require_account_open_date():
    broker = finam_broker(transactions=[transaction("later", day=3)])
    del broker.data["open_account_date"]
    observed = load_cash_flows(
        broker, account(opened=None), START + timedelta(days=2), END, require_inception=False,
    )
    assert observed.complete
    assert observed.started_at == START + timedelta(days=2)
    assert [flow.id for flow in observed.flows] == ["later"]
    assert not load_cash_flows(broker, account(opened=None), None, END).complete


def test_observed_window_still_rejects_ambiguous_transfers_inside_window():
    broker = finam_broker(transactions=[transaction("transfer", day=3, category="TRANSFER", amount="50")])
    observed = load_cash_flows(
        broker, account(), START + timedelta(days=2), END, require_inception=False,
    )
    assert not observed.complete
    assert any("TRANSFER" in reason for reason in observed.reasons)


def test_requested_partial_history_is_never_marked_complete():
    history = load_cash_flows(finam_broker(), account(), START + timedelta(days=1), END)
    assert not history.complete
    assert history.reasons


def test_finam_unresolvable_transaction_limit_fails_instead_of_truncation():
    broker = finam_broker(transactions=[transaction("a"), transaction("b")], settings={"transaction_limit": 2})
    with pytest.raises(RuntimeError, match="микросекунды"):
        load_cash_flows(broker, account(), START, START + timedelta(microseconds=1))


def test_finam_inclusive_upper_endpoint_is_filtered_without_losing_inside_operations():
    broker = finam_broker()
    original = broker._request

    def request(method, path, **options):
        if path.endswith("/transactions"):
            return {"transactions": [transaction("a"), transaction("end", day=4)]}
        return original(method, path, **options)

    broker._request = request
    history = load_cash_flows(broker, account(), START, END)
    assert history.complete
    assert [flow.id for flow in history.flows] == ["a"]


@pytest.mark.parametrize("change", ["missing_id", "bad_sign", "nonfinite"])
def test_finam_invalid_transaction_data_fails(change):
    rows = [transaction("a")]
    if change == "missing_id":
        del rows[0]["id"]
    elif change == "bad_sign":
        rows[0]["transaction_category"] = "WITHDRAW"
    else:
        rows[0]["change"]["units"] = "NaN"
    with pytest.raises(RuntimeError):
        load_cash_flows(finam_broker(transactions=rows), account(), START, END)


def test_finam_shared_transfer_id_preserves_both_legs_without_claiming_complete():
    rows = [transaction(
        "shared", category="TRANSFER", amount=amount, change_qty={"value": quantity},
        symbol="TEST@MISX", symbols=["TEST@MISX"], transaction_name="Перевод ценных бумаг",
    ) for amount, quantity in [("-60000.80", "-56"), ("60000.80", "56")]]
    original = deepcopy(rows)
    history = load_cash_flows(finam_broker(transactions=rows + [deepcopy(rows[0])]), account(), START, END)
    repeated = load_cash_flows(finam_broker(transactions=list(reversed(rows))), account(), START, END)
    assert not history.complete
    assert len(history.flows) == 2
    assert len({flow.id for flow in history.flows}) == 2
    assert {flow.id for flow in history.flows} == {flow.id for flow in repeated.flows}
    assert {flow.amount for flow in history.flows} == {Decimal("-60000.80"), Decimal("60000.80")}
    assert all(flow.kind == "other" for flow in history.flows)
    assert any("разные записи" in reason for reason in history.reasons)
    assert rows == original


def test_finam_conflicting_deposit_values_remain_visible_and_incomplete():
    rows = [transaction("shared", amount=str(value)) for value in (100, 101, 102)]
    history = load_cash_flows(finam_broker(transactions=rows), account(), START, END)
    assert not history.complete
    assert [flow.amount for flow in history.flows] == [Decimal("100"), Decimal("101"), Decimal("102")]
    assert len({flow.id for flow in history.flows}) == 3


@pytest.mark.parametrize("category,amount,quantity,name", [
    ("DEPOSIT", "60000", "56", "Зачисление ценных бумаг"),
    ("WITHDRAW", "-60000", "-56", "Списание ценных бумаг"),
    ("WITHDRAW", "0", "-49", "Списание ценных бумаг"),
    ("WITHDRAW", "0", None, "Списание ценных бумаг"),
    ("DEPOSIT", "0", "2", "Ввод"),
])
def test_finam_security_deposit_and_withdrawal_are_not_assumed_cash_flows(category, amount, quantity, name):
    row = transaction(
        "security", category=category, amount=amount, symbol="TEST@MISX", symbols=["TEST@MISX"],
        transaction_name=name,
    )
    if quantity is not None:
        row["change_qty"] = {"value": quantity}
    history = load_cash_flows(finam_broker(transactions=[row]), account(), START, END)
    assert not history.complete
    assert history.flows[0].kind == "other"
    assert any("ценных бумаг" in reason for reason in history.reasons)


def redemption_rows():
    return [
        transaction("cash", amount="1000"),
        transaction(
            "redemption", day=1, category="INCOME", amount="24500", symbol="BOND@MISX", symbols=["BOND@MISX"],
            transaction_name="Доход от погашения ценных бумаг",
        ),
        transaction(
            "coupon", day=1, category="INCOME", amount="644.35", symbol="BOND@MISX", symbols=["BOND@MISX"],
            transaction_name="Купонный доход",
        ),
        transaction(
            "writeoff", day=1, category="WITHDRAW", amount="0", change_qty={"value": "-49"},
            symbol="BOND@MISX", symbols=["BOND@MISX"], transaction_name="Списание ценных бумаг",
        ),
    ]


def test_finam_matched_redemption_is_internal_and_preserves_real_external_deposit():
    history = load_cash_flows(finam_broker(transactions=redemption_rows()), account(), START, END)
    assert history.complete
    by_id = {flow.id: flow for flow in history.flows}
    assert by_id["writeoff"].kind == "other"
    assert by_id["writeoff"].amount == 0
    assert by_id["redemption"].kind == "income"
    assert by_id["coupon"].amount == Decimal("644.35")
    assert sum(flow.amount for flow in history.flows if flow.kind == "external") == Decimal("1000")


@pytest.mark.parametrize("change", ["wrong_symbol", "different_day", "coupon_only", "multiple_symbols", "multiple_payments", "nonzero_writeoff"])
def test_finam_redemption_requires_unambiguous_matching_payment(change):
    rows = redemption_rows()
    if change == "wrong_symbol":
        rows[1].update(symbol="OTHER@MISX", symbols=["OTHER@MISX"])
    elif change == "different_day":
        rows[1]["timestamp"] = (START + timedelta(days=2)).isoformat()
    elif change == "coupon_only":
        rows[1]["transaction_name"] = "Купонный доход"
    elif change == "multiple_symbols":
        rows[1]["symbols"].append("OTHER@MISX")
    elif change == "multiple_payments":
        rows.append({**rows[1], "id": "another-redemption"})
    else:
        rows[3]["change"] = finam_money("-24500")
    history = load_cash_flows(finam_broker(transactions=rows), account(), START, END)
    assert not history.complete
    assert next(flow for flow in history.flows if flow.id == "writeoff").kind == "other"


def test_finam_redemption_matching_uses_moscow_day_across_utc_midnight():
    rows = redemption_rows()
    rows[1]["timestamp"] = (START + timedelta(hours=22)).isoformat()
    rows[2]["timestamp"] = (START + timedelta(hours=22)).isoformat()
    rows[3]["timestamp"] = (START + timedelta(days=1, hours=1)).isoformat()
    history = load_cash_flows(finam_broker(transactions=rows), account(), START, END)
    assert history.complete


def test_finam_redemption_payment_outside_requested_window_cannot_prove_writeoff():
    rows = redemption_rows()
    rows[1]["timestamp"] = (START + timedelta(hours=22)).isoformat()
    rows[3]["timestamp"] = (START + timedelta(days=1, hours=1)).isoformat()
    history = load_cash_flows(
        finam_broker(transactions=rows), account(), START + timedelta(days=1), END, require_inception=False,
    )
    assert not history.complete


def test_tbank_cursor_history_includes_overnights_and_deduplicates_page_overlap():
    row = operation("a")
    broker = tbank_broker(pages=[
        {"items": [row, operation("b", 1, "DIVIDEND", "5")], "hasNext": True, "nextCursor": "next"},
        {"items": [deepcopy(row), operation("c", 2, "OUTPUT", "-10")], "hasNext": False},
    ])
    history = load_cash_flows(broker, account("tbank"), START, END)
    assert history.complete
    assert [flow.id for flow in history.flows] == ["a", "b", "c"]
    assert [flow.kind for flow in history.flows] == ["external", "income", "external"]
    assert broker.calls[0][1]["withoutOvernights"] is False
    assert broker.calls[1][1]["cursor"] == "next"
    assert sum(flow.amount for flow in history.flows if flow.kind == "external") == Decimal("90")


@pytest.mark.parametrize("kind", ["INPUT_SECURITIES", "OUTPUT_SECURITIES", "TRANS_BS_BS", "BUY_CARD", "SELL_CARD", "DIV_EXT", "UNKNOWN"])
def test_tbank_security_transfers_and_combined_card_operations_are_incomplete(kind):
    broker = tbank_broker(pages=[{"items": [operation("x", kind=kind)], "hasNext": False}])
    history = load_cash_flows(broker, account("tbank"), START, END)
    assert not history.complete
    assert history.reasons


def test_tbank_common_internal_operations_do_not_hide_complete_funding():
    operations = [
        operation("deposit"), operation("trade", kind="BUY", amount="-50"),
        operation("fee", kind="BROKER_FEE", amount="-1"),
        operation("tax", kind="DIVIDEND_TAX", amount="-1"),
        operation("margin", kind="ACCRUING_VARMARGIN", amount="10"),
        operation("coupon", kind="COUPON", amount="2"),
    ]
    history = load_cash_flows(tbank_broker(pages=[{"items": operations}]), account("tbank"), START, END)
    assert history.complete
    assert [flow.kind for flow in history.flows] == ["external", "other", "fee", "tax", "other", "income"]


@pytest.mark.parametrize("pages", [
    [{"items": [], "hasNext": True}],
    [{"items": [], "hasNext": True, "nextCursor": "loop"}, {"items": [], "hasNext": True, "nextCursor": "loop"}],
    [{"items": [operation("a", state="OPERATION_STATE_CANCELED")]}],
    [{"items": [operation("a", brokerAccountId="other")]}],
    [{"items": [operation("a"), operation("a", amount="99")]}],
])
def test_tbank_invalid_pagination_or_operations_cannot_claim_complete(pages):
    with pytest.raises(RuntimeError):
        load_cash_flows(tbank_broker(pages=deepcopy(pages)), account("tbank"), START, END)


def test_tbank_inclusive_upper_endpoint_is_excluded_from_requested_flow_period():
    rows = [operation("a"), operation("end", day=4)]
    history = load_cash_flows(tbank_broker(pages=[{"items": rows}]), account("tbank"), START, END)
    assert [flow.id for flow in history.flows] == ["a"]
