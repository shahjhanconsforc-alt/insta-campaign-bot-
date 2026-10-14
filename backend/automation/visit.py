"""Campaign visit - slowly scroll Instagram home and collect visible usernames."""
import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError

from backend import activity, config, control, sessions, settings
from backend.automation import instagram as ig
from backend.automation import playwright_runner as browser
from backend.automation import typing as human_typing

HOME = "https://www.instagram.com/"
DEFAULT_DURATION_S = 90.0

NO_WINDOW = (
    "No Chrome window is open for that account. Open its session first - "
    "Accounts -> Check Login Status opens the logged-in one."
)


# Live state of a scrape, so the panel can show names as they land instead of
# nothing for 90s. Keyed by account: two accounts can scrape at once, and a
# single shared dict would blend their names together.
_progress: dict[int | None, dict] = {}


def visit_progress(account_id: int | None = None) -> dict:
    """What the scrape for this account has found so far."""
    state = _progress.get(account_id)
    if not state:
        return {"running": False, "names": [], "count": 0, "scrolls": 0,
                "elapsed": 0, "profiles": []}
    # `profiles` is copied like `names`: the scrape appends to it while this is
    # being serialised, and a live list mutating mid-encode would blow up.
    return {**state, "names": list(state["names"]),
            "count": len(state["names"]),
            "profiles": list(state.get("profiles") or [])}


# Search-Scrapping: several searches back to back, so the panel needs each
# round's names as they land rather than everything at the end. Keyed by account
# for the same reason as above — two accounts search at once, with their own
# random seeds, and one shared dict would blend their results.
_search: dict[int | None, dict] = {}


def search_progress(account_id: int | None = None) -> dict:
    """What Search-Scrapping on this account has found so far."""
    state = _search.get(account_id)
    if not state:
        return {"running": False, "names": [], "count": 0, "round": 0,
                "rounds": 0, "seed": None, "seeds": [], "per_round": [],
                "profiles": []}
    return {**state, "names": list(state["names"]), "count": len(state["names"]),
            "seeds": list(state["seeds"]), "per_round": list(state["per_round"]),
            "profiles": list(state.get("profiles") or [])}


# Same idea for the campaign: the request runs for minutes, so the panel needs
# to see each name's outcome as it lands rather than all of them at the end.
_campaign: dict[int | None, dict] = {}


def campaign_progress(account_id: int | None = None) -> dict:
    """Per-name outcomes for the campaign on this account, so far."""
    state = _campaign.get(account_id)
    if not state:
        return {"running": False, "results": [], "done": 0, "total": 0,
                "opened": 0, "written": 0, "sent": 0, "current": None,
                "waiting": False, "next_batch_in": 0, "next_batch_total": 0,
                "batch_no": 0, "batches": 0}
    return {**state, "results": list(state["results"])}


def clear_progress() -> dict:
    """Forget what finished runs left behind, so a fresh start reads empty.

    Only ever called with nothing running (the route checks), so this can't
    pull the state out from under a live job.
    """
    scrapes, campaigns = len(_progress), len(_campaign)
    _progress.clear()
    _campaign.clear()
    return {"cleared_scrapes": scrapes, "cleared_campaigns": campaigns}


def account_page(account_id: int | None):
    """The Page for one account's window.

    Everything here goes through this rather than browser.current_page(): the
    'current' window is whichever was touched last, so with two accounts running
    at once it flips under you and one job ends up driving the other's window.
    Given an account, we look its window up by profile and can't be confused.
    """
    if account_id is None:
        if not browser.is_open():
            raise RuntimeError(NO_WINDOW)
        return browser.current_page()
    if not browser.is_open(sessions.profile_dir(account_id)):
        raise RuntimeError(NO_WINDOW)
    return browser.page_for(sessions.profile_dir(account_id))
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
RESERVED_PATHS = {
    "about",
    "accounts",
    "activity",
    "ad_settings",
    "accounts/login",
    "accounts/emailsignup",
    "accounts/password/reset",
    "accounts/confirm_email",
    "api",
    "challenge",
    "direct",
    "explore",
    "graphql",
    "help",
    "legal",
    "media",
    "oauth",
    "p",
    "privacy",
    "reel",
    "reels",
    "settings",
    "stories",
    "terms",
    "tv",
    "web",
}

_RESERVED_JSON = json.dumps(sorted(RESERVED_PATHS))


def _on_instagram(page) -> bool:
    try:
        return "instagram.com" in (page.url or "")
    except Exception:  # noqa: BLE001
        return False


def _is_home_page(page) -> bool:
    try:
        parsed = urlparse(page.url or "")
        host = (parsed.netloc or "").lower()
        return host.endswith("instagram.com") and parsed.path in ("", "/")
    except Exception:  # noqa: BLE001
        return False


# ── Readiness polling: wait for the page, not the clock ──────────────────
#
# Every fixed `wait_for_timeout(2500)` in a scrape spends its FULL 2.5s on every
# single step, whether the page was ready in 200ms or genuinely needed the lot.
# Multiplied by a navigation per username, that blind waiting was where nearly
# all of the scrape's wall-clock went. Polling a cheap JS predicate several
# times a second instead returns the moment the page really is ready and only
# spends the whole budget when the page really is slow — same reliability, a
# fraction of the time.
READY_POLL_MS = 90          # how often a readiness predicate is re-checked


async def _wait_ready(page, predicate_js: str, cap_ms: int, floor_ms: int = 0,
                      arg=None) -> bool:
    """Poll `predicate_js` — a JS arrow returning truthy once the page is ready —
    until it's true or `cap_ms` has passed. `floor_ms` is a small settle spent up
    front regardless, so a page that reports ready instantly still gets a beat to
    paint. `arg` is handed to the predicate, so a gate can be told WHAT it is
    waiting for (a username, say) instead of settling for "something painted".
    Returns True if the predicate came back truthy in time.

    A failing evaluate (the page is mid-navigation) is not an answer — it just
    means try again on the next poll.
    """
    if floor_ms > 0:
        await page.wait_for_timeout(floor_ms)
    end = time.monotonic() + max(0, cap_ms) / 1000
    while True:
        try:
            if await page.evaluate(predicate_js, arg):
                return True
        except Exception:  # noqa: BLE001 — mid-navigation; the next poll retries
            pass
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(READY_POLL_MS / 1000)


# The home feed has painted once a post or a profile link is in the DOM.
_FEED_READY_JS = """() => {
  const main = document.querySelector('main') || document.body;
  return !!main.querySelector('article, a[href^="/"] img, svg[aria-label="Home"]');
}"""

# The profile's own bio/category/link block. Instagram's class names are
# obfuscated but the "_a…" family is stable across deploys, and each role has its
# own discriminating pair:
#     _aacy _aad6   the category line      ("Public figure")
#     _aacx _aad7   the bio text           ("Husband to @feistylittlej …")
#     _aacw _aada   the external link      ("www.livingr3.com and 2 more")
# Match the CLASS, not the tag. Instagram ships the category as a <div>, the full
# name as an <h1> carrying the category's own classes, the bio as a <span>, and
# the link as a <div> inside a <button> — and it moves roles between elements
# from deploy to deploy while the pairs stay put. `_aad7` alone identifies the
# bio, so a tag-agnostic match on it survives the bio becoming a <div> tomorrow;
# `span._aacx` stays as a second anchor and can't catch the link, which is a div.
BIO_SEL = '._aad7[dir="auto"], span._aacx[dir="auto"]'
# The category is a <div>; the <h1> beside it carries the SAME pair and holds the
# full name, so the div/h1 split is what keeps those two apart.
BIO_CATEGORY_SEL = 'div._aacy[dir="auto"], div._aad6[dir="auto"]'
BIO_NAME_SEL = 'h1[dir="auto"]'
# A single external link is an <a> through Instagram's redirector. TWO OR MORE
# collapse into a <button> whose label reads "www.example.com and 2 more" — no
# <a> anywhere — which is why `link` came back null on every multi-link profile.
BIO_LINK_SEL = 'a[href*="l.instagram.com"], a[rel~="me"]'
BIO_LINK_TEXT_SEL = 'div._aada[dir="auto"], div._aacw[dir="auto"]'
BIO_THREADS_SEL = 'a[href*="threads.com/@"], a[href*="threads.net/@"]'

# A profile is ready once the header it painted belongs to the username we asked
# for — see the comment inside. Ceilings: the header itself, then a short second
# look for the bio block, which paints a beat behind the stats.
PROFILE_READY_CAP_MS = 8000
PROFILE_BIO_SETTLE_MS = 1500

_PROFILE_READY_JS = """(name) => {
  // Ready means THIS profile has rendered — not merely that some chrome exists.
  //
  // Opening a profile from the feed or the search flyout is a CLIENT-SIDE
  // navigation: the document we came FROM survives, and with it every <meta
  // og:*> it carried, until Instagram gets round to swapping them. The old gate
  // accepted any og:description longer than 10 characters, so on a click it fired
  // against the FEED's meta immediately — before the profile had painted a single
  // pixel — and the read that followed found no bio block at all. That is where
  // the blank bios came from, and it is why every check below is tied to the
  // username we asked for rather than to "something is on screen".
  const low = (name || '').toLowerCase();
  if (!low) return !!(document.querySelector('%BIO%') || document.querySelector('%CAT%'));
  const path = location.pathname.replace(/^\\/+|\\/+$/g, '').toLowerCase();
  if (path !== low) return false;                  // the URL hasn't turned over yet
  const t = document.body ? document.body.innerText || '' : '';
  // Private or gone is an answer too — there is nothing more to wait for.
  if (/This\\s+account\\s+is\\s+private|Sorry, this page|isn't available/i.test(t)) return true;
  // The stats row's own links carry the username, so finding it proves the header
  // on screen is THIS profile's and not the one we were looking at a moment ago.
  if (document.querySelector('a[href="/' + low + '/followers/"]')) return true;
  // Same for the avatar, whose alt is "<name>'s profile picture".
  for (const img of document.querySelectorAll('header img[alt], main img[alt]')) {
    const alt = (img.getAttribute('alt') || '').toLowerCase();
    if (alt.includes(low) && alt.includes('profile picture')) return true;
  }
  // And the og meta — but only once it names this account.
  const og = document.querySelector('meta[property="og:description"]');
  if (og && (og.getAttribute('content') || '').toLowerCase().includes('(@' + low + ')')) return true;
  return false;
}""".replace("%BIO%", BIO_SEL).replace("%CAT%", BIO_CATEGORY_SEL)

# The header's stats paint a beat ahead of the bio block, so a read fired the
# instant the gate above opens can still miss the bio on a slow profile. This is
# a SHORT second look. An account with no bio, no category and no link is
# perfectly ordinary, so it is a ceiling to spend, never a requirement to meet.
_PROFILE_BIO_READY_JS = """() => !!(
  document.querySelector('%BIO%') || document.querySelector('%CAT%')
  || document.querySelector('%LINK%') || document.querySelector('%LINKTEXT%')
)""".replace("%BIO%", BIO_SEL).replace("%CAT%", BIO_CATEGORY_SEL) \
    .replace("%LINKTEXT%", BIO_LINK_TEXT_SEL).replace("%LINK%", BIO_LINK_SEL)

# What the search flyout is showing right now: the usernames of every avatar'd
# result row (the same shape _HARVEST_SEARCH_JS reads), plus whether Instagram is
# still querying. The username list doubles as a FINGERPRINT — comparing it with
# the one taken before typing is how _wait_search_results tells this seed's
# results from the previous seed's, which stay on screen while the new query is
# in flight.
_SEARCH_RESULTS_JS = """() => {
  const names = [];
  for (const a of document.querySelectorAll('a[href^="/"]')) {
    if (a.closest('nav')) continue;
    if (!a.querySelector('img[alt*="profile picture" i]')) continue;
    const p = (a.getAttribute('href') || '').split('?')[0].split('/').filter(Boolean);
    if (p.length === 1) names.push(p[0].toLowerCase());
  }
  const busy = !!document.querySelector('svg[aria-label*="Loading" i]');
  return { key: names.join(','), n: names.length, busy };
}"""


async def _search_results_key(page) -> str:
    """Fingerprint of the results the search flyout is showing right now — take
    it BEFORE typing a seed, hand it to _wait_search_results afterwards."""
    try:
        snap = await page.evaluate(_SEARCH_RESULTS_JS)
    except Exception:  # noqa: BLE001 — no panel to read is an empty fingerprint
        return ""
    return str((snap or {}).get("key") or "")


async def _type_search_seed(page, seed: str) -> None:
    """Type a seed into the focused search box one character at a time at a
    human, jittered pace. page.keyboard.type()'s own `delay` is a fixed metronome;
    a real person's gaps between keys are uneven, and the difference is visible."""
    for ch in seed:
        await page.keyboard.type(ch)
        await page.wait_for_timeout(random.uniform(*SEARCH_TYPE_DELAY_RANGE_MS))


async def _wait_search_results(page, before: str, cap_ms: int | None = None) -> bool:
    """Wait until the flyout holds the results for the seed just typed.

    `before` is the fingerprint from _search_results_key taken before typing.
    Instagram leaves the previous query's rows on screen while it fetches the new
    ones, so "there are rows in the panel" is not an answer — "the rows CHANGED,
    nothing is still loading, and they've stopped moving" is. Two identical reads
    in a row is what makes the harvest that follows see the whole list rather than
    its first arrival.

    True once this seed's results are up. False if the seed genuinely returned
    nothing, or the panel never turned over inside `cap_ms`.
    """
    await page.wait_for_timeout(SEARCH_DEBOUNCE_MS)   # Instagram hasn't even asked yet
    now = time.monotonic()
    end = now + max(0, SEARCH_SUGGEST_WAIT_MS if cap_ms is None else cap_ms) / 1000
    empty_ok_at = now + SEARCH_EMPTY_MIN_S            # "no results" needs to settle too
    stable, hits, empties = "", 0, 0
    while True:
        try:
            snap = await page.evaluate(_SEARCH_RESULTS_JS) or {}
        except Exception:  # noqa: BLE001 — mid-render; the next poll retries
            snap = {}
        key, count, busy = str(snap.get("key") or ""), snap.get("n") or 0, snap.get("busy")
        if busy:
            stable, hits, empties = "", 0, 0
        elif count and key != before:
            hits = hits + 1 if key == stable else 1
            stable, empties = key, 0
            if hits >= 2:
                return True
        elif not count and time.monotonic() >= empty_ok_at:
            # The panel cleared and stayed cleared: this seed has no matches. Say
            # so now instead of spending the whole ceiling on it.
            empties += 1
            stable, hits = "", 0
            if empties >= 2:
                return False
        else:
            stable, hits = "", 0
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(READY_POLL_MS / 1000)


async def _ensure_home(page) -> None:
    """Make sure the visible page is the Instagram home feed."""
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await _wait_ready(page, _FEED_READY_JS, 2500)
        return

    if not _is_home_page(page):
        activity.log("visit", "Instagram window is on another page - returning Home", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await _wait_ready(page, _FEED_READY_JS, 2500)
        return

    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(250)


async def _collect_visible_usernames(page) -> list[str]:
    """Return profile-like usernames that are visible right now."""
    script = """() => {
      const reserved = new Set(%s);
      const seen = new Set();
      const out = [];
      const isVisible = el => {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
          return false;
        }
        const rect = el.getBoundingClientRect();
        return rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
      };
      for (const node of document.querySelectorAll('a[href], span[dir="auto"]')) {
        const link = node.matches('a[href]') ? node : node.closest('a[href]');
        if (!link) continue;
        if (!isVisible(node) && !isVisible(link)) continue;
        let path = '';
        try {
          path = new URL(link.href, location.origin).pathname;
        } catch {
          continue;
        }
        const parts = path.split('/').filter(Boolean);
        if (parts.length !== 1) continue;
        const name = parts[0];
        const lower = name.toLowerCase();
        if (!/^[A-Za-z0-9._]{1,30}$/.test(name)) continue;
        if (reserved.has(lower)) continue;
        if (seen.has(lower)) continue;
        seen.add(lower);
        out.push(name);
      }
      return out;
    }""" % _RESERVED_JSON

    try:
        names = await page.evaluate(script)
    except Exception:  # noqa: BLE001
        return []
    return [str(name) for name in names if name and USERNAME_RE.fullmatch(str(name))]


def _add_names(seen: set[str], names: list[str], batch: list[str]) -> None:
    """Record a batch of usernames in order, skipping duplicates."""
    for name in batch:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)


# How the scroll handles the bottom of the feed and a stuck page. The wait is a
# CEILING, not a fixed cost: _wait_for_growth returns the instant the page grows,
# so a quick poll here means a normal lazy-load costs a few hundred ms and only a
# genuinely dead bottom spends the whole budget.
BOTTOM_WAIT_S = 3.0        # at the bottom, wait UP TO this long for new posts
BOTTOM_POLL_S = 0.2
STUCK_RELOADS_AT = 3       # this many no-growth bottoms in a row = stuck -> refresh

# Human-like backtracking: a real reader scrolls down a while, then flicks back up
# a little to re-read, without reloading the page. After this many forward steps the
# scroll glides UP one or two steps (no refresh), then carries on down.
BACKSCROLL_EVERY = 5              # back-scroll once per this many forward steps
BACKSCROLL_STEPS = (1, 2)         # glide back one or two steps, picked at random

# The batched scrape's stall guard: how many steps in a row may add nothing new
# before a batch is abandoned, and how many whole batches may add nothing before
# the run ends as "dried up".
#
# Each idle batch now REVIVES its surface first (reloads the feed / reopens
# Search — see on_revive), so these count "tried to recover and still got
# nothing", not plain bad luck. That is why they are higher than the 8/3 they
# replace: back then an idle batch was simply written off, and an unlimited run
# ended the moment the feed hit its bottom, however much a reload would have
# served up.
SCRAPE_IDLE_STEPS = 12
SCRAPE_IDLE_BATCHES = 5

# Search-burst pacing.
#
# This used to type at a flat 40ms per character — a 1-5 character seed was in
# the box inside 200ms — and then call the panel "ready" as soon as it held ANY
# avatar'd result row. From round two on, those rows are the PREVIOUS seed's
# results, which stay on screen the whole time Instagram is querying the new one.
# So the gate opened instantly on stale rows, the harvest re-read names it
# already had, and the next round fired at once: type, clear, type, clear, five
# or six bursts a second, with the occasional one landing late enough to catch a
# genuine refresh. That is the "writes random letters very fast, then one shows
# results" behaviour.
#
# Both halves are fixed: a human, jittered per-character delay, and a gate that
# waits for results belonging to THIS seed (see _wait_search_results).
SEARCH_TYPE_DELAY_MS = 40            # Actions' Search+Follow — one deliberate query
SEARCH_TYPE_DELAY_RANGE_MS = (110, 240)   # per character, jittered — the harvest
SEARCH_PRETYPE_PAUSE_S = (0.25, 0.6)      # beat between clearing the box and typing
SEARCH_READ_PAUSE_S = (0.7, 1.6)          # dwell over the results before the next seed
SEARCH_SUGGEST_WAIT_MS = 5000        # ceiling for THIS seed's results to land
SEARCH_DEBOUNCE_MS = 350             # Instagram's own debounce before it even asks
SEARCH_EMPTY_MIN_S = 1.2             # before believing "this seed has no results"
PANEL_SETTLE_MS = 120             # the beat after an Escape, for the flyout to fold away

# Bio mode dwells on each profile it opens for a moment before moving on, so the
# visits aren't machine-gun-regular. This is deliberately its OWN short range and
# not the scroll pause: the pause knob sets the rhythm of SCROLLING, and reusing
# it here charged every profile a second dwell on top of the page's own load.
BIO_DWELL_RANGE = (0.15, 0.45)    # seconds


async def _refresh_feed(page) -> None:
    """Reload the home feed fresh and wait for it to settle. Used at the start of
    every scrape and whenever the page gets stuck."""
    try:
        await page.reload(wait_until="domcontentloaded")
    except Exception:  # noqa: BLE001
        try:
            await page.goto(HOME, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
    # Up to 2.5s, but back the moment the feed has posts in it.
    await _wait_ready(page, _FEED_READY_JS, 2500)


async def _wait_for_growth(page, prev_height: float, deadline: float) -> bool:
    """At the bottom, wait for Instagram to lazy-load more posts — the page grows
    taller when it does. True if it grew before the wait (or the run) ran out."""
    end = min(time.monotonic() + BOTTOM_WAIT_S, deadline)
    while time.monotonic() < end:
        await asyncio.sleep(BOTTOM_POLL_S)
        try:
            height = await page.evaluate("() => document.documentElement.scrollHeight")
        except Exception:  # noqa: BLE001
            return False
        if height > prev_height + 4:
            return True
    return False


# Move one step's distance CONTINUOUSLY across that step's pause, a few pixels
# per animation frame, instead of jumping the whole way and then sitting still.
# The distance and the elapsed time are identical either way — this only changes
# how the movement looks, and gives a small step a genuinely fine, even glide.
#
# The promise is guarded by a setTimeout: requestAnimationFrame stops firing in a
# minimised or background window, and without the guard evaluate() would hang for
# the rest of the run. setTimeout is throttled there too but still fires, so the
# call always returns and the caller closes any shortfall in one hop.
_GLIDE_JS = """async ({ fraction, ms }) => {
  const doc = document.documentElement;
  const before = window.scrollY;
  // 60px floor, not 240: the old one silently swallowed everything below ~27%
  // of the window, so the whole bottom half of the 5-100% range did the same
  // thing. A glided step still makes real progress when it's small.
  const delta = Math.max(60, Math.round(window.innerHeight * fraction));
  const target = before + delta;
  const report = glided => ({
    before, after: window.scrollY, target, glided, height: doc.scrollHeight,
    atBottom: window.innerHeight + window.scrollY >= doc.scrollHeight - 4,
  });

  // Hidden window: no frame clock to animate on, so jump and tell the caller to
  // spend the step's time as a plain wait. Checking up front matters — waiting
  // on the timer guard below would cost well over a second on EVERY step.
  if (document.hidden) { window.scrollTo(0, target); return report(false); }

  const started = performance.now();
  await new Promise(resolve => {
    let settled = false;
    const finish = () => { if (!settled) { settled = true; resolve(); } };
    const tick = now => {
      const p = Math.min(1, (now - started) / ms);
      window.scrollTo(0, before + delta * p);
      if (p < 1) requestAnimationFrame(tick); else finish();
    };
    requestAnimationFrame(tick);
    // Last resort for a frame clock that stalls while the page still claims to
    // be visible — without it evaluate() would hang for the rest of the run.
    setTimeout(finish, ms + 1500);
  });
  return report(true);
}"""

# The mirror of _GLIDE_JS: glide UPWARD by one step for the human backtrack. Same
# guarded animation, clamped at the top (scrollY 0) so it never overshoots.
_GLIDE_UP_JS = """async ({ fraction, ms }) => {
  const before = window.scrollY;
  const delta = Math.max(60, Math.round(window.innerHeight * fraction));
  const target = Math.max(0, before - delta);
  const dist = before - target;                    // >= 0
  const report = glided => ({
    before, after: window.scrollY, target, glided, atTop: window.scrollY <= 2,
  });
  if (dist <= 0) return report(true);              // already at the top
  if (document.hidden) { window.scrollTo(0, target); return report(false); }

  const started = performance.now();
  await new Promise(resolve => {
    let settled = false;
    const finish = () => { if (!settled) { settled = true; resolve(); } };
    const tick = now => {
      const p = Math.min(1, (now - started) / ms);
      window.scrollTo(0, before - dist * p);
      if (p < 1) requestAnimationFrame(tick); else finish();
    };
    requestAnimationFrame(tick);
    setTimeout(finish, ms + 1500);
  });
  return report(true);
}"""


async def visit_home_feed(
    duration_s: float | None = None,
    account_id: int | None = None,
) -> dict:
    """Scroll one account's Instagram home feed for the whole duration and
    collect usernames.

    That account's window must already be open. The feed is refreshed first,
    then scrolled continuously until the time is up — it waits for new posts to
    load at the bottom, and refreshes and carries on if the page gets stuck. It
    is never sent back to the top: it stops wherever it is.
    """
    page = account_page(account_id)
    # Read once per run, so a mid-run Settings edit can't change the pace halfway.
    cfg = settings.scroll()
    run_for = float(duration_s if duration_s is not None else cfg["scroll_duration_s"])
    run_for = max(60.0, min(run_for, 300.0))       # up to 5 minutes

    await _ensure_home(page)
    if not await ig.is_logged_in(page):
        raise RuntimeError(
            "Instagram is not logged in in this window. Open the saved session first."
        )

    # Requirement: refresh the feed before scrolling starts. With two accounts
    # scraping at once each reloads its own window, so both refresh at the start.
    activity.log("visit", "Refreshing the feed before scrolling", "ok")
    await _refresh_feed(page)

    pause_range = (cfg["scroll_pause_min_ms"] / 1000, cfg["scroll_pause_max_ms"] / 1000)
    step_range = (cfg["scroll_step_min_pct"] / 100, cfg["scroll_step_max_pct"] / 100)

    activity.log(
        "visit",
        f"Scrolling Instagram home feed for about {int(round(run_for))}s "
        f"(pause {pause_range[0]:g}-{pause_range[1]:g}s, "
        f"step {cfg['scroll_step_min_pct']}-{cfg['scroll_step_max_pct']}% of the window)",
        "ok",
    )

    started = time.monotonic()
    deadline = started + run_for
    seen: set[str] = set()
    names: list[str] = []
    scrolls = 0
    reloads = 0
    bottom_streak = 0      # consecutive bottoms where nothing new loaded
    fwd_since_back = 0     # forward steps since the last human backtrack

    # `names` by reference: the loop appends to it, so the panel polling
    # visit_progress() sees each name the moment it lands.
    _progress[account_id] = {
        "running": True, "names": names, "scrolls": 0, "reloads": 0,
        "elapsed": 0, "duration": run_for, "account_id": account_id,
    }

    async def harvest() -> None:
        before = len(names)
        _add_names(seen, names, await _collect_visible_usernames(page))
        added = len(names) - before
        if added:
            activity.log("visit", f"Captured {added} name(s); total {len(names)}", "ok")

    stopped = False
    try:
        # Keep scrolling until the time is up — nothing else ends the loop except
        # the Stop button. A quiet feed or a bottom no longer stops it early.
        while time.monotonic() < deadline:
            try:
                await control.checkpoint()      # Stop/Pause land here, between scrolls
            except control.Stopped:
                stopped = True
                activity.log("visit", "Scrape stopped — keeping the names found so far", "warn")
                break

            await harvest()

            # One scroll step, GLIDED rather than jumped: the step's distance is
            # spread across the step's pause, a few pixels per animation frame,
            # so the feed slides by instead of teleporting. Same distance and the
            # same wall-clock time as before — only the motion differs — so the
            # coverage and lazy-load headroom are unchanged. The smaller the
            # step, the finer the glide.
            # Any failure here means the page is wedged (navigating, frozen) —
            # refresh and carry on rather than give up.
            glide_ms = random.uniform(*pause_range) * 1000
            try:
                m = await page.evaluate(_GLIDE_JS, {
                    "fraction": random.uniform(*step_range),
                    "ms": glide_ms,
                })
            except Exception as exc:  # noqa: BLE001
                activity.log("visit", f"Page stuck ({exc}) — refreshing and continuing", "warn")
                await _refresh_feed(page)
                reloads += 1
                bottom_streak = 0
                fwd_since_back = 0          # a refresh jumps to the top; restart the count
                _progress[account_id].update(reloads=reloads)
                continue

            # A hidden window can't animate, so it jumped instead. Spend the
            # step's time as a plain wait, exactly as this loop used to, and the
            # pace stays identical whether or not anyone is watching.
            if not m["glided"]:
                await asyncio.sleep(glide_ms / 1000)
            scrolls += 1
            await harvest()

            moved = m["after"] > m["before"] + 2
            if not moved or m["atBottom"]:
                # At the bottom: wait for Instagram to lazy-load more posts.
                if await _wait_for_growth(page, m["height"], deadline):
                    bottom_streak = 0            # new posts arrived, keep going
                else:
                    bottom_streak += 1
                    # Nothing new after several waits = the page is stuck.
                    # Refresh and resume scrolling (still within the time budget).
                    if bottom_streak >= STUCK_RELOADS_AT:
                        activity.log("visit", "Feed stuck at the bottom — refreshing", "warn")
                        await _refresh_feed(page)
                        reloads += 1
                        bottom_streak = 0
                        fwd_since_back = 0      # top again after the refresh
                        _progress[account_id].update(reloads=reloads)
            else:
                bottom_streak = 0

            # Human backtrack: every few forward steps, flick back up a step or two —
            # WITHOUT refreshing — the way a real reader re-reads, then carry on down.
            # Names revealed on the way up are harvested too (dedup keeps it clean).
            fwd_since_back += 1
            if fwd_since_back >= BACKSCROLL_EVERY:
                fwd_since_back = 0
                moved = await _feed_scroll_back(
                    page, step_range, pause_range, random.randint(*BACKSCROLL_STEPS))
                if moved:
                    activity.log("visit", f"Scrolled back up {moved} step(s) — no refresh", "ok")
                    await harvest()

            _progress[account_id].update(
                scrolls=scrolls, elapsed=round(time.monotonic() - started, 1)
            )
    finally:
        # Mark it finished whatever happened — a crash that left "running" true
        # would have the panel polling a scrape that is never coming back.
        _progress[account_id].update(running=False, scrolls=scrolls,
                                     elapsed=round(time.monotonic() - started, 1))

    # Deliberately NOT scrolled back to top — leave the feed wherever it stopped.

    elapsed = round(time.monotonic() - started, 1)
    activity.log(
        "visit",
        f"Home feed scroll {'stopped' if stopped else 'done'}: "
        f"{len(names)} unique name(s) across {scrolls} scroll(s), {reloads} refresh(es)",
        "ok",
    )
    return {
        "url": page.url,
        "duration_seconds": elapsed,
        "scrolls": scrolls,
        "reloads": reloads,
        "count": len(names),
        "names": names,
        "usernames": names,
        "stopped": stopped,
    }


# ── "Both" technique: batched scroll <-> search on one continuous session ──
#
# A "Both" account collects by scrolling, then after a random-sized batch of new
# names switches to searching, then back — alternating for the whole run. The
# scroll is ONE continuous pass (never reset to the top between batches), and the
# search is an in-place burst on the sidebar Search panel, closed again before
# scrolling resumes. `_run_both_batches` owns ONLY the alternation/counting, so
# it is unit-testable with fake collectors; scrape_both_batched wires the real
# page steps into it.

async def refresh_window(account_id: int | None = None) -> dict:
    """Reload one account's window back onto a fresh Instagram home feed.

    The Scrapping panel calls this before restarting an account whose run fell
    over: a wedged page is nearly always what broke it, and starting a new run
    against that same wedged page just breaks it the same way.
    """
    page = account_page(account_id)
    await _ensure_home(page)
    await _refresh_feed(page)
    return {"url": page.url, "account_id": account_id}


async def _run_batched(*, names, seen, target, bmin, bmax,
                       start_technique, switch_enabled,
                       on_scroll, on_search, on_switch=None, on_revive=None,
                       delay_min=0.0, delay_max=0.0, on_pause=None,
                       progress=None, max_idle_steps=SCRAPE_IDLE_STEPS,
                       max_idle_batches=SCRAPE_IDLE_BATCHES):
    """Collect until `target` unique names land (or Stop) — no time limit.

    `target <= 0` means UNLIMITED: keep collecting for as long as the surfaces keep
    yielding new names. Nothing runs away — the stall guard below is what ends an
    unlimited run, exactly as it already ended a capped one that dried up early.

    Each batch gathers a random [bmin, bmax] NEW names with the current technique
    (an async step returning the usernames it just saw), then pauses for a random
    [delay_min, delay_max] seconds via `on_pause`, then — if `switch_enabled` —
    flips scroll<->search (`on_switch` opens/closes the search panel). Because
    there is no clock, a stall guard ends the run when new names dry up: a batch
    that adds nothing counts as idle, and `max_idle_batches` idle batches in a row
    stop it. Returns True if Stop was hit, else False.
    """
    technique = start_technique
    idle_batches = 0
    unlimited = target <= 0
    goal_label = "∞" if unlimited else str(target)

    def _ended(reason: str) -> None:
        """Record WHY the loop is ending. An unlimited run that finishes looks
        identical from outside whether Stop was pressed or the surfaces dried
        up — and "why did it stop?" is unanswerable after the fact, because the
        activity log is in memory only. So the reason rides the progress state
        and comes back with the result."""
        if progress is not None:
            progress.update(end_reason=reason)

    while unlimited or len(names) < target:
        batch = random.randint(bmin, bmax)
        batch_goal = len(names) + batch if unlimited else min(len(names) + batch, target)
        batch_start = len(names)
        if progress is not None:
            progress.update(technique=technique, batch_target=batch,
                            batch_start=batch_start, target=target)
        activity.log("visit",
                     f"Batch: {technique} — up to {batch} more "
                     f"({len(names)}/{goal_label} so far)", "ok")
        step = on_scroll if technique == "scroll" else on_search
        idle_steps = 0
        while len(names) < batch_goal:
            try:
                await control.checkpoint()          # Stop/Pause land between steps
            except control.Stopped:
                _ended("stopped")
                return True
            before = len(names)
            _add_names(seen, names, await step() or [])
            if len(names) == before:
                idle_steps += 1
                if idle_steps >= max_idle_steps:
                    break                           # this surface isn't yielding now
            else:
                idle_steps = 0

        # Stall guard: a whole batch that found nothing new nudges the counter;
        # enough empty batches in a row means the names have dried up — stop.
        if len(names) == batch_start:
            idle_batches += 1
            # …but first, REVIVE the surface. A home feed scrolled to its bottom
            # keeps serving the same posts forever — only a reload makes Instagram
            # rank a fresh set — and a Search panel typed into all run starts
            # repeating its suggestions. Without this an unlimited run ended the
            # moment the feed bottomed out, which looked like "the feed dried up"
            # when it had simply never been asked for more.
            if on_revive is not None and idle_batches < max_idle_batches:
                activity.log(
                    "visit",
                    f"Nothing new this batch — reloading {technique} and trying again "
                    f"({idle_batches}/{max_idle_batches})", "warn")
                try:
                    await on_revive(technique)
                except control.Stopped:
                    _ended("stopped")
                    return True
                except Exception as exc:  # noqa: BLE001 — a failed revive costs this go only
                    activity.log("visit", f"Couldn't reload {technique}: {exc}", "warn")
            if idle_batches >= max_idle_batches:
                activity.log(
                    "visit",
                    f"No new names for {max_idle_batches} batches, even after reloading "
                    f"— stopping", "warn")
                _ended("dry")
                return False
        else:
            idle_batches = 0

        if not unlimited and len(names) >= target:
            _ended("target")
            break

        # Pause between batches: stop for a random spell before the next one.
        if delay_max > 0:
            wait = random.uniform(min(delay_min, delay_max), delay_max)
            if wait > 0:
                if progress is not None:
                    progress.update(technique="pause", batch_target=0,
                                    pause_s=round(wait, 1))
                activity.log("visit", f"Pausing {wait:.1f}s before the next batch", "ok")
                if on_pause is not None:
                    try:
                        await on_pause(wait)
                    except control.Stopped:
                        _ended("stopped")
                        return True

        # Switch technique for 'both'; single-technique runs stay put. A switch
        # that can't be made (the Search flyout won't open, say) is NOT fatal: we
        # stay on the technique that is currently working and try again after the
        # next batch, rather than flipping to a surface that isn't there.
        if switch_enabled:
            nxt = "search" if technique == "scroll" else "scroll"
            switched = True
            if on_switch is not None:
                try:
                    switched = (await on_switch(technique, nxt)) is not False
                except control.Stopped:
                    _ended("stopped")
                    return True
                except Exception as exc:  # noqa: BLE001
                    activity.log("visit",
                                 f"Couldn't switch to {nxt} ({exc}) — staying on {technique}",
                                 "warn")
                    switched = False
            if switched:
                technique = nxt
    _ended("target")            # the while condition ran out — the count was met
    return False


# Auto-restart: a wedged window — the classic being "Locator.click: Timeout
# 5000ms exceeded" when the Search rail won't take a click — used to end an
# account's whole run with a red "failed —" on its card. Instead the scrape saves
# what it has collected, reloads the window, and starts that account over from
# zero, up to this many times before it finally gives up and reports the error.
SCRAPE_MAX_AUTO_RESTARTS = 5


class PageWedged(RuntimeError):
    """The window is stuck in a way a reload fixes. Raised where the scrape knows
    the page — not the request — is what went wrong, so the restart loop below
    treats it like a Playwright fault instead of a real answer such as "this
    window isn't logged in", which restarting could never fix."""


async def scrape_batched(account_id: int | None = None, technique: str = "both",
                         target_count: int = 0, batch_min: int | None = None,
                         batch_max: int | None = None, scrape_bio: bool = False) -> dict:
    """Collect usernames for one account in random-sized batches with a pause
    between each — no time limit. It stops when new names dry up, on Stop, or (if
    one was asked for) when the count is reached.

    `target_count` 0 — the default — means UNLIMITED: collect everything the feed
    and search will give up, however many that is. Pass a positive number to stop
    early at that many. An unlimited run still terminates: the stall guard ends it
    once the surfaces stop yielding anything new.

    technique:
      'scroll' — batches of feed scrolling
      'search' — batches of Search harvesting
      'both'   — alternates scroll<->search each batch

    With `scrape_bio` (the Scrapping panel's scroll/search), each NEW username is
    also opened — its profile is visited, the bio read, and {username, bio} saved
    to usernames_bio.json — exactly as the Runs profile scrape does, just sourced
    from scroll and search instead of scroll alone. Far slower (a navigation per
    name), so it's off by default: the fast name-only path (Runs' "open each
    profile" unticked) leaves it off.

    Progress (technique, current batch size, pause countdown, count/target) rides
    visit_progress(), so each account card shows what it's doing right now.
    """
    page = account_page(account_id)
    cfg = settings.scroll()
    target = max(0, int(target_count or 0))          # 0 = unlimited
    mode = technique if technique in ("scroll", "search", "both") else "both"
    bmin = int(batch_min if batch_min is not None else cfg.get("scrape_batch_min", 10))
    bmax = int(batch_max if batch_max is not None else cfg.get("scrape_batch_max", 20))
    bmin, bmax = max(1, min(bmin, bmax)), max(1, max(bmin, bmax))
    dmin = float(cfg.get("scrape_batch_delay_min", 0))
    dmax = float(cfg.get("scrape_batch_delay_max", 0))
    dmin, dmax = max(0.0, min(dmin, dmax)), max(0.0, max(dmin, dmax))

    await _ensure_home(page)
    if not await ig.is_logged_in(page):
        raise RuntimeError(
            "Instagram is not logged in in this window. Open the saved session first."
        )

    pause_range = (cfg["scroll_pause_min_ms"] / 1000, cfg["scroll_pause_max_ms"] / 1000)
    step_range = (cfg["scroll_step_min_pct"] / 100, cfg["scroll_step_max_pct"] / 100)

    started = time.monotonic()
    seen: set[str] = set()
    names: list[str] = []
    seeds: list[str] = []
    self_name = _account_username(account_id)
    self_lower = (self_name or "").lower()
    start_tech = "search" if mode == "search" else "scroll"
    # {username, bio} per visited profile, by reference in the progress state so
    # the panel's live list fills each name's bio in as it's read.
    profiles: list[dict] = []

    _progress[account_id] = {
        "running": True, "names": names, "elapsed": 0, "account_id": account_id,
        "technique": start_tech, "batch_target": 0, "batch_start": 0,
        "target": target, "mode": mode, "pause_left": 0, "seeds": seeds,
        "bios_saved": 0, "profiles": profiles,
        # Bumped each time a wedged window sends this account back to 0, so the
        # panel can show "restarted x2" rather than looking like it stalled.
        "restarts": 0, "restarting": False, "last_error": None,
        # Why the run ended: 'stopped' (the Stop button), 'dry' (the stall guard —
        # no new names for several batches) or 'target' (the count was reached).
        "end_reason": None,
    }
    prog = _progress[account_id]

    def tick() -> None:
        prog.update(elapsed=round(time.monotonic() - started, 1))

    fwd_steps = [0]        # forward scroll steps since the last backtrack (mutable for the closure)

    async def on_scroll():
        # one glided scroll step, then read whatever profiles are now visible
        glide_ms = random.uniform(*pause_range) * 1000
        try:
            m = await page.evaluate(_GLIDE_JS, {
                "fraction": random.uniform(*step_range), "ms": glide_ms})
        except Exception as exc:  # noqa: BLE001 — page wedged; refresh and move on
            activity.log("visit", f"Page stuck ({exc}) — refreshing and continuing", "warn")
            await _refresh_feed(page)
            fwd_steps[0] = 0                         # a refresh jumps us to the top; start the count over
            return []
        if not m["glided"]:                         # hidden window can't animate
            await asyncio.sleep(glide_ms / 1000)
        if not (m["after"] > m["before"] + 2) or m["atBottom"]:
            # no clock now, so give the bottom-load its normal window to fill
            await _wait_for_growth(page, m["height"], time.monotonic() + BOTTOM_WAIT_S + 5)
        collected = await _collect_visible_usernames(page)
        # Bio mode: open each NEW face's profile for its bio → usernames_bio.json,
        # stepping back to the feed after each (go_back keeps our scroll spot) so
        # scrolling carries straight on. Same per-profile step the Runs scrape runs.
        if scrape_bio:
            fresh = [n for n in collected
                     if n.lower() not in seen and n.lower() != self_lower]
            if fresh:
                # Bank the names BEFORE opening any of them. One scroll step can
                # surface 20-30 faces and each costs a real navigation to read, so
                # the visiting runs for a minute or more — and until this step
                # returned, _run_batched had added nothing, leaving every account
                # card reading "· 0" throughout. Counting them now means the list
                # fills at once and each bio drops in underneath as it is read.
                _add_names(seen, names, fresh)
                prog["bios_saved"] = prog.get("bios_saved", 0) + await _visit_profiles_and_store(
                    page, fresh, pause_range=pause_range,
                    restore=lambda: _return_to_feed(page), sink=profiles)
        # Human backtrack: every few forward steps flick back up a step or two — no
        # refresh — the way a real reader re-reads, then the next step carries on down.
        fwd_steps[0] += 1
        if fwd_steps[0] >= BACKSCROLL_EVERY:
            fwd_steps[0] = 0
            moved = await _feed_scroll_back(
                page, step_range, pause_range, random.randint(*BACKSCROLL_STEPS))
            if moved:
                activity.log("visit", f"Scrolled back up {moved} step(s) — no refresh", "ok")
        tick()
        return collected

    async def on_search():
        # one search burst (a random-shaped seed), harvest its suggestions
        # Fingerprint what's on screen BEFORE clearing: whatever the panel shows
        # while the new query is in flight is THIS, and mistaking it for the new
        # seed's results is what made the bursts fire back to back.
        before = await _search_results_key(page)
        try:
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(random.uniform(*SEARCH_PRETYPE_PAUSE_S) * 1000)
        seed = _random_search_seed()                # a random one of nine shapes
        await _type_search_seed(page, seed)         # human pace, not 40ms/char
        seeds.append(seed)
        prog.update(seed=seed)
        # Wait for THIS seed's suggestions — the panel changing, settling, and no
        # spinner — not merely for rows to be present. Back in ~600ms on a normal
        # burst; the ceiling is only spent when Instagram is genuinely slow.
        landed = await _wait_search_results(page, before)
        # Nothing of this seed's own arrived, so whatever is in the panel belongs
        # to the last burst — harvesting it would only re-count names we already
        # have. Take the empty result and let the next seed try.
        found = []
        if landed:
            try:
                found = await page.evaluate(
                    _HARVEST_SEARCH_JS, {"reserved": sorted(RESERVED_PATHS), "self": self_name})
            except Exception:  # noqa: BLE001 — a bad burst mustn't end the run
                found = []
        # Read the results the way a person would before typing the next thing.
        await page.wait_for_timeout(random.uniform(*SEARCH_READ_PAUSE_S) * 1000)

        # Bio mode: open each NEW suggestion's profile for its bio →
        # usernames_bio.json, then reopen the Search panel so the next burst can
        # type into it again (visiting a profile navigates away and closes it).
        #
        # This reopen is load-bearing in a way scroll's is not. Scroll gets its
        # surface back with go_back() — cheap and reliable. Search has to re-click
        # the sidebar rail, FROM a profile page, and that is the flaky click
        # _open_search_panel_resilient exists to fight. If it loses, the next burst
        # types into nothing, harvests nothing, and the stall guard reports "the
        # names dried up" — which is a lie, and the reason search-with-bios looked
        # like it collected far less than scroll-with-bios.
        if scrape_bio:
            fresh = [n for n in found
                     if n.lower() not in seen and n.lower() != self_lower]
            if fresh:
                _add_names(seen, names, fresh)   # count them now, not a minute later
                prog["bios_saved"] = prog.get("bios_saved", 0) + await _visit_profiles_and_store(
                    page, fresh, pause_range=pause_range, sink=profiles)
                # Note the result: _open_search_panel returns False, it does not
                # raise, so the old bare try/except could never have caught this.
                if not await _open_search_panel():
                    prog.update(last_error="Search panel lost after reading bios")
                    activity.log(
                        "visit",
                        f"Read {len(fresh)} bio(s) but the Search panel didn't reopen — "
                        "the next burst has nowhere to type. Reviving the surface.",
                        "warn")
                    # Don't hand back an empty burst and let the stall guard call it
                    # dry: rebuild the surface now, while we still know why it broke.
                    await on_revive("search")
        tick()
        return found

    async def _open_search_panel() -> bool:
        # Retries with a feed reload between goes, so a rail that won't take a
        # click doesn't end the run — see _open_search_panel_resilient.
        return await _open_search_panel_resilient(page)

    async def _close_search_panel():
        # Escape (twice: clear text, then collapse the flyout) returns to the feed
        # at its scroll position. Harmless on the feed if nothing is open.
        for _ in range(2):
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(PANEL_SETTLE_MS)

    async def on_switch(_frm, to) -> bool:
        """Flip the surface. Returns False if it couldn't — the caller then STAYS
        on the technique that is working rather than switching to a dead one."""
        if to == "search":
            return await _open_search_panel()
        await _close_search_panel()
        return True

    async def on_revive(tech) -> None:
        """The current surface has stopped yielding — reload it.

        Scrolling reaches the bottom of the home feed and every further step then
        shows the SAME posts: Instagram only ranks a fresh set on a reload. The
        Search panel goes the same way, its suggestions narrowing to the handful
        of accounts this session keeps landing on. Reloading gives both something
        new to hand over, so an unlimited run keeps collecting instead of ending
        the first time its feed runs out.
        """
        if tech == "search":
            await _close_search_panel()
            await _refresh_feed(page)
            await _open_search_panel()      # logs + retries internally
        else:
            await _refresh_feed(page)
        fwd_steps[0] = 0                    # a reload puts us back at the top

    async def on_pause(seconds):
        # Idle for `seconds`, counting the pause down in progress, Stop-responsive.
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await control.checkpoint()              # Stop lands here
            left = end - time.monotonic()
            prog.update(technique="pause", pause_left=round(max(0.0, left), 1))
            tick()
            await control.sleep(min(0.5, max(0.0, left)))
        prog.update(pause_left=0)

    async def _prepare_surface() -> None:
        """Put the window on the surface this attempt starts from: a freshly
        reloaded feed, plus the Search panel open if that's the starting
        technique. Every attempt — the first and every restart — begins here, so
        a restart genuinely starts from the top of a clean page."""
        await _refresh_feed(page)
        if mode == "search":                        # search-only starts in the panel
            if not await _open_search_panel():
                raise PageWedged(
                    "Couldn't open Instagram's Search panel in this window — the "
                    "sidebar Search button never took a click."
                )

    def _salvage() -> int:
        """Persist what this attempt collected before the counters go back to
        zero, so a restart costs time and never names."""
        if not names:
            return 0
        kept = len(names)
        try:
            from backend import db

            db.record_scraped_usernames(list(reversed(names)))
        except Exception as exc:  # noqa: BLE001 — a save hiccup mustn't block the restart
            activity.log("visit", f"Couldn't save names before restarting: {exc}", "warn")
        return kept

    stopped = False
    restarts = 0
    salvaged = 0
    try:
        while True:
            try:
                await _prepare_surface()
                stopped = await _run_batched(
                    names=names, seen=seen, target=target, bmin=bmin, bmax=bmax,
                    start_technique=start_tech, switch_enabled=(mode == "both"),
                    on_scroll=on_scroll, on_search=on_search, on_switch=on_switch,
                    on_revive=on_revive, delay_min=dmin, delay_max=dmax,
                    on_pause=on_pause, progress=prog)
                break
            except control.Stopped:                 # Stop is an answer, not a fault
                stopped = True
                break
            except (PlaywrightError, PageWedged) as exc:
                # A page-level fault: a click that timed out, a navigation that
                # died, a window that wedged. Recoverable — reload and start over.
                if control.stopping():              # Stop won the race; don't restart
                    stopped = True
                    break
                if restarts >= SCRAPE_MAX_AUTO_RESTARTS:
                    activity.log(
                        "visit",
                        f"Scrape gave up after {restarts} restart(s): {_one_line(exc)}",
                        "error")
                    raise
                restarts += 1
                salvaged += _salvage()
                activity.log(
                    "visit",
                    f"Scrape hit '{_one_line(exc)}' — kept {salvaged} name(s) so far, "
                    f"refreshing the window and starting this account again from 0 "
                    f"(restart {restarts}/{SCRAPE_MAX_AUTO_RESTARTS})",
                    "warn")
                # Back to zero, IN PLACE: the progress dict holds these very lists
                # by reference, so rebinding them would leave the panel polling the
                # old ones forever. `bios_saved` is deliberately NOT reset — those
                # bios are already written to usernames_bio.json and the tally is
                # of what was saved, not of progress toward the target.
                names.clear()
                seen.clear()
                seeds.clear()
                profiles.clear()
                prog.update(restarts=restarts, restarting=True, technique=start_tech,
                            batch_target=0, batch_start=0, pause_left=0,
                            last_error=_one_line(exc))
                try:
                    await _close_search_panel()     # never restart with it over the feed
                except Exception:  # noqa: BLE001
                    pass
                prog.update(restarting=False)
    finally:
        if mode in ("search", "both"):
            try:
                await _close_search_panel()         # never leave it over the feed
            except Exception:  # noqa: BLE001
                pass
        prog.update(running=False, elapsed=round(time.monotonic() - started, 1))

    elapsed = round(time.monotonic() - started, 1)
    bios_saved = prog.get("bios_saved", 0)
    # 'stopped' always wins: a Stop pressed while the loop was mid-batch ends it
    # before any other reason can be recorded.
    end_reason = "stopped" if stopped else (prog.get("end_reason") or "dry")
    why = {"stopped": "stopped by the Stop button",
           "dry": "no new names for several batches — the surfaces dried up",
           "target": "reached its target count"}[end_reason]
    activity.log(
        "visit",
        f"Scrape ({mode}) ended — {why}: "
        f"{len(names)}{'' if target <= 0 else f'/{target}'} name(s)"
        + (f" (+{salvaged} kept from {restarts} restart(s))" if restarts else "")
        + (f", {bios_saved} bio(s) → usernames_bio.json" if scrape_bio else ""),
        "ok",
    )
    return {
        "url": page.url, "duration_seconds": elapsed, "count": len(names),
        "names": names, "usernames": names, "seeds": seeds,
        "target": target, "mode": mode, "stopped": stopped,
        "bios_saved": bios_saved, "profiles": profiles,
        # How many times a wedged window sent this account back to 0, and how many
        # names were saved before those restarts — so the panel can say so.
        "restarts": restarts, "salvaged": salvaged,
        # Why this run ended: 'stopped' | 'dry' | 'target'. Without it an
        # unlimited run that was Stopped and one that dried up read identically.
        "end_reason": end_reason,
    }


# ── Profile-visiting scrape: click each username, read their profile ──────
#
# scrape_batched just reads usernames off feed links. This instead opens EACH new
# username's profile with a real click and reads what the profile shows (full
# name, bio, follower/following/post counts, verified/private), then returns to
# the feed and carries on. Far slower — one navigation per name — so it visits a
# handful of profiles where scrape_batched would collect hundreds of names.

# Watchdog: instead of stopping when the feed goes quiet, a stuck window is
# reloaded and the scrape carries on. If no NEW profile lands for this long the
# window counts as stuck; after this many stuck-reloads with still nothing new,
# we conclude it's truly dead and give up.
PROFILE_STALL_RELOAD_S = 300.0     # 5 minutes with no new profile ⇒ reload the window
PROFILE_MAX_DEAD_RELOADS = 5       # this many stuck-reloads yielding nothing ⇒ give up

# Read what an open profile page shows. Counts come from the header's stats list
# (followers/following are links, posts is not); full name and bio fall back to
# the og:description/og:title meta, which stays stable when the obfuscated header
# markup doesn't. `name` is the username we clicked, kept as-is.
_PROFILE_DATA_JS = """(name) => {
  const parseNum = (s) => {
    if (s == null) return null;
    const cleaned = String(s).trim().replace(/[,\\s]/g, '');
    const m = cleaned.match(/^([\\d.]+)([KMB]?)/i);
    if (!m) return null;
    let n = parseFloat(m[1]);
    if (isNaN(n)) return null;
    const u = (m[2] || '').toUpperCase();
    if (u === 'K') n *= 1e3; else if (u === 'M') n *= 1e6; else if (u === 'B') n *= 1e9;
    return Math.round(n);
  };
  const header = document.querySelector('header') || document.body;
  const countFromLink = (suffix) => {
    const a = header.querySelector('a[href$="/' + suffix + '/"]')
           || document.querySelector('a[href$="/' + suffix + '/"]');
    if (!a) return null;
    const titled = a.querySelector('[title]');
    if (titled && titled.getAttribute('title')) return parseNum(titled.getAttribute('title'));
    return parseNum(a.textContent);
  };
  let followers = countFromLink('followers');
  let following = countFromLink('following');
  let posts = null;
  for (const item of header.querySelectorAll('ul li')) {
    if (item.querySelector('a[href$="/followers/"], a[href$="/following/"]')) continue;
    const t = item.querySelector('[title]');
    posts = parseNum((t && t.getAttribute('title')) || item.textContent);
    if (posts != null) break;
  }
  const verified = !!header.querySelector('svg[aria-label="Verified"]');
  const isPrivate = /This\\s+account\\s+is\\s+private/i.test(document.body.innerText || '');

  // Full name + a (truncated) bio from the og meta — a stable fallback when the
  // obfuscated header markup shifts.
  let fullName = null, metaBio = null;
  const og = document.querySelector('meta[property="og:description"]');
  if (og) {
    const c = og.getAttribute('content') || '';
    if (followers == null) { const m = c.match(/([\\d.,KMB]+)\\s+Followers/i); if (m) followers = parseNum(m[1]); }
    if (following == null) { const m = c.match(/([\\d.,KMB]+)\\s+Following/i); if (m) following = parseNum(m[1]); }
    if (posts == null)     { const m = c.match(/([\\d.,KMB]+)\\s+Posts/i);     if (m) posts = parseNum(m[1]); }
    const dash = c.indexOf(' - ');
    // Instagram's own lead-in sits between the counts and the name on the
    // current format ("… Posts - See Instagram photos and videos from David J
    // Harris Jr (@davidjharrisjr)"). Left in, it became part of every full_name.
    const tail = (dash >= 0 ? c.slice(dash + 3) : c)
      .replace(/^See\\s+Instagram\\s+photos\\s+and\\s+videos\\s+from\\s+/i, '');
    const nm = tail.match(/^(.*?)\\s*\\(@[^)]+\\)/);
    if (nm) fullName = nm[1].trim() || null;
    const bm = tail.match(/on Instagram:\\s*["\\u201c]?([\\s\\S]*?)["\\u201d]?\\s*$/i);
    if (bm) metaBio = bm[1].trim() || null;
  }
  if (!fullName) {
    const ogt = document.querySelector('meta[property="og:title"]');
    if (ogt) { const m = (ogt.getAttribute('content') || '').match(/^(.*?)\\s*\\(@/); if (m) fullName = m[1].trim() || null; }
  }

  // Bio from the DOM. It lives in a <span dir="auto"> that keeps its line breaks
  // as <br> and VERY OFTEN contains @mention / #hashtag <a> links plus a "more"
  // expander (a nested role="button" wrapping a second span of the same class)
  // — e.g. class _ap3a… (obfuscated and rotating, so we match by STRUCTURE not
  // class). Crucially we must NOT skip a span just because it holds links: the
  // bio's own @mentions are links, and skipping them is what made this pick the
  // username by mistake. <br> becomes a newline; anchor text (e.g. @rizzlending)
  // stays as part of its line.
  const brText = (el) => {
    const tmp = document.createElement('div');
    tmp.innerHTML = (el.innerHTML || '').replace(/<br\\s*\\/?>/gi, '\\n');
    let t = (tmp.textContent || '').replace(/\\u00a0/g, ' ')
      .split('\\n').map(s => s.trim()).join('\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
    return t.replace(/\\s*[…\\.]{1,3}\\s*more$/i, '').trim();   // drop the expander's trailing "… more"
  };
  // The full name also lives in the header's <h1>, which carries the CATEGORY's
  // own classes (they differ only by tag). og:title is the first fallback; this
  // is the second, for a client-side navigation where the meta hasn't been
  // swapped yet. Plenty of profiles leave it blank, which is an answer too.
  if (!fullName) {
    const h1 = document.querySelector('%NAME%');
    if (h1) {
      const t = brText(h1);
      if (t && t.toLowerCase() !== (name || '').toLowerCase()) fullName = t;
    }
  }
  const uname = (name || '').toLowerCase();
  const nameLow = (fullName || '').toLowerCase();

  // Instagram's own chrome, which is NOT a bio. Every one of these was landing in
  // usernames_bio as though it were: "Messages" is the DM button's label and
  // "444 posts" is the stats line, and on a half-painted profile they were the
  // only <span dir="auto"> on the page, so the scoring fallback returned them.
  // A read that matches one of these yields null — an empty bio is the truth, and
  // the row can be re-scraped later; a fake bio is silently wrong forever.
  const CHROME = /^(?:\\d[\\d,.\\s]*(?:posts?|followers?|following)|messages?|message|follow(?:ing|ers|ed)?|edit profile|view profile|profile|contact|email|call|shop|share|discover people|suggested for you|note\\.\\.\\.|you'?ve been on instagram.*)$/i;
  const isChrome = (t) => !t || CHROME.test(t.trim());

  // 1. The bio's own element, matched by class. This is the whole fix: the block
  //    below is a fallback for when Instagram rotates these classes, not the
  //    primary path.
  const readBio = () => {
    const spans = Array.from(document.querySelectorAll('%BIO%'));
    // The "… more" expander nests a second element of the same class inside the
    // first, so prefer the OUTERMOST — it holds the full text either way.
    const outer = spans.filter(s => !spans.some(o => o !== s && o.contains(s)));
    for (const span of outer) {
      if (span.closest('nav') || span.closest('ul')) continue;
      // The selector is tag-agnostic on purpose (Instagram moves these roles
      // between <span> and <div>), so rule out the two neighbours that share
      // part of the class list: the category line and the link label.
      if (span.matches('%CAT%') || span.matches('%LINKTEXT%')) continue;
      if (span.closest('button')) continue;              // the link button's own label
      const t = brText(span);
      if (!t || isChrome(t)) continue;
      const low = t.toLowerCase();
      if (low === uname || low === '@' + uname) continue;
      if (nameLow && low === nameLow) continue;
      return t;
    }
    return null;
  };

  const findBio = (scope) => {
    let best = null, bestScore = -1;
    for (const span of scope.querySelectorAll('span[dir="auto"]')) {
      // Skip the username link's own text (inside an <a>), the stats list, and the
      // nav rail — but NOT spans that merely contain links (bios do).
      if (span.closest('a') || span.closest('ul') || span.closest('nav')) continue;
      const t = brText(span);
      if (!t) continue;
      const low = t.toLowerCase();
      if (low === uname || low === '@' + uname) continue;    // the bare username
      if (nameLow && low === nameLow) continue;              // the full-name line
      if (isChrome(t)) continue;                             // a button/stat label
      const html = span.innerHTML || '';
      const score = t.length
        + (/<br/i.test(html) ? 1000 : 0)                     // multi-line ⇒ almost surely the bio
        + (/role="button"/i.test(html) ? 800 : 0)            // the "… more" expander
        + (/<a[\\s>]/i.test(html) ? 400 : 0);                // @mention / #hashtag links
      if (score > bestScore) { bestScore = score; best = t; }
    }
    return best;
  };
  // Class-matched first; the scoring heuristic only as a fallback.
  let domBio = readBio();
  if (!domBio) domBio = findBio(header);
  if (!domBio) { const main = document.querySelector('main'); if (main) domBio = findBio(main); }

  // The category line ("Artist", "Digital creator", "Clothing (Brand)") — a
  // label Instagram itself assigns, so it is worth keeping alongside the bio.
  let category = null;
  const catEl = document.querySelector('%CAT%');
  if (catEl) { const t = brText(catEl); if (t && !isChrome(t)) category = t; }

  // The profile's external link, unwrapped from Instagram's l.instagram.com
  // redirector so it stores as the real destination.
  let link = null, links_more = 0;
  const a = document.querySelector('%LINK%');
  if (a && a.href) {
    try {
      const u = new URL(a.href);
      const target = u.searchParams.get('u');
      link = target ? decodeURIComponent(target).split('?')[0] : a.href;
    } catch (e) { link = a.href; }
  }
  // TWO OR MORE links don't render an <a> at all: they collapse into a <button>
  // labelled "www.example.com and 2 more" that opens a modal. The label is then
  // the only place the link exists on the page, so read it — and keep the count
  // of the ones behind the modal, which is a fact about the profile in itself.
  if (!link) {
    const el = document.querySelector('%LINKTEXT%');
    if (el) {
      let t = brText(el);
      const m = t.match(/^(.*?)\\s+and\\s+(\\d+)\\s+more$/i);
      if (m) { t = m[1].trim(); links_more = parseInt(m[2], 10) || 0; }
      if (t && /^(?:https?:\\/\\/)?[\\w-]+(?:\\.[\\w-]+)+(?:[\\/?#]|$)/i.test(t)) {
        link = /^https?:\\/\\//i.test(t) ? t : 'https://' + t;
      }
    }
  }

  // The Threads handle Instagram links beside the bio — same person, and it is
  // right there on the page.
  let threads = null;
  const th = document.querySelector('%THREADS%');
  if (th) {
    const m = (th.getAttribute('href') || '').match(/threads\\.(?:com|net)\\/@([\\w.]+)/i);
    if (m) threads = m[1];
  }

  let bio = domBio || metaBio;
  if (isChrome(bio)) bio = null;          // never hand back chrome as a bio
  return { full_name: fullName, bio, category, link, links_more, threads,
           followers, following, posts,
           verified, private: isPrivate, url: location.href };
}""".replace("%BIO%", BIO_SEL).replace("%CAT%", BIO_CATEGORY_SEL) \
    .replace("%NAME%", BIO_NAME_SEL).replace("%LINKTEXT%", BIO_LINK_TEXT_SEL) \
    .replace("%LINK%", BIO_LINK_SEL).replace("%THREADS%", BIO_THREADS_SEL)


async def _feed_scroll_step(page, step_range, pause_range) -> None:
    """One glided scroll of the home feed, waiting out a bottom-load if we hit it.
    A wedged page is refreshed rather than allowed to stall the run."""
    glide_ms = random.uniform(*pause_range) * 1000
    try:
        m = await page.evaluate(_GLIDE_JS, {
            "fraction": random.uniform(*step_range), "ms": glide_ms})
    except Exception as exc:  # noqa: BLE001 — page wedged; refresh and move on
        activity.log("visit", f"Feed stuck ({exc}) — refreshing", "warn")
        await _refresh_feed(page)
        return
    if not m["glided"]:                             # hidden window can't animate
        await asyncio.sleep(glide_ms / 1000)
    if not (m["after"] > m["before"] + 2) or m["atBottom"]:
        await _wait_for_growth(page, m["height"], time.monotonic() + BOTTOM_WAIT_S + 5)


async def _feed_scroll_back(page, step_range, pause_range, steps: int) -> int:
    """Glide the feed UPWARD by `steps` step(s) — the human 'flick back up to
    re-read' motion — WITHOUT refreshing the page. Stops early at the top. A wedged
    page just ends the backtrack (the caller's next forward step handles it).
    Returns how many steps actually moved."""
    moved = 0
    for _ in range(max(1, int(steps))):
        glide_ms = random.uniform(*pause_range) * 1000
        try:
            m = await page.evaluate(_GLIDE_UP_JS, {
                "fraction": random.uniform(*step_range), "ms": glide_ms})
        except Exception:  # noqa: BLE001 — a failed back-scroll just stops; forward carries on
            break
        if not m["glided"]:                         # hidden window can't animate
            await asyncio.sleep(glide_ms / 1000)
        moved += 1
        if m["atTop"]:                              # nothing above us — don't keep trying
            break
    return moved


async def _open_profile_and_collect(page, name: str) -> dict | None:
    """Click the feed link for @name to open the profile, read what it shows, and
    return a data dict. Falls back to navigating straight to the profile if the
    click is blocked; returns None only if it couldn't be opened at all."""
    opened = False
    try:
        loc = page.locator(f'a[href="/{name}/"]').first
        if await loc.count():
            try:
                await loc.scroll_into_view_if_needed(timeout=2500)
            except Exception:  # noqa: BLE001 — off-screen is fine, the click still tries
                pass
            await loc.click(timeout=5000)
            opened = True
    except Exception:  # noqa: BLE001 — click blocked; navigate straight there
        opened = False
    if not opened:
        try:
            await page.goto(f"{HOME}{name}/", wait_until="domcontentloaded")
            opened = True
        except Exception:  # noqa: BLE001
            activity.log("visit", f"Couldn't open @{name}'s profile", "warn")
            return None

    # Let the profile render. The gate is tied to `name`, so it cannot open on
    # the page we came from — which is exactly what the old og-meta gate did on a
    # client-side navigation, handing the reader a feed with no bio on it. It
    # still returns the moment THIS profile's header is up (usually a few hundred
    # ms) and only spends the ceiling on a genuinely slow one.
    ready = await _wait_ready(page, _PROFILE_READY_JS, PROFILE_READY_CAP_MS,
                              floor_ms=120, arg=name)
    if not ready:
        # Either the click didn't actually navigate (Instagram swallows one now
        # and then, and the old code called that "opened") or the profile is
        # slow. Go straight to the URL and give it one more go.
        try:
            await page.goto(f"{HOME}{name}/", wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        ready = await _wait_ready(page, _PROFILE_READY_JS, PROFILE_READY_CAP_MS, arg=name)
    if ready:
        # The stats row paints a beat ahead of the bio block, so give the bio its
        # own short look-in before reading. No bio is a legitimate answer, hence a
        # ceiling rather than a requirement.
        await _wait_ready(page, _PROFILE_BIO_READY_JS, PROFILE_BIO_SETTLE_MS)
    else:
        activity.log("visit", f"@{name}'s profile didn't finish loading — "
                              "reading whatever is there", "warn")
    try:
        data = await page.evaluate(_PROFILE_DATA_JS, name)
    except Exception:  # noqa: BLE001 — a bad read mustn't sink the whole run
        data = {}
    data = {"username": name, **(data or {})}
    followers = data.get("followers")
    bio = data.get("bio")
    activity.log(
        "visit",
        f"Opened @{name}"
        + (f" — {followers} followers" if followers is not None else "")
        + (f" · bio {len(bio)} chars" if bio else " · no bio on the profile"),
        "ok")
    return data


async def _return_to_feed(page) -> None:
    """Back to the home feed after a profile visit — go_back keeps the feed's
    scroll position; a forced Home is the fallback if it lands elsewhere."""
    try:
        await page.go_back(wait_until="domcontentloaded")
    except Exception:  # noqa: BLE001
        pass
    if not _is_home_page(page):
        try:
            await page.goto(HOME, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
    # A back-navigation restores from the bfcache and is usually ready at once —
    # so poll for the feed (ceiling 1.5s) instead of always paying the 1.5s.
    await _wait_ready(page, _FEED_READY_JS, 1500)


def _persist_profile(name: str, data: dict) -> None:
    """Save one scraped profile everywhere it belongs: the bare name to Data
    (usernames.txt, so it joins the campaign name set), the {username, bio} pair
    to usernames_bio.txt in MinIO, and a richer record in the local store. A
    save hiccup is logged, never raised — it must not sink the scrape.

    A profile whose bio could not be read is saved with NO bio rather than with
    whatever text happened to be on screen: the bio store's own "unknown"
    sentinel then marks it as still-to-read, and a later scrape can fill it in.
    Writing a button label here is what filled the store with "Messages".
    """
    try:
        from backend import db

        db.add_unique_names([name])
        # usernames_bio.txt keeps ONLY the username + bio, nothing else.
        db.add_username_bios([{"username": name, "bio": data.get("bio")}])
        db.insert({
            "type": "instagram-profile", "title": name,
            "body": (data.get("bio") or ""),
            "full_name": data.get("full_name"),
            # Instagram's OWN category label ("Artist", "Digital creator") and the
            # profile's external link — both read straight off the bio block, and
            # the category in particular is a stronger interest signal than
            # anything guessable from the bio text.
            "ig_category": data.get("category"),
            "link": data.get("link"),
            # How many further links sit behind the "and N more" button, and the
            # Threads handle beside the bio — both read off the same block.
            "links_more": data.get("links_more"),
            "threads": data.get("threads"),
            "followers": data.get("followers"),
            "following": data.get("following"),
            "posts": data.get("posts"),
            "verified": data.get("verified"),
            "private": data.get("private"),
            "profile_url": data.get("url"),
        })
    except Exception as exc:  # noqa: BLE001 — a save hiccup mustn't end the run
        activity.log("visit", f"Couldn't save @{name}: {exc}", "warn")


async def _visit_profiles_and_store(page, names, *, pause_range, restore=None,
                                    sink=None) -> int:
    """Open each of `names`' profiles in turn, read the bio + profile data, and
    persist {username, bio} to usernames_bio.json (plus Data + the local store).

    This is the RUNS profile-scrape's per-name step, factored out so the
    Scrapping panel's scroll and search can reuse it: once they collect a fresh
    username off the feed / search suggestions, we open that profile and save its
    bio the same way the Runs scrape does.

    Best-effort and Stop-aware — a Stop press ends the visiting early, keeping
    everything saved so far. `restore` (an async no-arg callable) runs after each
    profile to put the browser back where the caller needs it next (the feed for
    scrolling, the Search panel for searching); without it the browser is left on
    the last profile opened. `sink` is a list each {username, bio} is appended to
    as it lands — held by reference in the progress state, so the panel's live
    list can show a bio the moment it's read. Returns how many bios were saved.
    """
    saved = 0
    # The dwell between profiles is capped at BIO_DWELL_RANGE. Opening a profile
    # already costs a real navigation, so charging it the full SCROLLING pause on
    # top made every bio cost seconds of doing nothing — the cap keeps the visits
    # irregular without that.
    dwell = (min(pause_range[0], BIO_DWELL_RANGE[0]),
             min(pause_range[1], BIO_DWELL_RANGE[1]))
    for name in names:
        try:
            await control.checkpoint()          # Stop/Pause land between profiles
        except control.Stopped:
            break
        data = await _open_profile_and_collect(page, name)
        await control.sleep(random.uniform(*dwell))   # a human dwell (Stop-aware)
        if data is not None:
            _persist_profile(name, data)
            saved += 1
            if sink is not None:
                sink.append({"username": name, "bio": data.get("bio")})
        if restore is not None:
            try:
                await restore()
            except Exception:  # noqa: BLE001 — a failed restore is retried next step
                pass
        if control.stopping():                  # Stop pressed during the visit — end here
            break
    return saved


async def scrape_profiles(account_id: int | None = None, technique: str = "scroll",
                          target_count: int = 0,
                          duration_min: float | None = None) -> dict:
    """Scroll the home feed and, for each NEW username, click into their profile,
    collect what it shows (full name, bio, follower/following/post counts,
    verified/private), save it, then return to the feed and carry on.

    Two stopping modes:
      • `duration_min` set (> 0) — run for exactly that many minutes: it keeps
        scraping (reloading a stuck window) for the WHOLE time and does NOT stop
        early on the target count or a dry feed. Only the clock or Stop ends it.
      • `duration_min` unset — stop when the feed truly dries up (the watchdog
        gives up after PROFILE_MAX_DEAD_RELOADS fruitless reloads), on Stop, or —
        if `target_count` is positive — once that many profiles are collected.

    `target_count` 0 — the default — means UNLIMITED: keep opening profiles for as
    long as the feed keeps producing faces. The dead-reload watchdog is what ends
    such a run, so it terminates on a genuinely dry feed rather than spinning.

    Each username is saved to MinIO (so it joins the Data name set) and a richer
    per-profile record is written to the local store. Names + per-profile data
    ride visit_progress(), so the Runs preview lists each profile as it lands.
    `technique` is accepted for signature parity with scrape_batched; this mode
    always sources faces from the feed.
    """
    page = account_page(account_id)
    cfg = settings.scroll()
    target = max(0, int(target_count or 0))          # 0 = unlimited
    duration_s = max(0.0, float(duration_min or 0) * 60.0)
    self_name = (_account_username(account_id) or "").lower()

    await _ensure_home(page)
    if not await ig.is_logged_in(page):
        raise RuntimeError(
            "Instagram is not logged in in this window. Open the saved session first."
        )

    pause_range = (cfg["scroll_pause_min_ms"] / 1000, cfg["scroll_pause_max_ms"] / 1000)
    step_range = (cfg["scroll_step_min_pct"] / 100, cfg["scroll_step_max_pct"] / 100)

    started = time.monotonic()
    deadline = (started + duration_s) if duration_s > 0 else None
    seen: set[str] = set()
    names: list[str] = []
    profiles: list[dict] = []

    # `names`/`profiles` by reference so visit_progress() sees each one the moment
    # it lands, exactly as the other scrapes do.
    _progress[account_id] = {
        "running": True, "names": names, "profiles": profiles, "elapsed": 0,
        "account_id": account_id, "mode": "profiles", "target": target,
        "current": None, "reloads": 0, "stalled": False,
        "time_limit_s": duration_s,
    }
    prog = _progress[account_id]

    def tick() -> None:
        prog.update(elapsed=round(time.monotonic() - started, 1), count=len(profiles))

    # Loop guard: with a time limit, run until the clock is up regardless of the
    # count; without one, run until the target count is reached — or forever, when
    # unlimited, in which case only the dead-reload watchdog or Stop ends the loop.
    def keep_running() -> bool:
        if deadline is not None:
            return time.monotonic() < deadline
        if target <= 0:
            return True
        return len(profiles) < target

    if deadline is not None:
        activity.log("visit",
                     f"Profile scrape will run for {int(duration_s // 60)} min "
                     f"{int(duration_s % 60)}s — it won't stop before then", "ok")

    await _refresh_feed(page)

    stopped = False
    reloads = 0                    # how many times the stuck window has been reloaded
    dead_reloads = 0               # consecutive stuck-reloads that produced nothing new
    last_progress = time.monotonic()   # when the last NEW profile landed (the stall clock)
    try:
        while keep_running():
            try:
                await control.checkpoint()          # Stop/Pause land between profiles
            except control.Stopped:
                stopped = True
                activity.log("visit", "Profile scrape stopped — keeping what was collected", "warn")
                break

            # ── Watchdog: no new profile for 5 minutes ⇒ the window is stuck.
            # Reload it and start collecting again rather than stopping. Only after
            # several reloads that STILL find nothing do we accept it's dead.
            if time.monotonic() - last_progress >= PROFILE_STALL_RELOAD_S:
                dead_reloads += 1
                reloads += 1
                prog.update(reloads=reloads, stalled=True)
                # With a time limit we never give up early — keep reloading until
                # the clock is up. Only a count-bounded run concedes a dead feed.
                if deadline is None and dead_reloads > PROFILE_MAX_DEAD_RELOADS:
                    activity.log("visit",
                                 f"Window still stuck after {PROFILE_MAX_DEAD_RELOADS} reloads — giving up",
                                 "warn")
                    break
                activity.log("visit",
                             f"Scrape stuck for {int(PROFILE_STALL_RELOAD_S // 60)} min — "
                             f"reloading the window and restarting (reload #{reloads})", "warn")
                try:
                    await _refresh_feed(page)
                except Exception:  # noqa: BLE001 — a reload that itself fails just retries next cycle
                    pass
                last_progress = time.monotonic()    # give the reloaded window a fresh 5-minute budget
                continue

            # Read the feed; a wedged page is reloaded and retried, never fatal.
            try:
                fresh = [n for n in await _collect_visible_usernames(page)
                         if n.lower() not in seen and n.lower() != self_name]
            except Exception as exc:  # noqa: BLE001
                activity.log("visit", f"Feed read failed ({exc}) — reloading the window", "warn")
                try:
                    await _refresh_feed(page)
                except Exception:  # noqa: BLE001
                    pass
                reloads += 1
                prog.update(reloads=reloads)
                continue

            if not fresh:
                # No new faces on screen: scroll for more. A prolonged drought is
                # handled by the 5-minute watchdog above (reload), not an early stop.
                prog.update(stalled=False)
                await _feed_scroll_step(page, step_range, pause_range)
                tick()
                continue

            name = fresh[0]
            seen.add(name.lower())
            prog.update(current=name, stalled=False)

            data = await _open_profile_and_collect(page, name)
            # A human dwell on the profile before leaving it.
            try:
                await control.sleep(random.uniform(*pause_range))
            except control.Stopped:
                stopped = True
                await _return_to_feed(page)
                break
            await _return_to_feed(page)

            if data is None:                         # couldn't open it — skip on
                continue

            names.append(name)
            profiles.append(data)
            last_progress = time.monotonic()         # progress! reset the stall clock
            dead_reloads = 0                          # and the give-up counter
            # Persist as it lands (so a Stop mid-run keeps everything): the bare
            # name to usernames.txt (Data panel + campaign), the username+bio to
            # usernames_bio.json in MinIO, and a rich record in the local store.
            _persist_profile(name, data)
            tick()

            # Nudge the feed so the next round finds fresh faces.
            await _feed_scroll_step(page, step_range, pause_range)
    finally:
        prog.update(running=False, current=None, stalled=False, reloads=reloads,
                    count=len(profiles), elapsed=round(time.monotonic() - started, 1))

    elapsed = round(time.monotonic() - started, 1)
    timed_out = deadline is not None and not stopped
    how = "stopped" if stopped else ("time up" if timed_out else "done")
    activity.log(
        "visit",
        f"Profile scrape {how}: {len(profiles)} profile(s) in "
        f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
        + (f", {reloads} reload(s)" if reloads else ""),
        "ok",
    )
    return {
        "url": page.url, "duration_seconds": elapsed, "count": len(profiles),
        "names": names, "usernames": names, "profiles": profiles,
        "target": target, "mode": "profiles", "stopped": stopped, "reloads": reloads,
        "time_limit_s": duration_s, "timed_out": timed_out,
    }


async def visit_sidebar(pause: float | None = None, account_id: int | None = None) -> dict:
    """Backward-compatible alias for the campaign button."""
    return await visit_home_feed(account_id=account_id)


INBOX_URL = "https://www.instagram.com/direct/inbox/"


SEARCH_SEL = 'input[name="searchInput"]'

# In the DM inbox the search is sometimes a collapsed "Search" control that only
# renders its <input> once clicked. This finds and clicks that control, and is a
# no-op when the input is already there.
#
# It never clicks inside a link or the nav rail: the sidebar's own Search opens
# Instagram's GLOBAL search panel, which is a different thing entirely and would
# navigate away from the inbox.
_OPEN_SEARCH_JS = """() => {
  const vis = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 4 && r.height > 4;
  };
  const inNav = el => !!(el.closest('nav') || el.closest('a[href]'));

  const input = [...document.querySelectorAll(
      'input[name="searchInput"], input[placeholder*="search" i], input[aria-label*="search" i]')]
    .find(el => vis(el) && !inNav(el));
  if (input) return { found: 'input', clicked: false, seen: [] };

  const seen = [];
  const cands = [...document.querySelectorAll('[role="button"], button, div, span')]
    .filter(el => vis(el) && !inNav(el))
    .filter(el => {
      const label = (el.getAttribute('aria-label') || '').trim();
      const text = el.childElementCount === 0 ? (el.textContent || '').trim() : '';
      return /^search$/i.test(label) || /^search/i.test(text);
    });
  for (const el of cands) {
    seen.push(((el.getAttribute('aria-label') || el.textContent || '').trim()).slice(0, 40));
  }
  // Leftmost first: the conversation list column sits left of the thread pane.
  cands.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (ra.left - rb.left) || (ra.top - rb.top);
  });
  const target = cands[0];
  if (!target) return { found: 'none', clicked: false, seen };
  (target.closest('[role="button"], button') || target).click();
  return { found: 'control', clicked: true, seen };
}"""


async def _open_search_box(page) -> dict:
    """Make the DM search input exist, clicking the collapsed Search control if
    that's what the inbox is showing. Best-effort — the caller still waits on the
    input and reports properly if it never appears."""
    try:
        info = await page.evaluate(_OPEN_SEARCH_JS)
    except Exception as exc:  # noqa: BLE001
        return {"found": "error", "clicked": False, "error": str(exc)}
    if info.get("clicked"):
        activity.log("visit", "Clicked the inbox's Search control", "ok")
        await page.wait_for_timeout(600)      # let the input render
    return info


async def _clear_search(page):
    """Focus the DM search box and empty it, returning the locator. Clears with
    real keystrokes and checks the field afterwards — fill('') sets .value
    without React noticing, so the old query survives and the next one types
    onto the end of it."""
    box = page.locator(SEARCH_SEL).first
    try:
        await box.wait_for(state="visible", timeout=2500)
    except Exception:  # noqa: BLE001
        # Not rendered yet: the inbox is showing a collapsed Search control, so
        # click it and wait again. Only reached when the input ISN'T already
        # there, so the path that works today is untouched.
        info = await _open_search_box(page)
        if not info.get("clicked"):
            activity.log(
                "visit",
                "DM search input not found; search-like controls on the page: "
                + (", ".join(repr(s) for s in info.get("seen") or []) or "none"),
                "warn",
            )
        await box.wait_for(state="visible", timeout=8000)
    try:
        await box.click(timeout=4000)
    except Exception:  # noqa: BLE001
        # Something is sitting over the field — a toast, an open dropdown, the
        # nav rail on a narrow window. Playwright would keep retrying the click
        # for 30s; focusing puts the caret in it just as well, and instantly.
        activity.log("visit", "Search box click was blocked — focusing it instead", "warn")
        await box.focus()
    await page.wait_for_timeout(200)
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Delete")
    for _ in range(40):                       # backspace out whatever survived
        if not ((await box.input_value()) or ""):
            break
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(40)
    await page.wait_for_timeout(300)          # let the dropdown collapse
    return box


async def _type_into_search(page, text: str) -> str:
    """Clear the DM search box and type `text` into it. Returns what actually
    landed in the field. Types character-by-character through the keyboard —
    Instagram's search input is React-controlled, so setting .value directly
    (fill) often doesn't register; real keystrokes do."""
    box = await _clear_search(page)
    await page.keyboard.type(text, delay=90)  # into the focused box, key by key
    await page.wait_for_timeout(400)
    try:
        return (await box.input_value()) or ""
    except Exception:  # noqa: BLE001
        return ""


# Finds the first row of the DM search dropdown and hands back the element.
# Anchored on the avatar image rather than class names — Instagram's classes
# (x1lliihq, x1plvlek, ...) are obfuscated and rotate on every deploy. Every
# result type draws an avatar, so the topmost image below the search box is the
# first row; from there we climb to the clickable ancestor.
#
# The results only ever render in the column under the search input, so the
# hunt is restricted to it: once a conversation is open its avatars sit below
# the input too, and the thread header's one can sit ABOVE the first result —
# unrestricted, that header wins and the real row never gets clicked.
_FIND_ROW_JS = """() => {
  const input = document.querySelector('input[name="searchInput"]');
  if (!input) return null;
  const box = input.getBoundingClientRect();
  // Centre, not edges: a conversation pane butted up against the panel still
  // overlaps its edge, but nothing of it is centred in the search column.
  const inColumn = r => {
    const cx = r.left + r.width / 2;
    return cx > box.left - 40 && cx < box.right + 40;
  };
  const imgs = [...document.querySelectorAll('img')].filter(img => {
    const r = img.getBoundingClientRect();
    return r.width >= 24 && r.height >= 24 && r.top >= box.bottom - 4 && inColumn(r);
  });
  imgs.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  const img = imgs[0];
  if (!img) return null;
  const row = img.closest('[role="button"], a[href], [tabindex]');
  if (row) return row;
  // No clickable ancestor tagged: climb until the block spans the search box.
  let el = img;
  while (el.parentElement && el.getBoundingClientRect().width < box.width * 0.6) {
    el = el.parentElement;
  }
  return el;
}"""

# Reads a result row. `name` is the only field every result type carries — the
# verified badge, the bio and even the username line are all optional (group
# rows show "title" + "member, member" and have no handle at all).
_ROW_INFO_JS = """(row) => {
  const spans = [...row.querySelectorAll('span[dir="auto"]')]
    .map(s => (s.textContent || '').trim());
  const fallback = (row.innerText || '').split('\\n').map(s => s.trim());
  const seen = new Set();
  const lines = (spans.length ? spans : fallback).filter(t => {
    if (!t || seen.has(t)) return false;
    seen.add(t);
    return true;
  });
  const img = row.querySelector('img');
  return {
    lines,
    verified: !!row.querySelector('svg[aria-label="Verified"]'),
    avatar: img ? img.src : null,
  };
}"""


def _parse_row(info: dict) -> dict:
    """Turn the raw row text into fields. Only `name` is guaranteed."""
    verified = bool(info.get("verified"))
    lines = [str(line) for line in (info.get("lines") or []) if str(line).strip()]
    if verified:
        # Some browsers surface the badge's <title> as text; drop it once.
        if "Verified" in lines:
            lines.remove("Verified")

    name = lines[0] if lines else ""
    username = None
    if len(lines) > 1 and USERNAME_RE.fullmatch(lines[1]):
        username = lines[1]
    elif len(lines) == 1 and USERNAME_RE.fullmatch(lines[0]):
        # Account with no full name — the one line it has is the handle.
        username = lines[0]

    rest = [line for line in lines[1:] if line != username]
    return {
        "name": name,
        "username": username,
        "subtitle": rest[0] if rest else None,
        "verified": verified,
        "avatar": info.get("avatar"),
        "lines": lines,
    }


ROW_WAIT_MS = 6000
ROW_STABLE_GAP_MS = 350


async def _find_row(page):
    """The top row of the results column right now, or None."""
    handle = await page.evaluate_handle(_FIND_ROW_JS)
    row = handle.as_element()
    if row is None:
        await handle.dispose()
        return None
    return row


async def _wait_for_stable_row(page):
    """Wait for the results to settle and return (row, info), or (None, None).

    Reads the top row until it says the same thing twice running: the column
    re-renders while the query lands, so a single read can catch the previous
    query's row — or the inbox thread list, which lives in the same column and
    looks just like a result.
    """
    deadline = time.monotonic() + ROW_WAIT_MS / 1000
    last = None
    while time.monotonic() < deadline:
        row = await _find_row(page)
        if row is None:
            last = None
            await page.wait_for_timeout(ROW_STABLE_GAP_MS)
            continue
        info = _parse_row(await page.evaluate(_ROW_INFO_JS, row))
        key = "\n".join(info["lines"])
        if key and key == last:
            return row, info
        last = key
        await row.dispose()
        await page.wait_for_timeout(ROW_STABLE_GAP_MS)
    return None, None


async def _click_first_result(page) -> dict | None:
    """Read and click the first row of the DM search results. Returns what the
    row said, or None if the dropdown never rendered a row."""
    row, info = await _wait_for_stable_row(page)
    if row is None:
        return None
    try:
        await row.click(timeout=6000)
        await page.wait_for_timeout(1200)
    finally:
        await row.dispose()
    return info


BETWEEN_SEARCH_RANGE_S = (2.2, 4.0)

# Fallback gap between campaign actions if the setting can't be read. The live
# range comes from Settings (campaign_action_min/max) — a fresh random pick after
# each action (e.g. act, then 3–10 messages, then act again).
_CAMPAIGN_ACTION_EVERY = (3, 10)

# The message composer in an open conversation. It's a Lexical contenteditable,
# not an <input>, so it only takes real keystrokes. Anchored on the role and the
# editor flag: the classes rotate, and aria-placeholder ("Message...") is
# localised, so neither survives.
MESSAGE_SEL = 'div[contenteditable="true"][role="textbox"][data-lexical-editor="true"]'
MESSAGE_FALLBACK_SEL = 'div[contenteditable="true"][role="textbox"]'
DEFAULT_MESSAGE = "Hello"

# The composer's photo control, matched case-insensitively and by keyword so it
# survives Instagram's localised / reworded labels ("Add Photo or Video",
# "Attach a photo or video", "Add photo", …) — the exact-string match was why it
# silently missed and fell back to a wrong input.
MEDIA_ICON_SEL = (
    'button[aria-label*="photo" i], [role="button"][aria-label*="photo" i], '
    'button[aria-label*="attach" i], [role="button"][aria-label*="attach" i], '
    'button[aria-label*="upload" i], [role="button"][aria-label*="upload" i], '
    'button[aria-label*="image" i], [role="button"][aria-label*="image" i], '
    'button[aria-label*="media" i], [role="button"][aria-label*="media" i], '
    'button[title*="photo" i], [role="button"][title*="photo" i], '
    'button[title*="attach" i], [role="button"][title*="attach" i], '
    'button[title*="upload" i], [role="button"][title*="upload" i], '
    'button[title*="image" i], [role="button"][title*="image" i], '
    'button:has(svg[aria-label*="photo" i]), [role="button"]:has(svg[aria-label*="photo" i]), '
    'button:has(svg[aria-label*="attach" i]), [role="button"]:has(svg[aria-label*="attach" i]), '
    'button:has(svg[aria-label*="upload" i]), [role="button"]:has(svg[aria-label*="upload" i]), '
    'button:has(svg[aria-label*="image" i]), [role="button"]:has(svg[aria-label*="image" i]), '
    'svg[aria-label*="photo" i], svg[aria-label*="attach" i], '
    'svg[aria-label*="upload" i], svg[aria-label*="image" i]'
)
# Prefer the input that declares it takes images (the composer's), else any file
# input on the page.
FILE_INPUT_IMG_SEL = (
    'input[type="file"][accept*="image" i], input[type="file"][accept*=".jpg" i], '
    'input[type="file"][accept*=".jpeg" i], input[type="file"][accept*=".png" i]'
)
FILE_INPUT_SEL = 'input[type="file"]'
# In an open DM the blue Send appears once there's something to send — the
# fallback for media with no caption, where Enter may not have focus.
SEND_BUTTON_SEL = (
    'div[role="button"]:has-text("Send"), button:has-text("Send"), '
    '[aria-label="Send" i]'
)

# asyncio.sleep can't resolve finer than the OS timer — ~15.5ms on Windows. So
# sleeping per character floors the pace there no matter how low Speed is set:
# Speed 1 would still cost ~15ms a key. Instead the per-key delays are banked and
# paid in one sleep once they're worth a real one, which honours the configured
# average rate while letting fast settings actually be fast.
_SLEEP_FLOOR_MS = 15.0


async def _type_message_humanlike(page, text: str) -> None:
    """Type `text` into the already-focused composer at the Config panel's pace.

    Speed / jitter / the delete-and-retype corrections all come from Config →
    Typing, so the panel's Speed slider is the one place that governs this. It
    used to be a hardcoded 85ms + 90ms jitter, which silently ignored the
    setting and made every message crawl.

    Never presses Enter and never types a newline (the caller has already
    flattened `text` to one line).
    """
    cfg = settings.typing()
    owed_ms = 0.0
    pending: list[str] = []

    async def flush() -> None:
        """Send the buffered characters as ONE keyboard.type() call.

        Each call is a round-trip to the browser (~4ms), which is a floor the
        delay settings can't get under — a per-character call meant a 65-char
        message cost ~270ms in round-trips alone however low Speed was set.
        Handing the run to Playwright in one go still emits real per-key events
        (which the composer needs) at about a third of the cost.
        """
        if pending:
            await page.keyboard.type("".join(pending))
            pending.clear()

    for event in human_typing.plan_keystrokes(text, cfg):
        if event["key"] == human_typing.BACKSPACE:
            await flush()                            # keep the order exact
            await page.keyboard.press(human_typing.BACKSPACE)
        else:
            pending.append(event["key"])             # handles case, '@', '.'
        owed_ms += event["delay_ms"]
        if owed_ms >= _SLEEP_FLOOR_MS:
            await flush()                            # pace only where it's real
            await asyncio.sleep(owed_ms / 1000)
            owed_ms = 0.0
    await flush()
    if owed_ms > 0:
        await asyncio.sleep(owed_ms / 1000)


# Any message/caption editor. When an image is staged (Both mode), the caption
# box can be the image-preview overlay's field rather than the inbox composer,
# and the inbox one may be hidden behind it — so pick the best visible textbox
# at runtime instead of trusting DOM order.
VISIBLE_EDITOR_SEL = 'div[contenteditable="true"][role="textbox"]'


async def _best_visible_textbox(page):
    """Return the lowest visible textbox on the page, or None if there isn't
    one yet."""
    boxes = page.locator(VISIBLE_EDITOR_SEL)
    best = None
    best_top = None
    for i in range(await boxes.count()):
        box = boxes.nth(i)
        try:
            info = await box.evaluate("""el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return {
                  visible: s.display !== 'none' && s.visibility !== 'hidden' &&
                           r.width > 0 && r.height > 0,
                  top: r.top,
                };
            }""")
        except Exception:  # noqa: BLE001
            continue
        if not info.get("visible"):
            continue
        top = float(info.get("top") or 0)
        if best is None or top >= best_top:
            best = box
            best_top = top
    return best


async def _caption_box(page):
    """The visible text editor to write into — the overlay caption when an image
    is staged, otherwise the normal composer."""
    box = await _best_visible_textbox(page)
    if box is not None:
        return box
    box = page.locator(MESSAGE_SEL).first
    if await box.count():
        return box
    return page.locator(MESSAGE_FALLBACK_SEL).first


async def _write_message(page, text: str, for_caption: bool = False) -> dict:
    """Type `text` into the open conversation's composer and leave it unsent.

    Never presses Enter, and never types a newline — in this composer Enter IS
    the send button. Any newline in `text` is collapsed to a space first, so a
    multi-line message can't send itself.

    `for_caption` is for Both mode, where the text is the caption for an image
    that was just staged: it targets whatever editor is visible (the preview
    overlay's caption box can differ from the inbox composer) and types even if
    the field isn't empty, since a placeholder/leftover must not block the
    caption.
    """
    safe = " ".join(str(text).split())
    if not safe:
        raise RuntimeError("nothing to write")

    if for_caption:
        box = await _caption_box(page)
    else:
        box = page.locator(MESSAGE_SEL).first
        if not await box.count():
            box = page.locator(MESSAGE_FALLBACK_SEL).first
    await box.wait_for(state="visible", timeout=8000)

    existing = ((await box.inner_text()) or "").strip()
    # Don't clobber a real draft in plain text mode. For a caption we own the
    # field, so clear anything there and write regardless.
    if existing and not for_caption:
        return {"written": False, "text": existing, "reason": "composer already had text"}

    await box.click()
    await page.wait_for_timeout(200)
    if existing and for_caption:
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(120)
    await _type_message_humanlike(page, safe)   # varied delay + corrections; no Enter, ever
    await page.wait_for_timeout(300)

    try:
        landed = ((await box.inner_text()) or "").strip()
    except Exception:  # noqa: BLE001 — overlay may re-render; assume the keys landed
        landed = safe
    # Lenient for captions (the field can hold extra whitespace/markup); exact
    # for a plain message.
    ok = (safe in landed) if for_caption else (landed == safe)
    if not ok:
        return {"written": False, "text": landed, "reason": f"typed {safe!r}, box holds {landed!r}"}
    return {"written": True, "text": landed, "reason": None}


async def _staged_preview_count(page) -> int:
    """How many local upload previews are staged in the composer right now.

    A file picked for upload renders as a blob:-backed <img> before it's sent;
    already-sent images come from Instagram's CDN, not blob:. So a rise in
    blob: images is proof THIS file landed in THIS open conversation — the check
    that tells attach-worked from attach-silently-did-nothing.
    """
    try:
        return await page.evaluate(
            "() => Array.from(document.images)"
            ".filter(i => (i.currentSrc || i.src || '').startsWith('blob:')).length"
        )
    except Exception:  # noqa: BLE001
        return 0


async def _all_file_inputs(page):
    """Every file input on the page, image-accepting ones first — the composer's
    photo input is usually one of these. Returned as a list of locators."""
    inputs = []
    imaged = page.locator(FILE_INPUT_IMG_SEL)
    for i in range(await imaged.count()):
        inputs.append(imaged.nth(i))
    generic = page.locator(FILE_INPUT_SEL)
    for i in range(await generic.count()):
        inputs.append(generic.nth(i))
    return inputs


async def _set_files_on_any(page, paths: list[str]) -> bool:
    """Set `paths` on a file input, clearing it first so the same file re-fires a
    change event (Instagram reuses ONE hidden input across conversations, so
    re-setting the same path is otherwise a silent no-op — the reason later names
    got nothing). Tries image inputs first, then any input. True if one accepted
    the files."""
    tried = False
    for file_input in await _all_file_inputs(page):
        try:
            await file_input.wait_for(state="attached", timeout=3000)
        except Exception:  # noqa: BLE001
            continue
        tried = True
        try:
            try:
                await file_input.set_input_files([])       # clear stale selection
                await page.wait_for_timeout(100)
            except Exception:  # noqa: BLE001 — clearing is best-effort
                pass
            await file_input.set_input_files(paths)
            return True
        except Exception:  # noqa: BLE001 — try the next input
            continue
    return tried and False


async def _click_photo_icon(page, paths: list[str]) -> bool:
    """Click the composer's photo icon and hand `paths` to the chooser it opens.
    Best-effort — used to reveal/trigger a lazy file input."""
    try:
        icons = page.locator(MEDIA_ICON_SEL)
        count = await icons.count()
        if not count:
            return False
        for i in range(count - 1, -1, -1):
            icon = icons.nth(i)
            try:
                if not await icon.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            try:
                async with page.expect_file_chooser(timeout=5000) as chooser_info:
                    await icon.click(timeout=5000)
                chooser = await chooser_info.value
                await chooser.set_files(paths)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def _files_payload(paths: list[str]) -> list[dict]:
    """Read each image as base64 + name + mime, for injecting into the page."""
    out = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                data = base64.b64encode(fh.read()).decode()
        except Exception:  # noqa: BLE001
            continue
        out.append({
            "b64": data,
            "name": os.path.basename(p),
            "mime": mimetypes.guess_type(p)[0] or "image/jpeg",
        })
    return out


# Drop the file straight onto the message composer as a real File in a
# DataTransfer — this is the "paste the photo into the textbox" path, and it
# works when Instagram has no static file input or ignores set_input_files.
_DROP_JS = """async (files) => {
  const editors = [...document.querySelectorAll('div[contenteditable="true"][role="textbox"]')];
  editors.sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    return rb.top - ra.top;
  });
  const target = editors.find(el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' &&
           r.width > 0 && r.height > 0;
  }) || document.querySelector('div[contenteditable="true"][role="textbox"]')
    || document.querySelector('div[contenteditable="true"]');
  if (!target) return false;
  const dt = new DataTransfer();
  for (const f of files) {
    const res = await fetch('data:' + f.mime + ';base64,' + f.b64);
    const blob = await res.blob();
    dt.items.add(new File([blob], f.name, { type: f.mime }));
  }
  const r = target.getBoundingClientRect();
  const base = { bubbles: true, cancelable: true, composed: true, dataTransfer: dt,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  target.dispatchEvent(new DragEvent('dragenter', base));
  target.dispatchEvent(new DragEvent('dragover', base));
  target.dispatchEvent(new DragEvent('drop', base));
  try { target.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt })); } catch (e) {}
  return true;
}"""


# The OS clipboard is one shared resource, but accounts run concurrently on
# purpose. Without this lock two campaigns can interleave "set clipboard" and
# "Ctrl+V" and each paste the other's image.
_clipboard_lock = asyncio.Lock()


_xclip_owner = None     # the xclip process serving the clipboard right now (Linux)


async def _clipboard_set_image_x11(path: str) -> bool:
    """Linux: put one image on the X clipboard with xclip (image/png or
    image/jpeg). xclip stays alive as the selection owner until something else
    takes the clipboard, so it is started detached and the previous one is
    retired first."""
    global _xclip_owner
    import shutil
    import subprocess

    if not shutil.which("xclip") or not os.environ.get("DISPLAY"):
        return False
    mime = mimetypes.guess_type(path)[0] or "image/png"
    if mime not in ("image/png", "image/jpeg"):
        return False
    try:
        if _xclip_owner and _xclip_owner.poll() is None:
            _xclip_owner.terminate()
        # -quiet keeps xclip in the foreground (by default it forks and the
        # parent exits at once, which looked like a failure).
        _xclip_owner = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-quiet", "-t", mime, "-i", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.6)
        targets = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        return mime in targets                  # the clipboard now offers the image
    except Exception:  # noqa: BLE001
        return False


async def _clipboard_set_files(paths: list[str]) -> bool:
    """Put real files on the OS clipboard, the way Explorer's Copy does.

    Windows: PowerShell Set-Clipboard with the file paths. Linux with a desktop
    (DISPLAY set): the image bytes via xclip. Returns False anywhere else (or on
    failure) so the caller just falls through to the other attach methods.
    """
    if not paths:
        return False
    if sys.platform.startswith("linux"):
        return await _clipboard_set_image_x11(paths[0])
    if sys.platform != "win32":
        return False
    quoted = ",".join('"{}"'.format(p.replace('"', '`"')) for p in paths)
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", f"Set-Clipboard -Path {quoted}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — any failure just means "try another way"
        return False


async def _paste_via_keyboard(page, box, paths: list[str]) -> bool:
    """The genuine copy-and-paste: real files on the clipboard, real Ctrl+V.

    This is the one path that produces a TRUSTED paste event — byte-for-byte
    what a person pressing Ctrl+V produces. A dispatched ClipboardEvent carries
    the file just as well but arrives with isTrusted=false, and Instagram's
    composer advertises itself as interactive for keydown/input/click only
    (`data-interactable="|keydown||input||click|"`), so the synthetic event is
    the one it is least likely to honour. Hence: try the real thing first.
    """
    # Hold the clipboard from the moment it's filled until the paste has landed,
    # so a second account can't overwrite it in between.
    async with _clipboard_lock:
        if not await _clipboard_set_files(paths):
            return False
        try:
            try:
                await box.click(timeout=4000)      # focus it the way a user would
            except Exception:  # noqa: BLE001
                await box.focus(timeout=4000)
            await page.wait_for_timeout(150)
            await page.keyboard.press("Control+V")
            await page.wait_for_timeout(400)       # let Chrome read the clipboard
            return True
        except Exception:  # noqa: BLE001
            return False


async def _drop_on_composer(page, payload: list[dict]) -> bool:
    """Fire a synthetic file-drop (and paste) with the image onto the composer."""
    if not payload:
        return False
    try:
        return bool(await page.evaluate(_DROP_JS, payload))
    except Exception:  # noqa: BLE001
        return False


async def _wait_for_upload_surface(page, timeout_ms: int = 15000) -> dict:
    """Poll until the open thread shows a visible composer plus a way to add a
    photo (a file input or the photo icon). Returns what was found and how
    long it took; never raises."""
    started = time.monotonic()
    found = {"ready": False, "inputs": 0, "icons": 0, "waited_ms": 0}
    while True:
        try:
            n_in = await page.locator(FILE_INPUT_SEL).count()
            icons = page.locator(MEDIA_ICON_SEL)
            n_icon = 0
            for i in range(await icons.count()):
                try:
                    if await icons.nth(i).is_visible():
                        n_icon += 1
                except Exception:  # noqa: BLE001
                    pass
            box = await _best_visible_textbox(page)
            found.update(inputs=n_in, icons=n_icon)
            if box is not None and (n_in or n_icon):
                found["ready"] = True
                break
        except Exception:  # noqa: BLE001 — page mid-navigation; keep polling
            pass
        found["waited_ms"] = int((time.monotonic() - started) * 1000)
        if found["waited_ms"] >= timeout_ms:
            break
        await page.wait_for_timeout(300)
    found["waited_ms"] = int((time.monotonic() - started) * 1000)
    return found


async def _attach_media(page, paths: list[str]) -> dict:
    """Attach `paths` into the open conversation, once per name, trying every
    method until a staged preview appears.

    Order: (1) DROP the file straight onto the message box (the "paste into the
    textbox" path); (2) set files on the composer's <input type=file>; (3)
    click the photo icon → file chooser. It re-checks for a preview after each,
    and never bails — the caller still presses Enter to send whatever is staged."""
    if not paths:
        raise RuntimeError("no media to attach")

    media_names = [os.path.basename(p) for p in paths]

    for i, name in enumerate(media_names, start=1):
        activity.log(
            "visit",
            f"[image {i}] {name} copied",
            "ok",
            kind="media-copy",
            media_index=i,
            media_name=name,
        )

    # Wait for THIS conversation's composer AND its upload surface (the hidden
    # file input / "Add Photo or Video" icon). Right after the search result is
    # clicked the thread is still rendering: the previous composer may still be
    # on screen while the new thread has no file input yet — attaching at that
    # moment found "0 file inputs, 0 photo icons" and the DM went out text-only.
    surface = await _wait_for_upload_surface(page, timeout_ms=15000)
    box = await _best_visible_textbox(page)
    if box is None:
        box = page.locator(MESSAGE_SEL).first
        if not await box.count():
            box = page.locator(MESSAGE_FALLBACK_SEL).first
    try:
        await box.wait_for(state="visible", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    if not surface["ready"]:
        activity.log(
            "visit",
            f"    media: upload surface not found after {surface['waited_ms']} ms "
            f"(url {page.url})",
            "warn",
        )

    before = await _staged_preview_count(page)
    payload = _files_payload(paths)          # read bytes once, reused by drop

    # Diagnostic — what upload surfaces exist right now.
    try:
        n_any_in = await page.locator(FILE_INPUT_SEL).count()
        n_img_in = await page.locator(FILE_INPUT_IMG_SEL).count()
        n_icon = await page.locator(MEDIA_ICON_SEL).count()
        activity.log(
            "visit",
            f"    media: {n_any_in} file input(s) ({n_img_in} image), {n_icon} photo icon(s)",
            "ok",
        )
    except Exception:  # noqa: BLE001
        pass

    async def landed(tries: int = 20) -> bool:
        # ~7 s: the staged preview of a multi-megabyte image takes a few
        # seconds to render on a small VM.
        for _ in range(tries):
            await page.wait_for_timeout(350)
            if await _staged_preview_count(page) > before:
                return True
        return False

    used = "none"
    verified = False

    if sys.platform == "win32":
        methods = [
            ("paste",        lambda: _paste_via_keyboard(page, box, paths)),
            ("input",        lambda: _set_files_on_any(page, paths)),
            ("file_chooser", lambda: _click_photo_icon(page, paths)),
            ("input",        lambda: _set_files_on_any(page, paths)),   # retry after icon revealed one
            ("drop",         lambda: _drop_on_composer(page, payload)),
        ]
    else:
        methods = [
            ("input",        lambda: _set_files_on_any(page, paths)),
            ("file_chooser", lambda: _click_photo_icon(page, paths)),
            ("paste",        lambda: _paste_via_keyboard(page, box, paths)),
            ("input",        lambda: _set_files_on_any(page, paths)),
            ("drop",         lambda: _drop_on_composer(page, payload)),
        ]
    for name, run in methods:
        try:
            issued = await run()
        except Exception:  # noqa: BLE001
            issued = False
        if issued and used == "none":
            used = name
        elif issued:
            used = name
        if issued and await landed():
            verified = True
            used = name
            for i, name in enumerate(media_names, start=1):
                activity.log(
                    "visit",
                    f"[image {i}] {name} pasted",
                    "ok",
                    kind="media-paste",
                    media_index=i,
                    media_name=name,
                )
            break

    await page.wait_for_timeout(900)         # settle before caption / Enter

    if used != "none" and not verified:
        for i, name in enumerate(media_names, start=1):
            activity.log(
                "visit",
                f"[image {i}] {name} pasted (unconfirmed)",
                "warn",
                kind="media-paste",
                media_index=i,
                media_name=name,
            )

    # Debug: screenshot the DM right after attaching so the result can be
    # inspected (does the photo show in the composer or not).
    try:
        shot = config.BACKEND_DIR / "data" / "debug_media.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(shot))
    except Exception:  # noqa: BLE001
        pass

    return {"attached": len(paths), "via": used, "verified": verified}


async def _press_enter_send(page) -> bool:
    """Send whatever is staged in the open conversation. Focuses the VISIBLE
    editor (the image-preview overlay's caption box in Both mode, else the
    composer) and presses Enter — which here IS the send button; if that raises,
    clicks an explicit Send button. Returns True if a send was issued."""
    box = await _caption_box(page)
    try:
        if await box.count():
            await box.click(timeout=4000)
            await page.wait_for_timeout(150)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(700)
        return True
    except Exception:  # noqa: BLE001 — fall back to an explicit Send button
        pass
    try:
        btn = page.locator(SEND_BUTTON_SEL).first
        if await btn.count() and await btn.is_visible():
            await btn.click(timeout=4000)
            await page.wait_for_timeout(700)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _deliver(
    page, text: str | None, media_paths: list[str], mode: str, do_send: bool
) -> dict:
    """Compose one message into the open conversation per `mode`, then send it
    when `do_send` (the Press ENTER master switch) is on.

    Modes:
      * text  — type the text only.
      * media — attach the image(s) only.
      * both  — attach the image(s), then type the text as the caption.

    Returns what happened: whether text was written, how much media attached,
    and whether Enter was pressed to send. Nothing is sent when `do_send` is off
    — everything is left staged in the composer, the app's original behaviour."""
    text = " ".join(str(text or "").split())
    result = {
        "mode": mode, "text_written": False, "media_attached": 0,
        "media_via": None, "sent": False, "text": "", "reason": None,
        # kept for the panel's existing "written" tick — composed successfully.
        "written": False,
    }

    if mode in ("media", "both"):
        if not media_paths:
            result["reason"] = "no media attached for this mode"
            return result
        att = await _attach_media(page, media_paths)
        result["media_via"] = att.get("via")
        result["media_verified"] = bool(att.get("verified"))
        # Count it as attached as long as an attach was actually issued. We do NOT
        # bail when the preview wasn't detected: pressing Enter below sends
        # whatever is staged, and if truly nothing attached, Enter on an empty
        # composer sends nothing — so a detection miss can't swallow the image.
        if att.get("via") and att.get("via") != "none":
            result["media_attached"] = att["attached"]
        else:
            # No way to attach was found. In Both mode the image IS part of the
            # message, so don't quietly send the text alone — report it.
            result["reason"] = "image not attached (no photo input/icon found) — not sent"
            return result
        if not att.get("verified"):
            result["reason"] = "attached but preview not confirmed — sent anyway"

    if mode in ("text", "both") and text:
        # Wrapped so a caption hiccup can't throw away an image that already
        # attached — in Both mode the media matters even if the caption misses.
        try:
            written = await _write_message(page, text, for_caption=(mode == "both"))
        except Exception as exc:  # noqa: BLE001
            reason = str(exc).split("\nCall log:")[0].strip()
            written = {"written": False, "text": "", "reason": reason[:160]}
        result["text_written"] = bool(written.get("written"))
        result["text"] = written.get("text", "")
        if not result["text_written"]:
            result["reason"] = written.get("reason")
            # In text mode the text is the whole message — a miss means there is
            # nothing to send, so stop here.
            if mode == "text":
                return result

    if mode == "text" and not text:
        result["reason"] = "no message text"
        return result

    # Composed at least something (text landed or media attached).
    result["written"] = bool(result["text_written"] or result["media_attached"])

    # If Stop was pressed while composing this one, don't fire the send — leave
    # it staged in the composer. Stop must actually stop messages going out.
    if do_send and control.stopping():
        result["reason"] = "stopped before send — left staged"
        return result

    if do_send:
        result["sent"] = await _press_enter_send(page)
        if not result["sent"]:
            result["reason"] = result["reason"] or "could not press Enter to send"
    return result


def _plan_batches(total: int, size_min: int, size_max: int) -> list[int]:
    """Split `total` rows into random-sized batches in [size_min, size_max].

    Sizes are picked per batch and clamped so the last one never overshoots, so
    the sizes always add back up to exactly `total`."""
    size_min = max(1, int(size_min))
    size_max = max(size_min, int(size_max))
    sizes: list[int] = []
    remaining = int(total)
    while remaining > 0:
        step = random.randint(size_min, size_max)
        step = max(1, min(step, remaining))
        sizes.append(step)
        remaining -= step
    return sizes


async def _batch_delay(account_id: int | None, seconds: float,
                       next_batch_no: int, total_batches: int) -> None:
    """Wait `seconds` between batches, publishing a live countdown for the panel.

    The delay stays responsive to Stop (returns at once) and Pause (parks) via
    control.sleep, and writes `next_batch_in` into the campaign progress each
    second so the Campaign view can show the remaining time."""
    state = _campaign.get(account_id)
    deadline = time.monotonic() + max(0.0, seconds)
    activity.log(
        "visit",
        f"Batch done — waiting {int(round(seconds))}s before batch "
        f"{next_batch_no}/{total_batches}",
        "ok",
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or control.stopping():
            break
        if state is not None:
            state.update(
                waiting=True,
                next_batch_in=int(round(remaining)),
                next_batch_total=int(round(seconds)),
                next_batch_no=next_batch_no,
                batches=total_batches,
            )
        await control.sleep(min(1.0, remaining))   # 1s ticks; Stop/Pause aware
    if state is not None:
        state.update(waiting=False, next_batch_in=0)


# The pool of actions the campaign can interleave between rows — each is one of
# the Actions-panel behaviours, called with the running account and picked at
# random. Lazy lambdas: the functions are defined later in this module but all
# exist by the time a campaign runs.
_CAMPAIGN_ACTIONS = [
    ("Home + scroll",   lambda aid: home_actions(account_id=aid)),
    ("Open Reels",      lambda aid: click_menu_item("Reels", account_id=aid)),
    ("Reel Like",       lambda aid: click_reel_action("Like", account_id=aid)),
    ("Reel Comment",    lambda aid: click_reel_action("Comment", account_id=aid)),
    ("Notifications",   lambda aid: click_notification_filters(account_id=aid)),
    ("Search + Follow", lambda aid: open_search(account_id=aid, click_first=True, follow=True)),
]


_CAMPAIGN_ACTION_FNS = dict(_CAMPAIGN_ACTIONS)   # name -> coroutine factory


async def _run_campaign_action(account_id, action_name) -> str:
    """Perform ONE pre-planned action (by name), then return to the DM inbox so
    the next name can be searched. Returns 'done' or 'failed' — a flaky action is
    logged and skipped, never sinking the campaign. Stop propagates out."""
    fn = _CAMPAIGN_ACTION_FNS.get(action_name)
    activity.log("visit", f"Campaign action: {action_name}", "ok")
    status = "done"
    if fn is not None:
        try:
            await fn(account_id)
        except control.Stopped:
            raise
        except Exception as exc:  # noqa: BLE001 — a flaky action mustn't end the run
            status = "failed"
            first = str(exc).splitlines()[0] if str(exc) else exc
            activity.log("visit", f"Campaign action {action_name!r} failed: {first}", "warn")
    try:
        await open_messages(account_id=account_id)   # back to DMs for the next row
    except Exception as exc:  # noqa: BLE001
        activity.log("visit", f"Couldn't reopen Messages after the action: {exc}", "warn")
    return status


async def search_usernames(
    usernames: list[str],
    message: str | None = DEFAULT_MESSAGE,
    account_id: int | None = None,
    messages: list[str] | None = None,
    mode: str = "text",
    media_paths: list[str] | None = None,
    media_per_name: list[str | None] | None = None,
) -> dict:
    """Run a list of usernames through the DM search box one at a time: type the
    name, open the top result, deliver a message into that conversation, then
    move on to the next.

    Each name gets `message` by default, or — when `messages` is given (parallel
    to `usernames`) — its own message from that list. The campaign panel uses
    this to write a randomly-assigned draft per name.

    `mode` picks what is delivered: "text" writes the text, "media" uploads an
    image, "both" uploads an image then writes the text as a caption. The image
    for a name is its own entry in `media_per_name` (parallel to `usernames`, so
    each name can get a different randomly-assigned image); a name with no entry
    there falls back to the shared `media_paths`. Whether it is actually SENT is
    the Config panel's Press ENTER switch: on = press Enter after composing
    (sends it), off = leave it staged in the composer, unsent. A name that finds
    no result, or whose row won't click, is recorded and the run carries on.
    """
    page = account_page(account_id)         # raises if that account has no window

    mode = (mode or "text").lower().strip()
    if mode not in ("text", "media", "both"):
        raise RuntimeError(f"Unknown campaign mode {mode!r}.")
    media_paths = list(media_paths or [])
    per_name_media = list(media_per_name) if media_per_name is not None else None
    has_any_media = bool(media_paths) or bool(per_name_media and any(per_name_media))
    if mode in ("media", "both") and not has_any_media:
        raise RuntimeError(f"{mode!r} mode needs at least one image attached.")

    # Read the send switch once per run, so a mid-run Config edit can't flip
    # half the list from staged to sent.
    do_send = settings.press_enter()

    # Keep each name paired with its own message AND its own assigned image,
    # dropping blank names (and their message/image) together so the lists stay
    # aligned.
    raw_msgs = list(messages) if messages is not None else None
    queue: list[str] = []
    queue_msgs: list[str | None] = []
    queue_media: list[str | None] = []
    for idx, raw in enumerate(usernames):
        name = str(raw).strip().lstrip("@")
        if not name:
            continue
        queue.append(name)
        if raw_msgs is not None and idx < len(raw_msgs):
            queue_msgs.append(raw_msgs[idx])
        else:
            queue_msgs.append(message)
        if per_name_media is not None and idx < len(per_name_media):
            queue_media.append(per_name_media[idx])
        else:
            queue_media.append(None)
    if not queue:
        raise RuntimeError("No usernames to search.")

    # Optional batching: shuffle this account's list, cut it into random-sized
    # batches, and wait a random number of minutes between them. Shuffling first
    # means each batch is a random subset of the selected range rather than the
    # next slice in order.
    bcfg = settings.batch()
    batch_ends: set[int] = set()      # 1-based positions a batch ends after
    batch_of: dict[str, int] = {}     # name -> its batch number (known up front)
    total_batches = 1
    if bcfg["batch_enabled"] and len(queue) > 1:
        order = list(range(len(queue)))
        random.shuffle(order)
        queue = [queue[i] for i in order]
        queue_msgs = [queue_msgs[i] for i in order]
        queue_media = [queue_media[i] for i in order]
        sizes = _plan_batches(len(queue), bcfg["batch_size_min"], bcfg["batch_size_max"])
        total_batches = len(sizes)
        acc = 0
        for size in sizes[:-1]:       # no delay after the final batch
            acc += size
            batch_ends.add(acc)
        # Which batch each name lands in — settled here, so the panel can colour
        # every row by its batch from the start rather than only as it's reached.
        bno = 1
        for pos, nm in enumerate(queue, start=1):
            batch_of[nm] = bno
            if pos in batch_ends:
                bno += 1
        activity.log(
            "visit",
            f"Batching {len(queue)} name(s) into {total_batches} batch(es) of "
            f"{bcfg['batch_size_min']}–{bcfg['batch_size_max']}, "
            f"{bcfg['batch_delay_min']}–{bcfg['batch_delay_max']} min between",
            "ok",
        )

    await open_messages(account_id=account_id)

    results: list[dict] = []
    opened = 0
    written = 0
    sent = 0
    batch_no = 1                      # which batch we're currently working through
    # Plan the interleaved actions UP FRONT — like the batches — so empty action
    # rows show from the start. Positions come from the configured gap (Settings →
    # campaign_action_*), independent of the batch breaks: after a random 3–10
    # (etc.) rows there's an action, then a fresh gap, and so on across the list.
    act_lo = max(1, int(bcfg.get("campaign_action_min", _CAMPAIGN_ACTION_EVERY[0])))
    act_hi = max(act_lo, int(bcfg.get("campaign_action_max", _CAMPAIGN_ACTION_EVERY[1])))
    action_plan: list[dict] = []
    _pos = random.randint(act_lo, act_hi)
    while _pos < len(queue):          # no action after the very last row
        _aname, _ = random.choice(_CAMPAIGN_ACTIONS)
        action_plan.append({"seq": _pos, "action": _aname, "status": "pending"})
        _pos += random.randint(act_lo, act_hi)

    # `results` by reference — the panel polling campaign_progress() sees each
    # name's outcome the moment the loop appends it, so ticks appear as the run
    # goes rather than all at once when the request finally returns.
    _campaign[account_id] = {
        "running": True, "results": results, "done": 0, "total": len(queue),
        "opened": 0, "written": 0, "sent": 0, "current": None,
        "mode": mode, "send": do_send, "account_id": account_id,
        # batching / countdown state, read by the Campaign view
        "waiting": False, "next_batch_in": 0, "next_batch_total": 0,
        "batch_no": batch_no, "batches": total_batches, "batch_map": batch_of,
        "actions": action_plan,      # planned action rows (pending -> done/failed)
    }

    stopped = False
    try:
        yield_state = _campaign[account_id]
        for index, name in enumerate(queue, start=1):
            # Outside the per-name try below on purpose: that block turns any
            # exception into "this name failed" and carries on, which would
            # swallow a Stop and keep the run going through the whole list.
            try:
                await control.checkpoint()
            except control.Stopped:
                stopped = True
                activity.log("visit", f"Campaign stopped before {name!r}", "warn")
                break

            yield_state.update(current=name, done=index - 1)
            entry: dict = {
                "query": name, "opened": False, "error": None,
                "result": None, "message": None,
            }
            try:
                typed = await _type_into_search(page, name)
                if typed != name:
                    # The box holds something other than this name — the results
                    # belong to that, not to us. Never click on a mismatch.
                    raise RuntimeError(f"search box holds {typed!r}, not {name!r}")
                info = await _click_first_result(page)
            except Exception as exc:  # noqa: BLE001 — one bad name must not end the run
                entry["error"] = str(exc).split("\nCall log:")[0].strip()
                activity.log("visit", f"[{index}/{len(queue)}] {name}: {entry['error']}", "warn")
            else:
                if info is None:
                    entry["error"] = "no results"
                    activity.log("visit", f"[{index}/{len(queue)}] {name}: no results", "warn")
                else:
                    entry["opened"] = True
                    entry["result"] = info
                    entry["typed"] = typed
                    opened += 1
                    label = info["name"] or "(unnamed)"
                    if info["username"]:
                        label += f" (@{info['username']})"
                    activity.log("visit", f"[{index}/{len(queue)}] {name} -> {label}", "ok")

                    # Honour Stop/Pause right before a fresh send: Stop halts
                    # here (this name opened but isn't messaged), Pause parks
                    # here — so neither has to wait out the whole current send
                    # and then the next name before taking effect.
                    try:
                        await control.checkpoint()
                    except control.Stopped:
                        stopped = True
                        results.append(entry)
                        activity.log("visit", f"Campaign stopped before sending to {name!r}", "warn")
                        break

                    msg = queue_msgs[index - 1]
                    # This name's own randomly-assigned image, or the shared pool
                    # when it wasn't given one.
                    name_media = queue_media[index - 1]
                    media_for_name = [name_media] if name_media else media_paths
                    # text mode needs a message; media/both always deliver (the
                    # image is the content). Skip only text-with-no-message.
                    if mode != "text" or msg:
                        try:
                            entry["message"] = await _deliver(
                                page, msg, media_for_name, mode, do_send
                            )
                        except Exception as exc:  # noqa: BLE001
                            reason = str(exc).split("\nCall log:")[0].strip()
                            entry["message"] = {
                                "mode": mode, "written": False, "text_written": False,
                                "media_attached": 0, "sent": False, "text": "",
                                "reason": reason,
                            }
                        m = entry["message"]
                        if m.get("written"):
                            written += 1
                            if m.get("sent"):
                                sent += 1
                            parts = []
                            if m.get("media_attached"):
                                confirm = "confirmed" if m.get("media_verified") else "unconfirmed preview"
                                parts.append(
                                    f"uploaded {m['media_attached']} image(s) via {m.get('media_via')} ({confirm})"
                                )
                            if m.get("text_written"):
                                parts.append("wrote caption" if mode == "both" else "wrote text")
                            what = " + ".join(parts) or "composed"
                            state = "sent" if m.get("sent") else "staged (not sent)"
                            activity.log("visit", f"    {what} to {label} — {state}", "ok")
                        else:
                            activity.log(
                                "visit",
                                f"    did not deliver to {label}: {m.get('reason')}",
                                "warn",
                            )

            # Which batch this name ran in, so the Campaign view can show it per
            # row. Stamped here rather than planned up front because the batch
            # split is decided per account at run time.
            entry["batch_no"] = batch_no
            entry["batches"] = total_batches
            # Appended before the pause below, so a tick shows the moment the
            # name is done rather than a couple of seconds later.
            results.append(entry)
            yield_state.update(done=index, opened=opened, written=written, sent=sent)

            if index < len(queue):
                # If an action was planned after this row, run it now and mark it
                # done/failed. Independent of the batch breaks below.
                planned = next((a for a in action_plan
                                if a["seq"] == index and a["status"] == "pending"), None)
                if planned:
                    try:
                        planned["status"] = await _run_campaign_action(
                            account_id, planned["action"])
                    except control.Stopped:
                        stopped = True
                        break
                    yield_state.update(actions=list(action_plan))

                if index in batch_ends:
                    # End of a batch — wait a random number of minutes before the
                    # next one, with a live countdown. A random delay of 0 (both
                    # bounds 0) just rolls straight on.
                    batch_no += 1
                    yield_state.update(batch_no=batch_no)
                    delay_s = random.uniform(
                        bcfg["batch_delay_min"], bcfg["batch_delay_max"]
                    ) * 60
                    if delay_s > 0:
                        await _batch_delay(account_id, delay_s, batch_no, total_batches)
                    # Stop pressed during the wait ends the run at the next
                    # checkpoint; break now so no further batch is started.
                    if control.stopping():
                        stopped = True
                        break
                else:
                    # Interruptible: Stop wakes it immediately, Pause parks it — the
                    # top-of-loop checkpoint then turns a Stop into the break.
                    await control.sleep(random.uniform(*BETWEEN_SEARCH_RANGE_S))
    finally:
        # However the run ends, stop the panel polling a campaign that is over.
        _campaign[account_id].update(
            running=False, current=None, done=len(results),
            opened=opened, written=written, sent=sent,
            waiting=False, next_batch_in=0,
        )
        # Record who this run actually USED — a message composed (staged) or sent
        # into their conversation — as +1 to their used_count in usernames_bio.json.
        # Whatever was reached before a Stop still counts.
        try:
            used = [r["query"] for r in results
                    if r.get("message") and (r["message"].get("written")
                                             or r["message"].get("sent"))]
            if used:
                from backend import db

                await asyncio.to_thread(db.bump_username_uses, used)
                activity.log("visit", f"Recorded campaign use of {len(used)} handle(s)", "ok")
        except Exception as exc:  # noqa: BLE001 — usage tracking must never sink the run
            activity.log("visit", f"Couldn't record campaign usage: {exc}", "warn")
        # …and how each reached handle ENDED: "sent" (or staged when Press ENTER
        # is off), or "failed" with the reason — search miss, nothing composed,
        # image not attached, send not issued. Names the run never reached (Stop)
        # are left as they were.
        try:
            sent_names, failed_names = [], {}
            for r in results:
                name = r.get("query")
                msg = r.get("message")
                if not name:
                    continue
                if r.get("error") or not r.get("opened"):
                    failed_names[name] = r.get("error") or "no results"
                elif msg is None:
                    continue                       # stopped before this one was messaged
                elif msg.get("sent") or (msg.get("written") and not do_send):
                    sent_names.append(name)
                else:
                    failed_names[name] = msg.get("reason") or "message not sent"
            if sent_names or failed_names:
                from backend import db

                await asyncio.to_thread(db.record_campaign_outcomes, sent_names, failed_names)
                activity.log(
                    "visit",
                    f"Recorded outcomes: {len(sent_names)} sent, {len(failed_names)} failed",
                    "ok" if not failed_names else "warn",
                )
        except Exception as exc:  # noqa: BLE001
            activity.log("visit", f"Couldn't record campaign outcomes: {exc}", "warn")

    # Counts describe what was ATTEMPTED, not the queue: a run stopped after 3
    # of 53 has not "missed" 50 — it never reached them.
    attempted = len(results)
    wrote_note = ""
    if written:
        what = "per-name messages" if messages is not None else repr(message)
        outcome = f"sent to {sent}" if do_send else f"staged in {written} (nothing sent)"
        wrote_note = f"; {mode} — composed {what} into {written}, {outcome}"
    activity.log(
        "visit",
        f"Search campaign {'stopped' if stopped else 'done'}: "
        f"opened {opened} of {attempted} attempted" + wrote_note,
        "ok",
    )
    return {
        "count": attempted,
        "opened": opened,
        "missed": attempted - opened,
        "written": written,
        "sent": sent,
        "mode": mode,
        "send": do_send,
        "message": message,
        "results": results,
        "url": page.url,
        "stopped": stopped,
        "queued": len(queue),
        "not_reached": len(queue) - attempted,
    }


# The sidebar's own Search — the 4th icon in the rail (Home, Reels, Messages,
# Search, Notifications, Create, Profile). Anchored on the svg's aria-label,
# which is stable; the class names next to it are obfuscated and rotate on every
# Instagram deploy. This is Instagram's GLOBAL search panel, a different thing
# from the search box inside the DM inbox.
SIDEBAR_SEARCH_SEL = 'svg[aria-label="Search"]'
SEARCH_PANEL_INPUT_SEL = (
    'input[placeholder*="search" i], input[aria-label*="search" i], '
    'input[name="searchInput"]'
)

# Opening the Search flyout: how long one click gets, and how many whole goes the
# opener gets (each failed go reloads the feed first, so the rail is rebuilt).
SEARCH_CLICK_TIMEOUT_MS = 5000
SEARCH_PANEL_ATTEMPTS = 3


def _one_line(exc: Exception) -> str:
    """A Playwright error's first line — its "Call log:" tail runs to pages and
    the panel has one line to show it in."""
    lines = str(exc).split("\nCall log:")[0].strip().splitlines()
    return lines[0].strip() if lines else repr(exc)


async def _open_search_panel_once(page) -> None:
    """One go at opening the sidebar Search flyout and focusing its input.

    Instagram's rail is a React nav whose real click target moves between deploys
    — the svg, the <a> around it, or a <div role="button"> wrapper — and ANY of
    them can be covered for a moment by a story tray, a toast, or a modal that is
    still fading out. A plain click on the wrong one of those is exactly the
    "Locator.click: Timeout 5000ms exceeded" that used to end the whole run, so
    every plausible target is tried in turn, then force-clicked past whatever is
    covering it, then finally clicked synthetically. A synthetic event is a poor
    imitation of a press, but an opened panel beats a dead scrape.

    Raises if the panel still isn't open and focused at the end of all that.
    """
    icon = page.locator(SIDEBAR_SEARCH_SEL).first
    await icon.wait_for(state="visible", timeout=8000)

    link = icon.locator("xpath=ancestor::a[1]")
    button = icon.locator('xpath=ancestor::*[@role="button"][1]')
    targets = []
    if await link.count():
        targets.append(link)
    if await button.count():
        targets.append(button)
    targets.append(icon)

    async def _try(press) -> bool:
        for target in targets:
            try:
                await press(target)
                return True
            except Exception:  # noqa: BLE001 — this target didn't take; try the next
                continue
        return False

    clicked = (
        await _try(lambda t: t.click(timeout=SEARCH_CLICK_TIMEOUT_MS))
        or await _try(lambda t: t.click(timeout=SEARCH_CLICK_TIMEOUT_MS, force=True))
        or await _try(lambda t: t.dispatch_event("click"))
    )
    if not clicked:
        raise RuntimeError("The sidebar Search button could not be clicked.")

    # No blind "let the panel slide open" wait — waiting for the input IS that wait.
    box = page.locator(SEARCH_PANEL_INPUT_SEL).first
    await box.wait_for(state="visible", timeout=8000)
    try:
        await box.click(timeout=2500)
    except Exception:  # noqa: BLE001 — an overlay is in the way; focus works too
        await box.focus()


async def _open_search_panel_resilient(page, *, attempts: int = SEARCH_PANEL_ATTEMPTS,
                                       refresh: bool = True) -> bool:
    """Open the Search flyout, reloading the feed between failed goes.

    A rail that won't take a click is nearly always a page that has drifted —
    a stale render, a stuck overlay, a half-navigated route — and a reload fixes
    it. Returns True once the panel is open, False if every go failed; it never
    raises, so a caller can carry on with the other technique instead of dying.
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            await _open_search_panel_once(page)
            if attempt > 1:
                activity.log("visit", f"Search panel opened on try {attempt}", "ok")
            return True
        except Exception as exc:  # noqa: BLE001
            activity.log(
                "visit",
                f"Search panel wouldn't open (try {attempt}/{attempts}): {_one_line(exc)}",
                "warn",
            )
            if attempt >= attempts or control.stopping():
                return False
            if refresh:
                await _refresh_feed(page)       # a rebuilt rail is a clickable rail
            else:
                await page.wait_for_timeout(600)
    return False


# Search seeds typed into Search to shake out a fresh set of suggestions. The
# harvest draws a random SHAPE each round (see _random_search_seed) so a run
# explores letters, numbers, the username-legal specials ( . _ ), and mixes of
# all three — far more variety than one fixed 2-letter seed. The Actions
# Search+Follow still uses a plain 2-letter alpha seed, which reliably returns a
# result to open.
_SEARCH_SEED_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_SEARCH_SEED_LEN = 2               # the reliable-alpha length (used for Follow)
_SEARCH_ROUNDS_FALLBACK = 100      # only used if the setting can't be read
_SEED_DIGITS = "0123456789"
_SEED_SPECIAL = "._"               # the only specials an Instagram username allows


def _rand_str(pool: str, n: int) -> str:
    """`n` random characters from `pool` (at least 1)."""
    return "".join(random.choice(pool) for _ in range(max(1, n)))


def _mix_seed(*pools: str) -> str:
    """A shuffled seed with at least one character from EACH given pool, plus a
    few extra from the combined pool — so it's a genuine combination."""
    chars = [random.choice(p) for p in pools]           # one guaranteed per pool
    combined = "".join(pools)
    for _ in range(random.randint(0, 2)):               # 0-2 extra chars
        chars.append(random.choice(combined))
    random.shuffle(chars)
    return "".join(chars)


def _random_search_seed() -> str:
    """One random search seed, chosen from nine shapes:
      1-4) letters of length 1, 2, 4, 5
      5)   a 1-4 digit number
      6)   1-3 specials ( . _ )
      7)   numbers + specials
      8)   numbers + letters
      9)   numbers + specials + letters
    """
    A, D, S = _SEARCH_SEED_ALPHABET, _SEED_DIGITS, _SEED_SPECIAL
    shapes = (
        lambda: _rand_str(A, 1),
        lambda: _rand_str(A, 2),
        lambda: _rand_str(A, 4),
        lambda: _rand_str(A, 5),
        lambda: _rand_str(D, random.randint(1, 4)),
        lambda: _rand_str(S, random.randint(1, 3)),
        lambda: _mix_seed(D, S),
        lambda: _mix_seed(D, A),
        lambda: _mix_seed(D, S, A),
    )
    return random.choice(shapes)()

# Pull the usernames out of the open search-results panel.
#
# Anchored on the row's own <a href="/name/"> plus its avatar image: every result
# draws one, and the class names beside them are obfuscated and rotate on every
# Instagram deploy, so matching those would break within days. Requiring the
# avatar is what separates a result row from the rail's own links — the sidebar's
# Profile entry is also a single-segment href.
_HARVEST_SEARCH_JS = """({ reserved, self }) => {
  const skip = new Set(reserved);
  const mine = (self || '').toLowerCase();
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href^="/"]')) {
    if (a.closest('nav')) continue;              // the rail, not a result
    if (!a.querySelector('img[alt*="profile picture" i]')) continue;
    // Read the attribute rather than a.href: the resolved property depends on
    // the document origin, and the raw path is all we need.
    let path = a.getAttribute('href') || '';
    if (!path.startsWith('/')) continue;
    path = path.split('?')[0].split('#')[0];
    const parts = path.split('/').filter(Boolean);
    if (parts.length !== 1) continue;            // /name/ only — not /explore/p/...
    const name = parts[0];
    const key = name.toLowerCase();
    if (!name || seen.has(key) || key === mine) continue;
    if (skip.has(key)) continue;
    if (!/^[A-Za-z0-9._]+$/.test(name)) continue;
    seen.add(key);
    out.push(name);
  }
  return out;
}"""


def _account_username(account_id: int | None) -> str:
    """The account's own Instagram handle, so the harvest doesn't collect the
    logged-in user from their own avatar. Best-effort — an empty string just
    means one extra name to filter by hand."""
    if account_id is None:
        return ""
    try:
        from backend import db

        account = db.get_account(account_id) or {}
        return str(account.get("login") or "").strip().lstrip("@")
    except Exception:  # noqa: BLE001 — never let a lookup break the scrape
        return ""


# The topmost result row in the open Search panel: an <a href="/name/"> with a
# profile-picture avatar, not in the nav rail. Tags it so Playwright can click it
# with a trusted click (real navigation), and returns its username.
_MARK_FIRST_RESULT_JS = """() => {
  const rows = [...document.querySelectorAll('a[href^="/"]')].filter(a => {
    if (a.closest('nav')) return false;
    if (!a.querySelector('img[alt*="profile picture" i]')) return false;
    const parts = (a.getAttribute('href') || '').split('?')[0].split('#')[0]
      .split('/').filter(Boolean);
    return parts.length === 1;
  });
  rows.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  const first = rows[0];
  if (!first) return null;
  first.setAttribute('data-first-result', '1');
  return first.getAttribute('href').split('/').filter(Boolean)[0];
}"""


async def _click_first_search_result(page) -> str | None:
    """Open the first result in the Search panel. Returns its username, or None
    if there were no results."""
    name = await page.evaluate(_MARK_FIRST_RESULT_JS)
    if not name:
        return None
    try:
        await page.locator('[data-first-result="1"]').first.click(timeout=5000)
    except Exception:  # noqa: BLE001 — click blocked; go straight to the profile
        await page.goto(f"{HOME}{name}/", wait_until="domcontentloaded")
    return name


# The profile's Follow action. Matched by the button whose visible text is
# EXACTLY "Follow" (the nested-div structure Instagram uses), so it never hits
# "Following" / "Follow back" / "Requested". Tagged for a trusted Playwright
# click. Class names are obfuscated and rotate, so text is the only stable hook.
_FIND_FOLLOW_JS = """() => {
  const btns = [...document.querySelectorAll('button, [role="button"]')];
  const visible = b => {
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const follow = btns.find(b => b.textContent.trim() === 'Follow' && visible(b));
  if (follow) { follow.setAttribute('data-follow-btn', '1'); return 'follow'; }
  const done = btns.find(b => ['Following', 'Requested'].includes(b.textContent.trim()));
  return done ? 'already' : 'none';
}"""


async def _click_follow(page) -> str:
    """Click Follow on the open profile. Returns 'followed', 'already-following',
    or 'no-button'. Never clicks a 'Following'/'Requested' button (already done)."""
    try:
        state = await page.evaluate(_FIND_FOLLOW_JS)
    except Exception:  # noqa: BLE001
        return "no-button"
    if state == "already":
        activity.log("visit", "Profile already followed", "ok")
        return "already-following"
    if state != "follow":
        activity.log("visit", "No Follow button found on the profile", "warn")
        return "no-button"
    try:
        await page.locator('[data-follow-btn="1"]').first.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        activity.log("visit", f"Follow click failed: {exc}", "warn")
        return "no-button"
    activity.log("visit", "Clicked Follow", "ok")
    return "followed"


# Every menu item in the Instagram left rail, so they can be clicked in turn.
# Anchored on each item's own icon aria-label (Home, Search, Reels, …) or, for
# the Profile item, its avatar image — those are stable, the class names next to
# them are obfuscated and rotate on every deploy. Restricted to the left rail so
# links elsewhere on the page aren't mistaken for menu items.
_MENU_ITEMS_JS = """() => {
  const seen = new Set();
  const out = [];
  const push = (label, sel) => {
    if (!label || seen.has(label)) return;
    seen.add(label);
    out.push({ label, sel });
  };
  for (const svg of document.querySelectorAll('svg[aria-label]')) {
    const a = svg.closest('a[role="link"], a[href], [role="button"]');
    if (!a) continue;
    const r = a.getBoundingClientRect();
    if (r.left > 260 || r.width === 0) continue;        // left rail only
    const label = svg.getAttribute('aria-label').trim();
    push(label, `svg[aria-label="${label.replace(/"/g, '\\\\"')}"]`);
  }
  // Profile item is an avatar image, not a labelled icon.
  for (const img of document.querySelectorAll('img[alt*="profile picture" i]')) {
    const a = img.closest('a[role="link"], a[href]');
    if (!a) continue;
    const r = a.getBoundingClientRect();
    if (r.left > 260 || r.width === 0) continue;
    push('Profile', 'img[alt*="profile picture" i]');
    break;
  }
  return out;
}"""


async def click_all_menu(account_id: int | None = None) -> dict:
    """Click every button in the Instagram left-rail menu, one after another.

    The Actions panel's menu button: it walks the sidebar (Home, Search, Reels,
    Messages, Notifications, Create, Profile, …) and clicks each in turn, pausing
    between and pressing Escape so an opened panel (Search/Notifications/Create)
    doesn't block the next click. Returns which items were clicked.
    """
    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # A create/upload item can pop a file chooser; auto-dismiss so nothing hangs.
    page.on("filechooser", lambda fc: asyncio.ensure_future(fc.set_files([])))

    try:
        items = await page.evaluate(_MENU_ITEMS_JS)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Couldn't read the Instagram menu ({exc}).") from exc
    if not items:
        raise RuntimeError(
            "No menu items found. Is the window on a logged-in Instagram page?"
        )

    clicked: list[str] = []
    failed: list[str] = []
    for item in items:
        await control.checkpoint()             # Stop lands between clicks
        label, sel = item["label"], item["sel"]
        try:
            icon = page.locator(sel).first
            link = icon.locator("xpath=ancestor::a[1]")
            target = link if await link.count() else icon
            try:
                await target.click(timeout=5000)
            except Exception:  # noqa: BLE001 — fall back to the icon itself
                await icon.click(timeout=5000)
            clicked.append(label)
            activity.log("visit", f"Clicked menu: {label}", "ok")
        except Exception as exc:  # noqa: BLE001 — one bad item mustn't stop the rest
            failed.append(label)
            activity.log("visit", f"Menu {label!r} not clickable: "
                         f"{str(exc).splitlines()[0][:80]}", "warn")
        await page.wait_for_timeout(1200)      # let it navigate / open
        try:
            await page.keyboard.press("Escape")   # close any panel it opened
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(300)

    activity.log("visit", f"Menu walk done — clicked {len(clicked)}: "
                 + ", ".join(clicked), "ok")
    return {"clicked": clicked, "failed": failed,
            "count": len(clicked), "url": page.url}


# These live inside the "More" flyout (the ☰ at the bottom of the rail), not the
# rail itself — so they need More opened first.
_MENU_MORE_ITEMS = {"settings", "also from meta", "your activity", "saved",
                    "switch appearance", "report a problem", "log out"}


async def _click_menu_match(loc) -> bool:
    """Click the first element `loc` matches, climbing to its clickable ancestor
    (an <a> or role=button) so the nav handles the click. False if nothing there."""
    try:
        el = loc.first
        if not await el.count():
            return False
    except Exception:  # noqa: BLE001
        return False
    for anc in ("xpath=ancestor-or-self::a[1]",
                "xpath=ancestor-or-self::*[@role='button'][1]"):
        cand = el.locator(anc)
        try:
            if await cand.count():
                await cand.first.click(timeout=5000)
                return True
        except Exception:  # noqa: BLE001 — try the next ancestor / the element itself
            pass
    try:
        await el.click(timeout=5000)
        return True
    except Exception:  # noqa: BLE001
        return False


# Click a link/button whose FULL visible text is exactly `text` — for the More
# flyout options (Settings, Your activity, Saved, …), which are <a>/[role=button]
# with the label in nested spans. Marks it for a trusted click.
_MARK_TEXT_EL_JS = """(text) => {
  document.querySelectorAll('[data-text-el]').forEach(e => e.removeAttribute('data-text-el'));
  const want = text.trim();
  // Visible text of an element, EXCLUDING any <svg> subtree. Instagram puts a
  // <title> inside each menu icon, so raw textContent duplicates the label
  // ("SettingsSettings") or prepends the icon name ("Theme iconSwitch
  // appearance") -- an exact === match on textContent never fires. Skipping svg
  // gives the true visible label.
  const vis = (el) => {
    let t = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) t += n.nodeValue;
      else if (n.nodeType === 1 && n.tagName.toLowerCase() !== 'svg') t += vis(n);
    }
    return t;
  };
  const els = [...document.querySelectorAll('a, button, [role="button"], [role="link"]')];
  const el = els.find(e => vis(e).trim() === want &&
                           e.getBoundingClientRect().width > 0);
  if (!el) return false;
  el.setAttribute('data-text-el', '1');
  return true;
}"""


async def _click_by_text(page, text: str) -> bool:
    """Click the visible <a>/button/[role] whose whole text equals `text`."""
    try:
        if not await page.evaluate(_MARK_TEXT_EL_JS, text):
            return False
        await page.locator('[data-text-el="1"]').first.click(timeout=5000)
        return True
    except Exception:  # noqa: BLE001
        return False


async def click_settings_option(option: str, account_id: int | None = None) -> dict:
    """Open the Settings/More menu, click one option (Settings, Your activity,
    Saved, Switch appearance, Report a problem), then close.

    Each Actions-panel Settings button maps to one of these. The menu is the ☰
    flyout at the bottom of the rail; the option is clicked by its exact text.
    """
    label = (option or "").strip()
    if not label:
        raise ValueError("No settings option given.")

    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # 1. Open the More/Settings flyout (bottom of the rail).
    await _click_menu_match(
        page.locator('svg[aria-label="More"], svg[aria-label="Settings"]'))
    await page.wait_for_timeout(1000)

    # 2. Click the option by its exact text.
    if not await _click_by_text(page, label):
        raise RuntimeError(f"Couldn't find {label!r} in the Settings menu.")
    activity.log("visit", f"Settings: clicked {label}", "ok")
    await page.wait_for_timeout(1600)          # let it navigate / open its dialog

    # 3. Close whatever it opened (dialog/menu). Harmless after a navigation.
    try:
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass

    return {"option": label, "clicked": True, "url": page.url}


async def click_menu_item(item: str, account_id: int | None = None) -> dict:
    """Click ONE named button in the Instagram menu (Home, Search, New post, …).

    Each Actions-panel button maps to one item. Rail items are matched by their
    aria-label; items that live in the bottom "More" flyout (Settings, Also from
    Meta, …) get More opened first, then are clicked by their text.
    """
    label = (item or "").strip()
    if not label:
        raise ValueError("No menu item given.")

    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # A create/upload item may pop a file chooser — auto-dismiss so nothing hangs.
    page.on("filechooser", lambda fc: asyncio.ensure_future(fc.set_files([])))

    # 1. Direct hit: an element labelled with this item (the rail icons).
    clicked = await _click_menu_match(page.locator(f'[aria-label="{label}" i]'))

    # 2. Otherwise it's inside the More flyout — open More, then click by text.
    if not clicked and label.lower() in _MENU_MORE_ITEMS:
        await _click_menu_match(
            page.locator('[aria-label="More" i], [aria-label="Settings" i]'))
        await page.wait_for_timeout(900)
        clicked = await _click_menu_match(page.get_by_text(label, exact=True))

    # 3. Last resort: any visible element with exactly this text.
    if not clicked:
        clicked = await _click_menu_match(page.get_by_text(label, exact=True))

    if not clicked:
        raise RuntimeError(f"Couldn't find {label!r} in the Instagram menu.")
    activity.log("visit", f"Clicked menu: {label}", "ok")
    await page.wait_for_timeout(600)
    return {"item": label, "clicked": True, "url": page.url}


# The reel comment box. Instagram uses two shapes depending on the layout: a
# plain <input>/<textarea> with placeholder "Add a comment…", or a Lexical
# contenteditable. Match either — by placeholder/aria-label first, then the lone
# visible textbox the Comment click reveals.
COMMENT_BOX_SEL = (
    'input[placeholder*="comment" i], input[aria-label*="comment" i], '
    'textarea[placeholder*="comment" i], textarea[aria-label*="comment" i], '
    '[contenteditable="true"][aria-label*="comment" i], '
    '[contenteditable="true"][aria-placeholder*="comment" i], '
    '[contenteditable="true"][role="textbox"]'
)

# Click a button/[role=button] whose visible text is EXACTLY `text` (e.g. "Post").
# Text is the stable hook; the class names rotate. Marks it for a trusted click,
# clearing any prior marker first.
_CLICK_TEXT_BTN_JS = """(text) => {
  document.querySelectorAll('[data-text-btn]').forEach(e => e.removeAttribute('data-text-btn'));
  const els = [...document.querySelectorAll('button, [role="button"]')];
  const el = els.find(b => b.textContent.trim() === text &&
                           b.getBoundingClientRect().width > 0);
  if (!el) return false;
  el.setAttribute('data-text-btn', '1');
  return true;
}"""


async def _click_button_text(page, text: str) -> bool:
    """Click the visible button whose text is exactly `text`. False if none."""
    try:
        if not await page.evaluate(_CLICK_TEXT_BTN_JS, text):
            return False
        await page.locator('[data-text-btn="1"]').first.click(timeout=4000)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _type_reel_comment(page, text: str) -> bool:
    """Type `text` into the reel's comment box, then press Enter to post it.
    The box is React/Lexical-controlled, so it's typed key by key (setting .value
    wouldn't fire the events the editor listens for)."""
    box = page.locator(COMMENT_BOX_SEL).first
    try:
        await box.wait_for(state="visible", timeout=6000)
    except Exception:  # noqa: BLE001
        activity.log("visit", "Comment box didn't appear", "warn")
        return False
    try:
        await box.click(timeout=4000)             # focus it, the way a user does
    except Exception:  # noqa: BLE001 — overlay; focusing works too
        try:
            await box.focus()
        except Exception:  # noqa: BLE001
            return False
    await page.wait_for_timeout(200)
    await page.keyboard.type(text, delay=50)      # real keystrokes
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")            # Enter posts the comment
    activity.log("visit", f"Commented {text!r} (Enter)", "ok")
    await page.wait_for_timeout(500)
    return True


# The action bar on a reel, in order. The like toggle is one entry that matches
# either state (Like or Unlike) so it's pressed once — not liked then unliked.
# Anchored on the svg aria-labels, which are stable across Instagram's obfuscated
# class churn.
_REEL_ACTIONS = [
    ("Like",    'svg[aria-label="Like"], svg[aria-label="Unlike"]'),
    ("Comment", 'svg[aria-label="Comment"]'),
    ("Repost",  'svg[aria-label="Repost"]'),
    ("Share",   'svg[aria-label="Share"]'),
    ("Save",    'svg[aria-label="Save"]'),
    ("More",    'svg[aria-label="More"]'),
]

# A bank of short, positive reel comments. One is picked at random each time the
# Reels action reaches the Comment step, then TYPED key-by-key (never pasted), so
# every reel gets a varied, human-looking comment.
REEL_COMMENTS = [
    "Very informative",
    "Good",
    "Nice",
    "Awesome",
    "Great content",
    "Love this",
    "So helpful",
    "Amazing",
    "Well done",
    "Keep it up",
    "Fantastic",
    "Really cool",
    "This is great",
    "Super useful",
    "Nicely done",
]


def _pick_reel_comment() -> str:
    """A random comment from the bank (REEL_COMMENTS)."""
    return random.choice(REEL_COMMENTS)


async def click_reel_actions(account_id: int | None = None,
                             comment: str | None = None) -> dict:
    """Open Reels, then click the current reel's action buttons one by one
    (Like, Comment, Repost, Share, Save, More).

    The Actions panel's Reels button. Each button is clicked once, with a pause
    and an Escape after — Comment/Share/More/Save open a panel or dialog, and
    Escape closes it before the next click. On the Comment step, a comment is
    typed into the box and posted: `comment` if one was passed, otherwise a
    random pick from REEL_COMMENTS. Returns what was done.
    """
    # One random comment from the bank unless a specific one was passed in.
    # Picked once, up front, so the log + response report exactly what's typed.
    comment = (comment or "").strip() or _pick_reel_comment()

    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # 1. Open Reels (the sidebar item), then let a reel load.
    if not await _click_menu_match(page.locator('svg[aria-label="Reels"]')):
        await page.goto(f"{HOME}reels/", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # 2. Click each action in turn — the first (current reel's) match only.
    clicked: list[str] = []
    failed: list[str] = []
    commented = False
    for name, sel in _REEL_ACTIONS:
        await control.checkpoint()             # Stop lands between actions
        loc = page.locator(sel).first
        try:
            if not await loc.count():
                continue                       # this action isn't present
        except Exception:  # noqa: BLE001
            continue
        if await _click_menu_match(page.locator(sel)):
            clicked.append(name)
            activity.log("visit", f"Reel: clicked {name}", "ok")
        else:
            failed.append(name)
            activity.log("visit", f"Reel: {name} not clickable", "warn")
            continue

        # The Comment click opens the box — type the comment and post it.
        if name == "Comment" and comment:
            await page.wait_for_timeout(1000)   # box appears
            commented = await _type_reel_comment(page, comment)
            await page.wait_for_timeout(600)
            continue                            # don't Escape a posted comment

        await page.wait_for_timeout(1200)      # let a panel/dialog open
        try:
            await page.keyboard.press("Escape")   # close it before the next
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(400)

    activity.log("visit", f"Reel actions done — clicked {len(clicked)}: "
                 + ", ".join(clicked), "ok")
    return {"clicked": clicked, "failed": failed, "commented": commented,
            "comment": comment if commented else None,
            "count": len(clicked), "url": page.url}


# name -> selector, so a single reel action can be looked up by name.
_REEL_ACTION_SEL = dict(_REEL_ACTIONS)


async def click_reel_action(action: str, account_id: int | None = None,
                            comment: str | None = None) -> dict:
    """Open Reels and click ONE reel action (Like, Comment, Repost, Share, Save,
    or More) — one action per call.

    Each Actions-panel Reels button maps to one of these. Opens Reels, clicks the
    named action once, then Escape-closes whatever it opened. On the Comment
    action a random comment from REEL_COMMENTS is typed (never pasted) and posted
    with Enter — `comment`, if passed, overrides the random pick. Returns what
    was done.
    """
    name = (action or "").strip()
    sel = _REEL_ACTION_SEL.get(name)
    if not sel:
        raise ValueError(f"Unknown reel action: {action!r}")

    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # 1. Open Reels, let a reel load.
    if not await _click_menu_match(page.locator('svg[aria-label="Reels"]')):
        await page.goto(f"{HOME}reels/", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # 2. Click just this one action on the current reel.
    await control.checkpoint()
    loc = page.locator(sel).first
    try:
        if not await loc.count():
            raise RuntimeError(f"Reel action {name!r} isn't on this reel.")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Reel action {name!r} isn't on this reel.") from exc
    if not await _click_menu_match(page.locator(sel)):
        raise RuntimeError(f"Reel action {name!r} wasn't clickable.")
    activity.log("visit", f"Reel: clicked {name}", "ok")

    # 3. Comment types + posts; the others just close what they opened.
    commented = False
    comment_text: str | None = None
    if name == "Comment":
        comment_text = (comment or "").strip() or _pick_reel_comment()
        await page.wait_for_timeout(1000)              # box appears
        commented = await _type_reel_comment(page, comment_text)
    else:
        await page.wait_for_timeout(1200)              # a panel/dialog opens
        try:
            await page.keyboard.press("Escape")        # close it
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(400)

    return {"action": name, "clicked": True, "commented": commented,
            "comment": comment_text if commented else None, "url": page.url}


# The Notifications panel's filter tabs (All, People you follow, Comments, …)
# each carry aria-selected — a distinctive hook that separates them from every
# other button on the page. Text within is what we click by.
_NOTIF_TABS_JS = """() => [...document.querySelectorAll('[role="button"][aria-selected]')]
  .filter(el => el.getBoundingClientRect().width > 0)
  .map(el => el.textContent.trim())
  .filter(Boolean);"""

_MARK_NOTIF_TAB_JS = """(label) => {
  document.querySelectorAll('[data-notif-tab]').forEach(e => e.removeAttribute('data-notif-tab'));
  const el = [...document.querySelectorAll('[role="button"][aria-selected]')]
    .find(e => e.textContent.trim() === label && e.getBoundingClientRect().width > 0);
  if (!el) return false;
  el.setAttribute('data-notif-tab', '1');
  return true;
}"""


async def click_notification_filters(account_id: int | None = None) -> dict:
    """Open Notifications, then click each of its filter tabs one by one
    (All, People you follow, Comments, Follows, Tags & mentions, Verified).

    The Actions panel's Notifications button. Tabs are found by their
    aria-selected marker and clicked by their text. Returns which were clicked.
    """
    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # Open the Notifications panel (no URL — it's a flyout, so it must be clicked).
    if not await _click_menu_match(page.locator('svg[aria-label="Notifications"]')):
        raise RuntimeError(
            "Couldn't open Notifications. Is the window on a logged-in Instagram page?"
        )
    await page.wait_for_timeout(2200)          # let the panel + tabs render

    try:
        labels = await page.evaluate(_NOTIF_TABS_JS)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Couldn't read the notification tabs ({exc}).") from exc
    if not labels:
        raise RuntimeError("No notification filter tabs found — the panel may still be loading.")

    clicked: list[str] = []
    failed: list[str] = []
    for label in labels:
        await control.checkpoint()             # Stop lands between tabs
        try:
            if await page.evaluate(_MARK_NOTIF_TAB_JS, label):
                await page.locator('[data-notif-tab="1"]').first.click(timeout=5000)
                clicked.append(label)
                activity.log("visit", f"Notifications: clicked {label!r}", "ok")
            else:
                failed.append(label)
        except Exception:  # noqa: BLE001 — one bad tab mustn't stop the rest
            failed.append(label)
            activity.log("visit", f"Notifications: {label!r} not clickable", "warn")
        await page.wait_for_timeout(1000)      # let the filtered list load

    # Close the Notifications panel with its X (svg aria-label="Close").
    closed = False
    try:
        closed = await _click_menu_match(page.locator('svg[aria-label="Close"]'))
        if closed:
            activity.log("visit", "Closed the Notifications panel", "ok")
        else:
            await page.keyboard.press("Escape")   # fall back to Escape
    except Exception:  # noqa: BLE001
        pass

    activity.log("visit", f"Notification filters done — clicked {len(clicked)}: "
                 + ", ".join(clicked), "ok")
    return {"clicked": clicked, "failed": failed, "closed": closed,
            "count": len(clicked), "url": page.url}


# A profile's tab bar (Posts, Reels, Reposts, Tagged). They sit in a
# [role="tablist"] of <a> links — the tablist scopes them, and each keeps its
# label in the svg's aria-label or <title>. href is stable across the navigation
# each tab triggers, so it's the click key.
_PROFILE_TABS_JS = """() => [...document.querySelectorAll('[role="tablist"] a[href]')]
  .filter(a => a.getBoundingClientRect().width > 0)
  .map(a => {
    const svg = a.querySelector('svg');
    const label = (svg && (svg.getAttribute('aria-label')
      || (svg.querySelector('title') && svg.querySelector('title').textContent))) || a.getAttribute('href');
    return { href: a.getAttribute('href'), label: label.trim() };
  });"""


async def home_actions(account_id: int | None = None) -> dict:
    """Open Home, scroll a little, click a username, then click each tab on the
    profile that opens (Posts, Reels, Reposts, Tagged).

    The Actions panel's Home button. Reuses the scrape's username collector
    (_collect_visible_usernames) to find who's in the feed.
    """
    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # 1. Home feed.
    if not await _click_menu_match(page.locator('svg[aria-label="Home"]')):
        await page.goto(HOME, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    # 2. Scroll a little, gathering visible usernames (the scrape's own collector).
    names: list[str] = []
    for _ in range(5):
        await control.checkpoint()
        for found in await _collect_visible_usernames(page):
            if found not in names:
                names.append(found)
        if names:
            break
        try:
            await page.evaluate("() => window.scrollBy(0, Math.round(window.innerHeight * 0.7))")
        except Exception:  # noqa: BLE001
            pass
        await control.sleep(1.2)
    if not names:
        raise RuntimeError("No usernames found in the feed to open.")

    # 3. Click the first username → their profile.
    target = names[0]
    if not await _click_menu_match(page.locator(f'a[href="/{target}/"]')):
        await page.goto(f"{HOME}{target}/", wait_until="domcontentloaded")
    activity.log("visit", f"Home: opened @{target}", "ok")
    await page.wait_for_timeout(2800)          # let the profile render

    # 4. Click each profile tab in turn (href is stable across the navigations).
    try:
        tabs = await page.evaluate(_PROFILE_TABS_JS)
    except Exception:  # noqa: BLE001
        tabs = []
    tabs_clicked: list[str] = []
    for tab in tabs:
        await control.checkpoint()
        loc = page.locator(f'[role="tablist"] a[href="{tab["href"]}"]').first
        try:
            if await loc.count():
                await loc.click(timeout=5000)
                tabs_clicked.append(tab["label"])
                activity.log("visit", f"Profile tab: {tab['label']}", "ok")
        except Exception:  # noqa: BLE001 — one tab mustn't stop the rest
            pass
        await page.wait_for_timeout(1200)

    activity.log("visit", f"Home actions done — @{target}, tabs: "
                 + ", ".join(tabs_clicked), "ok")
    return {"username": target, "tabs": tabs_clicked, "found": len(names),
            "url": page.url}


async def open_search(account_id: int | None = None, rounds: int | None = None,
                      term: str | None = None, click_first: bool = False,
                      follow: bool = False) -> dict:
    """Click the sidebar's Search, type into it, and harvest the suggestions.

    Modes:
      • no `term` — the Search-Scrapping harvest: each round types two fresh
        random letters and reads the suggested account rows. Round count comes
        from Settings (⚙️ → Search rounds) unless passed explicitly.
      • with `term` — the Actions panel's search bar: types that one query and
        reads whoever Instagram suggests for it (a single round).
      • `click_first` — after the (single) search, open the first result's
        profile. The Actions panel's Search button uses this with a 2-letter
        random query.

    Names are deduplicated. Read-only unless click_first opens a profile.
    """
    if click_first:
        rounds = 1                             # one search, then open the top hit
    query = (term or "").strip()
    if query:
        rounds = 1                             # one search, the user's query
    elif rounds is None:
        try:
            rounds = settings.scroll().get("search_rounds", _SEARCH_ROUNDS_FALLBACK)
        except Exception:  # noqa: BLE001 — never let a settings read break the run
            rounds = _SEARCH_ROUNDS_FALLBACK
    rounds = max(1, int(rounds))
    # The Search-Scrapping harvest (no user query, and not the Actions "open
    # first" button) is the only mode that ALSO scrapes bios: each round's new
    # suggestions get their profile opened and {username, bio} saved to
    # usernames_bio.json, mirroring the Runs profile scrape. The look-ups
    # (term / click_first) stay read-only.
    harvest_mode = not query and not click_first
    try:
        _cfg = settings.scroll()
        pause_range = (_cfg["scroll_pause_min_ms"] / 1000, _cfg["scroll_pause_max_ms"] / 1000)
    except Exception:  # noqa: BLE001 — a settings hiccup falls back to a sane dwell
        pause_range = (0.6, 1.6)
    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await _wait_ready(page, _FEED_READY_JS, 2000)

    # Open the sidebar Search flyout and focus its input. Reused to REOPEN the
    # panel after the harvest opens a profile for its bio (a profile visit
    # navigates away and closes the flyout). `strict` raises on failure for the
    # first open; the mid-harvest reopens are best-effort (return without raising).
    async def _open_search_flyout(strict: bool) -> None:
        # Retries with a feed reload between goes and every click target tried in
        # turn, so a rail that momentarily won't take a click doesn't kill the run
        # — see _open_search_panel_resilient. `strict` (the first open) raises if
        # every go failed; the mid-harvest reopens are best-effort.
        if await _open_search_panel_resilient(page):
            return
        if strict:
            raise RuntimeError(
                "Couldn't open Search in the Instagram sidebar. Is the window on a "
                "logged-in Instagram page?"
            )

    await _open_search_flyout(strict=True)
    activity.log("visit", "Clicked Search in the sidebar", "ok")

    self_name = _account_username(account_id)
    self_lower = (self_name or "").lower()
    names: list[str] = []
    seen: set[str] = set()
    seeds: list[str] = []
    per_round: list[dict] = []
    bios_saved = 0
    # {username, bio} per visited profile — by reference below, so the panel's
    # live list fills each name's bio in as the harvest reads it.
    profiles: list[dict] = []

    # `names` by reference: the panel polling search_progress() sees each round's
    # results the moment they land, rather than nothing until all rounds finish.
    _search[account_id] = {
        "running": True, "names": names, "round": 0, "rounds": rounds,
        "seed": None, "seeds": seeds, "per_round": per_round,
        "profiles": profiles, "account_id": account_id,
    }
    state = _search[account_id]

    for index in range(1, rounds + 1):
        await control.checkpoint()             # Stop/Pause land between rounds
        state.update(round=index)

        # What the panel holds BEFORE this round types anything. Instagram leaves
        # the last round's rows up while it fetches the next lot, so this is the
        # fingerprint _wait_search_results needs to tell them apart.
        before = await _search_results_key(page)

        # Clear first, or the next seed types onto the end of the last one.
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(PANEL_SETTLE_MS)
        await page.wait_for_timeout(random.uniform(*SEARCH_PRETYPE_PAUSE_S) * 1000)

        # Type key by key: the search input is React-controlled, so setting
        # .value directly doesn't register but real keystrokes do. A user query
        # (Actions panel) is typed as-is; the Search+Follow button uses a plain
        # 2-letter alpha seed (reliable result to open); the harvest draws a
        # random one of nine shapes (letters/numbers/specials/combos).
        if query:
            seed = query
        elif click_first:
            seed = _rand_str(_SEARCH_SEED_ALPHABET, _SEARCH_SEED_LEN)
        else:
            seed = _random_search_seed()
        # A deliberate one-off query (Actions) keeps its steady metronome; the
        # harvest, which types round after round, gets the human jitter.
        if query or click_first:
            await page.keyboard.type(seed, delay=SEARCH_TYPE_DELAY_MS)
        else:
            await _type_search_seed(page, seed)
        seeds.append(seed)
        state.update(seed=seed)
        # Wait for THIS seed's results: the panel has to CHANGE, stop loading and
        # settle. "Rows are present" was the old test, and from round two on the
        # rows present were the previous seed's — so every round passed instantly
        # and the box was cleared and retyped several times a second.
        #
        # A deliberate one-off query waits on `""` instead: it only ever runs one
        # round, so there is no previous seed to be confused with, and demanding a
        # CHANGE would misread "the result was already showing under Recent" as a
        # failure and drop the very name the caller asked to open.
        landed = await _wait_search_results(page, "" if (query or click_first) else before)

        # Whatever is on screen when nothing landed belongs to the last round,
        # not to this seed, so there is nothing here worth reading.
        found = []
        if landed:
            try:
                found = await page.evaluate(
                    _HARVEST_SEARCH_JS,
                    {"reserved": sorted(RESERVED_PATHS), "self": self_name},
                )
            except Exception as exc:  # noqa: BLE001 — one bad round mustn't end the run
                activity.log("visit", f"Round {index} ({seed!r}) could not be read: {exc}", "warn")
                found = []

        fresh = []
        for name in found:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)          # in place — the poller sees it at once
            fresh.append(name)
        per_round.append({"round": index, "seed": seed,
                          "found": len(found), "new": fresh})
        activity.log(
            "visit",
            f"Search {index}/{rounds} {seed!r} — {len(found)} result(s), "
            f"{len(fresh)} new (total {len(names)})"
            + ("" if landed else " — nothing came back for this seed"),
            "ok" if landed else "warn",
        )

        # Look at the results before reaching for the next seed. Without this the
        # rounds run back to back and the box visibly flickers between queries.
        if index < rounds and not (query or click_first):
            await page.wait_for_timeout(random.uniform(*SEARCH_READ_PAUSE_S) * 1000)

        # Bio mode (Search-Scrapping): open each new suggestion's profile, read
        # the bio, and save {username, bio} to usernames_bio.json — then reopen
        # the Search flyout for the next round, since the visits closed it.
        if harvest_mode and fresh:
            visit_names = [n for n in fresh if n.lower() != self_lower]
            if visit_names:
                bios_saved += await _visit_profiles_and_store(
                    page, visit_names, pause_range=pause_range, sink=profiles)
                if index < rounds:
                    await _open_search_flyout(strict=False)

    state.update(running=False, round=rounds, seed=None)

    # Actions panel's Search button: open the first result's profile, then
    # optionally click Follow on it.
    opened_first = None
    followed = None
    if click_first:
        try:
            opened_first = await _click_first_search_result(page)
        except Exception as exc:  # noqa: BLE001 — opening is best-effort
            activity.log("visit", f"Couldn't open first result: {exc}", "warn")
        activity.log(
            "visit",
            f"Opened first result: @{opened_first}" if opened_first
            else f"No results to open for {seeds[-1]!r}",
            "ok" if opened_first else "warn",
        )
        if opened_first and follow:
            await page.wait_for_timeout(1800)   # let the profile render
            try:
                followed = await _click_follow(page)
            except Exception as exc:  # noqa: BLE001 — following is best-effort
                activity.log("visit", f"Follow step failed: {exc}", "warn")
                followed = "no-button"
    else:
        activity.log(
            "visit",
            f"Search-Scrapping done — {len(names)} username(s) from {rounds} "
            f"search(es): " + ", ".join(seeds)
            + (f" · {bios_saved} bio(s) → usernames_bio.json" if harvest_mode else ""),
            "ok",
        )

    return {
        "opened": True, "search_open": True, "rounds": rounds, "seeds": seeds,
        "per_round": per_round, "usernames": names, "count": len(names),
        "opened_first": opened_first, "followed": followed,
        "bios_saved": bios_saved, "profiles": profiles,
        "url": page.url,
    }


async def open_messages(
    search_text: str | None = None,
    click_first: bool = False,
    account_id: int | None = None,
) -> dict:
    """Click the Messages button in the Instagram sidebar on the already-open
    window, opening the DM inbox. If `search_text` is given, also click the
    inbox search box and type it there. If `click_first` is set, click the first
    search result too, which opens that conversation.

    `focus_search` opens the inbox and nothing more than its search box: focused
    and empty, waiting to be typed into by hand. That's the Scrapping panel's
    Search-Scrapping button — it stops exactly where a manual search begins.

    Never sends anything — at most it opens a thread and leaves it sitting there.
    """
    page = account_page(account_id)
    if not _on_instagram(page):
        activity.log("visit", "Window is not on Instagram - opening Home first", "warn")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # Click the Messages nav item. It's an <a href="/direct/inbox/"> wrapping an
    # svg[aria-label="Messages"] — match either.
    clicked = False
    try:
        link = page.locator('a[href="/direct/inbox/"]').first
        if not await link.count():
            link = page.locator('svg[aria-label="Messages"]').first.locator("xpath=ancestor::a[1]")
        await link.wait_for(state="visible", timeout=6000)
        await link.click(timeout=6000)
        clicked = True
    except Exception as exc:  # noqa: BLE001 — fall back to opening the inbox URL
        activity.log("visit", f"Messages button not clickable ({exc}); opening inbox URL", "warn")
        await page.goto(INBOX_URL, wait_until="domcontentloaded")

    await page.wait_for_timeout(1500)
    activity.log("visit", "Opened Messages (DM inbox)", "ok")

    typed = None
    if search_text:
        try:
            typed = await _type_into_search(page, search_text)
            activity.log("visit", f"Typed {typed!r} into the DM search box", "ok")
        except Exception as exc:  # noqa: BLE001
            activity.log("visit", f"Could not type into the search box: {exc}", "warn")
            raise RuntimeError(
                f"Opened Messages, but couldn't type into the search box ({exc}). "
                "The inbox may still be loading — try again."
            ) from exc

    first_result = None
    if click_first and typed:
        # Let the dropdown settle before reading it — results re-render as the
        # query lands, and the top row swaps around while that happens.
        await page.wait_for_timeout(1200)
        try:
            first_result = await _click_first_result(page)
        except Exception as exc:  # noqa: BLE001
            activity.log("visit", f"Could not click the first result: {exc}", "warn")
        else:
            if first_result is None:
                activity.log("visit", f"No search results for {typed!r}", "warn")
            else:
                label = first_result["name"] or "(unnamed)"
                if first_result["username"]:
                    label += f" (@{first_result['username']})"
                activity.log("visit", f"Opened first result: {label}", "ok")

    return {
        "opened": True,
        "clicked": clicked,
        "search_text": search_text,
        "typed": typed,
        "first_result": first_result,
        "url": page.url,
    }


# ─── Comments campaign ───────────────────────────────────────────────────────
# HOME → scroll → like → comment, post after post, until Stop. One random
# message from the campaign's drafts is typed (never pasted) under each post.
# Progress is kept per account so the panel can show live counters.

_comments: dict[int | None, dict] = {}

# A feed post. Instagram renders each one as an <article>; the action bar
# inside carries the Like/Comment svgs and the inline comment box.
POST_SEL = "article"
POST_LIKE_SEL = 'svg[aria-label="Like"]'
POST_LIKED_SEL = 'svg[aria-label="Unlike"]'
POST_COMMENT_ICON_SEL = 'svg[aria-label="Comment"]'
POST_COMMENT_BOX_SEL = (
    'textarea[aria-label*="comment" i], textarea[placeholder*="comment" i], '
    '[contenteditable="true"][aria-label*="comment" i], '
    '[contenteditable="true"][aria-placeholder*="comment" i], '
    'form textarea, form [contenteditable="true"][role="textbox"]'
)
# Pace. "Peaceful": a human reads a post for a while before reacting, and
# never fires comments back to back.
COMMENT_GAP_S = (12.0, 28.0)          # between posts
COMMENT_READ_S = (2.5, 6.0)           # looking at a post before liking it
COMMENT_SCROLL_STEP = (0.55, 0.95)    # fraction of the window per scroll
COMMENT_SCROLL_PAUSE = (0.8, 1.6)     # seconds per glide


# Interstitials that cover the feed: Instagram's "You've reached your daily
# limit" sheet, the notifications prompt, "Add Instagram to your home screen"…
# Each has a close/dismiss control; nothing else on the feed matches these.
_FEED_OVERLAY_SEL = (
    'svg[aria-label="Close"]', 'svg[aria-label="close"]',
    'button:has-text("Not now")', 'button:has-text("Not Now")',
    'button:has-text("Dismiss")', 'button:has-text("OK")',
)


async def _dismiss_feed_overlays(page) -> int:
    """Close whatever sheet/dialog is covering the feed. Returns how many were
    dismissed. Best-effort and quick: nothing there costs ~0.2 s."""
    closed = 0
    for sel in _FEED_OVERLAY_SEL:
        loc = page.locator(sel).first
        try:
            if not await loc.count() or not await loc.is_visible():
                continue
            anc = loc.locator("xpath=ancestor-or-self::*[@role='button' or self::button][1]")
            target = anc.first if await anc.count() else loc
            await target.click(timeout=3000)
            closed += 1
            activity.log("visit", f"Comments: dismissed an overlay ({sel})", "ok")
            await page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            continue
    return closed


def comments_progress(account_id: int | None = None) -> dict:
    """Live counters of the comments campaign for one account (or all)."""
    if account_id is not None:
        return dict(_comments.get(account_id) or {"running": False})
    return {str(k): v for k, v in _comments.items()}


def _pick_comment_message(messages: list[str] | None) -> str:
    pool = [" ".join(str(m).split()) for m in (messages or []) if str(m).strip()]
    return random.choice(pool) if pool else _pick_reel_comment()


async def _post_key(article) -> str:
    """Something stable to remember a post by: its /p/ or /reel/ link, else the
    first username in it plus its position."""
    try:
        return await article.evaluate("""el => {
          const a = el.querySelector('a[href^="/p/"], a[href^="/reel/"], a[href*="/p/"]');
          if (a) return a.getAttribute('href');
          const u = el.querySelector('a[href^="/"][role="link"], header a');
          return (u ? u.getAttribute('href') : '?') + '@' + Math.round(el.getBoundingClientRect().top + window.scrollY);
        }""")
    except Exception:  # noqa: BLE001
        return ""


async def _next_feed_post(page, seen: set[str]):
    """The first <article> not handled yet, scrolled into view. None if the
    feed shows nothing new right now."""
    posts = page.locator(POST_SEL)
    try:
        n = await posts.count()
    except Exception:  # noqa: BLE001
        return None, ""
    for i in range(n):
        art = posts.nth(i)
        key = await _post_key(art)
        if not key or key in seen:
            continue
        try:
            if not await art.is_visible():
                continue
            await art.scroll_into_view_if_needed(timeout=4000)
        except Exception:  # noqa: BLE001
            continue
        return art, key
    return None, ""


async def _like_post(page, article) -> str:
    """Like the post if it isn't liked yet. Returns liked / already / missing."""
    try:
        if await article.locator(POST_LIKED_SEL).count():
            return "already"
        icon = article.locator(POST_LIKE_SEL)
        if not await icon.count():
            return "missing"
        if await _click_menu_match(icon):
            await page.wait_for_timeout(random.randint(500, 1100))
            return "liked"
    except Exception:  # noqa: BLE001
        pass
    return "missing"


async def _comment_on_post(page, article, text: str) -> bool:
    """Type `text` under the post and submit it. Uses the inline box when the
    feed shows one, otherwise the Comment icon (which may open the post's
    dialog). Returns True when the comment was submitted."""
    box = article.locator(POST_COMMENT_BOX_SEL).first
    opened_dialog = False
    try:
        visible = bool(await box.count()) and await box.is_visible()
    except Exception:  # noqa: BLE001
        visible = False
    if not visible:
        if not await _click_menu_match(article.locator(POST_COMMENT_ICON_SEL)):
            return False
        await page.wait_for_timeout(1200)
        dialog = page.locator('div[role="dialog"]').last
        try:
            if await dialog.count() and await dialog.is_visible():
                opened_dialog = True
                box = dialog.locator(POST_COMMENT_BOX_SEL).first
            else:
                box = article.locator(POST_COMMENT_BOX_SEL).first
            await box.wait_for(state="visible", timeout=6000)
        except Exception:  # noqa: BLE001
            return False

    try:
        try:
            await box.click(timeout=4000)
        except Exception:  # noqa: BLE001
            await box.focus()
        await page.wait_for_timeout(random.randint(250, 600))
        await page.keyboard.type(text, delay=random.randint(45, 95))
        await page.wait_for_timeout(random.randint(500, 1200))
        # Enter posts in the inline box; the dialog wants the Post button.
        posted = False
        if opened_dialog:
            posted = await _click_button_text(page, "Post")
        if not posted:
            await page.keyboard.press("Enter")
            posted = True
        await page.wait_for_timeout(1500)
        # Confirmation: the box emptied (Instagram clears it once the comment
        # is accepted).
        try:
            left = (await box.input_value()) if await box.evaluate("el => el.tagName === 'TEXTAREA'") \
                else (await box.inner_text())
            if (left or "").strip():
                # still there — try the explicit Post button once
                if await _click_button_text(page, "Post"):
                    await page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass
        return posted
    finally:
        if opened_dialog:
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                pass


async def comments_campaign(account_id: int | None = None,
                            messages: list[str] | None = None) -> dict:
    """HOME → scroll → like → comment, until Stop is pressed.

    Walks the home feed post by post: scrolls to the next unseen post, reads it
    for a moment, likes it, types one random message from `messages` (the
    campaign's drafts; the built-in bank when none) and posts it, waits a
    human-sized gap, scrolls on. Never ends on its own — a stalled feed is
    refreshed — so the Stop button is how it finishes."""
    page = account_page(account_id)
    state = {
        "running": True, "account_id": account_id, "posts": 0, "liked": 0,
        "commented": 0, "skipped": 0, "scrolls": 0, "refreshes": 0,
        "last": None, "last_comment": None, "error": None,
        "started_at": time.strftime("%H:%M:%S"),
    }
    _comments[account_id] = state
    seen: set[str] = set()
    pool = [m for m in (messages or []) if str(m).strip()]
    activity.log("visit", f"Comments campaign started ({len(pool) or len(REEL_COMMENTS)} message(s)) — HOME → scroll → like → comment", "ok")

    try:
        await _ensure_home(page)
        await page.wait_for_timeout(2500)
        await _dismiss_feed_overlays(page)
        idle_scrolls = 0
        while True:
            await control.checkpoint()
            article, key = await _next_feed_post(page, seen)
            if article is None:
                # Nothing new on screen: close any sheet covering the feed,
                # scroll on; after a while, refresh.
                if await _dismiss_feed_overlays(page):
                    continue
                idle_scrolls += 1
                state["scrolls"] += 1
                await _feed_scroll_step(page, COMMENT_SCROLL_STEP, COMMENT_SCROLL_PAUSE)
                await control.sleep(random.uniform(0.8, 1.8))
                if idle_scrolls >= 8:
                    idle_scrolls = 0
                    state["refreshes"] += 1
                    activity.log("visit", "Comments: feed shows nothing new — refreshing Home", "warn")
                    await _refresh_feed(page)
                    await page.wait_for_timeout(2500)
                    await _dismiss_feed_overlays(page)
                continue
            idle_scrolls = 0
            seen.add(key)
            state["posts"] += 1
            state["last"] = key

            # read the post for a moment, like a person would
            await control.sleep(random.uniform(*COMMENT_READ_S))
            await control.checkpoint()
            liked = await _like_post(page, article)
            if liked == "liked":
                state["liked"] += 1
            activity.log("visit", f"Comments: post {state['posts']} {key} — like: {liked}", "ok")

            await control.sleep(random.uniform(1.0, 2.5))
            await control.checkpoint()
            text = _pick_comment_message(pool)
            try:
                ok = await _comment_on_post(page, article, text)
            except control.Stopped:
                raise
            except Exception as exc:  # noqa: BLE001 — one post must not end the run
                ok = False
                activity.log("visit", f"Comments: couldn't comment on {key}: {str(exc).splitlines()[0][:120]}", "warn")
            if ok:
                state["commented"] += 1
                state["last_comment"] = text
                activity.log("visit", f"Comments: commented {text!r} on {key}", "ok")
            else:
                state["skipped"] += 1
                activity.log("visit", f"Comments: no comment box on {key} — skipped", "warn")

            # the peaceful part: a long, varied gap before the next post
            await control.sleep(random.uniform(*COMMENT_GAP_S))
            await control.checkpoint()
            state["scrolls"] += 1
            await _feed_scroll_step(page, COMMENT_SCROLL_STEP, COMMENT_SCROLL_PAUSE)
            await control.sleep(random.uniform(0.6, 1.5))
    except control.Stopped:
        activity.log("visit", f"Comments campaign stopped — {state['posts']} post(s): "
                     f"{state['liked']} liked, {state['commented']} commented", "warn")
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc).split("\nCall log:")[0].strip()[:200]
        activity.log("visit", f"Comments campaign ended with an error: {state['error']}", "error")
    finally:
        state["running"] = False
        state["ended_at"] = time.strftime("%H:%M:%S")
    return dict(state)
