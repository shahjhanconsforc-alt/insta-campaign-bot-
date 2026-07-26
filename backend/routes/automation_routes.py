import asyncio

from fastapi import APIRouter, HTTPException
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel

from backend import activity
from backend.automation import cursor
from backend.automation import playwright_runner as browser
from backend.automation.visit import DEFAULT_MESSAGE

router = APIRouter(prefix="/api/automation", tags=["automation"])


class UrlBody(BaseModel):
    url: str | None = None


class OpenBody(BaseModel):
    url: str | None = None
    account_id: int | None = None


def _profile_for(account_id: int | None):
    """The Chrome profile an account's session lives in, or None for the
    default profile."""
    if not account_id:
        return None
    from backend import db, sessions

    if not db.get_account(account_id):
        raise HTTPException(404, f"No account {account_id}")
    return sessions.profile_dir(account_id)


def _fail(exc: Exception) -> HTTPException:
    """Map internal errors onto honest status codes + readable messages."""
    if isinstance(exc, browser.BadUrl):
        return HTTPException(400, str(exc))
    if isinstance(exc, PlaywrightError):
        # Strip Playwright's multi-line "Call log:" tail — the UI shows one line.
        return HTTPException(502, str(exc).split("\nCall log:")[0].strip())
    return HTTPException(500, str(exc))


@router.post("/open")
async def open_browser(body: OpenBody | None = None):
    """Launch the visible Chrome window.

    `account_id` picks that account's profile, so the login it performs is
    saved against that account. Without it the shared default profile is used —
    which is how every account's session used to end up in one place,
    overwriting the last.
    """
    try:
        return await browser.open_browser(
            body.url if body else None,
            profile=_profile_for(body.account_id if body else None),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure becomes a clean HTTP error
        raise _fail(exc) from exc


@router.post("/goto")
async def goto(body: UrlBody):
    try:
        return await browser.goto(body.url or "")
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


class CredsBody(BaseModel):
    account_id: int | None = None


class ClickLoginBody(BaseModel):
    account_id: int | None = None
    # "cursor" = real pointer (PyAutoGUI); "inspect" = Playwright DOM click.
    method: str = "cursor"


class VisitBody(BaseModel):
    duration_seconds: float | None = None
    account_id: int | None = None


def _account_name(account_id: int | None) -> str:
    """A human label for an account id — same rule sessions.py uses."""
    if account_id is None:
        return "the open window"
    from backend import db

    account = db.get_account(account_id)
    if not account:
        return f"account #{account_id}"
    return account.get("label") or account.get("login") or f"account #{account_id}"


@router.post("/type-credentials")
async def type_credentials(body: CredsBody | None = None):
    """Step 4: type Instagram credentials into the login form on screen.

    Uses the chosen saved account, else IG_USERNAME/IG_PASSWORD from .env.
    Does not submit — that's Accounts → Log in selected.
    """
    from backend import config, db
    from backend.automation import instagram as ig

    account_id = body.account_id if body else None
    if account_id:
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(404, f"No account {account_id}")
        user, pwd = account["login"], account["password"]
    else:
        user, pwd = config.IG_USERNAME, config.IG_PASSWORD

    if not user or not pwd:
        raise HTTPException(
            400,
            "No credentials. Pick a saved account, or set IG_USERNAME/IG_PASSWORD in .env.",
        )
    if not browser.is_open():
        raise HTTPException(409, "No Chrome window is open.")

    # Type into THIS account's profile. Relying on whatever window happens to be
    # open is how a login ends up saved against the wrong account.
    profile = _profile_for(account_id)
    if profile and browser.current_profile() != profile:
        raise HTTPException(
            409,
            f"Chrome is open on a different profile ({browser.current_profile().name}). "
            f"Tick step 1 so it opens {profile.name} for this account.",
        )

    try:
        return await ig.fill_credentials(browser.current_page(), user, pwd)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc


@router.post("/click-login")
async def click_login(body: ClickLoginBody | None = None):
    """Step 5: press the Log in button on the form step 4 just filled in.

    Kept apart from step 4 so the credentials can be checked on screen before
    anything is submitted. Reads the session back afterwards — the cookie only
    reaches disk once Chrome flushes it, so the login's own say-so isn't enough.

    `method` picks how the button is pressed: "cursor" walks the real mouse to
    it (what the UI tries first), "inspect" clicks it via the DOM (the fallback
    once the cursor has missed its 5 goes).
    """
    from backend import db, sessions
    from backend.automation import instagram as ig

    account_id = body.account_id if body else None
    method = (body.method if body else None) or "cursor"
    if method not in ("cursor", "inspect"):
        raise HTTPException(400, f"Unknown click method {method!r}.")
    if account_id and not db.get_account(account_id):
        raise HTTPException(404, f"No account {account_id}")
    if not browser.is_open():
        raise HTTPException(409, "No Chrome window is open.")

    # Same guard as step 4: submitting on another account's profile would save
    # this login against the wrong account.
    profile = _profile_for(account_id)
    if profile and browser.current_profile() != profile:
        raise HTTPException(
            409,
            f"Chrome is open on a different profile ({browser.current_profile().name}). "
            f"Tick step 1 so it opens {profile.name} for this account.",
        )

    try:
        outcome = await ig.click_login(browser.current_page(), method=method)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc

    if account_id:
        live = bool(outcome.get("logged_in"))
        db.update_account(
            account_id,
            {
                "last_login": db._now(),  # noqa: SLF001 — same package, one clock
                "last_result": outcome.get("action"),
                # Chrome still holds the cookie file open, so the disk read below
                # can only shrug. What the page itself just said is the better
                # evidence — record it, or a successful login reads "Logged Out"
                # until the window is closed.
                "logged_in": live,
                "locked": live,
            },
        )
        outcome["session"] = sessions.refresh(account_id)

    # A challenge/2FA/rejection is a real answer, not a transport failure — it
    # comes back 200 with the detail, so the step shows why rather than "error".
    return outcome


class NewTabBody(BaseModel):
    account_id: int | None = None


class CursorUrlBody(UrlBody):
    account_id: int | None = None


@router.post("/new-tab")
async def new_tab(body: NewTabBody | None = None):
    try:
        return await cursor.new_tab(account_id=body.account_id if body else None)
    except cursor.CursorError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/cursor-url")
async def cursor_url(body: CursorUrlBody):
    """Drive the REAL mouse to Chrome's address bar, click, and type the URL.

    Unlike /goto (which navigates via Playwright), this uses OS-level input —
    it moves your actual pointer and takes focus for a few seconds.
    """
    if not body.url:
        raise HTTPException(400, "url is required")
    try:
        profile = cursor.profile_for(body.account_id)
        result = await cursor.type_url(browser.normalize_url(body.url), account_id=body.account_id)
        await asyncio.sleep(1.5)                       # let Enter navigate
        # Chrome's NTP swaps renderers — re-point at the real tab, on THIS
        # account's window rather than whichever was last touched.
        return {**result, **browser.sync_active_page(profile)}
    except browser.BadUrl as exc:
        raise HTTPException(400, str(exc)) from exc
    except cursor.CursorError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/visit")
async def visit(body: VisitBody | None = None):
    """Campaign: slowly scroll one account's Instagram home feed on its
    already-open window and collect visible usernames."""
    from backend import db, tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    try:
        duration = body.duration_seconds if body else None
        try:
            with tasks.claim(account_id, "Scrapping"):
                result = await visitor.visit_home_feed(duration, account_id=account_id)
        except (PlaywrightError, RuntimeError):
            # The scrape fell over part-way. Whatever it captured before then is
            # still worth keeping — save it to Data so a mid-run failure never
            # throws away the names already found, then re-raise for the handler
            # below to turn into an honest HTTP error.
            salvaged = visitor.visit_progress(account_id).get("names") or []
            if salvaged:
                db.record_scraped_usernames(list(reversed(salvaged)))
                activity.log(
                    "visit",
                    f"Scrape failed mid-run — saved {len(salvaged)} name(s) collected so far to Data",
                    "warn",
                )
            raise
        names = result.get("usernames") or result.get("names") or []

        # Reversed so get_all()'s newest-first read gives them back in feed
        # order. Names already in Data are skipped, not stored twice.
        outcome = db.record_scraped_usernames(list(reversed(names)))
        saved, skipped = outcome["added"], outcome["skipped"]

        result["saved_records"] = len(saved)
        result["duplicates"] = skipped
        result["account"] = _account_name(account_id)
        return result
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc


class ScrapePrefsBody(BaseModel):
    # 🎯 target count and/or a map of account_id -> technique. Partial updates ok.
    target: int | None = None
    techniques: dict[str, str] | None = None


@router.get("/scrape-prefs")
async def get_scrape_prefs():
    """The saved Scrapping panel prefs (🎯 target + per-account technique), so the
    panel reloads them after a restart or refresh."""
    from backend import db
    return db.get_scrape_prefs()


@router.put("/scrape-prefs")
async def put_scrape_prefs(body: ScrapePrefsBody):
    """Persist a partial change to the Scrapping prefs — saved to the store file
    permanently, no need to re-save."""
    from backend import db
    return db.save_scrape_prefs(
        {"target": body.target, "techniques": body.techniques})


class BatchScrapeBody(BaseModel):
    account_id: int | None = None
    # 'scroll' | 'search' | 'both' — the account's chosen technique.
    technique: str | None = "both"
    # stop this account once it has collected this many usernames.
    # 0 (the default) = UNLIMITED — collect everything the surfaces yield.
    target_count: int | None = 0
    # profile scrape only: run for this many minutes and stop only when the time
    # is up (never earlier). 0/None = stop on the target count instead.
    duration_min: float | None = None
    # Scrapping panel scroll/search: also open each new username's profile, read
    # its bio, and save {username, bio} to usernames_bio.json (the Runs scrape's
    # bio step, applied to scroll & search). Off = the fast, name-only path.
    scrape_bio: bool | None = False


@router.post("/scrape-batched")
async def scrape_batched(body: BatchScrapeBody | None = None):
    """Scrape for one account: collect usernames in random-sized batches (⚙️ → batch
    range) with a pause between each (⚙️ → batch pause). 'both' alternates
    scroll/search each batch; 'scroll'/'search' stay on that method. No time limit —
    it stops when names dry up, on Stop, or at `target_count` if one was given.
    `target_count` 0/omitted = unlimited. Names save to Data and progress rides
    visit-progress."""
    from backend import db, tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    technique = (body.technique if body else None) or "both"
    target = max(0, int((body.target_count if body else None) or 0))   # 0 = unlimited
    scrape_bio = bool(body.scrape_bio) if body else False
    try:
        try:
            with tasks.claim(account_id, f"Scrapping ({technique})"):
                result = await visitor.scrape_batched(
                    account_id=account_id, technique=technique, target_count=target,
                    scrape_bio=scrape_bio)
        except (PlaywrightError, RuntimeError):
            # Keep whatever was collected before it fell over, same as /visit.
            salvaged = visitor.visit_progress(account_id).get("names") or []
            if salvaged:
                db.record_scraped_usernames(list(reversed(salvaged)))
                activity.log(
                    "visit",
                    f"Scrape failed mid-run — saved {len(salvaged)} name(s) so far to Data",
                    "warn",
                )
            raise
        names = result.get("usernames") or result.get("names") or []
        outcome = db.record_scraped_usernames(list(reversed(names)))
        result["saved_records"] = len(outcome["added"])
        result["duplicates"] = outcome["skipped"]
        result["account"] = _account_name(account_id)
        return result
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc


@router.post("/scrape-profiles")
async def scrape_profiles(body: BatchScrapeBody | None = None):
    """Profile-visiting scrape for one account: scroll the feed and, for each new
    username, CLICK into their profile, collect what it shows (full name, bio,
    follower/following/post counts, verified/private), save it, and carry on until
    the feed dries up, Stop, or `target_count` profiles if one was given
    (0/omitted = unlimited). Slower than /scrape-batched since it opens every
    profile with a real navigation. Each profile is saved as it lands, so a Stop
    mid-run keeps everything so far. Progress (names + per-profile data) rides
    visit-progress."""
    from backend import db, tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    technique = (body.technique if body else None) or "scroll"
    target = max(0, int((body.target_count if body else None) or 0))   # 0 = unlimited
    duration_min = (body.duration_min if body else None) or 0
    try:
        try:
            with tasks.claim(account_id, "Profile scrape"):
                result = await visitor.scrape_profiles(
                    account_id=account_id, technique=technique, target_count=target,
                    duration_min=duration_min)
        except (PlaywrightError, RuntimeError):
            # Keep whatever was collected before it fell over, same as /scrape-batched.
            salvaged = visitor.visit_progress(account_id).get("names") or []
            if salvaged:
                db.record_scraped_usernames(list(reversed(salvaged)))
                activity.log(
                    "visit",
                    f"Profile scrape failed mid-run — saved {len(salvaged)} name(s) to Data",
                    "warn",
                )
            raise
        result["account"] = _account_name(account_id)
        return result
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc


class MenuBody(BaseModel):
    account_id: int | None = None
    # One menu button to click (Home, Search, New post, Settings, …). With no
    # item, every rail button is clicked in turn.
    item: str | None = None
    # Reel-actions only: the comment to type + post on the Comment step. When
    # None, the backend picks a random one from its comment bank (REEL_COMMENTS).
    comment: str | None = None


@router.post("/settings-option")
async def settings_option(body: MenuBody | None = None):
    """Actions panel Settings buttons: open the Settings/More menu, click one
    option (`item`: Settings, Your activity, Saved, Switch appearance, Report a
    problem), then close."""
    from backend import tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    option = (body.item if body else None) or ""
    try:
        with tasks.claim(account_id, f"Settings: {option}"):
            result = await visitor.click_settings_option(option, account_id=account_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc
    result["account"] = _account_name(account_id)
    return result


@router.post("/home-actions")
async def home_actions(body: MenuBody | None = None):
    """Actions panel Home button: open Home, scroll a little, click a username,
    then click each tab (Posts, Reels, Reposts, Tagged) on the opened profile."""
    from backend import tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    try:
        with tasks.claim(account_id, "Home actions"):
            result = await visitor.home_actions(account_id=account_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc
    result["account"] = _account_name(account_id)
    return result


@router.post("/notification-filters")
async def notification_filters(body: MenuBody | None = None):
    """Actions panel Notifications button: open Notifications and click each of
    its filter tabs one by one (All, People you follow, Comments, …)."""
    from backend import tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    try:
        with tasks.claim(account_id, "Notification filters"):
            result = await visitor.click_notification_filters(account_id=account_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc
    result["account"] = _account_name(account_id)
    return result


@router.post("/reel-actions")
async def reel_actions(body: MenuBody | None = None):
    """Actions panel Reels button: open Reels and click the current reel's action
    buttons one by one (Like, Comment, Repost, Share, Save, More)."""
    from backend import tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    comment = body.comment if body else None      # None -> backend picks random
    try:
        with tasks.claim(account_id, "Reel actions"):
            result = await visitor.click_reel_actions(account_id=account_id, comment=comment)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc
    result["account"] = _account_name(account_id)
    return result


@router.post("/reel-action")
async def reel_action(body: MenuBody | None = None):
    """Actions panel per-Reel buttons: open Reels and click ONE action (`item`:
    Like, Comment, Repost, Share, Save, or More). On Comment, a random comment is
    typed and posted (unless `comment` overrides it)."""
    from backend import tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    action = (body.item if body else None) or ""
    comment = body.comment if body else None      # None -> backend picks random
    try:
        with tasks.claim(account_id, f"Reel: {action}"):
            result = await visitor.click_reel_action(
                action, account_id=account_id, comment=comment)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc
    result["account"] = _account_name(account_id)
    return result


@router.post("/click-menu")
async def click_menu(body: MenuBody | None = None):
    """Actions panel: click a named Instagram menu button (`item`) — or, with no
    item, walk the whole left-rail menu."""
    from backend import tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    item = (body.item if body else None) or None
    try:
        with tasks.claim(account_id, f"Menu: {item}" if item else "Menu walk"):
            result = (await visitor.click_menu_item(item, account_id=account_id)
                      if item else
                      await visitor.click_all_menu(account_id=account_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc
    result["account"] = _account_name(account_id)
    return result


class SearchBody(BaseModel):
    account_id: int | None = None
    # How many two-letter searches to run, harvesting each one's suggestions.
    rounds: int | None = None
    # A specific query to type into Instagram's Search (Actions panel search bar).
    # When set, one search of that term runs and results are NOT saved to Data.
    term: str | None = None
    # Actions panel Search button: type a 2-letter random query, then open the
    # first result's profile.
    click_first: bool = False
    # …and click Follow on that opened profile.
    follow: bool = False


@router.post("/open-search")
async def open_search(body: SearchBody | None = None):
    """Click Search in the Instagram sidebar and type into it.

    - With `term`: search that exact query once and return whoever it suggests
      (the Actions panel's search bar). Results are NOT saved to Data — it's a
      look-up, not a harvest.
    - Without `term`: the Search-Scrapping harvest — several random two-letter
      searches whose suggestions ARE collected into Data.

    Either way it's the sidebar's global Search, read only: nothing clicked/sent."""
    from backend import db, tasks
    from backend.automation import visit as visitor

    account_id = body.account_id if body else None
    term = (body.term if body else None) or None
    click_first = bool(body.click_first) if body else False
    follow = bool(body.follow) if body else False
    job = "Search + follow" if follow else ("Search + open" if click_first
          else ("Instagram search" if term else "Search-Scrapping"))
    try:
        with tasks.claim(account_id, job):
            result = await visitor.open_search(
                account_id=account_id,
                rounds=body.rounds if body else None,
                term=term,
                click_first=click_first,
                follow=follow,
            )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc

    result["account"] = _account_name(account_id)
    if click_first or term:
        # Actions panel (Search button or search bar) — return, don't touch Data.
        if term:
            result["term"] = term
        return result

    names = result.get("usernames") or []
    outcome = db.record_scraped_usernames(names)
    result["saved_records"] = len(outcome["added"])
    result["duplicates"] = outcome["skipped"]
    activity.log(
        "visit",
        f"Search-Scrapping saved {len(outcome['added'])} new name(s) to Data "
        f"({len(outcome['skipped'])} already there)",
        "ok",
    )
    return result


class MessagesBody(BaseModel):
    search_text: str | None = None
    click_first: bool = False
    account_id: int | None = None


@router.post("/open-messages")
async def open_messages(body: MessagesBody | None = None):
    """Click the Messages button in the Instagram sidebar on the already-open
    window — opens the DM inbox. If `search_text` is given, also type it into the
    inbox search box; if `click_first` is set, open the top result too. Sends
    nothing either way."""
    from backend.automation import visit as visitor

    from backend import tasks

    account_id = body.account_id if body else None
    try:
        with tasks.claim(account_id, "Open messages"):
            return await visitor.open_messages(
                body.search_text if body else None,
                click_first=body.click_first if body else False,
                account_id=account_id,
            )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc


class SearchCampaignBody(BaseModel):
    usernames: list[str]
    message: str | None = DEFAULT_MESSAGE
    account_id: int | None = None
    # Optional per-username messages, parallel to `usernames` — the campaign
    # panel sends a randomly-assigned draft per name. Falls back to `message`.
    messages: list[str] | None = None
    # What to deliver: "text" (message only), "media" (image only) or "both"
    # (image + the message as a caption).
    mode: str = "text"
    # Shared image pool (stored filenames from POST /api/campaign/media) attached
    # to any name that has no per-name image.
    media: list[str] | None = None
    # Per-name assigned images, parallel to `usernames` — the panel assigns each
    # name a random image, so different names get different ones. Each entry is a
    # stored filename, or "" / null for "use the shared pool".
    medias: list[str | None] | None = None


@router.post("/search-campaign")
async def search_campaign(body: SearchCampaignBody):
    """Open the DM inbox, then run each username through the search box, open its
    top result and deliver its message into that conversation before moving to
    the next.

    `mode` picks what is delivered (text / media / both). Each name gets its own
    randomly-assigned image from `medias` (or the shared `media` pool). Whether
    it is actually sent is the Config panel's Press ENTER switch: on = Enter is
    pressed after composing (it sends), off = it is left staged, unsent."""
    from backend import tasks
    from backend.automation import media as media_store
    from backend.automation import visit as visitor

    try:
        media_paths = media_store.resolve(body.media or [])
        # Resolve each name's assigned image to a real path (keeping the None
        # gaps, which fall back to the shared pool).
        media_per_name = None
        if body.medias is not None:
            media_per_name = [
                media_store.resolve([name_media])[0] if name_media else None
                for name_media in body.medias
            ]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        with tasks.claim(body.account_id, "Campaign"):
            return await visitor.search_usernames(
                body.usernames, body.message,
                account_id=body.account_id, messages=body.messages,
                mode=body.mode, media_paths=media_paths,
                media_per_name=media_per_name,
            )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(502, str(exc).split("\nCall log:")[0].strip()) from exc


@router.get("/visit-progress")
async def visit_progress(account_id: int | None = None):
    """Names found so far by a scrape in flight — polled so the panel can list
    them as they land rather than showing nothing for 90 seconds."""
    from backend.automation import visit as visitor

    return visitor.visit_progress(account_id)


@router.get("/search-progress")
async def search_progress(account_id: int | None = None):
    """Names found so far by a Search-Scrapping run in flight — polled so the
    panel can list each iteration's results as they land."""
    from backend.automation import visit as visitor

    return visitor.search_progress(account_id)


@router.get("/campaign-progress")
async def campaign_progress(account_id: int | None = None):
    """Per-name outcomes for a campaign in flight — polled so the panel can tick
    each name as its "Hello" lands, instead of all of them at the end."""
    from backend.automation import visit as visitor

    return visitor.campaign_progress(account_id)


@router.get("/control")
async def control_state():
    """Are jobs stopping or paused, and what's running."""
    from backend import control, tasks

    return {**control.state(), "running": tasks.running()}


@router.post("/control/stop")
async def control_stop():
    """End every running job at its next step. Chrome is left open and logged in."""
    from backend import control, tasks

    control.stop()
    activity.log("control", "Stop pressed — ending running jobs, keeping Chrome open", "warn")
    return {**control.state(), "running": tasks.running()}


async def _drain(seconds: float) -> dict:
    """Wait for running jobs to let go of their accounts, up to `seconds`."""
    from backend import tasks

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while tasks.running() and loop.time() < deadline:
        await asyncio.sleep(0.25)
    return tasks.running()


@router.post("/control/stop-all")
async def control_stop_all():
    """Stop every scrape and campaign. Chrome stays open, just refreshed.

    The Stop button's big brother. Jobs halt between steps, so this asks them to
    stop and gives them a moment to actually reach a checkpoint and drop their
    claim. The windows are then RELOADED, not closed — a stopped job can leave a
    half-typed message or a staged image in a composer, and a reload clears that
    while keeping the window and its login, which is the expensive thing.

    The stop flag is cleared afterwards, otherwise the next run the user starts
    would die at its very first checkpoint. Unlike /control/reset this does NOT
    call clear_progress() — the queue, the Campaign view and the scrape results
    are the user's data and must survive a Stop untouched.
    """
    from backend import control, tasks

    stopped = sorted(set(tasks.running().values()))
    control.stop()
    still = await _drain(8.0)

    # Only refresh once the jobs have let go: reloading under a live job would
    # pull the page out from under whatever it was mid-way through typing.
    refreshed = {"reloaded": [], "failed": [], "windows_open": 0}
    if not still:
        refreshed = await browser.reload_windows()
        control.reset()          # flags only — finished runs' progress is kept
        activity.log(
            "control",
            f"Stop all — {', '.join(stopped) or 'nothing'} stopped, "
            f"{len(refreshed['reloaded'])} window(s) refreshed and left open, "
            "panel data kept",
            "warn",
        )
    else:
        # Leave the stop flag set: those jobs die at their next checkpoint, and
        # re-arming now would let one sail straight past it.
        activity.log(
            "control",
            "Stop all — still finishing: "
            f"{', '.join(sorted(set(still.values())))}; windows left untouched",
            "warn",
        )
    return {
        **control.state(),
        "running": tasks.running(),
        "stopped": stopped,
        "windows_refreshed": refreshed["reloaded"],
        "windows_open": refreshed["windows_open"],
    }


@router.post("/control/pause")
async def control_pause():
    """Park every running job at its next step until Resume."""
    from backend import control, tasks

    control.pause()
    activity.log("control", "Paused — jobs will park at their next step", "warn")
    return {**control.state(), "running": tasks.running()}


@router.post("/control/resume")
async def control_resume():
    from backend import control, tasks

    control.resume()
    activity.log("control", "Resumed", "ok")
    return {**control.state(), "running": tasks.running()}


@router.post("/control/reset")
async def control_reset():
    """Clear the stop/pause flags and drop the leftover progress of finished
    jobs, so the next run starts clean. Chrome windows and sessions are left
    exactly as they are."""
    from backend import control, tasks
    from backend.automation import visit as visitor

    still = tasks.running()
    if still:
        raise HTTPException(
            409,
            f"Still running: {', '.join(still.values())}. Press Stop first, "
            "then wait for it to finish.",
        )
    control.reset()
    visitor.clear_progress()
    activity.log("control", "Reset — flags cleared, Chrome untouched", "ok")
    return {**control.state(), "running": tasks.running()}


@router.get("/tasks")
async def running_tasks():
    """Which accounts are mid-job right now, keyed by account id."""
    from backend import tasks

    return {"running": tasks.running()}


@router.get("/type-progress")
async def type_progress():
    """Live state of the address-bar typing — polled while step 3 runs so the
    UI can mirror each character as it lands in Chrome's real address bar."""
    return cursor.type_progress()


@router.post("/ensure")
async def ensure(body: CredsBody | None = None):
    """Guarantee this account's Chrome window is open AND on screen.

    Called before every retry: a step that failed because its window was
    closed, minimised, or behind another one gets a visible window to try
    against, instead of failing the same way five times.
    """
    account_id = body.account_id if body else None
    profile = _profile_for(account_id)
    if account_id and not db_has(account_id):
        raise HTTPException(404, f"No account {account_id}")

    opened = False
    if not browser.is_open(profile):
        await browser.open_browser(profile=profile)
        opened = True

    focused = False
    try:
        await asyncio.to_thread(cursor.focus_window, browser.current_profile())
        focused = True
    except cursor.CursorError:
        pass  # a window we can't foreground is still open; the retry can proceed
    return {"opened": opened, "focused": focused, "profile": _rel_profile(profile)}


def db_has(account_id: int) -> bool:
    from backend import db

    return db.get_account(account_id) is not None


def _rel_profile(profile):
    return profile.name if profile else "default"


@router.post("/screenshot")
async def screenshot():
    try:
        return await browser.screenshot()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/status")
async def status():
    return await browser.status()


@router.post("/close")
async def close():
    return await browser.close_browser()
