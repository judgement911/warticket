#!/usr/bin/env python3
"""
Drive the Dyandra checkout up to — and never through — the payment step.

The monitor tells you a sale opened. This walks the clicking: picks the tier,
sets the quantity, fills your details, and advances until the next thing it
would touch is a payment. Then it stops, screenshots, shouts at your phone,
and leaves the browser open with your session in it so you finish by hand.

    python3 checkout.py login              # log in once, by hand; session persists
    python3 checkout.py inspect [URL]      # dump real selectors off the live page
    python3 checkout.py run [URL]          # walk the checkout, halt at payment

It does not know the store's DOM in advance. Run `inspect` on the real page
first and paste the selectors it prints into config.json -> checkout.steps.
Guessed selectors on drop day are how you end up staring at a spinner.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright  (Chromium is already on this box)")

WIB = timezone(timedelta(hours=7))
HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
PROFILE_PATH = HERE / "profile.json"
USER_DATA = HERE / "browser-profile"
SHOTS = HERE / "shots"

# The pre-installed browser. PLAYWRIGHT_BROWSERS_PATH can disagree with the
# pip package's expected build, so the path is explicit and overridable.
CHROME = os.environ.get("CHROME_PATH",
                        "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
HEADLESS = os.environ.get("HEADLESS", "") == "1"

# ---------------------------------------------------------------- the guard
#
# Everything below exists because a step map is a guess about someone else's
# HTML, and a wrong guess here spends money. The rule is: this program never
# performs the act of paying. It gets you to the page where you decide.

# Clicking any of these is, or may be, an irreversible purchase.
PAYMENT_WORDS = re.compile(
    r"(bayar|pay\b|payment|pembayaran|checkout\s*now|place\s*order|"
    r"confirm\s*(and\s*)?(pay|order|purchase)|buat\s*pesanan|submit\s*order|"
    r"complete\s*(order|purchase)|selesaikan\s*pembayaran|kartu\s*kredit|"
    r"credit\s*card|virtual\s*account|transfer\s*bank|e-?wallet|gopay|ovo|"
    r"dana\b|shopeepay|qris)", re.I)

# Typing into any of these means handling card data. Never.
CARD_FIELDS = re.compile(
    r"(card[-_ ]?number|cardnumber|ccnum|cc[-_]number|cvv|cvc|csc|"
    r"security[-_ ]?code|exp(iry|iration)?[-_ ]?(date|month|year)?|"
    r"nomor[-_ ]?kartu|kode[-_ ]?keamanan|pin\b|password|sandi)", re.I)

# Only these profile keys are ever typed. A card number sitting in profile.json
# by mistake is not going to be helpfully filled in for you.
ALLOWED_PROFILE_KEYS = {
    "title", "full_name", "first_name", "last_name", "email", "phone",
    "mobile", "id_number", "id_type", "birth_date", "address", "city",
    "postcode", "country", "notes",
}

# ------------------------------------------------------------------ the queue
#
# Indonesian sales put a waiting room in front of the form, and the slot is won
# by whoever asks for it first — not by whoever has their details ready. So the
# entry click is the first thing that happens, before any typing, and the form
# is filled only once a slot is actually held.

# "You are still in line." Both languages, because the room is often localised
# by IP rather than by choice.
QUEUE_WORDS = re.compile(
    r"(waiting\s*room|you\s*are\s*in\s*(the\s*)?line|place\s*in\s*(the\s*)?"
    r"(queue|line)|queue\s*position|estimated\s*wait|queue[-\s]?it|"
    r"do\s*not\s*(refresh|close)|don'?t\s*(refresh|close)|"
    r"ruang\s*tunggu|antrian|posisi\s*(anda|antrian)|mohon\s*tunggu|"
    r"sedang\s*(memproses|diproses)|jangan\s*(me)?refresh|"
    r"harap\s*tunggu|menunggu\s*giliran)", re.I)

# What the "get me into the queue" control tends to say. Deliberately excludes
# anything PAYMENT_WORDS would catch — guard_click still has the last word.
BUY_WORDS = re.compile(
    r"(beli\s*tiket|beli|buy\s*ticket|buy\s*now|buy\b|get\s*tickets?|"
    r"book\s*now|pesan\s*tiket|pesan\s*sekarang|masuk\s*antrian|"
    r"join\s*(the\s*)?queue|enter\s*(the\s*)?queue|antri|ikut\s*antrian|"
    r"tiket\s*sekarang)", re.I)

# Field names that mean "this is the buyer form", i.e. we are through.
BUYER_HINTS = re.compile(
    r"(name|nama|e-?mail|phone|telp|telepon|hp\b|mobile|ktp|nik\b|"
    r"identit(y|as)|passport|paspor|birth|lahir|address|alamat)", re.I)

# ...except these, which wear buyer-shaped names on pages that are not the
# buyer form. A waiting room's "email me when it is my turn" box matches
# BUYER_HINTS on the word email alone, and mistaking it for the checkout is
# how the queue gets abandoned one step from the front.
NON_BUYER_FIELDS = re.compile(
    r"(notify|notifikasi|subscribe|langganan|newsletter|remind|"
    r"search|cari|query|promo|voucher|kupon|coupon|referral|kode\s*ref)", re.I)

CLICKABLES = "a, button, [role='button'], input[type='submit'], input[type='button']"


class StillQueued(Exception):
    """Asked to type buyer details while the waiting room is still up."""


def queue_visible(page) -> str | None:
    """The queue text currently on screen, if any."""
    try:
        body = page.inner_text("body", timeout=3000)
    except Exception:
        return None
    m = QUEUE_WORDS.search(body or "")
    if not m:
        return None
    # a little context around the hit, so logs show "posisi antrian: 1.234"
    start = max(0, m.start() - 30)
    return " ".join((body[start:m.end() + 60]).split())


def buyer_fields(page) -> int:
    """How many visible buyer-form inputs are on screen (card fields ignored)."""
    n = 0
    try:
        loc = page.locator("input:visible, textarea:visible, select:visible")
        for i in range(min(loc.count(), 40)):
            el = loc.nth(i)
            blob = " ".join((el.get_attribute(a) or "") for a in
                            ("name", "id", "autocomplete", "placeholder", "type"))
            if CARD_FIELDS.search(blob) or NON_BUYER_FIELDS.search(blob):
                continue
            if BUYER_HINTS.search(blob):
                n += 1
    except Exception:
        pass
    return n


def admitted(page, step: dict | None = None) -> bool:
    """
    Are we through the queue and looking at the real form?

    An explicit marker wins if the step map supplies one. Otherwise: two or
    more buyer fields is unambiguous, while a single one only counts once the
    queue text is gone — a waiting room with one "notify me" email box is
    exactly the page this must not mistake for the checkout.
    """
    step = step or {}
    sel, txt = step.get("admitted_selector"), step.get("admitted_text")
    if sel or txt:
        try:
            loc = (page.locator(sel) if sel
                   else page.get_by_text(txt, exact=False)).first
            return loc.count() > 0 and loc.is_visible()
        except Exception:
            return False
    fields = buyer_fields(page)
    if fields >= 2:
        return True
    return fields >= 1 and queue_visible(page) is None


def find_buy_control(page):
    """First visible clickable that offers to get us into the sale."""
    try:
        loc = page.locator(CLICKABLES)
        for i in range(min(loc.count(), 120)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                blob = " ".join(filter(None, (
                    el.inner_text(timeout=1000) or "",
                    el.get_attribute("value") or "",
                    el.get_attribute("aria-label") or "")))
            except Exception:
                continue
            if BUY_WORDS.search(blob):
                return el
    except Exception:
        pass
    return None


def wib() -> str:
    return datetime.now(WIB).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{wib()}] {msg}", flush=True)


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        sys.exit(f"{path.name} is not valid JSON: {e}")


def notify(text: str) -> None:
    """Reuse the monitor's Telegram credentials, if they're in the env."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chats = [c.strip() for c in
             os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        return
    try:
        import httpx
        for chat in chats:
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text,
                             "parse_mode": "HTML"}, timeout=10)
    except Exception as e:      # a failed ping must never abort a live checkout
        log(f"telegram failed (continuing): {e}")


def shot(page, label: str) -> Path | None:
    SHOTS.mkdir(exist_ok=True)
    p = SHOTS / f"{int(time.time())}-{label}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        return p
    except Exception as e:
        log(f"screenshot failed: {e}")
        return None


# ---------------------------------------------------------------- browser
def browser(pw, headless: bool | None = None):
    """
    A persistent profile, so you log in by hand once and the cookie survives.
    Your password never passes through this program.
    """
    USER_DATA.mkdir(exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA),
        executable_path=CHROME,
        headless=HEADLESS if headless is None else headless,
        viewport={"width": 420, "height": 900},   # phone-shaped: mobile queues
        locale="id-ID",
        timezone_id="Asia/Jakarta",
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )


def describe(el) -> str:
    """A human-readable handle on an element, for logs and guard messages."""
    try:
        txt = (el.inner_text() or "").strip().replace("\n", " ")[:60]
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        ident = el.get_attribute("id") or el.get_attribute("name") or ""
        return f"<{tag}{'#' + ident if ident else ''}> {txt!r}"
    except Exception:
        return "<element>"


def guard_click(el, target_hint: str = "") -> None:
    """
    Refuse to click anything that looks like it completes a purchase — even if
    the step map explicitly asked for it. The step map is a guess; this isn't.

    Judged on the element itself plus how the step targeted it. A step's `note`
    is deliberately excluded: it is your comment, not the store's HTML, and
    writing "advance to payment" should not block the click it describes.
    """
    parts = [target_hint]
    try:
        parts.append(el.inner_text() or "")
        for attr in ("value", "name", "id", "href", "aria-label", "data-testid"):
            parts.append(el.get_attribute(attr) or "")
    except Exception:
        pass
    blob = " ".join(parts)
    hit = PAYMENT_WORDS.search(blob)
    if hit:
        raise PaymentReached(f"{describe(el)} matches {hit.group(0)!r}")


class PaymentReached(Exception):
    """Not an error. The point where a human takes over."""


# ---------------------------------------------------------------- actions
def find(page, step: dict, timeout: float):
    """Locate one element by selector, or by visible text."""
    if step.get("selector"):
        loc = page.locator(step["selector"]).first
    elif step.get("text"):
        # exact-ish first, then substring — sale pages reuse words heavily
        loc = page.get_by_text(step["text"], exact=False).first
        if step.get("role"):
            loc = page.get_by_role(step["role"],
                                   name=step["text"], exact=False).first
    else:
        raise ValueError(f"step needs 'selector' or 'text': {step}")
    loc.wait_for(state="visible", timeout=timeout * 1000)
    return loc


def do_fill_profile(page, profile: dict) -> int:
    """
    Type the buyer's details into whatever the form calls those fields.
    Card-shaped fields are skipped even if something asked for them.
    """
    filled = 0
    for key, value in profile.items():
        if key.startswith("_"):
            continue
        if key not in ALLOWED_PROFILE_KEYS:
            log(f"  profile key {key!r} is not in the allowlist — skipped")
            continue
        if CARD_FIELDS.search(key):
            log(f"  refusing to type payment-shaped key {key!r}")
            continue
        hit = False
        # match on name/id/placeholder/autocomplete — stores label these badly
        for pat in (f'input[name*="{key}" i]', f'input[id*="{key}" i]',
                    f'input[autocomplete*="{key.replace("_", "-")}" i]',
                    f'input[placeholder*="{key.replace("_", " ")}" i]',
                    f'textarea[name*="{key}" i]'):
            loc = page.locator(pat).first
            try:
                if loc.count() == 0 or not loc.is_visible():
                    continue
                name_blob = " ".join(
                    (loc.get_attribute(a) or "")
                    for a in ("name", "id", "autocomplete", "placeholder"))
                if CARD_FIELDS.search(name_blob):
                    log(f"  field for {key!r} looks card-shaped — skipped")
                    break
                loc.fill(str(value), timeout=5000)
                log(f"  filled {key}")
                filled += 1
                hit = True
                break
            except Exception:
                continue
        if hit:
            continue
        # id_type and country are normally dropdowns, not text boxes.
        for pat in (f'select[name*="{key}" i]', f'select[id*="{key}" i]'):
            loc = page.locator(pat).first
            try:
                if loc.count() == 0 or not loc.is_visible():
                    continue
                try:
                    loc.select_option(label=str(value), timeout=5000)
                except Exception:
                    loc.select_option(str(value), timeout=5000)
                log(f"  selected {key}")
                filled += 1
                hit = True
                break
            except Exception:
                continue
        if not hit:
            log(f"  no field found for {key!r}")
    return filled


def run_step(page, step: dict, profile: dict, default_timeout: float) -> None:
    action = step.get("do", "click")
    # "Is this element here?" is a fast question, and an optional step that
    # isn't there should cost a moment, not the full patience budget — on drop
    # day those seconds are the whole race.
    if step.get("optional") and "timeout" not in step:
        timeout = min(default_timeout, float(
            os.environ.get("OPTIONAL_STEP_TIMEOUT", 2.5)))
    else:
        timeout = float(step.get("timeout", default_timeout))
    label = step.get("note") or step.get("text") or step.get("selector") or action
    # what the step aimed at, minus the human-written note
    target_hint = " ".join(str(step.get(k, "")) for k in ("text", "selector"))

    if action == "enter_queue":
        # The whole point: fire this the instant the page is up. No form, no
        # profile read, no waiting on a selector that may appear a beat late.
        # At t=0 the button is often absent or disabled for a second or two, so
        # this retries in a tight loop rather than failing the step.
        window = float(step.get("window", 90))
        every = float(step.get("retry_every", 0.35))
        deadline = time.time() + window
        attempts = 0
        while True:
            attempts += 1
            try:
                if step.get("selector") or step.get("text"):
                    el = find(page, step, 1.0)
                else:
                    el = find_buy_control(page)
                if el is not None:
                    guard_click(el, target_hint)
                    el.click(timeout=3000)
                    took = window - (deadline - time.time())
                    log(f"  IN — entry clicked after {took:.1f}s "
                        f"({attempts} attempt(s))")
                    return
            except PaymentReached:
                raise
            except Exception:
                pass
            if time.time() >= deadline:
                raise PWTimeout(
                    f"no queue entry control found in {window:g}s "
                    f"({attempts} attempts) — run `inspect` and give this step "
                    f"an explicit selector")
            page.wait_for_timeout(int(every * 1000))

    elif action == "wait_for_queue":
        # Queues run long, so the default patience is half an hour, not 15s.
        timeout = float(step.get("timeout", 1800))
        every = float(step.get("poll_every", 2))
        deadline = time.time() + timeout
        waited_from = time.time()
        last = ""
        while True:
            if admitted(page, step):
                mins = (time.time() - waited_from) / 60
                log(f"  THROUGH the queue after {mins:.1f} min — "
                    f"{buyer_fields(page)} buyer field(s) on screen")
                notify(f"✅ <b>THROUGH THE QUEUE</b> after {mins:.1f} min\n\n"
                       f"filling the form now — get ready to check and pay")
                return
            status = queue_visible(page)
            if status and status != last:
                last = status
                log(f"  queue: {status[:120]}")
            if time.time() >= deadline:
                raise PWTimeout(
                    f"still queued after {timeout/60:.0f} min "
                    f"(last saw {last[:80]!r})")
            # Deliberately no reload: a waiting room hands out a place on first
            # contact and a manual refresh is how you hand it back.
            page.wait_for_timeout(int(every * 1000))

    elif action == "click":
        el = find(page, step, timeout)
        guard_click(el, target_hint)
        el.click(timeout=timeout * 1000)
        log(f"  clicked {label}")

    elif action == "select":
        el = find(page, step, timeout)
        el.select_option(str(step["value"]), timeout=timeout * 1000)
        log(f"  selected {step['value']} in {label}")

    elif action == "fill":
        if CARD_FIELDS.search(step.get("selector", "") + str(step.get("value", ""))):
            raise PaymentReached(f"step would type card data into {label}")
        el = find(page, step, timeout)
        el.fill(str(step["value"]), timeout=timeout * 1000)
        log(f"  filled {label}")

    elif action == "fill_profile":
        # Typing into a waiting room accomplishes nothing at best, and at worst
        # fills its "notify me" box with your details and drops the real form.
        if not step.get("allow_in_queue"):
            waiting = queue_visible(page)
            if waiting and not admitted(page, step):
                raise StillQueued(
                    f"waiting room still up ({waiting[:70]!r}) — not typing "
                    f"into it. Put a wait_for_queue step before this one.")
        n = do_fill_profile(page, profile)
        log(f"  filled {n} profile field(s)")

    elif action == "wait_for":
        find(page, step, timeout)
        log(f"  saw {label}")

    elif action == "goto":
        page.goto(step["url"], wait_until="domcontentloaded",
                  timeout=timeout * 1000)
        log(f"  went to {step['url']}")

    elif action == "screenshot":
        shot(page, step.get("note", "step"))

    elif action == "sleep":
        time.sleep(float(step.get("seconds", 1)))

    else:
        raise ValueError(f"unknown step action {action!r}")


def payment_visible(page) -> str | None:
    """Are we already looking at a payment page? Then we're done."""
    try:
        body = page.inner_text("body")[:6000]
    except Exception:
        return None
    hit = PAYMENT_WORDS.search(body)
    return hit.group(0) if hit else None


# ---------------------------------------------------------------- commands
def cmd_login(url: str) -> None:
    if HEADLESS:
        sys.exit("login needs a visible browser — unset HEADLESS")
    log("opening the store. Log in by hand, then press Enter here.")
    with sync_playwright() as pw:
        ctx = browser(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        input("   ...logged in? press Enter to save the session: ")
        ctx.close()
    log(f"session saved to {USER_DATA}/ — keep it private, it is your login")


def cmd_inspect(url: str) -> None:
    """
    Dump every clickable and fillable thing on the page, with real selectors.

    This is the step that replaces guessing. Whatever it prints is what the
    store actually serves; paste the bits you need into config.json.
    """
    out = HERE / "selectors-dump.txt"
    with sync_playwright() as pw:
        ctx = browser(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log(f"loading {url}")
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(1500)
        rows = page.evaluate("""() => {
            const pick = e => {
                const id = e.id ? '#' + e.id : '';
                const nm = e.getAttribute('name');
                const tid = e.getAttribute('data-testid');
                const cls = (e.className && typeof e.className === 'string')
                    ? '.' + e.className.trim().split(/\\s+/).slice(0,3).join('.')
                    : '';
                let sel = e.tagName.toLowerCase();
                if (id) sel += id;
                else if (tid) sel += `[data-testid="${tid}"]`;
                else if (nm) sel += `[name="${nm}"]`;
                else sel += cls;
                return {
                    tag: e.tagName.toLowerCase(),
                    selector: sel,
                    testid: tid || '',
                    text: (e.innerText || e.value || '').trim().slice(0, 70),
                    href: e.getAttribute('href') || '',
                    type: e.getAttribute('type') || '',
                    placeholder: e.getAttribute('placeholder') || '',
                    disabled: !!e.disabled,
                    visible: !!(e.offsetWidth || e.offsetHeight),
                };
            };
            const sels = 'a,button,input,select,textarea,[role=button]';
            return [...document.querySelectorAll(sels)].map(pick);
        }""")
        title = page.title()
        body_len = len(page.content())
        ctx.close()

    vis = [r for r in rows if r["visible"]]
    lines = [f"# {url}", f"# title: {title}", f"# {wib()} WIB",
             f"# {len(rows)} interactive elements ({len(vis)} visible), "
             f"{body_len} bytes of rendered HTML", ""]
    for r in vis:
        flags = " ".join(f for f in (
            "DISABLED" if r["disabled"] else "",
            # surfaced even when an id won the selector: generated ids rotate
            # between deploys, an intentional test id usually doesn't
            f'data-testid={r["testid"]}' if r.get("testid") else "",
            f"type={r['type']}" if r["type"] else "",
            f"href={r['href'][:50]}" if r["href"] else "",
            f"placeholder={r['placeholder'][:30]}" if r["placeholder"] else "",
        ) if f)
        lines.append(f"{r['selector']:44} {r['text'][:40]!r:44} {flags}")
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:60]))
    if len(lines) > 60:
        print(f"... {len(lines) - 60} more lines")
    log(f"full dump -> {out}")
    if body_len < 2000:
        log("WARNING: almost no rendered HTML. If this page is a JS shell, the "
            "HTTP monitor in main.py cannot see it either — that target needs "
            "this browser, not httpx.")


def cmd_run(url: str) -> int:
    config = load(CONFIG_PATH, {})
    co = config.get("checkout") or {}
    steps = co.get("steps") or []
    profile = {k: v for k, v in load(PROFILE_PATH, {}).items()
               if not k.startswith("_")}
    step_timeout = float(co.get("step_timeout", 15))

    if not steps:
        sys.exit("config.json has no checkout.steps — run `inspect` first and "
                 "build the step list from the real selectors.")
    if not profile:
        log("profile.json is empty — nothing to type into the buyer form. "
            "Copy profile.example.json and fill it in.")

    log(f"checkout run on {url}")
    log(f"{len(steps)} steps, {len(profile)} profile field(s)")
    started = time.time()

    with sync_playwright() as pw:
        ctx = browser(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(step_timeout * 1000)
        halted = None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            for i, step in enumerate(steps, 1):
                log(f"step {i}/{len(steps)}: {step.get('do', 'click')}")
                # Stop before acting if we already landed on payment.
                seen = payment_visible(page)
                if seen and not step.get("allow_on_payment"):
                    halted = f"payment page reached ({seen!r}) before step {i}"
                    break
                try:
                    run_step(page, step, profile, step_timeout)
                except PaymentReached as e:
                    halted = f"step {i} blocked: {e}"
                    break
                except StillQueued as e:
                    halted = f"step {i}: {e}"
                    break
                except PWTimeout:
                    if step.get("optional"):
                        log("  not there, step is optional — moving on")
                        continue
                    what = (step.get("note") or step.get("text")
                            or step.get("selector"))
                    halted = (f"step {i} timed out after {step_timeout}s "
                              f"({what})")
                    # The commonest cause, and the one worth naming out loud:
                    # the monitor hands over whatever URL it detected, which is
                    # often already past the first steps of the map.
                    if i <= 3 and page.url.rstrip("/") != url.rstrip("/"):
                        log(f"  note: started at {url}")
                        log(f"        now at {page.url}")
                    log("  if the sale link drops you further along than this "
                        "step expects, mark the early steps \"optional\": true "
                        "so the map picks up wherever you land")
                    break
                page.wait_for_timeout(int(float(step.get("settle", 0.4)) * 1000))
            else:
                halted = "all steps done"

            png = shot(page, "handover")
            took = time.time() - started
            here = page.url
            log("")
            log("=" * 62)
            log(f"STOPPED: {halted}")
            log(f"you are at: {here}")
            log(f"took {took:.1f}s")
            log("Browser is open. Check the details, then pay by hand.")
            log("This program does not pay. It never will.")
            log("=" * 62)
            notify(
                f"🎫 <b>CHECKOUT READY — YOUR TURN</b>\n\n"
                f"{halted}\n\n"
                f"👉 {here}\n"
                f"⏰ {wib()} WIB · {took:.1f}s\n\n"
                f"<i>Browser is open on the box. Verify the total and pay "
                f"yourself — the bot stops here.</i>")
            if png:
                log(f"screenshot: {png}")

            if not HEADLESS:
                input("press Enter to close the browser: ")
        finally:
            ctx.close()
    return 0


USAGE = __doc__


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = args[0]
    config = load(CONFIG_PATH, {})
    co = config.get("checkout") or {}
    fallback = co.get("url") or ""
    if not fallback:
        for t in config.get("targets", []):
            if t.get("enabled", True):
                fallback = t.get("url", "")
                break
    url = args[1] if len(args) > 1 else fallback

    if cmd == "login":
        if not url:
            sys.exit("usage: checkout.py login <store-url>")
        cmd_login(url)
    elif cmd == "inspect":
        if not url:
            sys.exit("usage: checkout.py inspect <url>")
        cmd_inspect(url)
    elif cmd == "run":
        if not url:
            sys.exit("usage: checkout.py run <checkout-url>")
        return cmd_run(url)
    else:
        print(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
