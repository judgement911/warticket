#!/usr/bin/env python3
"""
WAR TIKET BOT  —  multi-site drop monitor with Telegram alerts.
© built for Ade

It WATCHES and TELLS YOU. It does not buy. You buy.

Env vars required:
    TELEGRAM_TOKEN    from @BotFather
    TELEGRAM_CHAT_ID  from @userinfobot
Optional:
    DRY_RUN=1         print alerts instead of sending (for testing)

Run:  python main.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------- setup

WIB = timezone(timedelta(hours=7))
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
# comma-separated: DM, group, channel — alerts go to all of them
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

HERE = Path(__file__).parent
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

    # default: raw content hash
    strip = rule.get("ignore_pattern")
    cleaned = re.sub(strip, "", body) if strip else body
    h = hashlib.sha256(cleaned.encode("utf-8", "ignore")).hexdigest()
    old = prev.get("hash")
    marker["hash"] = h
    if old and old != h:
        return True, "page content changed", marker
    return False, "", marker


# ---------------------------------------------------------------- polling

def interval_for(target: dict, state: dict) -> float:
    """Normal cadence, or hot cadence near a known drop time."""
    normal = float(target.get("interval", 8))
    hot = float(target.get("hot_interval", 1.5))

    if state.get("hot_until", 0) > now():
        return hot

    drop = target.get("drop_time")
    if drop:
        try:
            t = datetime.fromisoformat(drop)
            if t.tzinfo is None:
                t = t.replace(tzinfo=WIB)
            secs_away = t.timestamp() - now()
            window = float(target.get("hot_window_min", 10)) * 60
            if -300 < secs_away < window:
                return hot
        except Exception:
            pass
    return normal


async def check(client: httpx.AsyncClient, target: dict, state: dict) -> None:
    name = target["name"]
    tstate = state["targets"].setdefault(name, {})
    url = target["url"]

    headers = dict(BASE_HEADERS)
    headers.update(target.get("headers", {}))
    if target.get("referer"):
        headers["Referer"] = target["referer"]

    try:
        r = await client.get(url, headers=headers, timeout=15, follow_redirects=True)
        body, status = r.text, r.status_code
    except Exception as e:
        tstate["fails"] = tstate.get("fails", 0) + 1
        if tstate["fails"] in (5, 25, 100):
            log(f"{name}: {tstate['fails']} consecutive errors — {e}")
        return

    if status in (403, 429) or status >= 500:
        tstate["fails"] = tstate.get("fails", 0) + 1
        # back off hard so we don't dig the hole deeper
        tstate["cooldown_until"] = now() + min(300, 5 * (2 ** min(tstate["fails"], 6)))
        if tstate["fails"] in (3, 10):
            log(f"{name}: HTTP {status} x{tstate['fails']}, backing off")
        if target.get("rule", {}).get("type") != "status_ok":
            return
    else:
        tstate["fails"] = 0

    fired, detail, marker = evaluate(target.get("rule", {}), body, status, tstate)
    tstate.update(marker)
    tstate["last_check"] = now()

    if not fired:
        return

    cooldown = float(target.get("cooldown", 600))
    if now() - tstate.get("fired_at", 0) < cooldown:
        return
    tstate["fired_at"] = now()

    if state.get("muted"):
        log(f"{name} FIRED (muted): {detail}")
        return

    msg = (
        f"🚨🚨 <b>{name}</b> 🚨🚨\n\n"
        f"{detail}\n"
        f"⏰ {wib()} WIB\n\n"
        f"👉 {target.get('open_url', url)}\n\n"
        f"<i>GO GO GO</i>"
    )
    await send(client, msg)
    log(f"*** ALERT: {name} — {detail}")

    if target.get("once"):
        target["_done"] = True


# ---------------------------------------------------------------- commands

async def poll_commands(client: httpx.AsyncClient, config: dict, state: dict) -> None:
    """Let Ade drive the bot from Telegram itself — no laptop needed."""
    if DRY_RUN or not BOT_TOKEN:
        return
    while True:
        try:
            r = await client.get(
                f"{TG}/getUpdates",
                params={"offset": state.get("tg_offset", 0), "timeout": 25},
                timeout=35,
            )
            for upd in r.json().get("result", []):
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
                        lines.append(f"✅ {t['name']} — {age}{flag}")
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

                elif cmd == "/test":
                    await send(client, "🚨🚨 <b>TEST ALERT</b> 🚨🚨\n\nif this is loud, you're ready.")

                else:
                    await send(client,
                               "/status /here /hot [min] /cool /mute /unmute /reset /test",
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
        await send(client, f"🤖 still watching {len(config['targets'])} targets · {wib()} WIB",
                   silent=True)


async def persist(state: dict) -> None:
    while True:
        await asyncio.sleep(30)
        save_state(state)


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
        log(f"starting · {len(targets)} targets · DRY_RUN={DRY_RUN}")
        await send(client, f"🤖 war tiket bot online · {len(targets)} targets · {wib()} WIB",
                   silent=True)

        jobs = [watch(client, t, state) for t in targets]
        jobs.append(heartbeat(client, config, state))
        jobs.append(persist(state))
        jobs.append(poll_commands(client, config, state))
        await asyncio.gather(*jobs)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("bye")
