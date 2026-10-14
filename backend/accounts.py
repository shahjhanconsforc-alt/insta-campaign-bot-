"""Account store + multi-account login.

Each account gets its OWN Chrome profile directory
(backend/data/profiles/acct-<id>), because a Chrome profile holds exactly one
Instagram session — sharing one profile would mean each login silently evicted
the previous account.

Logging in is MANUAL: the Login button only opens the account's Chrome window
on Instagram's login page. The person types their credentials there, and the
status check reads the live page's `sessionid` cookie to flip the account to
"Logged In" — no automated typing or clicking.
"""
import asyncio
import shutil

from backend import activity, db, sessions
from backend.automation import instagram as ig
from backend.automation import playwright_runner as browser

PROFILES_DIR = sessions.PROFILES_DIR


def profile_dir(account_id: int):
    return sessions.profile_dir(account_id)


def _public(account: dict) -> dict:
    """What the UI sees — never the password. Session facts come from disk, so
    a session that expired while we were closed reads as logged out."""
    session = sessions.describe(account)
    # Is this account's session window on screen right now? That's what turns
    # the card yellow and takes it out of the login queue.
    open_profiles = {w["profile"] for w in browser.open_windows()}
    session["window_open"] = session["session_path"] in open_profiles

    # With the window open, Chrome locks the cookie file and it reads "Unknown".
    # But the live check that opened the window looked at the page itself, which
    # is better evidence — so use it rather than shrug.
    if session["window_open"] and session["status"] == "Unknown":
        live = bool(account.get("logged_in"))
        session["status"] = "Logged In" if live else "Logged Out"
        session["logged_in"] = live
        session["reason"] = "From the live page check (cookie file is locked while Chrome is open)."
    # Chrome writes cookies to disk lazily: right after a manual login the page
    # already has a sessionid while the cookie file still says logged out. The
    # live check (live_check) wrote what the page said — prefer it while the
    # window is open.
    elif session["window_open"] and account.get("logged_in") and not session["logged_in"]:
        session["status"] = "Logged In"
        session["logged_in"] = True
        session["session_valid"] = True
        session["reason"] = "Logged in (seen on the live page; not yet flushed to disk)."

    return {
        "id": account["id"],
        "label": account.get("label") or "",
        "login": account.get("login", ""),
        "password_len": len(account.get("password") or ""),
        "last_login": account.get("last_login"),
        "last_result": account.get("last_result"),
        "created_at": account.get("created_at"),
        **session,
    }




def list_accounts() -> list[dict]:
    return [_public(a) for a in db.list_accounts()]


def add_account(label: str, login: str, password: str) -> dict:
    login = (login or "").strip()
    if not login:
        raise ValueError("Email or phone is required.")
    # The password is optional: login is manual, typed into Chrome by the person.
    # Keeping it here is only a convenience for the eye icon.
    password = password or ""
    if any(a["login"].lower() == login.lower() for a in db.list_accounts()):
        raise ValueError(f"{login} is already saved.")
    return _public(db.add_account({"label": label, "login": login, "password": password}))


def reveal(account_id: int) -> str:
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    return account.get("password") or ""


def remove_account(account_id: int) -> bool:
    """Drop the account and its saved session together — leaving an orphaned
    profile behind would silently log the next account in as this one."""
    removed = db.remove_account(account_id)
    if removed:
        shutil.rmtree(profile_dir(account_id), ignore_errors=True)
    return removed


def _window_page(account_id: int):
    """This account's open Page, or None when its window is not up."""
    return browser.page_for_profile(profile_dir(account_id))


async def _record_live(account: dict, logged_in: bool) -> None:
    """Write what the live page said onto the account record."""
    account_id = account["id"]
    changes = {"logged_in": logged_in, "session_last_used": db._now()}  # noqa: SLF001
    if logged_in:
        changes["locked"] = True
        if not account.get("session_created_at"):
            changes["session_created_at"] = db._now()  # noqa: SLF001
        if not account.get("logged_in"):
            # first time we see this login: stamp it
            changes["last_login"] = db._now()  # noqa: SLF001
            changes["last_result"] = "manual"
            who = account.get("label") or account["login"]
            activity.log("accounts", f"{who}: logged in (detected on the live page)", "ok")
            # Back the fresh session up right away (best-effort; Chrome keeps the
            # files open, so the cookie DB may be skipped — the close-time backup
            # completes it).
            try:
                await asyncio.to_thread(browser.backup_session, profile_dir(account_id))
            except Exception as exc:  # noqa: BLE001
                activity.log("accounts", f"{who}: session backup skipped ({exc})", "warn")
    else:
        changes["locked"] = False
    db.update_account(account_id, changes)


async def live_check(account_id: int) -> dict:
    """Status check for one account.

    With its window open: ask the live page for Instagram's `sessionid` cookie
    (the same signal the disk read uses, minus the flush delay). Without a
    window: re-read the stored session from disk.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    page = _window_page(account_id)
    if page is None:
        sessions.refresh(account_id)
        return {**_public(db.get_account(account_id)), "live": False}

    logged_in = await ig._has_session_cookie(page)  # noqa: SLF001 — same signal as disk
    await _record_live(account, logged_in)
    # disk may or may not have caught up; refresh() keeps the live fact when it can't read
    try:
        sessions.refresh(account_id)
    except Exception:  # noqa: BLE001
        pass
    if logged_in:
        # refresh() may have written False from a not-yet-flushed cookie file;
        # the page is the better witness while the window is open.
        db.update_account(account_id, {"logged_in": True, "locked": True})
    public = _public(db.get_account(account_id))
    if logged_in:
        public.update(status="Logged In", logged_in=True, session_valid=True,
                      reason="Logged in — sessionid cookie present on the live page.")
    else:
        public.update(status="Logged Out", logged_in=False, session_valid=False,
                      reason="Window open, no sessionid cookie yet — log in in Chrome.")
    return {**public, "live": True, "url": page.url}


async def live_check_all() -> dict:
    """Disk sweep for every account, then a live check for each open window."""
    result = sessions.sweep()
    live = 0
    for account in db.list_accounts():
        try:
            if _window_page(account["id"]) is None:
                continue
            on_disk = sessions.inspect(account["id"])["valid"]
            info = await live_check(account["id"])
            live += 1
            if info["logged_in"] != on_disk:          # the live answer wins
                result["logged_in"] += 1 if info["logged_in"] else -1
        except Exception as exc:  # noqa: BLE001
            activity.log("accounts", f"Live check failed for {account['id']}: {exc}", "warn")
    return {**result, "live_checked": live}


async def list_accounts_live() -> list[dict]:
    """The account list, with every open window checked live first."""
    for account in db.list_accounts():
        if _window_page(account["id"]) is not None:
            try:
                await live_check(account["id"])
            except Exception:  # noqa: BLE001
                pass
    return list_accounts()


async def manual_login(account_id: int) -> dict:
    """Open this account's Chrome window on Instagram so the person can log in.

    Nothing is typed or clicked. If the profile already holds a session the
    window opens logged in and we say so; otherwise it lands on the login page
    and the status check (live_check) picks the login up once it happens.
    """
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    who = account.get("label") or account["login"]

    page = _window_page(account_id)
    reused = page is not None
    if page is None:
        await browser.open_browser(ig.HOME, profile=profile_dir(account_id))
        page = _window_page(account_id)
        try:
            await page.wait_for_timeout(2000)      # let the stored session (if any) settle
        except Exception:  # noqa: BLE001
            pass

    logged_in = await ig._has_session_cookie(page)  # noqa: SLF001
    if not logged_in and "/accounts/login" not in (page.url or ""):
        try:
            await page.goto(ig.LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            activity.log("accounts", f"{who}: could not open the login page ({exc})", "warn")
    try:
        await asyncio.to_thread(browser.focus_window, profile_dir(account_id))
    except Exception:  # noqa: BLE001
        pass                                        # a window we can't raise is still open

    await _record_live(account, logged_in)
    if not logged_in:
        db.update_account(account_id, {"last_result": "manual_login_open"})
    activity.log(
        "accounts",
        f"{who}: {'already logged in' if logged_in else 'Chrome opened on the login page — log in by hand'}",
        "ok" if logged_in else "info",
    )
    return {
        "id": account_id,
        "who": who,
        "ok": True,
        "reused": reused,
        "logged_in": logged_in,
        "action": "already_logged_in" if logged_in else "manual_login",
        "detail": "Already logged in — the stored session loaded." if logged_in
        else "Chrome is open on Instagram's login page. Log in there; the status updates by itself.",
        "url": page.url,
        "session": _public(db.get_account(account_id)),
    }


async def login_accounts(ids: list[int], force: bool = False) -> dict:
    """Open a Chrome window per selected account for a manual login.

    `force` (New Login) drops the stored session first so the window opens
    logged out.
    """
    results = []
    for account_id in ids:
        account = db.get_account(account_id)
        if not account:
            results.append({"id": account_id, "ok": False, "detail": "No such account."})
            continue
        who = account.get("label") or account["login"]
        try:
            if force:
                await browser.close_browser(profile=profile_dir(account_id))
                await asyncio.sleep(1.0)            # let Chrome release the profile
                sessions.clear(account_id)
            results.append(await manual_login(account_id))
        except Exception as exc:  # noqa: BLE001 — one bad account must not stop the rest
            activity.log("accounts", f"{who} failed: {exc}", "error")
            db.update_account(account_id, {"logged_in": False, "last_result": "error"})
            results.append(
                {"id": account_id, "who": who, "ok": False, "action": "error", "detail": str(exc)[:200]}
            )

    return {
        "requested": len(ids),
        "logged_in": sum(1 for r in results if r.get("logged_in")),
        "opened": sum(1 for r in results if r.get("ok")),
        "results": results,
    }


async def new_login(account_id: int) -> dict:
    """Drop any stored session, then open a logged-out window for a fresh manual login."""
    if not db.get_account(account_id):
        raise KeyError(f"No account {account_id}")
    return await login_accounts([account_id], force=True)


async def open_account(account_id: int) -> dict:
    """Open Chrome on this account's profile without touching the login form."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    return await browser.open_browser(ig.HOME, profile=profile_dir(account_id))
