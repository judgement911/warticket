"""Registry. Add a handler here and /setplatform can reach it."""
from __future__ import annotations

from platforms.base import (BaseTicketingHandler, Buyer, HandoverReached,
                            Outcome, Target)
from platforms.loket import LoketHandler
from platforms.tiketcom import TiketComHandler

HANDLERS: dict[str, type[BaseTicketingHandler]] = {
    h.name: h for h in (TiketComHandler, LoketHandler)
}


def get_handler(name: str) -> BaseTicketingHandler:
    key = (name or "").strip().lower().replace(".", "").replace("-", "")
    if key not in HANDLERS:
        raise KeyError(f"unknown platform {name!r} — have: "
                       f"{', '.join(sorted(HANDLERS))}")
    return HANDLERS[key]()


def guess_platform(url: str) -> str | None:
    """Pick the handler whose domain appears in the URL."""
    low = (url or "").lower()
    for name, cls in HANDLERS.items():
        if any(d in low for d in cls.domains):
            return name
    return None


__all__ = ["HANDLERS", "get_handler", "guess_platform", "BaseTicketingHandler",
           "Buyer", "Target", "Outcome", "HandoverReached"]
