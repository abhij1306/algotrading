"""
Models Package
==============
Legacy models. Only `LiveOrder` is left (K6 removes it); HAT's models live in `app.hat.models`.
"""

from .live_order import LiveOrder

__all__ = ["LiveOrder"]
