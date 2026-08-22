#!/usr/bin/env python3
"""
War ticket bot — Telegram front end, Playwright engine, one process.

    python3 warbot.py

MUST run on the machine that has the browser. The Telegram commands and the
browser share memory here; splitting them across two hosts means /setplatform
sets a value the browser never sees.

    /workflow                 the whole procedure, start to finish
    /setplatform tiketcom     choose the site handler
    /setdata  Mr | Ade S | 08123456789 | a@b.com | Indonesia
    /settarget <url> | CAT 1 | 2
    /standby 14:00:00         idle, then burst at the gate
    /go                       run right now, no waiting
    /abort                    stop a run / cancel standby
    /status                   what is saved and what is armed

It selects a Virtual Account or QRIS and reports the number. It never touches
a card field, and it does not pay.

NOT INCLUDED: fingerprint spoofing or CAPTCHA solving. Tiket.com may well
detect this browser; if a challenge appears, solve it yourself in the window
and the run continues.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from checkout import CHROME, HEADLESS, WIB, browser, log, shot, wib
from platforms import Buyer, Outcome, Target, get_handler, guess_platform

HERE = Path(__file__).resolve().parent
STATE = HERE / "warstate.json"
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_IDS = [c.strip() for c in
            os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

WORKFLOW = """<b>How to use this, start to finish</b>

<b>Before the war</b>
1. <code>/setplatform tiketcom</code>
2. <code>/setdata Mr | Ade Saputra | 08123456789 | ade@mail.com | Indonesia</code>
3. <code>/settarget https://tiket.com/... | CAT 1 | 2</code>
4. <code>/status</code> — check all three saved correctly.
5. Log in to the site by hand in the browser window this opens, so the
   session is already authenticated. The bot never sees your password.

<b>At the gate</b>
6. <code>/standby 14:00:00</code> — the browser idles, then starts hammering
   1.5s before the time and enters the queue the moment the gate opens.
   Use <code>/go</code> instead if the sale is already live.

<b>What it does on its own</b>
7. Enters the queue, holds the slot (never reloads — that loses your place).
8. Once through: finds your category, sets the quantity, locks the seat.
9. Fills contact and visitor details from <code>/setdata</code>.
10. Picks Virtual Account or QRIS. Never a card — that path ends at an OTP.
11. Sends you the VA number or QRIS here.

<b>Your part</b>
12. Open your bank or e-wallet app and pay that VA/QRIS before it expires.
    The bot does not pay, and will not.

<b>If it stops early</b>
It tells you which stage and why, screenshots the page, and leaves the browser
open so you can finish by hand. A CAPTCHA is the usual reason — solve it in
the window and the run carries on.
"""


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    tmp.replace(STATE)


def buyer_of(s: dict) -> Buyer:
    return Buyer(**{k: v for k, v in (s.get("buyer") or {}).items()
                    if k in Buyer.__dataclass_fields__})


def target_of(s: dict) -> Target:
    return Target(**{k: v for k, v in (s.get("target") or {}).items()
                     if k in Target.__dataclass_fields__})


# ---------------------------------------------------------------- telegram
def send(text: str, to: str | None = None) -> None:
    if not BOT_TOKEN:
        print(f"[no telegram] {text}")
        return
    for chat in ([to] if to else CHAT_IDS):
        try:
            httpx.post(f"{TG}/sendMessage", timeout=15, json={
                "chat_id": chat, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True})
        except Exception as e:
            print(f"send failed: {e}")


# ---------------------------------------------------------------- the run
class Runner:
    """Owns the browser. Lives in its own thread so commands stay responsive."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.abort = threading.Event()
        self.stage = "idle"

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, state: dict, at: float | None) -> None:
        self.abort.clear()
        self.thread = threading.Thread(target=self._run, args=(state, at),
                                       daemon=True)
        self.thread.start()

    # ---- the burst
    def _burst(self, page, url: str, at: float | None, handler) -> None:
        """
        Idle until just before the gate, then navigate hard until it opens.

        Reloading early is free; reloading late is the whole ticket. The lead
        is deliberately short — arriving 1.5s early beats arriving 30s early
        and being rate-limited for it.
        """
        lead = float(os.environ.get("BURST_LEAD", 1.5))
        if at:
            while time.time() < at - lead:
                if self.abort.is_set():
                    raise RuntimeError("aborted while on standby")
                left = at - lead - time.time()
                time.sleep(min(left, 5))
            log(f"BURST at {wib()} — {lead:g}s lead")
        tries = 0
        deadline = (at or time.time()) + float(os.environ.get("BURST_FOR", 120))
        while True:
            tries += 1
            if self.abort.is_set():
                raise RuntimeError("aborted during burst")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            if handler.find_entry_control(page) is not None:
                log(f"  gate open after {tries} navigation(s)")
                return
            from checkout import queue_visible
            if queue_visible(page):
                log(f"  already in the waiting room after {tries}")
                return
            if time.time() > deadline:
                log(f"  no gate after {tries} navigations — carrying on anyway")
                return
            page.wait_for_timeout(250)

    def _run(self, state: dict, at: float | None) -> None:
        out = Outcome()
        plat = state.get("platform") or guess_platform(
            (state.get("target") or {}).get("url", "")) or "tiketcom"
        handler = get_handler(plat)
        buyer, target = buyer_of(state), target_of(state)
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                ctx = browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(15000)

                self.stage = out.stage = "standby"
                self._burst(page, target.url, at, handler)

                self.stage = out.stage = "queue"
                try:
                    handler.enter_queue(page)
                except TimeoutError as e:
                    log(f"  entry: {e} — may already be inside")
                waited = handler.wait_through_queue(page)
                send(f"✅ <b>through the queue</b> after {waited/60:.1f} min\n"
                     f"locking {target.quantity} × {target.category_name}…")

                self.stage = out.stage = "seat"
                handler.lock_seat(page, target)
                page.wait_for_timeout(1200)

                self.stage = out.stage = "form"
                n = handler.fill_contact(page, buyer)
                log(f"  filled {n} field(s)")
                page.wait_for_timeout(800)

                self.stage = out.stage = "payment"
                kind = handler.choose_payment(page, target.prefer_payment)
                page.wait_for_timeout(2500)
                got_kind, ref = handler.read_payment_reference(page)
                out.payment_kind = got_kind or kind
                out.payment_ref = ref
                out.url = page.url
                out.ok = bool(ref)
                out.stage = "done"

                png = shot(page, "handover")
                if ref:
                    send(f"💳 <b>{out.payment_kind} READY</b>\n\n"
                         f"<code>{html.escape(ref)}</code>\n\n"
                         f"pay this in your bank app before it expires.\n"
                         f"{out.url}")
                else:
                    send(f"⚠️ <b>{out.payment_kind or 'payment'} selected</b> but "
                         f"the number did not parse.\nRead it off the screen: "
                         f"{out.url}")
                log(f"screenshot: {png}")
                # hold the browser open so the human can finish
                while not self.abort.is_set():
                    time.sleep(2)

        except Exception as e:
            out.detail = f"{type(e).__name__}: {e}"
            log(f"STOPPED at {self.stage}: {out.detail}")
            send(f"🛑 <b>stopped at {html.escape(self.stage)}</b>\n\n"
                 f"<code>{html.escape(str(e))[:400]}</code>\n\n"
                 f"browser is open — finish by hand")
        finally:
            self.stage = "idle"


RUNNER = Runner()


# ---------------------------------------------------------------- commands
def parse_hms(text: str) -> float | None:
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text.strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    # "25:99:99" matches the shape and then explodes inside replace(); a typed
    # time is exactly the input that gets fat-fingered under pressure, and it
    # must come back as a message, not a stack trace.
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        return None
    now = datetime.now(WIB)
    try:
        when = now.replace(hour=h, minute=mi, second=s, microsecond=0)
    except ValueError:
        return None
    if when <= now:
        when += timedelta(days=1)      # a time already past means tomorrow
    return when.timestamp()


def handle(cmd: str, args: list[str], chat: str, state: dict) -> None:
    raw = " ".join(args)

    if cmd == "/workflow":
        send(WORKFLOW, to=chat)

    elif cmd == "/setplatform":
        try:
            h = get_handler(raw)
        except KeyError as e:
            send(f"❌ {html.escape(str(e))}", to=chat)
            return
        state["platform"] = h.name
        save_state(state)
        send(f"✅ platform: <b>{h.name}</b>", to=chat)

    elif cmd == "/setdata":
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4:
            send("format:\n<code>/setdata Mr | Full Name | 08123 | "
                 "a@b.com | Indonesia</code>", to=chat)
            return
        b = Buyer(title=parts[0], full_name=parts[1], mobile=parts[2],
                  email=parts[3], country=parts[4] if len(parts) > 4 else "Indonesia")
        state["buyer"] = asdict(b)
        save_state(state)
        send(f"✅ saved: {html.escape(b.title)} {html.escape(b.full_name)} · "
             f"{html.escape(b.email)}\n<i>no card is stored, ever</i>", to=chat)

    elif cmd == "/settarget":
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2:
            send("format:\n<code>/settarget https://... | CAT 1 | 2</code>",
                 to=chat)
            return
        url = parts[0] if parts[0].startswith("http") else \
            (state.get("target") or {}).get("url", "")
        rest = parts[1:] if parts[0].startswith("http") else parts
        qty = 1
        if rest and rest[-1].isdigit():
            qty = max(1, min(int(rest[-1]), 10))
            rest = rest[:-1]
        t = Target(url=url, category_name=rest[0] if rest else "", quantity=qty,
                   prefer_payment=(state.get("target") or {}).get(
                       "prefer_payment", "va"))
        state["target"] = asdict(t)
        if not state.get("platform"):
            g = guess_platform(url)
            if g:
                state["platform"] = g
        save_state(state)
        send(f"🎯 <b>{html.escape(t.category_name)}</b> × {t.quantity}\n"
             f"{html.escape(t.url or '(no url)')}\n"
             f"platform: {state.get('platform', '?')}", to=chat)

    elif cmd == "/standby":
        at = parse_hms(raw)
        if at is None:
            send("format: <code>/standby 14:00:00</code>", to=chat)
            return
        t = target_of(state)
        if not t.url:
            send("❌ no target url — /settarget first", to=chat)
            return
        if RUNNER.busy():
            send("already armed or running — /abort first", to=chat)
            return
        state["standby_at"] = at
        save_state(state)
        mins = (at - time.time()) / 60
        RUNNER.start(state, at)
        send(f"⏱️ <b>armed for {datetime.fromtimestamp(at, WIB):%H:%M:%S} WIB</b>"
             f" ({mins:.0f} min)\nbrowser idling; burst starts 1.5s before.",
             to=chat)

    elif cmd == "/go":
        if RUNNER.busy():
            send("already running — /abort first", to=chat)
            return
        if not target_of(state).url:
            send("❌ no target url — /settarget first", to=chat)
            return
        RUNNER.start(state, None)
        send("🚀 running now", to=chat)

    elif cmd == "/abort":
        RUNNER.abort.set()
        state.pop("standby_at", None)
        save_state(state)
        send("🛑 aborted", to=chat)

    elif cmd == "/status":
        b, t = buyer_of(state), target_of(state)
        miss = b.missing()
        at = state.get("standby_at")
        lines = [
            f"🤖 <b>{wib()} WIB</b>",
            f"platform: <b>{state.get('platform') or '(unset)'}</b>",
            f"buyer: {html.escape(b.full_name or '(unset)')}"
            + (f" ⚠️ missing {', '.join(miss)}" if miss else " ✅"),
            f"target: {html.escape(t.category_name or '(unset)')} × {t.quantity}",
            f"url: {html.escape(t.url or '(unset)')}",
            f"pay: {t.prefer_payment.upper()}",
            f"stage: <b>{RUNNER.stage}</b>",
        ]
        if at and at > time.time():
            lines.append(f"armed: {datetime.fromtimestamp(at, WIB):%H:%M:%S} WIB "
                         f"({(at-time.time())/60:.0f} min)")
        send("\n".join(lines), to=chat)

    else:
        send("unknown. try /workflow", to=chat)


def main() -> int:
    if not BOT_TOKEN:
        print("set TELEGRAM_TOKEN (and TELEGRAM_CHAT_ID)")
        return 1
    state = load_state()
    log(f"warbot up · platform={state.get('platform') or 'unset'} · "
        f"headless={HEADLESS} · chrome={CHROME}")
    send(f"🤖 warbot online · {wib()} WIB\n/workflow for the procedure")
    offset = state.get("tg_offset", 0)
    while True:
        try:
            r = httpx.get(f"{TG}/getUpdates", timeout=35,
                          params={"offset": offset, "timeout": 25})
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                state["tg_offset"] = offset
                m = upd.get("message") or {}
                text = (m.get("text") or "").strip()
                chat = str((m.get("chat") or {}).get("id", ""))
                if not text.startswith("/") or not chat:
                    continue
                cmd, *args = text.split()
                handle(cmd.split("@")[0].lower(), args, chat, state)
                save_state(state)
        except KeyboardInterrupt:
            RUNNER.abort.set()
            return 0
        except Exception as e:
            log(f"poll: {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
