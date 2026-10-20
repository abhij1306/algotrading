"""One typed order: what a caller asks to place, validated once.

`OrderIntent.from_params` is the only place an order request becomes an intent. The
execution service and the risk manager read the intent's typed fields instead of
sniffing raw dicts.

Choice fields are enums. They accept any letter case ("buy" is BUY) and reject anything
else with a fixed, user-facing message. Prices are `Decimal` or None. The cross-field
price rules are `order_price_error`: a LIMIT or SL needs a positive limit price and an SL or
SL-M a positive trigger. A MARKET or SL-M order carries no limit price and a MARKET or
LIMIT order no trigger, whatever the request sent.

This module imports no broker code.
"""

import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Self, TypeVar

from pydantic import BaseModel, ValidationError, field_validator, model_validator

INVALID_QUANTITY_MESSAGE = "Quantity must be a positive integer"
INVALID_CLIENT_ORDER_ID_MESSAGE = "client_order_id must be a UUID v4"
INVALID_MODE_MESSAGE = "Invalid mode. Use PAPER or LIVE"
INVALID_SIDE_MESSAGE = "Invalid order side. Expected BUY or SELL."
INVALID_ORDER_TYPE_MESSAGE = "Invalid order type. Expected MARKET, LIMIT, SL or SL-M."
INVALID_PRODUCT_MESSAGE = "Invalid product. Expected INTRADAY, CNC, MARGIN, MIS, CO, BO or MTF."
INVALID_INSTRUMENT_TYPE_MESSAGE = "Invalid instrument type. Expected EQ, FUT, CE or PE."


class Choice(StrEnum):
    """A string enum that accepts its values in any letter case, padded or not."""

    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if isinstance(value, str):
            key = value.strip().upper()
            for member in cls:
                if member.value == key:
                    return member
        return None


class Mode(Choice):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(Choice):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Choice):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class Product(Choice):
    """Fyers product types, plus MIS, which the broker plugin sends as INTRADAY."""

    INTRADAY = "INTRADAY"
    CNC = "CNC"
    MARGIN = "MARGIN"
    MIS = "MIS"
    CO = "CO"
    BO = "BO"
    MTF = "MTF"


class InstrumentType(Choice):
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"


STOP_ORDER_TYPES = frozenset({OrderType.SL, OrderType.SL_M})
LIMIT_PRICED_ORDER_TYPES = frozenset({OrderType.LIMIT, OrderType.SL})

_CHOICE_MESSAGES: dict[type[Choice], str] = {
    Mode: INVALID_MODE_MESSAGE,
    Side: INVALID_SIDE_MESSAGE,
    OrderType: INVALID_ORDER_TYPE_MESSAGE,
    Product: INVALID_PRODUCT_MESSAGE,
    InstrumentType: INVALID_INSTRUMENT_TYPE_MESSAGE,
}

C = TypeVar("C", bound=Choice)


def parse_choice(choice: type[C], value: object) -> C:
    """`value` as a member of `choice`, in any letter case; ValueError (user-facing) else."""
    try:
        return choice(value)
    except ValueError:
        raise ValueError(_CHOICE_MESSAGES[choice]) from None


class InvalidQuantityError(ValueError):
    """An order quantity that is not a positive whole number. The message is user-facing."""

    def __init__(self) -> None:
        super().__init__(INVALID_QUANTITY_MESSAGE)


def coerce_quantity(value: object) -> int:
    """An order quantity as a positive int.

    Accepts an int, an integral float or Decimal (5.0) and a string of digits ("5").
    Rejects booleans, fractions (2.5, "2.5"), anything else non-numeric, and values <= 0,
    raising InvalidQuantityError. Never truncates: 2.9 is rejected, not read as 2.
    """
    if isinstance(value, bool):
        raise InvalidQuantityError()
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise InvalidQuantityError()
        quantity = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise InvalidQuantityError()
        quantity = int(value)
    elif isinstance(value, str):
        try:
            quantity = int(value.strip())
        except ValueError:
            raise InvalidQuantityError() from None
    else:
        raise InvalidQuantityError()
    if quantity <= 0:
        raise InvalidQuantityError()
    return quantity


def normalize_client_order_id(value: object) -> str | None:
    """Canonical lowercase UUID v4 string for a client_order_id, or None when absent.

    Raises ValueError when the value is present but not a UUID v4.
    """
    if value is None or value == "":
        return None
    try:
        parsed = uuid.UUID(str(value))
    except ValueError as exc:
        raise ValueError(INVALID_CLIENT_ORDER_ID_MESSAGE) from exc
    if parsed.version != 4:
        raise ValueError(INVALID_CLIENT_ORDER_ID_MESSAGE)
    return str(parsed)


def optional_decimal(value: object) -> Decimal | None:
    """`value` as a finite Decimal, or None when it is missing or not a number.

    A float goes through str(), so 0.1 is exactly 0.1.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    return result if result.is_finite() else None


def order_price_error(order_type: str, price: object, trigger_price: object) -> str | None:
    """Message when an order's prices are invalid for its type, else None.

    Same rules and wording as FyersBroker.place_order / modify_order. A missing or
    non-numeric price counts as 0.
    """
    trigger = optional_decimal(trigger_price) or Decimal(0)
    limit = optional_decimal(price) or Decimal(0)
    if order_type in STOP_ORDER_TYPES and trigger <= 0:
        return f"{order_type} order requires a positive trigger price."
    if order_type in LIMIT_PRICED_ORDER_TYPES and limit <= 0:
        return f"{order_type} order requires a positive limit price."
    return None


class OrderIntent(BaseModel):
    """A validated order request. Field order is validation order: the first invalid
    field's message is the one reported."""

    mode: Mode = Mode.PAPER
    client_order_id: str | None = None
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    product: Product = Product.INTRADAY
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    instrument_type: InstrumentType | None = InstrumentType.EQ
    fyers_symbol: str | None = None
    strike_price: float | None = None
    expiry_date: str | None = None
    option_type: str | None = None
    user_id: str = "default"
    tag: str | None = None
    source: str = "MANUAL"
    is_live_confirmation_ack: bool = False
    risk_override_reason: str | None = None
    # PAPER simulator overrides; None uses the simulator defaults.
    paper_slippage_bps: Decimal | None = None
    paper_fee_bps: Decimal | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: object) -> Mode:
        return parse_choice(Mode, value)

    @field_validator("client_order_id", mode="before")
    @classmethod
    def _client_order_id(cls, value: object) -> str | None:
        return normalize_client_order_id(value)

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, value: object) -> Side:
        return parse_choice(Side, value)

    @field_validator("quantity", mode="before")
    @classmethod
    def _quantity(cls, value: object) -> int:
        return coerce_quantity(value)

    @field_validator("order_type", mode="before")
    @classmethod
    def _order_type(cls, value: object) -> OrderType:
        return parse_choice(OrderType, value)

    @field_validator("product", mode="before")
    @classmethod
    def _product(cls, value: object) -> Product:
        return parse_choice(Product, value)

    @field_validator("price", "trigger_price", "paper_slippage_bps", "paper_fee_bps", mode="before")
    @classmethod
    def _decimal(cls, value: object) -> Decimal | None:
        return optional_decimal(value)

    @field_validator("instrument_type", mode="before")
    @classmethod
    def _instrument_type(cls, value: object) -> InstrumentType | None:
        return None if value is None else parse_choice(InstrumentType, value)

    @field_validator("is_live_confirmation_ack", mode="before")
    @classmethod
    def _ack(cls, value: object) -> bool:
        return bool(value)

    @model_validator(mode="after")
    def _drop_unused_prices(self) -> Self:
        # A MARKET or SL-M order has no limit price, and only stop orders have a trigger:
        # a stray value must not price the order for risk or the paper simulator.
        if self.order_type not in LIMIT_PRICED_ORDER_TYPES:
            self.price = None
        if self.order_type not in STOP_ORDER_TYPES:
            self.trigger_price = None
        return self

    @property
    def price_error(self) -> str | None:
        """Why this order's prices are invalid for its type, or None. A PAPER order with
        one is stored REJECTED; a LIVE one is rejected by the broker plugin."""
        return order_price_error(self.order_type, self.price, self.trigger_price)

    @classmethod
    def from_params(cls, params: Mapping[str, Any], mode: object = None) -> Self:
        """The one constructor from a request: a request model's dump or a service dict.

        `mode` overrides params["mode"]; both missing means PAPER. The order type is read
        from "type" (the request field) or "order_type". Raises ValueError with a
        user-facing message for the first invalid field; a bad quantity raises
        InvalidQuantityError.
        """
        data = {key: value for key, value in params.items() if key in cls.model_fields}
        data["mode"] = mode or params.get("mode") or Mode.PAPER
        if "type" in params:
            data["order_type"] = params["type"]
        for key in ("order_type", "product"):
            # An explicit None means the default, as a missing key does.
            if data.get(key, ...) is None:
                del data[key]
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise _first_error(exc) from None


def _first_error(exc: ValidationError) -> ValueError:
    """The ValueError a field validator raised, else a generic one naming the field."""
    error = exc.errors()[0]
    cause = (error.get("ctx") or {}).get("error")
    if isinstance(cause, ValueError):
        return cause
    field = ".".join(str(part) for part in error["loc"]) or "order"
    return ValueError(f"Invalid {field}")


def as_intent(order: "OrderIntent | Mapping[str, Any]", mode: object = None) -> OrderIntent:
    """`order` if it is already an intent, else one built by OrderIntent.from_params."""
    if isinstance(order, OrderIntent):
        return order
    return OrderIntent.from_params(order, mode)
