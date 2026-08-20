# WAR TIKET BOT

Drop monitor for the Maroon 5 Jakarta 2027 sale. It watches the sale page,
and the instant a dead `href="#"` button turns into a real tiket.com queue
link it puts **that exact link** in your hand — Telegram, desktop
notification, and optionally an already-open browser tab.

## What it does not do

It does not buy. There is no auto-checkout, no CAPTCHA solver, no queue-token
replay, no card-details autofill. Automated purchasing on tiket.com is against
their terms and is specifically legislated against in a number of countries,
so the last click is yours. What this bot removes is the part you can actually
lose fairly: the 30–90 seconds between a window opening and you finding out.

## Run it

```bash
export TELEGRAM_TOKEN=...        # @BotFather
export TELEGRAM_CHAT_ID=...      # @userinfobot; comma-separate for DM+group
python main.py
```

| env var | effect |
| --- | --- |
| `DRY_RUN=1` | print alerts instead of sending — test without spamming yourself |
| `LOCAL_NOTIFY=1` | desktop notification on the machine running the bot |
| `AUTO_OPEN=1` | open the queue page in a local browser the moment it goes live |

`AUTO_OPEN` only helps if the bot runs on the laptop you're buying from. On
Railway, leave it off and rely on Telegram.

## Telegram commands

```
/status    what's alive, how stale, request latency
/links     every checkout link seen so far
/next      upcoming drops and time remaining
/hot [min] force fast polling
/cool      back to normal cadence
/ack       stop the repeat-alert nagging
/mute      /unmute   /reset   /here   /test
```

## How detection works

Each target does **one** fetch per poll and runs every rule in its `rules`
list against it, so adding a detector costs no extra requests.

| rule | fires when |
| --- | --- |
| `new_link` | a URL matching `pattern` appears that wasn't there last poll |
| `changed` | page content hash changes (volatile junk stripped first) |
| `appears` / `disappears` | `value` shows up / goes away |
| `regex` | `pattern` matches |
| `status_ok` | page starts returning 2xx |
| `json` / `json_changed` | value at `path` equals/exceeds/changes |

`new_link` is the one that matters. Notes on its behaviour:

- **First poll is a baseline only.** The page already links to `tiket.com` in
  its footer; alerting on that would be a false start. Set
  `fire_on_first: true` to override.
- **It reports the actual URL**, absolutised, and uses it as the alert button —
  you tap straight into the queue instead of hunting the page.
- **It ignores cooldown.** A new checkout link is never suppressed by a
  cooldown left over from some unrelated page edit.
- **It nags.** `alert_repeat` / `alert_repeat_every` re-shout until you `/ack`,
  because one notification arriving face-down is how this gets missed.

`new_link` also excludes static assets by default (`exclude_pattern`). Bundlers
name JS chunks after routes, so a Next.js store ships
`/_next/static/chunks/pages/checkout-9f2a1b.js` — that matches `/checkout` and
gets a new hash on every deploy. Without the exclusion a routine redeploy fires
a "LIVE LINK" alert pointing at a `.js` file. Set `exclude_pattern: ""` to keep
asset URLs.

`changed` strips per-request noise (CSRF meta tags, nonces, cache-busters,
timestamps, ms epochs, bundler fingerprints, Next.js build ids) before hashing.
Without that it fires every poll on a rotating token and again on every deploy,
and an alert you learn to ignore is worse than no alert. Only the *hash* part of
an asset name is dropped, so a meaningful rename — `banner-soon.png` →
`banner-live.png` — still registers.

Timing: `interval` normally, `hot_interval` within `hot_window_min` of
`drop_time`. Countdown pings land at T-1h, 15m, 5m, 60s and 10s. 403/429/5xx
back off exponentially — getting rate-limited an hour before the drop is the
one unrecoverable mistake.

## The part the bot can't do for you

Being fast at the buy button is preparation, not software:

- [ ] tiket.com account created, **logged in**, phone/email verified days early
- [ ] card saved in the account, or OVO/GoPay topped up past the total
- [ ] presale code in your clipboard (Artist Presale needs the S.I.N. code —
      the fan club target in `config.json` watches signups, off by default)
- [ ] know your tier, quantity and fallback tier *before* the clock hits
- [ ] one device, one tab. Multiple tabs and refresh-spam get you queue-banned
- [ ] wired connection if you have one; hotspot as backup
- [ ] `/test` fired and audible, phone off silent, bot `/status` green

## Targets currently configured

**NCT 127 5TH TOUR 'NEO CITY : JAKARTA — THE REDLINE'** · Indonesia Arena ·
3 Oct 2026 · Rp1,350,000–3,950,000 (+10% tax, +5% platform fee) · max 4 tickets
per transaction, one account.

| sale | when (WIB) | status |
| --- | --- | --- |
| NCTzen 127 membership presale | Tue 19 Aug, 14:00–21:59 | gone |
| Dyandra Global presale | Wed 20 Aug, 14:00–21:59 | gone |
| **General sale** | **Fri 21 Aug, 14:00 → sold out** | **this is the one** |

| target | state | note |
| --- | --- | --- |
| NCT 127 REDLINE — event page | on | `dyandraglobalstore-03.com`, the tour page |
| NCT 127 REDLINE — canonical store | on | `dyandraglobalstore.com`, hedge — see below |
| Dyandra Store `-02` | off | previous sale's mirror, most likely |
| Maroon 5 sale site | on | unchanged |
| Maroon 5 tiket.com / S.I.N. | off | unchanged |

Sold on Dyandra's own store, **not loket.com**. Both NCT targets carry
`drop_time: 2026-08-21T14:00:00`, so the 2s cadence and the countdown pings
switch on an hour before the drop by themselves.

### Why two NCT targets

Press coverage puts the general sale on `dyandraglobalstore.com`, while the
tour page itself is served from the `-03` mirror. Neither host was reachable
from the build environment, so which one grows the buy button is unverified.
Watching one and guessing wrong costs the ticket; watching both costs one extra
request every 30 seconds. Turn the canonical one off if you confirm the sale is
`-03` only.

### On the numbered mirror domains

Dyandra sells through `dyandraglobalstore.com` and spins up numbered mirrors
(`-02`, `-03`) to shed load during a heavy sale. A numbered lookalike is also
exactly how a card-harvesting clone presents itself, and this bot's whole job is
to make you tap a link fast without thinking about it. Before drop day, confirm
the mirror domain appears in Dyandra's own announcement — their site or verified
social account, not a link forwarded to you. If the mirror and the canonical
store ever disagree, trust the canonical store.

## Checkout automation (`checkout.py`)

The monitor tells you a sale opened. This does the clicking: picks the tier,
sets the quantity, fills your details from `profile.json`, advances — and stops
when the next thing it would touch is a payment.

    pip install playwright                     # Chromium is already installed
    python3 checkout.py login <store-url>      # log in by hand, once
    python3 checkout.py inspect <url>          # dump the real selectors
    python3 checkout.py run <checkout-url>     # walk it, halt at payment

### It does not pay

That line is load-bearing, not a disclaimer. Three independent guards, each
tested:

- **Never clicks a payment control.** Every click is checked against the
  element's own text and attributes first. `BAYAR SEKARANG`, `Pay Now`,
  `Buat Pesanan`, `Confirm and Pay`, a bank-transfer or GoPay/OVO/QRIS option —
  refused. A step map that explicitly names the pay button is still refused;
  the map is a guess about someone else's HTML, the guard isn't.
- **Never types card data.** Fields that look like a card number, CVV, expiry
  or password are skipped even when they sit inline with the buyer form, and
  only an allowlist of buyer keys is ever typed at all. Put a card number in
  `profile.json` and it will be ignored, loudly.
- **Stops on arrival at a payment page.** Checked before each step, so it halts
  even if your map would have carried on.

It then screenshots, pings your phone, and leaves the browser open with your
session in it. You check the total and pay. `profile.json`, `browser-profile/`
and `shots/` are gitignored — the profile directory *is* your login.

### Selectors are yours to fill in

`config.json -> checkout.steps` ships **empty**. Neither Dyandra domain was
reachable from the machine this was built on, so any selector written here
would be invented. Run `inspect` against the live page and build the list from
what it prints — it dumps every visible button, link, input and select with a
usable selector, plus `data-testid` where one exists (generated ids rotate
between deploys; an intentional test id usually doesn't).

`inspect` also answers the question that decides whether any of this works: if
it reports almost no rendered HTML, the page is a JavaScript shell, and the
HTTP monitor in `main.py` cannot see it either — that target needs this browser
rather than `httpx`.

### Mark the entry steps optional

The monitor hands over whatever URL it detected, which is often already several
steps into the flow. Steps marked `"optional": true` are skipped when their
element isn't on the page (in ~2.5s, not the full timeout), so one map works
whether the link drops you on the event page or straight into the cart.

### Automatic handoff

Set `launch_checkout: true` on a target and the monitor spawns the driver
against the detected link the moment the sale opens, so the clicking is already
done when you pick up the phone. Output goes to `checkout.log`. Off by default
— fill in the steps and rehearse first.

To rehearse the whole chain without notifying anyone or touching a real store,
point a target at a local fixture and run with `DRY_RUN=1 REHEARSE_CHECKOUT=1`.
`DRY_RUN` alone deliberately parks the browser, so a rehearsal can never go
clicking a live sale.

### Tests

    python3 test_checkout.py     # 51 assertions, weighted on the guards
    python3 test_cmds.py         # Telegram command handlers

`test_checkout.py` serves a fake Dyandra-shaped store locally — tier page,
buyer form with card inputs sitting next to the real ones, payment page — and
asserts the driver walks it, fills four fields, skips both card inputs, and
stops without paying. Nothing leaves the machine.

## When the bot goes quiet

Telegram allows exactly one `getUpdates` consumer per token. Start a second
instance and Telegram answers **409 Conflict** — and an error body carries no
`result`, which is indistinguishable from "no new messages" if you only read
`.get("result", [])`. The command loop used to spin on that at a few hundred
requests a second, logging nothing and telling nobody: alerts kept working,
`/status` answered sometimes or never, depending on which instance won the
race. Two `bot online` messages a few minutes apart is the tell.

It now backs off, logs the conflict, and messages you once — `sendMessage`
still works during a conflict, so that warning does arrive. It tells you again
when the duplicate goes away.

Each loop also runs under a supervisor. `asyncio.gather` used to pass the first
exception straight up through `main()`, so a crash in any one loop killed the
whole bot, silently — the same quiet death from the outside. A loop that dies
is now reported to Telegram and restarted with backoff while the others keep
running.

The heartbeat carries the answer to "are commands alive?", because it keeps
arriving when the command loop is dead: it appends a warning when commands
last answered more than five minutes ago. `heartbeat_hours` is 12 by default,
which is a long time to wait for that news — lower it as drop day approaches.

If the bot is silent, in order: check whether a second instance is running,
check the process is alive at all, then check `heartbeat_hours` against how
long it has actually been quiet.

