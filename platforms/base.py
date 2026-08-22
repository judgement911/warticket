"""
The strategy interface every ticketing site plugs into.

One sale looks the same everywhere at this altitude: get through the gate,
lock inventory, identify yourself, choose how to pay. What differs is only the
HTML, so the shape lives here and each site supplies its own selectors.

Safety vocabulary is imported from checkout.py rather than restated, so there
is exactly one definition of "this click spends money" in the repo.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from checkout import (            # single source of truth for the guards
    CARD_FIELDS,
    PaymentReached,
    StillQueued,
    admitted,
    buyer_fields,
    find_buy_control,
    guard_click,
    log,
    queue_visible,
)

# Payment methods we will pick, in preference order. Cards are excluded on
# purpose: a card flow ends in 3-D Secure and an OTP, which is both a wall this
# cannot pass and a place automation has no business being.
VA_WORDS = re.compile(
    r"(virtual\s*account|\bva\b|transfer\s*bank|bank\s*transfer|"
    r"bca|mandiri|bni\b|bri\b|permata|cimb|danamon)", re.I)
QRIS_WORDS = re.compile(r"(qris|qr\s*code|scan\s*qr)", re.I)
CARD_METHOD = re.compile(
    r"(kartu\s*kredit|credit\s*card|debit\s*card|kartu\s*debit|visa|mastercard)",
    re.I)

# A VA number is a long run of digits; QRIS is a long opaque payload.
VA_NUMBER = re.compile(r"\b(\d[\d\s-]{9,24}\d)\b")


class HandoverReached(Exception):
    """Not an error. The point where the human takes over."""


@dataclass
class Buyer:
    """Pre-saved identity. Deliberately has nowhere to put a card."""
    title: str = ""
    full_name: str = ""
    mobile: str = ""
    email: str = ""
    country: str = "Indonesia"

    def missing(self) -> list[str]:
        return [f for f in ("full_name", "mobile", "email")
                if not getattr(self, f)]

    def as_fields(self) -> dict[str, str]:
        return {"title": self.title, "full_name": self.full_name,
                "phone": self.mobile, "email": self.email,
                "country": self.country}


@dataclass
class Target:
    """What to buy."""
    url: str = ""
    category_name: str = ""
    quantity: int = 1
    prefer_payment: str = "va"          # "va" | "qris"


@dataclass
class Outcome:
    """What the run produced, for the Telegram report."""
    stage: str = "not started"
    ok: bool = False
    detail: str = ""
    payment_kind: str = ""
    payment_ref: str = ""
    url: str = ""
    screenshots: list = field(default_factory=list)


class BaseTicketingHandler:
    """
    Subclass this per site. Override only what actually differs.

    The order of the lifecycle is fixed and is the whole point: the gate is
    passed before any identity is typed, and inventory is locked before the
    form, because a held seat is what makes the form worth filling.
    """

    name = "base"
    #: substrings that identify this platform from a URL
    domains: tuple[str, ...] = ()
    #: does this site lock the seat before asking who you are?
    seat_before_data = True

    # ---------------------------------------------------------------- gate
    def enter_queue(self, page, window: float = 90, retry_every: float = 0.35):
        """Click into the waiting room. First action, needs no buyer data."""
        deadline = time.time() + window
        attempts = 0
        while True:
            attempts += 1
            el = self.find_entry_control(page)
            if el is not None:
                try:
                    guard_click(el, self.name)
                    el.click(timeout=3000)
                    log(f"  entry clicked ({attempts} attempt(s))")
                    return True
                except PaymentReached:
                    raise
                except Exception:
                    pass
            if time.time() >= deadline:
                raise TimeoutError(
                    f"no entry control in {window:g}s ({attempts} attempts)")
            page.wait_for_timeout(int(retry_every * 1000))

    def find_entry_control(self, page):
        return find_buy_control(page)

    def wait_through_queue(self, page, timeout: float = 1800,
                           poll_every: float = 2) -> float:
        """
        Hold until admitted. Never reloads — a waiting room hands out a place
        on first contact and a refresh hands it back.
        """
        started = time.time()
        deadline = started + timeout
        last = ""
        while True:
            if self.is_admitted(page):
                return time.time() - started
            status = queue_visible(page)
            if status and status != last:
                last = status
                log(f"  queue: {status[:110]}")
            if time.time() >= deadline:
                raise TimeoutError(f"still queued after {timeout / 60:.0f} min")
            page.wait_for_timeout(int(poll_every * 1000))

    def is_admitted(self, page) -> bool:
        """
        Through the gate — which is not the same as "looking at a form".

        On a seat-first site admission lands you on the category list, so
        waiting for buyer fields there would wait forever. The generic test is
        therefore: the queue text is gone and the page has something to act
        on. Handlers that can recognise their own post-queue page should say
        so precisely instead.
        """
        if queue_visible(page) is not None:
            return False
        if not self.seat_before_data:
            return admitted(page)
        if buyer_fields(page) >= 1:
            return True
        try:
            from checkout import CLICKABLES
            return page.locator(f"{CLICKABLES}, input, select").count() > 0
        except Exception:
            return False

    # ------------------------------------------------------------ inventory
    def lock_seat(self, page, target: Target) -> None:
        """Pick the category, set the quantity, commit. Site-specific."""
        raise NotImplementedError

    # ------------------------------------------------------------- identity
    def fill_contact(self, page, buyer: Buyer) -> int:
        """Type the buyer in. Refuses while a waiting room is still up."""
        if queue_visible(page) and not self.is_admitted(page):
            raise StillQueued("waiting room still up — not typing into it")
        from checkout import do_fill_profile
        return do_fill_profile(page, buyer.as_fields())

    # -------------------------------------------------------------- payment
    def choose_payment(self, page, prefer: str = "va") -> str:
        """
        Select a Virtual Account or QRIS. Never a card: that road ends at an
        OTP prompt, and this does not go near one.
        """
        want = [QRIS_WORDS, VA_WORDS] if prefer == "qris" else [VA_WORDS, QRIS_WORDS]
        for pattern in want:
            el = self._find_method(page, pattern)
            if el is None:
                continue
            label = self._label_of(el)
            if CARD_METHOD.search(label):
                continue
            el.click(timeout=5000)
            kind = "QRIS" if pattern is QRIS_WORDS else "VA"
            log(f"  selected {kind}: {label[:60]}")
            return kind
        raise HandoverReached("no VA or QRIS option found — choose by hand")

    def _label_of(self, el) -> str:
        try:
            return " ".join(filter(None, (
                el.inner_text(timeout=1000) or "",
                el.get_attribute("aria-label") or "",
                el.get_attribute("value") or "")))
        except Exception:
            return ""

    def _find_method(self, page, pattern):
        from checkout import CLICKABLES
        try:
            loc = page.locator(f"{CLICKABLES}, label, [class*='payment' i]")
            for i in range(min(loc.count(), 150)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    continue
                label = self._label_of(el)
                if CARD_METHOD.search(label):
                    continue
                if pattern.search(label):
                    return el
        except Exception:
            pass
        return None

    def read_payment_reference(self, page) -> tuple[str, str]:
        """
        Pull the VA number or QRIS payload off the final page.

        Returns (kind, reference). Card-shaped strings are never returned —
        if the page somehow shows a PAN, that is not ours to relay.
        """
        try:
            body = page.inner_text("body", timeout=5000)
        except Exception:
            return ("", "")
        if QRIS_WORDS.search(body):
            for line in body.splitlines():
                if QRIS_WORDS.search(line) and len(line.strip()) > 12:
                    return ("QRIS", line.strip()[:200])
            return ("QRIS", "QR shown on screen — scan it from the browser")
        for m in VA_NUMBER.finditer(body):
            ref = re.sub(r"[\s-]", "", m.group(1))
            if CARD_FIELDS.search(ref) or len(ref) > 20:
                continue
            # a card PAN is 13-19 digits and Luhn-valid; a VA generally is not
            if 13 <= len(ref) <= 19 and _luhn(ref):
                continue
            return ("VA", ref)
        return ("", "")


def _luhn(number: str) -> bool:
    total, alt = 0, False
    for ch in reversed(number):
        if not ch.isdigit():
            return False
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0
