"""
Conservative fill model for backtesting microstructure strategies.

Assumptions:
- Taker fills at best bid/ask + configurable slippage
- Binance USDM futures taker fee: 0.04% (4 bps) per side
- Slippage default: 1.5 bps (conservative for BTC/ETH at normal spread)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Binance USDM futures taker fee
TAKER_FEE_BPS = Decimal("4.0")
DEFAULT_SLIPPAGE_BPS = Decimal("1.5")


@dataclass(slots=True)
class Fill:
    price: Decimal
    slippage_bps: Decimal
    fee_bps: Decimal


class FillModel:
    def __init__(
        self,
        slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS,
        fee_bps: Decimal = TAKER_FEE_BPS,
    ):
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps

    def fill_entry_long(self, ask: Decimal, mid: Decimal) -> Fill:
        """Buy entry: taker lifts the ask + slippage."""
        return Fill(
            price=ask + mid * self.slippage_bps / Decimal("10000"),
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
        )

    def fill_entry_short(self, bid: Decimal, mid: Decimal) -> Fill:
        """Sell entry: taker hits the bid - slippage."""
        return Fill(
            price=bid - mid * self.slippage_bps / Decimal("10000"),
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
        )

    def fill_exit_long(self, bid: Decimal, mid: Decimal) -> Fill:
        """Exit long: sell at bid - slippage."""
        return Fill(
            price=bid - mid * self.slippage_bps / Decimal("10000"),
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
        )

    def fill_exit_short(self, ask: Decimal, mid: Decimal) -> Fill:
        """Exit short: buy at ask + slippage."""
        return Fill(
            price=ask + mid * self.slippage_bps / Decimal("10000"),
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
        )
