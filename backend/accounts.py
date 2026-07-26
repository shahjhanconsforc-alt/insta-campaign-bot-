"""Account store + multi-account login.

Each account gets its OWN Chrome profile directory
(backend/data/profiles/acct-<id>), because a Chrome profile holds exactly one
Instagram session — sharing one profile would mean each login silently evicted
the previous account.

Logins run sequentially, never in parallel: a profile is locked to one Chrome
process, and one window at a time is what a person would actually do.
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
    if not password:
        raise ValueError("Password is required.")
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


async def _login_one(account: dict) -> dict:
    """Log one account in and record the session. Assumes it is unlocked."""
    account_id = account["id"]
    who = account.get("label") or account["login"]
    activity.log("accounts", f"Logging in {who}…")
    page = await browser.require_page(profile=profile_dir(account_id))
    outcome = await ig.login(page, account["login"], account["password"])
    activity.log(
        "accounts",
        f"{who}: {outcome.get('detail') or outcome.get('action')}",
        "ok" if outcome.get("logged_in") else "warn",
    )
    db.update_account(
        account_id,
        {
            "last_login": db._now(),  # noqa: SLF001 — same package, one clock
            "last_result": outcome.get("action"),
        },
    )
    # The cookie only reaches disk once Chrome flushes it, so read back rather
    # than trusting the login's own say-so.
    session = sessions.refresh(account_id)
    return {"id": account_id, "who": who, "ok": bool(session["logged_in"]), **outcome, "session": session}


async def login_accounts(ids: list[int], force: bool = False) -> dict:
    """Log in each selected account, one at a time, each in its own profile.

    An account with a live session is skipped — one session per account, and a
    second login would only risk a challenge. `force` is the New Login path:
    it drops the session first.
    """
    results = []
    for account_id in ids:
        account = db.get_account(account_id)
        if not account:
            results.append({"id": account_id, "ok": False, "detail": "No such account."})
            continue

        who = account.get("label") or account["login"]
        if force:
            sessions.clear(account_id)
            account = db.get_account(account_id)
        elif sessions.is_locked(account_id):
            sessions.touch(account_id)
            results.append({
                "id": account_id, "who": who, "ok": True, "action": "already_logged_in",
                "detail": "Already has a live session — skipped. Use New Login to start fresh.",
                "session": sessions.describe(account),
            })
            continue

        try:
            results.append(await _login_one(account))
        except Exception as exc:  # noqa: BLE001 — one bad account must not stop the rest
            activity.log("accounts", f"{who} failed: {exc}", "error")
            db.update_account(account_id, {"logged_in": False, "last_result": "error"})
            results.append(
                {"id": account_id, "who": who, "ok": False, "action": "error", "detail": str(exc)[:200]}
            )

    return {
        "requested": len(ids),
        "logged_in": sum(1 for r in results if r.get("ok")),
        "results": results,
    }


async def new_login(account_id: int) -> dict:
    """Drop any stored session, unlock, then log in fresh and re-lock."""
    if not db.get_account(account_id):
        raise KeyError(f"No account {account_id}")
    await browser.close_browser()      # release the profile lock before deleting it
    sessions.clear(account_id)
    return await login_accounts([account_id], force=False)


async def open_account(account_id: int) -> dict:
    """Open Chrome on this account's profile without touching the login form."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")
    return await browser.open_browser(ig.HOME, profile=profile_dir(account_id))
