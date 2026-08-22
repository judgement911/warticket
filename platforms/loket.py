"""
Loket.com — skeleton.

Deliberately thin. The base lifecycle already covers gate, form and payment
generically; only lock_seat genuinely differs per site, and Loket's real
markup was never reachable from here to write it against. Filling this in
from a live `inspect` dump is a small job. Pretending it was already done
would be a much more expensive one.
"""
from __future__ import annotations

from platforms.base import BaseTicketingHandler, HandoverReached, Target


class LoketHandler(BaseTicketingHandler):
    name = "loket"
    domains = ("loket.com",)
    seat_before_data = True

    def lock_seat(self, page, target: Target) -> None:
        raise HandoverReached(
            "Loket's seat-locking step is not mapped yet. Run "
            "`python3 checkout.py inspect <loket-url>` on the live page and "
            "fill in LoketHandler.lock_seat — the rest of the flow already "
            "works generically.")
