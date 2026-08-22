"""
Tiket.com: seat first, identity second.

The order matters. Tiket holds inventory the moment you commit a category and
quantity, and the contact form comes after. Filling a form before the seat is
held is filling in a form for a seat somebody else just took.

SELECTORS ARE UNVERIFIED. The build environment could not reach tiket.com, so
every selector below is a *candidate list* rather than a known-good locator:
each step tries several shapes and takes the first that exists. Run
`python3 checkout.py inspect <live-url>` on the real page and pin the true
selectors into config.json -> platforms.tiketcom before you rely on this.
"""
from __future__ import annotations

import re

from checkout import log
from platforms.base import BaseTicketingHandler, HandoverReached, Target

# "commit this category" — the button that actually holds the seat
ORDER_WORDS = re.compile(r"(pesan|order|lanjut|continue|next|pilih|select)", re.I)
# quantity steppers are usually a +/- pair rather than a number input
PLUS_WORDS = re.compile(r"(^\+$|tambah|increase|plus|add)", re.I)


class TiketComHandler(BaseTicketingHandler):
    name = "tiketcom"
    domains = ("tiket.com",)
    seat_before_data = True

    # candidate selectors, tried in order
    CATEGORY_ROW = ["[data-testid*='category' i]", "[class*='category' i]",
                    "[class*='ticket-card' i]", "[class*='tier' i]", "li", "tr"]
    QTY_INPUT = ["input[type='number']", "input[name*='qty' i]",
                 "input[name*='quantity' i]", "input[name*='jumlah' i]"]

    def is_admitted(self, page) -> bool:
        """Through the queue means the tier list is on screen."""
        from checkout import queue_visible
        if queue_visible(page) is not None:
            return False
        return bool(self._visible_categories(page))

    def lock_seat(self, page, target: Target) -> None:
        if not target.category_name:
            raise HandoverReached("no category_name saved — /settarget first")
        row = self._find_category(page, target.category_name)
        if row is None:
            avail = self._visible_categories(page)
            raise HandoverReached(
                f"category {target.category_name!r} not on the page. "
                f"Seen: {', '.join(avail[:8]) or '(none)'}")
        log(f"  category matched: {target.category_name}")
        self._set_quantity(page, row, target.quantity)
        btn = self._find_order_button(page, row)
        if btn is None:
            raise HandoverReached("found the category but no order button")
        from checkout import guard_click
        guard_click(btn, "tiketcom order")
        btn.click(timeout=8000)
        log(f"  seat committed: {target.quantity} x {target.category_name}")

    # ------------------------------------------------------------- internals
    def _visible_categories(self, page) -> list[str]:
        out = []
        for sel in self.CATEGORY_ROW[:4]:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 30)):
                    t = (loc.nth(i).inner_text(timeout=500) or "").strip()
                    first = t.splitlines()[0].strip() if t else ""
                    if first and first not in out:
                        out.append(first[:40])
            except Exception:
                continue
            if out:
                break
        return out

    def _find_category(self, page, wanted: str):
        """Match the saved category name against each row's own text."""
        needle = wanted.strip().lower()
        for sel in self.CATEGORY_ROW:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 80)
            except Exception:
                continue
            for i in range(count):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    text = (el.inner_text(timeout=500) or "").lower()
                except Exception:
                    continue
                if needle in " ".join(text.split()):
                    return el
        return None

    def _set_quantity(self, page, row, qty: int) -> None:
        if qty <= 1:
            return
        for sel in self.QTY_INPUT:
            for scope in (row, page):
                try:
                    box = scope.locator(sel).first
                    if box.count() and box.is_visible():
                        box.fill(str(qty), timeout=3000)
                        log(f"  quantity set to {qty}")
                        return
                except Exception:
                    continue
        # no number box: click a + stepper (qty - 1) times
        for scope in (row, page):
            try:
                loc = scope.locator("button, [role='button']")
                for i in range(min(loc.count(), 40)):
                    el = loc.nth(i)
                    label = (el.inner_text(timeout=300) or "").strip()
                    aria = el.get_attribute("aria-label") or ""
                    if PLUS_WORDS.search(label) or PLUS_WORDS.search(aria):
                        for _ in range(qty - 1):
                            el.click(timeout=2000)
                            page.wait_for_timeout(120)
                        log(f"  quantity stepped to {qty}")
                        return
            except Exception:
                continue
        log(f"  WARNING: could not set quantity to {qty} — check before paying")

    def _find_order_button(self, page, row):
        from checkout import PAYMENT_WORDS
        for scope in (row, page):
            try:
                loc = scope.locator("button, a, [role='button']")
                for i in range(min(loc.count(), 60)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        label = (el.inner_text(timeout=400) or "").strip()
                    except Exception:
                        continue
                    if not label or PAYMENT_WORDS.search(label):
                        continue
                    if ORDER_WORDS.search(label):
                        return el
            except Exception:
                continue
        return None
