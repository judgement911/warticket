#!/usr/bin/env bash
# One-command setup for the war ticket bot. Run this on YOUR OWN computer,
# in the folder this file lives in:  bash setup.sh
#
# Installs the dependencies, then walks you through profile.json. Your details
# are typed by you, on your machine, into a gitignored file — they are never
# sent anywhere.
set -u

cd "$(dirname "$0")" || exit 1
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

PY=python3
command -v $PY >/dev/null 2>&1 || { bad "python3 not found — install Python 3.10+ first"; exit 1; }
ok "python3 $($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

say "1/3  Installing dependencies"
if $PY -m pip install -q -r requirements.txt; then ok "httpx + playwright installed"
else bad "pip install failed — scroll up for the reason"; exit 1; fi

if [ "${SKIP_BROWSER:-}" = "1" ]; then
  ok "skipping browser download (SKIP_BROWSER=1)"
else
  printf '  downloading Chromium, this takes a minute...\n'
  if $PY -m playwright install chromium >/dev/null 2>&1; then ok "Chromium ready"
  else bad "browser download failed — rerun: python3 -m playwright install chromium"; fi
fi

say "2/3  Your buyer details"
_PROMPT=$(mktemp "${TMPDIR:-/tmp}/wt-XXXXXX.py")
cat > "$_PROMPT" <<'PYEOF'
import json, os, sys
from pathlib import Path

path = Path("profile.json")
existing = {}
if path.exists():
    try:
        existing = json.loads(path.read_text())
    except Exception:
        pass
    filled = [k for k, v in existing.items() if v and not k.startswith("_")]
    if filled:
        print(f"  profile.json already has {len(filled)} field(s) filled.")
        if input("  Redo it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  keeping what you have")
            sys.exit(0)

if not sys.stdin.isatty():
    print("  not a terminal — copy profile.example.json to profile.json by hand")
    sys.exit(0)

FIELDS = [
    ("full_name", "Full name (as on your KTP)", True),
    ("email",     "Email",                      True),
    ("phone",     "Phone (08...)",              True),
    ("id_number", "KTP number",                 True),
    ("birth_date","Date of birth (YYYY-MM-DD)",  False),
    ("address",   "Address",                    False),
    ("city",      "City",                       False),
    ("postcode",  "Postcode",                   False),
]
print("  Enter your details. Press Enter to skip an optional one.")
print("  There is no card field — the bot stops before payment.\n")
out = {"id_type": "KTP", "country": "Indonesia"}
for key, label, required in FIELDS:
    while True:
        val = input(f"  {label}: ").strip() or existing.get(key, "")
        if val or not required:
            break
        print("    needed for the ticket form")
    out[key] = val

path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
try:
    os.chmod(path, 0o600)
except Exception:
    pass
print(f"\n  saved profile.json ({sum(1 for v in out.values() if v)} fields, owner-only)")
PYEOF
$PY "$_PROMPT"; rm -f "$_PROMPT"

say "3/3  Next: log in to the store"
STORE=$($PY -c "
import json
try:
    c=json.load(open('config.json'))
    print(next((t['url'] for t in c.get('targets',[]) if t.get('enabled',True) and t.get('url')),''))
except Exception: print('')")
[ -n "$STORE" ] || STORE="https://dyandraglobalstore-03.com/"
cat <<TXT

  Run this and log in BY HAND — your password stays yours,
  and the session is saved so you are not logging in at 2pm:

      python3 checkout.py login $STORE

  Then at the drop, when the Telegram alert arrives:

      python3 checkout.py run <link-from-the-alert>

TXT
