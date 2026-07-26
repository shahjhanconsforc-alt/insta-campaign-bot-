# insta-bot — Agent Console

Python (FastAPI) backend + vanilla HTML/CSS/JS frontend.
Browser automation via **Playwright**.

## Quick start

```bash
pip install -r requirements.txt
playwright install chrome        # only if you don't have Google Chrome
python -m backend.main
```

Open <http://localhost:4000>.

## Layout

```
backend/
  main.py                  FastAPI app, serves frontend + mounts routers
  config.py                every .env value is read here, nowhere else
  agent/
    tools.py               Tool definitions + handlers
    registry.py            Tool lookup + catalogue for the planner
    planner.py             goal -> steps (keyword routing over the tool list)
    executor.py            runs steps in order, records the run
  automation/
    playwright_runner.py   owns one long-lived, visible Chrome window
  db/__init__.py           JSON-file store (atomic writes)
  data/                    store.json, screenshots/, seed.py
  routes/                  agent_routes, data_routes, campaign_routes, automation_routes
frontend/
  index.html  style.css  app.js
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/status` | server + db + browser summary |
| GET | `/api/agent/tools` | tool registry |
| POST | `/api/agent/plan` | build a plan, don't run it |
| POST | `/api/agent/run` | plan **and** execute |
| GET | `/api/agent/runs` | run history |
| GET/POST | `/api/data/records` | list / create records |
| GET | `/api/data/usernames-bio` | scraped handles + bios (`usernames_bio.json`); `?exclude_categorized=true` drops any handle that already has a category |
| GET | `/api/data/usernames-categories` | categorized handles + category tally, `used_count` joined from the bio store (read-only) |
| DELETE | `/api/data/records/{id}` | delete a record |
| POST | `/api/campaign/plan` | build a manual review queue from saved Instagram names |
| POST | `/api/automation/open` | **launch the visible Chrome window** |
| POST | `/api/automation/goto` | navigate the open window |
| POST | `/api/automation/screenshot` | capture the page |
| POST | `/api/automation/close` | close the window |

## Config (`.env`)

| Key | Default | Notes |
| --- | --- | --- |
| `PORT` | `4000` | |
| `DB_FILE` | `data/store.json` | relative to `backend/` |
| `PLAYWRIGHT_HEADLESS` | `false` | `false` = you see the window |
| `PLAYWRIGHT_CHANNEL` | `chrome` | or `chromium` for the bundled build |
| `PLAYWRIGHT_START_URL` | `https://www.instagram.com` | where `Open Chrome` lands; empty = blank window |
| `IG_USERNAME` / `IG_PASSWORD` | *(empty)* | optional — only for the `Log in` button |
| `PLAYWRIGHT_USER_DATA_DIR` | `data/chrome-profile` | real profile — log in once, stay logged in |
| `PLAYWRIGHT_CLEAN_LAUNCH` | `true` | no automation infobar, `navigator.webdriver` undefined |

## Automation sequence

`🖱️ Type in address bar` runs the ticked steps in order (all on by default):

1. **Open Chrome** — a blank window
2. **New Tab** — moves the pointer onto Chrome's **+** and clicks it
3. **Type in address bar** — types the URL with the real cursor, then Enter
4. **Type credentials** — types username + password into the Instagram login
   form, *with* the Config panel's delete-and-retype corrections. It does not
   submit; press Log in yourself, or use Accounts → Log in selected.

Step 4 takes its credentials from the account picked in the dropdown, or from
`IG_USERNAME`/`IG_PASSWORD` in `.env` when that's left on `.env`. Corrections
are ON here and OFF for step 3 — the login form has no inline autocomplete to
fight with. The typed values are read back out of the fields and compared.

Each box shows its state as it goes: **blue** waiting, **amber + spinner**
running, **green ✓** done, **red ✕** failed, **grey** unticked. A failure stops
the run and leaves the later steps untouched.

The UI drives the steps one call at a time rather than in a single backend
request — one request only returns once everything has finished, which cannot
drive a live spinner.

Untick any of them: leave `Open Chrome` off to reuse the window that's already
up, or run step 4 alone against a login page you're already on.

**Every queued account gets its own four steps**, drawn under its row — so with
three accounts you can read all three at once (one mid-run, one failed at step
2, one still waiting) instead of only whichever is running now. The checkboxes
at the top are the plan; the per-account strips are what happened.

Step 2 **verifies a tab actually appeared** rather than assuming, and retries up
to **5 times**. The + click is genuinely flaky: a just-launched window swallows
the first click, and the button slides along the tab strip as tabs open, so a
stale aim lands on a tab instead. Each attempt re-finds the button from scratch
and aims at its centre (the button's rect reaches the top of the window, so
picking anywhere inside it can land on the title bar). Without the tab-count
check a miss is silent — and the URL would be typed into the old tab. The new tab is then handed to Playwright, and after Enter the page handle
is re-synced: Chrome's new-tab page swaps renderers when it navigates, which
otherwise strands us on the stale `chrome://new-tab-page` object.

## Typing in the address bar (real cursor)

`Type in address bar` types the URL into Chrome's address bar at human pace and
presses Enter, using your **actual mouse and keyboard**.

It only moves the pointer when it has to. Chrome parks the caret in the address
bar on a new tab, so after step 2 it just starts typing (`clicked: false`). When
the caret is on a loaded page it travels to the box and clicks — then checks the
box actually took focus, because a click that misses leaves the keystrokes going
to the page while the omnibox keeps the old URL.

URLs are forgiving of typed spaces (`www. instagram.com` → `www.instagram.com`);
a space can't appear in a real URL, so it's always a typo. Text that isn't a URL
at all (`instagram`, `hello world`) is still rejected.

This needs OS-level input, not Playwright: Playwright drives the *page*, and
the omnibox is browser chrome — outside the DOM. `page.mouse` is
viewport-relative and Ctrl+L via Playwright lands in the page. So
`backend/automation/cursor.py` uses pywinauto (to find the address bar via
UIA) plus pyautogui (to move the real pointer).

What that costs you:

* **it takes over the mouse and keyboard** for a few seconds — hands off. Slam
  the pointer into a screen corner to abort (pyautogui failsafe);
* Windows only. Everything else in the app works without it;
* needs `PLAYWRIGHT_HEADLESS=false` — a hidden window has no address bar.

Two things it is careful about:

* **it only drives *our* Chrome.** The window is matched by the PID of the
  process whose command line carries our profile dir. Matching on window class
  is not enough — Electron apps (VS Code) also report `Chrome_WidgetWin`, and
  without the PID check it would happily type into your personal browser.
* **corrections are forced off here**, whatever the Config panel says. The
  omnibox inline-autocompletes and keeps the completion selected, so a
  backspace eats the selection rather than the last character and you navigate
  somewhere else. The typed text is read back out of the omnibox and compared
  before Enter is pressed; on a mismatch it retries once, then refuses.

## Output drawers

Panels that produce raw output (Agent, Automation, Instagram, Accounts, Config)
keep it in a drawer rather than a black box taking up half the panel. The
`>_` button at the right of the panel head slides it out down the left side;
it has its own ✕ to close.

A blue dot on `>_` means output landed while the drawer was shut, so a result
never disappears silently. Opening clears it.

The drawers are built in `buildTerminal()` (frontend/app.js) at load, from
whatever panels contain a `.out` — rather than the same markup copied into six
panels, where it would drift.

## Activity panel

A live feed of everything the app is doing, from every panel. An HTTP
middleware logs each `/api/*` call, and the slow/interesting operations
(browser launch, per-account login, agent steps) log semantic events of their
own — a request line alone won't tell you *why* a login took 40 seconds.

* streams over server-sent events, so it's live with no polling; `EventSource`
  reconnects by itself if the server restarts
* filter by source, or show warnings/errors only; `follow` pins to the newest
* in-memory ring buffer (600 events) — a window onto the running process, not
  an audit trail. Nothing is written to disk.

Endpoints polled on a timer (`/api/status`, health checks) are deliberately not
logged, or they'd bury the real events. The activity endpoints don't log
themselves either — that would feed back on itself.

## Campaign planner

The Campaign panel builds a manual review queue from Data records with
`type="instagram-name"`.

* `Start Campaign` saves a `campaign-draft` record with the selected targets
  and a message preview
* the queue is shown in the UI with copy buttons so you can review each name by
  hand
* it does not open Instagram Direct or send messages automatically

Use `Visit insta` separately if you still want to collect visible usernames
into the Data panel.

## Config panel

Tunes how credentials are typed. Saved in the JSON store, so changes survive a
restart without touching `.env`.

| Setting | Default | Meaning |
| --- | --- | --- |
| Speed | `120` | ms between keystrokes |
| Jitter | `60` | extra `0…n` ms per key, so the pace isn't robotic |
| Correct every | `8` | trigger a delete-and-retype after this many characters |
| Remove min/max | `1`–`3` | how many characters to delete, picked at random |
| Corrections | on | off = straight through, no backspaces |

`Preview` dry-runs the plan with no browser and shows a trace
(`test.use«er@exampl«««mple.com`, where `«` is a backspace) plus whether the
result still matches the input.

The planner in `backend/automation/typing.py` is a pure function, which is why
the preview can be exact. **Invariant: however many characters get deleted and
retyped, the field must end up with the original text.** Verified over ~2k
generated plans, including hostile settings; out-of-range values are clamped
(`min > max` is swapped, not rejected).

## Sessions

**The session IS the Chrome profile.** Instagram's `sessionid` cookie lives in
`backend/data/profiles/acct-<id>/Default/Network/Cookies`, which is a SQLite
file — so `backend/sessions.py` reads it directly and can answer "is this
account still logged in?" in ~5ms, offline, without launching a browser. That's
what makes the startup sweep and Check Login Status cheap. Cookie *values* are
DPAPI-encrypted and never touched; presence and expiry are enough.

Disk is the source of truth, never the stored flag — a session that expired
while the app was closed reads as Logged Out on the next boot.

**There are two kinds of profile.** Each account has its own
(`profiles/acct-<id>`), *and* there is a default one
(`PLAYWRIGHT_USER_DATA_DIR`, `data/chrome-profile`) that `Open Chrome` uses
when no account is active. The default profile collects its own Instagram
login, so **Clear All Sessions deletes it too** — otherwise the app could still
open already logged in after a "clear". The Accounts panel shows its state in
its own banner, since it has no card of its own.

Clearing **verifies the delete and admits failure**. `rmtree(ignore_errors=True)`
is a trap: Windows refuses to delete files a live Chrome holds, and the error
is swallowed — reporting "cleared" while the session sits on disk. Clearing now
closes Chrome, retries, checks the directory is gone, and returns **409** with
the offending paths if anything survived.

**Status can read `Unknown`.** While Chrome is running on a profile, Windows
locks its cookie store and it cannot be read at all. That reports Unknown, never
"Logged Out" — a read failure is not evidence of a missing session, and saying
otherwise would hide a live one. Close Chrome and re-check.

| Button | Does |
| --- | --- |
| **Log in selected** | hands the ticked accounts to the Automation panel and runs the sequence there |
| **New Login** | drops the stored session first, then the same handoff — a real fresh login |
| **Check Login Status** | **opens** each ticked account's stored session in its own window and verifies it there — the window stays open |
| **Clear All Sessions** | deletes every session directory; accounts and credentials are kept |

`Log in selected` and `New Login` don't log in behind your back. They **queue**
the ticked accounts into the **Automation** panel — every one listed with its
mail, masked password (👁 to reveal), and session status — and stop there.

**Nothing launches until you press Start Processing.** The sequence seizes your
real mouse and keyboard, so it must never begin on a panel switch. Drop an
account from the queue with ✕, or click a row to make it the active one.

**Each account gets its own window, and they all stay up.** The runner keeps a
window per profile rather than one global browser, so account 2 opening no
longer closes account 1 — you end with N Chrome windows side by side, one
logged into each account. Step 1 passes the account id so its login is saved
against *that* account; without it every session landed in the shared default
profile, overwriting the last.

Start Processing works through the queue in order (the steps reset visibly
between accounts). **A failure never closes a window and never stops the
queue** — the broken login is the one you want left on screen — so the run
carries on and reports `2/3 succeeded · failed: x`. `Close browser` closes them
all.

The `POST /api/accounts/{id}/new-login` endpoint still does the whole thing
server-side if you want it scripted.

Each card shows account name, login, platform, status, created, last used,
expiry, and storage path. Expiry tops out ~400 days out — that's Chrome's
cookie cap, not ours.

### Yellow cards = session open on screen

`Check Login Status` opens each ticked account's session in **its own window**
and checks the page itself, then leaves the window up. While that window is
open the card is **yellow** with a 🪟, and:

* it **cannot be selected for login** — it already is logged in, and a second
  login would fight its own window. Ticking it says so instead;
* it is dropped from the selection automatically if it was already ticked;
* it stays open until you **⏻ log out** (closes the window *and* deletes the
  session) or close the window by hand. The panel polls while it's on screen,
  so closing a window clears the yellow without hitting Refresh.

So the yellow cards are the accounts you're logged into, and the rest are the
ones still needing a login.

The check is live rather than a cookie-file read for a reason: the window has
to open anyway, and once Chrome holds the profile the cookie store is locked
and unreadable — so the page itself is the only honest authority. While a
window is open the card reports that live result rather than "Unknown".

### The two panels are linked

Clicking a card in **Accounts** makes it the *active* account; the
**Automation** dropdown follows it, and vice versa. It's kept in
`localStorage`, so it survives a reload.

Automation reacts to the active account's session:

* **valid session** → the login section says login will be skipped and unticks
  step 4;
* **no session** → it shows the account's details, explains why, and ticks
  step 4 so its credentials get typed.

## Accounts panel

Stores several Instagram accounts and **one saved session each**. Each account
gets its own Chrome profile at `backend/data/profiles/acct-<id>`, because a
Chrome profile holds exactly one Instagram session — share a profile and each
login silently evicts the last one. Verified: a cookie planted in account 1's
profile is invisible to account 2, and still there when you switch back.

* click a card to select it (blue + tick), `Log in selected` runs them **one at
  a time** — a profile is locked to a single Chrome process
* 👁 fetches the password only when clicked; it is not in the list response
* 🗑 removes the account **and its saved session**
* credentials type in at human pace (~160ms/char)

**Passwords are stored in plain text** in `backend/data/store.json` (gitignored,
local only). This is a convenience store, not a vault — anything with read
access to the folder can read them. Don't put an account here you couldn't
afford to lose.

Human-paced typing is **not** a cloaking trick and won't stop a challenge —
Instagram fingerprints far more than key timing. It's there because the login
form is React-controlled and only enables its submit button after real
keystroke events. And note: **several accounts logging in from one IP is the
strongest ban signal Instagram has.** Go slowly.

## Instagram session

**You log in once. It is remembered.** The session lives in the Chrome profile
at `backend/data/chrome-profile`, so `sessionid` survives restarts and
Instagram stops asking.

Verified: every cookie Instagram sets (`csrftoken`, `ig_did`, `mid`, `datr`, …)
survives a full browser close/reopen. `sessionid` is stored the same way.

Two ways to get that first session:

1. **By hand (recommended).** Click `Open Instagram`, log in in the window.
   Done — leave `IG_USERNAME`/`IG_PASSWORD` empty.
2. **With the `Log in` button.** Set `IG_USERNAME`/`IG_PASSWORD` in `.env`.
   It checks whether you're already logged in and does nothing if so.

Either way, 2FA and "suspicious login" challenges must be completed by hand in
the Chrome window — Instagram gates those deliberately, and typed-in
credentials make a challenge *more* likely, not less. `GET /api/instagram/status`
tells you where you stand.

To sign out / start fresh, delete `backend/data/chrome-profile`.

**One window at a time:** the profile is locked while in use. Two processes
using it at once fails with a profile-lock error.

## The Chrome window

`Open Chrome` lands on `PLAYWRIGHT_START_URL` (Instagram by default); blank that
setting for an empty window. Type a URL and hit `Navigate` to go elsewhere.

It launches a **persistent context** — a real Chrome with a real on-disk
profile, not a throwaway debug session:

* no *"Chrome is being controlled by automated test software"* infobar;
* `navigator.webdriver` is `false`, not `true`;
* cookies/logins persist in `backend/data/chrome-profile`, so an Instagram
  login survives restarts. Delete that folder to start clean.

Because the profile is locked while in use, one window at a time. If you see a
profile-lock error, close the leftover Chrome or point
`PLAYWRIGHT_USER_DATA_DIR` somewhere else.

Set `PLAYWRIGHT_CLEAN_LAUNCH=false` to go back to a stock automation launch.

## Adding a tool

Append a `Tool(...)` to `TOOLS` in `backend/agent/tools.py`. The registry, the
planner's prompt, and the UI's Tools panel all read from that list, so nothing
else needs editing.
