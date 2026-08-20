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

| target | state | note |
| --- | --- | --- |
| Dyandra Store `-03` | on | supplied mirror — **verify the domain**, see below |
| Dyandra Store canonical | on | `dyandraglobalstore.com`, the domain to trust |
| Dyandra Store `-02` | off | sibling mirror, enable on drop day for full cover |
| Maroon 5 sale site | on | `maroon5jakarta2027.com` |
| Maroon 5 tiket.com / S.I.N. | off | see notes in `config.json` |

Neither Dyandra target has a `drop_time` yet — set one and the hot cadence plus
countdown pings switch on automatically. The Maroon 5 drop time is
`2026-08-29T10:00:00` WIB; verify it against the official announcement, because
the bot is only as right as that field.

### On the numbered mirror domains

Dyandra sells through `dyandraglobalstore.com` and spins up numbered mirrors
(`-02`, `-03`) to shed load during a heavy sale. A numbered lookalike is also
exactly how a card-harvesting clone presents itself, and this bot's whole job is
to make you tap a link fast without thinking about it. Before drop day, confirm
the mirror domain appears in Dyandra's own announcement — their site or verified
social account, not a link forwarded to you. If the mirror and the canonical
store ever disagree, trust the canonical store.
