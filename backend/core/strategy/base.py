from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class IntentType(Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    FLATTEN = "flatten"


@dataclass(slots=True)
class TradeIntent:
    intent: IntentType
    symbol: str
    qty: Decimal
    reason: str = ""
