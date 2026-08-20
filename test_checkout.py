#!/usr/bin/env python3
"""
Tests for the checkout driver, weighted toward the one property that costs
money if it breaks: this program never performs a payment.

    python3 test_checkout.py

Serves a fake Dyandra-shaped store locally, so no traffic reaches anyone.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import functools
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import checkout as C  # noqa: E402

PAGES = {
    "index.html": """<html><title>Store</title><body>
        <h1>MAROON 5 ASIA 2027 — JAKARTA</h1><span>ON SALE</span>
        <a href="/tier.html" id="buy">BELI TIKET</a>
        <a href="/faq.html">Bantuan</a></body></html>""",
    "tier.html": """<html><title>Kategori</title><body>
        <button id="cat1" data-testid="tier-cat1">CAT 1 — Rp 3.500.000</button>
        <button id="cat2" data-testid="tier-cat2">CAT 2 — Rp 2.100.000</button>
        <select id="qty" name="qty"><option value="1">1</option>
        <option value="2">2</option><option value="4">4</option></select>
        <a href="/form.html" id="next">LANJUTKAN</a></body></html>""",
    # buyer details, with card inputs sitting right next to them as a trap
    "form.html": """<html><title>Data Pembeli</title><body>
        <form action="/pay.html" method="get">
        <input name="full_name" placeholder="Nama Lengkap">
        <input name="email" type="email" placeholder="Email">
        <input name="phone" placeholder="Nomor Telepon">
        <input name="id_number" placeholder="Nomor KTP">
        <input name="card_number" placeholder="Nomor Kartu">
        <input name="cvv" placeholder="Kode CVV">
        <button type="submit" id="next">LANJUTKAN</button>
        </form></body></html>""",
    "pay.html": """<html><title>Pembayaran</title><body>
        <h2>Pembayaran</h2><p>Total: Rp 7.000.000</p>
        <button id="paynow">BAYAR SEKARANG</button></body></html>""",
}

PROFILE = {
    "full_name": "Ade Prasetyo",
    "email": "ade@example.com",
    "phone": "081234567890",
    "id_number": "3174012345670001",
    # deliberately present: a real person will paste these in eventually
    "card_number": "4111111111111111",
    "cvv": "123",
}

WALK = [
    {"do": "click", "selector": "#buy", "note": "BELI TIKET"},
    {"do": "click", "selector": "[data-testid=tier-cat1]", "note": "CAT 1"},
    {"do": "select", "selector": "#qty", "value": "2"},
    {"do": "click", "selector": "#next", "note": "LANJUTKAN -> payment step"},
    {"do": "fill_profile"},
    {"do": "click", "selector": "#next", "note": "advance to payment"},
    {"do": "click", "selector": "#paynow", "note": "would pay — must be refused"},
]

PASS = FAIL = 0


def chk(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")


def truthy(label: str, cond, hint="") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label} {hint}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(root: Path) -> tuple[str, http.server.ThreadingHTTPServer]:
    for name, html in PAGES.items():
        (root / name).write_text(html)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):    # a test should not narrate GETs
            pass

    handler = functools.partial(Quiet, directory=str(root))
    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv


def run_driver(workdir: Path, steps: list, url: str, profile=None) -> str:
    """Run `checkout.py run` in its own dir so config/profile are isolated."""
    (workdir / "config.json").write_text(json.dumps(
        {"checkout": {"step_timeout": 8, "steps": steps}}))
    (workdir / "profile.json").write_text(json.dumps(
        PROFILE if profile is None else profile))
    for f in ("checkout.py",):
        (workdir / f).write_bytes((HERE / f).read_bytes())
    env = {**os.environ, "HEADLESS": "1"}
    env.pop("TELEGRAM_TOKEN", None)      # no pings out of a test
    p = subprocess.run([sys.executable, "checkout.py", "run", url],
                       cwd=workdir, env=env, capture_output=True,
                       text=True, timeout=180)
    return p.stdout + p.stderr


def main() -> int:
    print("=== guard patterns (unit) ===")
    pay_yes = ["BAYAR SEKARANG", "Pay Now", "Confirm and Pay", "Buat Pesanan",
               "Selesaikan Pembayaran", "Transfer Bank", "GoPay", "QRIS",
               "Place Order", "Complete Purchase"]
    pay_no = ["BELI TIKET", "LANJUTKAN", "CAT 1 — Rp 3.500.000", "Pilih Kategori",
              "Bantuan", "Masuk", "Daftar", "Berikutnya", "/checkout/session/abc"]
    for t in pay_yes:
        truthy(f"blocks {t!r}", C.PAYMENT_WORDS.search(t))
    for t in pay_no:
        truthy(f"allows {t!r}", not C.PAYMENT_WORDS.search(t))
    for f in ["card_number", "cardNumber", "cvv", "cvc", "expiry_month",
              "nomor_kartu", "security-code", "password"]:
        truthy(f"card field {f!r} refused", C.CARD_FIELDS.search(f))
    for f in ["full_name", "email", "phone", "id_number", "address", "city"]:
        truthy(f"buyer field {f!r} allowed", not C.CARD_FIELDS.search(f))
    truthy("card keys are outside the profile allowlist",
           not {"card_number", "cvv"} & C.ALLOWED_PROFILE_KEYS)

    with TemporaryDirectory() as td:
        root = Path(td) / "store"
        root.mkdir()
        base, srv = serve(root)
        try:
            print("\n=== A. full walk halts at payment ===")
            wd = Path(td) / "a"
            wd.mkdir()
            out = run_driver(wd, WALK, f"{base}/index.html")
            truthy("reached the buyer form", "filled 4 profile field(s)" in out)
            truthy("stopped on the payment page",
                   "STOPPED: payment page reached" in out)
            truthy("never clicked BAYAR SEKARANG",
                   "clicked would pay" not in out and "paynow" not in
                   out.split("STOPPED")[0].split("step 7")[-1])
            truthy("card_number left empty in the submitted form",
                   "card_number=&" in out or "card_number=" in out)
            truthy("cvv left empty", re.search(r"cvv=(&|\s|$)", out) is not None)
            truthy("card profile keys refused",
                   "'card_number' is not in the allowlist" in out
                   and "'cvv' is not in the allowlist" in out)
            truthy("says it will not pay", "does not pay" in out)

            print("\n=== B. told to click pay, on the payment page ===")
            # allow_on_payment defeats the page-level check on purpose, so the
            # element-level guard is the only thing left standing.
            wd = Path(td) / "b"
            wd.mkdir()
            out = run_driver(wd, [{"do": "click", "selector": "#paynow",
                                   "allow_on_payment": True,
                                   "note": "explicitly ordered"}],
                             f"{base}/pay.html")
            truthy("refuses anyway", "STOPPED: step 1 blocked" in out)
            truthy("names the matched word", "'BAYAR SEKARANG'" in out
                   or "bayar" in out.lower())
            truthy("no click logged", "  clicked" not in out)

            print("\n=== C. fill action pointed at a card field ===")
            wd = Path(td) / "c"
            wd.mkdir()
            out = run_driver(wd, [
                {"do": "click", "selector": "#buy"},
                {"do": "click", "selector": "#next"},
                {"do": "fill", "selector": "input[name=card_number]",
                 "value": "4111111111111111", "note": "must be refused"},
            ], f"{base}/index.html")
            truthy("refuses to type card data",
                   "would type card data" in out)

            print("\n=== D. card-shaped input beside buyer fields ===")
            wd = Path(td) / "d"
            wd.mkdir()
            out = run_driver(wd, [
                {"do": "click", "selector": "#buy"},
                {"do": "click", "selector": "#next"},
                {"do": "fill_profile"},
            ], f"{base}/index.html",
                profile={**PROFILE, "full_name": "Ade Prasetyo"})
            truthy("fills the four real fields",
                   "filled 4 profile field(s)" in out)
            truthy("skips the card inputs next to them",
                   "filled card_number" not in out)

            print("\n=== E. inspect dumps real selectors ===")
            wd = Path(td) / "e"
            wd.mkdir()
            (wd / "config.json").write_text("{}")
            (wd / "checkout.py").write_bytes((HERE / "checkout.py").read_bytes())
            p = subprocess.run([sys.executable, "checkout.py", "inspect",
                                f"{base}/tier.html"], cwd=wd,
                               env={**os.environ, "HEADLESS": "1"},
                               capture_output=True, text=True, timeout=120)
            dump = (wd / "selectors-dump.txt")
            truthy("wrote a dump", dump.exists())
            body = dump.read_text() if dump.exists() else ""
            truthy("found the tier button", "tier-cat1" in body)
            truthy("found the quantity select", "select#qty" in body)
            truthy("recorded the continue link", "LANJUTKAN" in body)
        finally:
            srv.shutdown()

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
