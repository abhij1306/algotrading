"""Shared setup for the broker properties: the real Fyers adapter over a recording stand-in for
the Fyers SDK model. Nothing reaches a real broker."""

from __future__ import annotations

from dataclasses import replace

from hat.fakes.fyers_sdk import RecordingSdk, broker_over

from app.hat.execution.fyers_adapter import FYERS_CAPABILITIES, FyersHatBroker

SYMBOLS = {"SBIN": "NSE:SBIN-EQ", "RELIANCE": "NSE:RELIANCE-EQ", "M&M": "NSE:M&M-EQ"}
# The adapter's capabilities with GTT for delivery (verified live).
GTT_READY = replace(FYERS_CAPABILITIES, gtt_delivery=True)


def adapter_over(sdk: RecordingSdk) -> FyersHatBroker:
    return FyersHatBroker(
        broker_over(sdk), instruments=lambda: list(SYMBOLS.items()), capabilities=GTT_READY
    )


__all__ = ["SYMBOLS", "adapter_over"]
