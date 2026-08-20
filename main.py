#!/usr/bin/env python3
"""
WAR TIKET BOT  —  multi-site drop monitor with Telegram alerts.
© built for Ade

It WATCHES, SHOUTS, and PUTS THE QUEUE LINK IN YOUR HAND. It does not buy.
You buy — deliberately, no automated purchasing, no CAPTCHA/queue bypass.

Env vars required:
    TELEGRAM_TOKEN    from @BotFather
    TELEGRAM_CHAT_ID  from @userinfobot
Optional:
    DRY_RUN=1         print alerts instead of sending (for testing)
    LOCAL_NOTIFY=1    desktop notification on the box running the bot
    AUTO_OPEN=1       open the queue page in a local browser the instant it goes
                      live (you still click Buy) — only useful if the bot runs
                      on the machine you are buying from

Run:  python main.py
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx

# ---------------------------------------------------------------- setup

WIB = timezone(timedelta(hours=7))
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
# comma-separated: DM, group, channel — alerts go to all of them
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
LOCAL_NOTIFY = os.environ.get("LOCAL_NOTIFY", "") == "1"
AUTO_OPEN = os.environ.get("AUTO_OPEN", "") == "1"
# DRY_RUN keeps the browser parked, so a rehearsal can never go clicking a real
# store. Set this to exercise the whole chain end to end against a fixture.
REHEARSE_CHECKOUT = os.environ.get("REHEARSE_CHECKOUT", "") == "1"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

HERE = Path(__file__).parent
CHECKOUT_PATH = HERE / "checkout.py"
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# What a real "sale is live" destination looks like. The sale site ships these
# buttons as href="#" until a window opens, then swaps in the true queue URL.
LIVE_LINK_DEFAULT = (r"(tiket\.com|loket\.com|queue-it\.net|ticketmaster|"
                     r"/checkout|/queue|/order|/booking)")

# Bundlers name JS chunks after routes, so a Next.js store ships
# /_next/static/chunks/pages/checkout-9f2a.js — which matches "/checkout" and
# gets a fresh hash on every redeploy. Excluded from link detection, or a
# routine deploy reads as the sale opening.
ASSET_NOISE_DEFAULT = (
    r"(?:/_next/|/_nuxt/|/static/|/assets/|/dist/|"
    r"\.(?:js|mjs|css|map|woff2?|ttf|eot|png|jpe?g|gif|svg|webp|ico|avif|mp4)"
    r"(?:$|[?#]))"
)

# hrefs that mean "button not wired up yet"
DEAD_HREFS = {"", "#", "/", "javascript:void(0)", "javascript:void(0);", "javascript:;"}

# Per-request junk that changes on every single poll. Stripped before hashing,
# because a "page changed" alert that is really just a rotated CSRF token trains
# you to ignore the alert that matters. Applies to `changed` rules only — link
# detection never strips anything.
VOLATILE_DEFAULT = "|".join([
    r"<meta[^>]*(?:csrf|nonce|token|build)[^>]*>",       # whole framework meta tag
    r"nonce=[\"'][^\"']*[\"']",
    r"(?:csrf|_token|authenticity_token|buildId)[\"']?\s*[:=]\s*[\"'][^\"']*[\"']",
    r"[?&](?:v|t|ts|_|cb|cache)=[0-9a-fA-F]{4,}",        # cache busters
    r"\b\d{13}\b",                                      # ms epoch
    r"\b\d{2}:\d{2}:\d{2}\b",                           # clocks
    r"\d{4}-\d{2}-\d{2}T[\d:.+Z-]+",                     # ISO timestamps
    # Bundler fingerprints: checkout-9f2a1b.js changes on every deploy while
    # the page says exactly the same thing. Only the hash is dropped, so a
    # meaningful rename (banner-soon.png -> banner-live.png) still registers.
    r"[-._][0-9a-f]{6,32}(?=\.(?:js|mjs|css|map|woff2?|ttf|png|jpe?g|gif|svg"
    r"|webp|ico|avif))",
    r"/_next/static/[A-Za-z0-9_-]{8,}/",                # Next.js build id
])


def now() -> float:
    return time.time()


def wib(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now(), WIB).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{wib()}] {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"state save failed: {e}")


# ---------------------------------------------------------------- links

def extract_links(body: str, pattern: str, base: str = "",
                  exclude: str | None = None) -> list[str]:
    """
    Pull every plausible checkout/queue destination out of a page.

    Two passes, because sale sites hide the real URL in both places: normal
    <a href> attributes, and bare URLs sitting in inline JS config blobs.

    `exclude` drops static assets whose filenames happen to contain route
    words — checked against both the raw href and the resolved URL.
    """
    candidates = [m.group(1).strip() for m in
                  re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', body, re.I)]
    candidates += re.findall(r'https?://[^\s"\'<>\\)]+', body)

    found: list[str] = []
    seen: set[str] = set()
    for href in candidates:
        if href.lower() in DEAD_HREFS:
            continue
        if exclude and re.search(exclude, href, re.I):
            continue
        if not re.search(pattern, href, re.I):
            continue
        full = (urljoin(base, href) if base else href).rstrip("\\")
        if exclude and re.search(exclude, full, re.I):
            continue
        if full in seen:
            continue
        seen.add(full)
        found.append(full)
    return found


# ---------------------------------------------------------------- local alerts

def _bell(times: int = 3) -> None:
    try:
        sys.stdout.write("\a" * times)
        sys.stdout.flush()
    except Exception:
        pass


def _desktop(title: str, body: str) -> None:
    """Best-effort desktop notification, whatever box this is running on."""
    safe = body.replace("\n", " ")[:180]
    try:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, safe],
                           timeout=5, check=False)
        elif shutil.which("osascript"):
            script = (f'display notification "{safe.replace(chr(34), chr(39))}" '
                      f'with title "{title}" sound name "Sosumi"')
            subprocess.run(["osascript", "-e", script], timeout=5, check=False)
        elif shutil.which("powershell.exe"):
            subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "[console]::beep(1000,700)"], timeout=5, check=False)
    except Exception as e:
        log(f"desktop notify failed: {e}")


def _browser(url: str) -> None:
    """Land a human on the queue page. The human does the buying."""
    try:
        opener = shutil.which("xdg-open") or shutil.which("open")
        if opener:
            subprocess.Popen([opener, url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open_new_tab(url)
    except Exception as e:
        log(f"browser open failed: {e}")


async def local_alert(title: str, body: str, url: str | None) -> None:
    """Noise on the local machine — survives a dead phone or muted Telegram."""
    await asyncio.to_thread(_bell)
    if LOCAL_NOTIFY:
        await asyncio.to_thread(_desktop, title, body)
    if AUTO_OPEN and url:
        await asyncio.to_thread(_browser, url)
        log(f"opened {url} locally — click Buy yourself")


# ---------------------------------------------------------------- telegram

async def send(client: httpx.AsyncClient, text: str, silent: bool = False, to=None) -> None:
    """
    Fire a Telegram message.
    to=None  -> broadcast to every chat in TELEGRAM_CHAT_ID (alerts)
    to=<id>  -> reply only to that chat (command responses)
    """
    if DRY_RUN or not BOT_TOKEN:
        print(f"\n=== ALERT (to={to or CHAT_IDS}) ===\n{text}\n=============\n", flush=True)
        return
    targets = [to] if to is not None else CHAT_IDS
    for chat in targets:
        for attempt in range(3):
            try:
                r = await client.post(
                    f"{TG}/sendMessage",
                    json={
                        "chat_id": chat,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "disable_notification": silent,
                    },
                    timeout=10,
                )
                if r.status_code == 200:
                    break
                log(f"telegram {r.status_code} for chat {chat}: {r.text[:120]}")
            except Exception as e:
                log(f"telegram error for chat {chat}: {e}")
            await asyncio.sleep(1 + attempt)


# ---------------------------------------------------------------- rule engine

def launch_checkout(url: str, name: str) -> int | None:
    """
    Hand the detected link straight to the browser driver.

    Fire-and-forget on purpose: the poll loop must not block on a browser, and
    a checkout that dies is not allowed to take the monitor down with it. The
    driver stops before payment on its own — see checkout.py.
    """
    if DRY_RUN and not REHEARSE_CHECKOUT:
        log(f"{name}: would launch checkout on {url} "
            f"(REHEARSE_CHECKOUT=1 to really run it)")
        return None
    if not CHECKOUT_PATH.exists():
        log(f"{name}: checkout.py missing, not launching")
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(CHECKOUT_PATH), "run", url],
            cwd=str(HERE),
            stdout=open(HERE / "checkout.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log(f"{name}: checkout launched (pid {proc.pid}) -> checkout.log")
        return proc.pid
    except Exception as e:
        log(f"{name}: could not launch checkout: {e}")
        return None


def dig(obj, path: str):
    """Walk a dotted path through dicts/lists: 'data.items.0.status'."""
    cur = obj
    for part in path.split("."):
        if part == "":
            continue
        try:
            cur = cur[int(part)] if part.lstrip("-").isdigit() else cur[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return cur


def evaluate(rule: dict, body: str, status: int, prev: dict) -> tuple[bool, str, dict]:
    """
    Decide whether this response should fire an alert.
    Returns (triggered, human_detail, marker_to_remember).
    """
    kind = rule.get("type", "changed")
    marker = dict(prev)

    if kind == "appears":
        needle = rule["value"]
        hit = needle.lower() in body.lower()
        marker["seen"] = hit
        # fire on the transition absent -> present (or on first sight if configured)
        was = prev.get("seen")
        if hit and (was is False or (was is None and rule.get("fire_on_first", True))):
            return True, f"found “{needle}”", marker
        return False, "", marker

    if kind == "disappears":
        needle = rule["value"]
        hit = needle.lower() in body.lower()
        marker["seen"] = hit
        if not hit and prev.get("seen") is True:
            return True, f"“{needle}” is gone", marker
        return False, "", marker

    if kind == "regex":
        m = re.search(rule["pattern"], body, re.I | re.S)
        marker["seen"] = bool(m)
        if m and prev.get("seen") is not True:
            snippet = (m.group(0)[:80]).replace("\n", " ")
            return True, f"regex matched: {snippet}", marker
        return False, "", marker

    if kind == "status_ok":
        ok = 200 <= status < 300
        marker["status"] = status
        if ok and prev.get("status") not in (None, *range(200, 300)):
            return True, f"page went live (HTTP {status})", marker
        return False, "", marker

    if kind in ("json", "json_changed"):
        try:
            data = json.loads(body)
        except Exception:
            # some sites bury JSON in the HTML (Next.js). try that.
            m = re.search(r'__NEXT_DATA__[^>]*>(\{.*?\})</script>', body, re.S)
            if not m:
                return False, "", marker
            try:
                data = json.loads(m.group(1))
            except Exception:
                return False, "", marker
        val = dig(data, rule.get("path", ""))
        sval = json.dumps(val, sort_keys=True)[:200]
        old = prev.get("value")
        marker["value"] = sval
        if kind == "json":
            want = rule.get("equals")
            if want is not None and str(val).lower() == str(want).lower() and old != sval:
                return True, f"{rule['path']} = {val}", marker
            gt = rule.get("greater_than")
            if gt is not None:
                try:
                    if float(val) > float(gt) and old != sval:
                        return True, f"{rule['path']} = {val} (> {gt})", marker
                except (TypeError, ValueError):
                    pass
            return False, "", marker
        # json_changed
        if old is not None and old != sval:
            return True, f"{rule['path']}: {old[:60]} → {sval[:60]}", marker
        return False, "", marker

    if kind == "new_link":
        # The one that matters: a dead href="#" button turning into a real
        # queue URL. Fires on links that were NOT there last poll.
        pattern = rule.get("pattern", LIVE_LINK_DEFAULT)
        # set exclude_pattern to "" to keep asset URLs
        exclude = rule.get("exclude_pattern", ASSET_NOISE_DEFAULT) or None
        found = extract_links(body, pattern, rule.get("_base", ""), exclude)
        old = set(prev.get("links") or [])
        fresh = [u for u in found if u not in old]

        # remember the union: a link that flickers away shouldn't re-alert
        marker["links"] = sorted(old | set(found))[:200]
        # first poll is a baseline — a tiket.com link already in the footer
        # today is not a drop. Needs its own flag: "no links yet" is a
        # perfectly normal baseline and must not look like "never checked".
        baselined = prev.get("link_baseline") is True
        marker["link_baseline"] = True
        marker.pop("hot_link", None)

        if fresh and (baselined or rule.get("fire_on_first", False)):
            marker["hot_link"] = fresh[0]
            listed = "\n".join(f"• {u}" for u in fresh[:5])
            more = f"\n(+{len(fresh) - 5} more)" if len(fresh) > 5 else ""
            plural = "S" if len(fresh) > 1 else ""
            return True, f"LIVE LINK{plural}:\n{listed}{more}", marker
        return False, "", marker

    # default: raw content hash
    cleaned = body
    if rule.get("ignore_volatile", True):
        cleaned = re.sub(VOLATILE_DEFAULT, "", cleaned, flags=re.I)
    strip = rule.get("ignore_pattern")
    if strip:
        cleaned = re.sub(strip, "", cleaned, flags=re.I)
    h = hashlib.sha256(cleaned.encode("utf-8", "ignore")).hexdigest()
    old = prev.get("hash")
    marker["hash"] = h
    if old and old != h:
        return True, "page content changed", marker
    return False, "", marker


# ---------------------------------------------------------------- polling

def drop_ts(target: dict) -> float | None:
    """Configured drop time as a unix timestamp; naive times are WIB."""
    drop = target.get("drop_time")
    if not drop:
        return None
    try:
        t = datetime.fromisoformat(drop)
        if t.tzinfo is None:
            t = t.replace(tzinfo=WIB)
        return t.timestamp()
    except Exception:
        return None


def interval_for(target: dict, state: dict) -> float:
    """Normal cadence, or hot cadence near a known drop time."""
    normal = float(target.get("interval", 8))
    hot = float(target.get("hot_interval", 1.5))

    if state.get("hot_until", 0) > now():
        return hot

    ts = drop_ts(target)
    if ts is not None:
        secs_away = ts - now()
        window = float(target.get("hot_window_min", 10)) * 60
        if -300 < secs_away < window:
            return hot
    return normal


async def check(client: httpx.AsyncClient, target: dict, state: dict) -> None:
    name = target["name"]
    tstate = state["targets"].setdefault(name, {})
    url = target["url"]
    # one fetch, many detectors: watch for the live link AND any page change
    rules = target.get("rules") or [target.get("rule", {})]

    headers = dict(BASE_HEADERS)
    headers.update(target.get("headers", {}))
    if target.get("referer"):
        headers["Referer"] = target["referer"]
    # Opt-in only. A CDN answering 304 when the page really did change would
    # hide the drop, so the target that matters always re-reads the full body.
    if target.get("conditional"):
        if tstate.get("etag"):
            headers["If-None-Match"] = tstate["etag"]
        if tstate.get("last_modified"):
            headers["If-Modified-Since"] = tstate["last_modified"]

    started = now()
    try:
        r = await client.get(url, headers=headers, timeout=15, follow_redirects=True)
        body, status = r.text, r.status_code
    except Exception as e:
        tstate["fails"] = tstate.get("fails", 0) + 1
        if tstate["fails"] in (5, 25, 100):
            log(f"{name}: {tstate['fails']} consecutive errors — {e}")
        return

    tstate["latency_ms"] = int((now() - started) * 1000)

    if status == 304:
        tstate["fails"] = 0
        tstate["last_check"] = now()
        return

    if status in (403, 429) or status >= 500:
        tstate["fails"] = tstate.get("fails", 0) + 1
        # back off hard so we don't dig the hole deeper
        tstate["cooldown_until"] = now() + min(300, 5 * (2 ** min(tstate["fails"], 6)))
        if tstate["fails"] in (3, 10):
            log(f"{name}: HTTP {status} x{tstate['fails']}, backing off")
        if not any(rl.get("type") == "status_ok" for rl in rules):
            return
    else:
        tstate["fails"] = 0
        if target.get("conditional"):
            if r.headers.get("ETag"):
                tstate["etag"] = r.headers["ETag"]
            if r.headers.get("Last-Modified"):
                tstate["last_modified"] = r.headers["Last-Modified"]

    markers = tstate.setdefault("rules", {})
    fired, details, hot = False, [], None
    for idx, rule in enumerate(rules):
        key = str(idx)
        try:
            f, detail, marker = evaluate({**rule, "_base": url}, body, status,
                                         markers.get(key, {}))
        except Exception as e:
            # a misconfigured rule crashes on every poll — say it once, then
            # rarely, instead of flooding the logs at hot cadence
            hits = tstate.get("rule_errors", {})
            hits[key] = hits.get(key, 0) + 1
            tstate["rule_errors"] = hits
            if hits[key] == 1 or hits[key] % 100 == 0:
                log(f"{name}: rule {idx} ({rule.get('type', 'changed')}) "
                    f"crashed x{hits[key]}: {e}")
            continue
        markers[key] = marker
        if f:
            fired = True
            details.append(detail)
            hot = hot or marker.get("hot_link")
    tstate["last_check"] = now()

    if not fired:
        return

    # A brand-new live checkout link is the entire point of this bot — never
    # let a cooldown from some unrelated page edit swallow it.
    urgent = bool(hot)
    cooldown = float(target.get("cooldown", 600))
    if not urgent and now() - tstate.get("fired_at", 0) < cooldown:
        return
    tstate["fired_at"] = now()

    detail = "\n".join(d for d in details if d)
    if state.get("muted"):
        log(f"{name} FIRED (muted): {detail}")
        return

    open_url = hot or target.get("open_url") or url
    msg = (
        f"🚨🚨 <b>{name}</b> 🚨🚨\n\n"
        f"{detail}\n"
        f"⏰ {wib()} WIB\n\n"
        f"👉 {open_url}\n\n"
        f"<i>GO GO GO — tap it, you buy</i>"
    )
    await send(client, msg)
    log(f"*** ALERT: {name} — {detail}")
    await local_alert(f"WAR TIKET — {name}", detail, open_url)

    # Keep shouting. One notification arriving while the phone is face-down
    # is exactly how this gets missed.
    if urgent or target.get("repeat_until_ack"):
        every = float(target.get("alert_repeat_every", 30))
        state["pending_ack"] = {
            "name": name,
            "url": open_url,
            "left": int(target.get("alert_repeat", 10)),
            "every": every,
            "next": now() + every,
        }

    # Only on a real detected link, and only once: relaunching a browser on
    # every poll would be its own outage.
    if target.get("launch_checkout") and hot and not tstate.get("checkout_started"):
        tstate["checkout_started"] = now()
        pid = launch_checkout(hot, name)
        if pid:
            await send(client, f"🤖 checkout driver started on <code>{hot}</code>"
                               f" — it will stop at the payment page and ping you.")

    if target.get("once"):
        target["_done"] = True


# ---------------------------------------------------------------- commands

async def poll_commands(client: httpx.AsyncClient, config: dict, state: dict) -> None:
    """Let Ade drive the bot from Telegram itself — no laptop needed."""
    if DRY_RUN or not BOT_TOKEN:
        return
    conflict = False
    backoff = 0.0
    while True:
        try:
            if backoff:
                await asyncio.sleep(backoff)
            r = await client.get(
                f"{TG}/getUpdates",
                params={"offset": state.get("tg_offset", 0), "timeout": 25},
                timeout=35,
            )
            data = r.json()
            # An error body has no "result", which is indistinguishable from
            # "no new messages" if you only ever read .get("result", []).
            # That is how this loop used to spin a few hundred times a second
            # against a 409 without logging a thing.
            if not data.get("ok", False):
                code = data.get("error_code")
                desc = str(data.get("description", ""))[:160]
                if code == 409:
                    backoff = 30.0
                    log("command poll: CONFLICT — another instance is polling "
                        f"getUpdates, commands are being eaten. {desc}")
                    if not conflict:
                        conflict = True
                        # sendMessage still works: only getUpdates conflicts,
                        # so this warning does reach the phone.
                        await send(client,
                                   "⚠️ <b>two bots are running</b>\n\n"
                                   "Another instance is polling Telegram, so "
                                   "your commands are being swallowed. Stop "
                                   "the duplicate.\n\n"
                                   "<i>Alerts still work — only commands are "
                                   "affected.</i>", silent=True)
                elif code == 429:
                    retry = float((data.get("parameters") or {})
                                  .get("retry_after", 5))
                    backoff = min(max(retry, 1.0), 120.0)
                    log(f"command poll: rate limited, waiting {backoff:g}s")
                else:
                    backoff = min(max(backoff * 2, 5.0), 120.0)
                    log(f"command poll: telegram said {code}: {desc}")
                continue

            backoff = 0.0
            if conflict:
                conflict = False
                log("command poll: conflict cleared, commands live again")
                await send(client, "✅ commands live again — duplicate is gone",
                           silent=True)
            state["cmd_ok_at"] = now()
            for upd in data.get("result", []):
                state["tg_offset"] = upd["update_id"] + 1
                m = upd.get("message") or {}
                text = m.get("text", "").strip()
                origin = (m.get("chat") or {}).get("id")
                if not text.startswith("/") or origin is None:
                    continue
                cmd, *args = text.split()
                cmd = cmd.split("@")[0].lower()   # /status@MyBot -> /status

                if cmd == "/here":
                    kind = (m.get("chat") or {}).get("type", "?")
                    await send(client,
                               f"chat id: <code>{origin}</code>\ntype: {kind}\n\n"
                               f"add this to TELEGRAM_CHAT_ID in Railway "
                               f"(comma-separated) to get alerts here.",
                               silent=True, to=origin)

                elif cmd == "/status":
                    lines = [f"🤖 alive · {wib()} WIB",
                             f"muted: {bool(state.get('muted'))}",
                             f"hot: {'yes' if state.get('hot_until',0) > now() else 'no'}", ""]
                    live = [t for t in config["targets"] if t.get("enabled", True)]
                    off = [t for t in config["targets"] if not t.get("enabled", True)]
                    for t in live:
                        ts = state["targets"].get(t["name"], {})
                        last = ts.get("last_check")
                        age = f"{int(now()-last)}s ago" if last else "starting…"
                        fails = ts.get("fails", 0)
                        flag = f" ⚠️ {fails} errors" if fails > 3 else ""
                        lat = ts.get("latency_ms")
                        speed = f" · {lat}ms" if lat else ""
                        lines.append(f"✅ {t['name']} — {age}{speed}{flag}")
                    if off:
                        lines.append(f"\n💤 sleeping ({len(off)})")
                        for t in off:
                            lines.append(f"   · {t['name']}")
                    await send(client, "\n".join(lines), silent=True, to=origin)

                elif cmd == "/hot":
                    mins = float(args[0]) if args and args[0].replace(".", "").isdigit() else 15
                    state["hot_until"] = now() + mins * 60
                    await send(client, f"🔥 hot mode ON for {mins:g} min", silent=True, to=origin)

                elif cmd == "/cool":
                    state["hot_until"] = 0
                    await send(client, "❄️ back to normal cadence", silent=True, to=origin)

                elif cmd == "/mute":
                    state["muted"] = True
                    await send(client, "🔇 muted", silent=True, to=origin)

                elif cmd == "/unmute":
                    state["muted"] = False
                    await send(client, "🔔 unmuted", silent=True, to=origin)

                elif cmd == "/reset":
                    state["targets"] = {}
                    await send(client, "♻️ baselines cleared", silent=True, to=origin)

                elif cmd == "/ack":
                    state["pending_ack"] = None
                    await send(client, "✅ got it — I'll stop nagging", silent=True, to=origin)

                elif cmd == "/links":
                    lines = ["🔗 checkout links seen so far:"]
                    found_any = False
                    for t in config["targets"]:
                        ts = state["targets"].get(t["name"], {})
                        urls = []
                        for mk in (ts.get("rules") or {}).values():
                            urls += mk.get("links") or []
                        if not urls:
                            continue
                        found_any = True
                        lines.append(f"\n<b>{t['name']}</b>")
                        for u in dict.fromkeys(urls):
                            lines.append(f"• {u}")
                    if not found_any:
                        lines.append("nothing yet — buttons still dead (href=#)")
                    await send(client, "\n".join(lines[:45]), silent=True, to=origin)

                elif cmd == "/next":
                    lines = ["⏳ upcoming drops:"]
                    for t in config["targets"]:
                        if not t.get("enabled", True) or not t.get("drop_time"):
                            continue
                        left = drop_ts(t)
                        if left is None:
                            continue
                        secs = int(left - now())
                        when = (f"in {secs // 3600}h {secs % 3600 // 60}m" if secs > 0
                                else f"{-secs // 60}m ago")
                        lines.append(f"• {t['name']} — {t['drop_time']} WIB ({when})")
                    if len(lines) == 1:
                        lines.append("none scheduled")
                    await send(client, "\n".join(lines), silent=True, to=origin)

                elif cmd == "/test":
                    await send(client, "🚨🚨 <b>TEST ALERT</b> 🚨🚨\n\nif this is loud, you're ready.")

                else:
                    await send(client,
                               "/status /here /links /next /hot [min] /cool "
                               "/mute /unmute /ack /reset /test",
                               silent=True, to=origin)
            save_state(state)
        except Exception as e:
            log(f"command poll: {e}")
            await asyncio.sleep(5)


# ---------------------------------------------------------------- runners

async def watch(client: httpx.AsyncClient, target: dict, state: dict) -> None:
    # stagger startup so we don't fire every request in the same millisecond
    await asyncio.sleep(random.uniform(0, 3))
    log(f"watching {target['name']}")
    while True:
        if target.get("_done"):
            return
        tstate = state["targets"].get(target["name"], {})
        cd = tstate.get("cooldown_until", 0)
        if cd > now():
            await asyncio.sleep(cd - now())
            continue
        try:
            await check(client, target, state)
        except Exception as e:
            log(f"{target['name']} check crashed: {e}")
        gap = interval_for(target, state)
        await asyncio.sleep(gap * random.uniform(0.85, 1.15))  # jitter


async def heartbeat(client: httpx.AsyncClient, config: dict, state: dict) -> None:
    every = float(config.get("heartbeat_hours", 6)) * 3600
    if every <= 0:
        return
    while True:
        await asyncio.sleep(every)
        save_state(state)
        note = ""
        # The heartbeat keeps arriving when the command loop is dead, so this
        # is the one place that can tell you commands stopped answering.
        seen = state.get("cmd_ok_at")
        if BOT_TOKEN and not DRY_RUN:
            if not seen:
                note = "\n⚠️ commands never connected"
            elif now() - seen > 300:
                note = (f"\n⚠️ commands last answered "
                        f"{int((now() - seen) / 60)} min ago")
        await send(client, f"🤖 still watching {len(config['targets'])} targets "
                           f"· {wib()} WIB{note}", silent=True)


async def persist(state: dict) -> None:
    while True:
        await asyncio.sleep(30)
        save_state(state)


async def nag(client: httpx.AsyncClient, state: dict) -> None:
    """Re-shout a live link until a human sends /ack."""
    while True:
        await asyncio.sleep(5)
        pending = state.get("pending_ack")
        if not pending or int(pending.get("left", 0)) <= 0 or state.get("muted"):
            continue
        if now() < pending.get("next", 0):
            continue
        pending["left"] = int(pending["left"]) - 1
        pending["next"] = now() + float(pending.get("every", 30))
        await send(client,
                   f"⏰ <b>STILL OPEN — {pending['name']}</b>\n\n"
                   f"👉 {pending['url']}\n\n<i>send /ack to shut me up</i>")
        # no url: don't reopen the browser on every repeat
        await local_alert(f"WAR TIKET — {pending['name']}", "still open", None)


async def countdown(client: httpx.AsyncClient, config: dict, state: dict) -> None:
    """
    Pre-drop pings, so you are already logged in with payment saved when the
    window opens. Being at the keyboard 60s early beats any amount of polling.
    """
    marks = [(10, "10 SECONDS"), (60, "60 seconds"), (300, "5 minutes"),
             (900, "15 minutes"), (3600, "1 hour")]
    drops = [(ts, t) for t in config["targets"]
             if t.get("enabled", True) and (ts := drop_ts(t)) is not None]
    if not drops:
        return
    sent = state.setdefault("countdown_sent", {})
    while True:
        await asyncio.sleep(5)
        for ts, target in drops:
            left = ts - now()
            if left <= 0:
                continue
            # fire only the tightest mark that applies, and retire the looser
            # ones — otherwise starting the bot 40s before a drop would fire
            # "1 hour", "15 minutes" and "5 minutes" all at once
            due = [secs for secs, _ in marks if left <= secs]
            if not due:
                continue
            tightest = min(due)
            key = f"{target['name']}|{tightest}"
            if sent.get(key):
                continue
            for secs, _ in marks:
                if secs >= tightest:
                    sent[f"{target['name']}|{secs}"] = True
            label = next(lbl for secs, lbl in marks if secs == tightest)
            await send(client,
                       f"⏳ <b>{label}</b> to {target['name']}\n\n"
                       f"logged in? payment saved? correct ticket tier picked?\n"
                       f"👉 {target.get('open_url', target['url'])}")
            log(f"countdown: {label} to {target['name']}")
            if tightest <= 60:
                await local_alert(f"WAR TIKET — {label}", target["name"],
                                  target.get("open_url", target["url"]))


async def supervise(client: httpx.AsyncClient, name: str, factory) -> None:
    """
    Keep one loop alive on its own.

    gather() used to hand the first exception straight up through main(), so a
    crash in any single loop killed the whole bot — and since nothing
    announced it, the bot simply went quiet. Now a loop that dies is reported
    and restarted, and the rest keep running.
    """
    delay = 5.0
    while True:
        try:
            await factory()
            log(f"{name}: returned on its own — restarting in {delay:g}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"{name}: CRASHED {e!r} — restarting in {delay:g}s")
            try:
                await send(client,
                           f"⚠️ <b>{name} crashed</b>\n\n"
                           f"<code>{html.escape(repr(e))[:300]}</code>\n\n"
                           f"restarting in {delay:g}s", silent=True)
            except Exception:
                pass                      # never let the report kill the retry
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300.0)


async def main() -> None:
    config = load_json(CONFIG_PATH, None)
    if not config or not config.get("targets"):
        sys.exit("config.json missing or has no targets")
    if not BOT_TOKEN and not DRY_RUN:
        sys.exit("set TELEGRAM_TOKEN (or DRY_RUN=1 to test)")

    state = load_json(STATE_PATH, {})
    state.setdefault("targets", {})

    limits = httpx.Limits(max_keepalive_connections=20, keepalive_expiry=300)
    async with httpx.AsyncClient(http2=False, limits=limits, follow_redirects=True) as client:
        targets = [t for t in config["targets"] if t.get("enabled", True)]
        log(f"starting · {len(targets)} targets · DRY_RUN={DRY_RUN} "
            f"· LOCAL_NOTIFY={LOCAL_NOTIFY} · AUTO_OPEN={AUTO_OPEN}")
        await send(client, f"🤖 war tiket bot online · {len(targets)} targets · {wib()} WIB",
                   silent=True)

        jobs = [supervise(client, f"watch:{t['name']}",
                          lambda t=t: watch(client, t, state))
                for t in targets]
        jobs += [
            supervise(client, "heartbeat", lambda: heartbeat(client, config, state)),
            supervise(client, "persist", lambda: persist(state)),
            supervise(client, "commands", lambda: poll_commands(client, config, state)),
            supervise(client, "nag", lambda: nag(client, state)),
            supervise(client, "countdown", lambda: countdown(client, config, state)),
        ]
        # return_exceptions so one loop that somehow escapes its supervisor
        # cannot take the others down with it
        await asyncio.gather(*jobs, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("bye")
