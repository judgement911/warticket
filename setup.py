#!/usr/bin/env python3
"""
First-time setup. Works the same on Windows, macOS and Linux:

    python3 setup.py         (macOS / Linux)
    python setup.py          (Windows)

Installs what the bot needs, then asks for your buyer details and writes
profile.json. Your details are typed by you, on your machine, into a file git
never commits — nothing is uploaded anywhere.

    SKIP_BROWSER=1   skip the Chromium download if you already have it
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIN = os.name == "nt"


def say(msg: str) -> None:
    print(f"\n{'=' * 58}\n{msg}\n{'=' * 58}")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def bad(msg: str) -> None:
    print(f"  [!!] {msg}")


def run(args: list[str], quiet: bool = True) -> bool:
    try:
        kw = {"cwd": HERE}
        if quiet:
            kw["stdout"] = subprocess.DEVNULL
            kw["stderr"] = subprocess.STDOUT
        return subprocess.call(args, **kw) == 0
    except Exception as e:
        bad(f"could not run {args[0]}: {e}")
        return False


FIELDS = [
    ("full_name",  "Full name (as on your KTP)",  True),
    ("email",      "Email",                       True),
    ("phone",      "Phone (08...)",               True),
    ("id_number",  "KTP number",                  True),
    ("birth_date", "Date of birth (YYYY-MM-DD)",  False),
    ("address",    "Address",                     False),
    ("city",       "City",                        False),
    ("postcode",   "Postcode",                    False),
]


def ask_profile() -> None:
    path = HERE / "profile.json"
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        filled = [k for k, v in existing.items() if v and not k.startswith("_")]
        if filled:
            print(f"  profile.json already has {len(filled)} field(s) filled.")
            if input("  Redo it? [y/N] ").strip().lower() not in ("y", "yes"):
                ok("keeping what you have")
                return

    if not sys.stdin.isatty():
        bad("no keyboard attached — run this from a terminal window")
        return

    print("  Type your details. Press Enter to skip an optional one.")
    print("  There is no card field: the bot stops before payment.\n")
    out = {"id_type": "KTP", "country": "Indonesia"}
    for key, label, required in FIELDS:
        prev = existing.get(key, "")
        hint = f" [{prev}]" if prev else ""
        while True:
            val = input(f"  {label}{hint}: ").strip() or prev
            if val or not required:
                break
            print("     the ticket form needs this one")
        out[key] = val

    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    if not WIN:
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    ok(f"saved profile.json ({sum(1 for v in out.values() if v)} fields)")


def store_url() -> str:
    try:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        for t in cfg.get("targets", []):
            if t.get("enabled", True) and t.get("url"):
                return t["url"]
    except Exception:
        pass
    return "https://dyandraglobalstore-03.com/"


def main() -> int:
    py = sys.executable or ("python" if WIN else "python3")
    if sys.version_info < (3, 10):
        bad(f"Python {sys.version_info[0]}.{sys.version_info[1]} is too old — "
            f"install Python 3.10 or newer from python.org")
        return 1
    ok(f"Python {sys.version_info[0]}.{sys.version_info[1]}")

    say("1 of 3   Installing what the bot needs")
    if run([py, "-m", "pip", "install", "-r", "requirements.txt"]):
        ok("httpx + playwright installed")
    else:
        bad("install failed. Try it by hand to see why:")
        bad(f"    {py} -m pip install -r requirements.txt")
        return 1

    if os.environ.get("SKIP_BROWSER") == "1":
        ok("skipping the browser download (SKIP_BROWSER=1)")
    else:
        print("  downloading Chromium — takes a minute, please wait...")
        if run([py, "-m", "playwright", "install", "chromium"]):
            ok("browser ready")
        else:
            bad("browser download failed. Retry later with:")
            bad(f"    {py} -m playwright install chromium")

    say("2 of 3   Your buyer details")
    ask_profile()

    say("3 of 3   Next step: log in to the store")
    shown = "python" if WIN else "python3"
    print(f"""
  Copy this line, run it, and log in BY HAND in the window
  that opens. Your password stays yours. The session is saved,
  so you are not logging in at 2pm:

      {shown} checkout.py login {store_url()}

  Then when the Telegram alert arrives at the drop:

      {shown} checkout.py run <paste-the-link-from-the-alert>
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
