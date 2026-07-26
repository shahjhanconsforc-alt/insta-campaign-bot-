"""Session store — one saved login per account.

A "session" here IS the account's Chrome profile directory: Instagram's
`sessionid` cookie lives in `<profile>/Default/Network/Cookies`, which is a
SQLite file. Reading it directly means we can answer "is this account still
logged in?" in milliseconds, offline, without launching a browser — which is
what makes the startup sweep and the status button cheap.

Cookie *values* are DPAPI-encrypted and we never touch them. Presence and
expiry are enough, and are stored in the clear.
"""
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import activity, config, db

PLATFORM = "instagram"
PROFILES_DIR = config.BACKEND_DIR / "data" / "profiles"

# Chrome stamps time in microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# The cookie that actually means "logged in". ds_user_id rides along and gives
# us Instagram's numeric id for the account.
SESSION_COOKIE = "sessionid"
USER_COOKIE = "ds_user_id"


def profile_dir(account_id: int) -> Path:
    return PROFILES_DIR / f"acct-{account_id}"


def cookie_db(account_id: int) -> Path:
    return profile_dir(account_id) / "Default" / "Network" / "Cookies"


def _read_cookies(source: Path) -> list[dict]:
    """Instagram cookies out of a Chrome cookie store.

    Copy the file aside and read the copy — Chrome keeps it open. While Chrome
    is *running* on this profile the copy is refused outright (Windows gives it
    an exclusive lock; SQLite's read-only/immutable URI can't get in either),
    so this raises and the caller reports **Unknown**. Never "Logged Out": a
    read failure is not evidence of a missing session, and saying so would
    hide a live one.
    """
    tmp = Path(tempfile.gettempdir()) / f"instabot-ck-{os.getpid()}-{source.parent.parent.name}.sqlite"
    try:
        shutil.copy2(source, tmp)
    except OSError as exc:
        raise OSError(f"profile is in use by a running Chrome ({exc.strerror or exc})") from exc
    try:
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT name, expires_utc FROM cookies WHERE host_key LIKE ?", ("%instagram.com",)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    finally:
        tmp.unlink(missing_ok=True)


def _chrome_time(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return _CHROME_EPOCH + timedelta(microseconds=value)
    except (OverflowError, OSError):
        return None


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def inspect(account_id: int) -> dict:
    """Read the saved session straight off disk. Never launches Chrome."""
    path = profile_dir(account_id)
    source = cookie_db(account_id)
    result = {
        "platform": PLATFORM,
        "path": str(path.relative_to(config.ROOT_DIR)),
        "exists": path.exists(),
        "valid": False,
        "expires_at": None,
        "user_id": None,
        "cookies": 0,
        "size_bytes": _dir_size(path) if path.exists() else 0,
        "reason": None,
    }
    if not source.exists():
        result["reason"] = "No session stored yet."
        return result

    try:
        rows = _read_cookies(source)
    except (sqlite3.Error, OSError) as exc:
        # unreadable != logged out. Saying "Logged Out" here would be a lie
        # that hides a live session.
        result["status_unknown"] = True
        result["reason"] = f"Couldn't read the cookie store ({exc}). Close Chrome and re-check."
        return result

    result["cookies"] = len(rows)
    session = next((r for r in rows if r["name"] == SESSION_COOKIE), None)
    if session is None:
        result["reason"] = "No sessionid cookie — logged out."
        return result

    expires = _chrome_time(session["expires_utc"])
    result["expires_at"] = expires.isoformat(timespec="seconds") if expires else None
    if expires and expires <= datetime.now(timezone.utc):
        result["reason"] = f"Session expired {expires.date()}."
        return result

    result["valid"] = True
    result["reason"] = "Valid session cookie on disk."
    if any(r["name"] == USER_COOKIE for r in rows):
        result["user_id"] = "present"
    return result


def describe(account: dict) -> dict:
    """Everything the UI shows for one account's session.

    When the profile is open in Chrome the cookie file is locked and inspect()
    comes back blank (status_unknown). We fall back to the facts cached on the
    account the last time we COULD read it — so the card keeps showing the
    session it obviously still has, instead of emptying every field.
    """
    info = inspect(account["id"])
    locked = bool(info.get("status_unknown"))
    return {
        "account_id": account["id"],
        "account_name": account.get("label") or account["login"],
        "login": account["login"],
        "platform": PLATFORM,
        "status": "Unknown" if locked
        else ("Logged In" if info["valid"] else "Logged Out"),
        "logged_in": info["valid"],
        # a locked file doesn't mean unlocked account — keep the lock while open
        "locked": bool(account.get("locked")) and (info["valid"] or locked),
        "session_created_at": account.get("session_created_at"),
        "session_last_used": account.get("session_last_used"),
        "session_path": info["path"],
        "session_valid": info["valid"],
        "session_expires_at": info["expires_at"] if not locked
        else account.get("session_expires_at"),
        "session_size_bytes": info["size_bytes"] or (account.get("session_size_bytes") or 0),
        "cookies": info["cookies"] or (account.get("session_cookies") or 0),
        "reason": info["reason"],
    }


def inspect_path(path: Path) -> dict:
    """Same read as inspect(), for a profile that isn't an account's."""
    source = path / "Default" / "Network" / "Cookies"
    result = {"path": _rel(path), "exists": path.exists(), "valid": False, "reason": "No session stored."}
    if not source.exists():
        return result
    try:
        rows = _read_cookies(source)
    except (sqlite3.Error, OSError) as exc:
        result["status_unknown"] = True
        result["reason"] = f"Couldn't read ({exc}). Close Chrome and re-check."
        return result

    session = next((r for r in rows if r["name"] == SESSION_COOKIE), None)
    if session is None:
        result["reason"] = "No sessionid — logged out."
        return result
    expires = _chrome_time(session["expires_utc"])
    if expires and expires <= datetime.now(timezone.utc):
        result["reason"] = f"Expired {expires.date()}."
        return result
    result["valid"] = True
    result["reason"] = "Logged in."
    result["expires_at"] = expires.isoformat(timespec="seconds") if expires else None
    return result


def default_profile() -> dict:
    """The shared profile 'Open Chrome' uses when no account is active.

    Surfaced deliberately: it collects its own Instagram login, and when that
    was invisible a "cleared" app could still open logged in.
    """
    info = inspect_path(config.PLAYWRIGHT_USER_DATA_DIR)
    return {
        "account_id": None,
        "account_name": "default profile (no account)",
        "login": "—",
        "platform": PLATFORM,
        "status": "Unknown" if info.get("status_unknown")
        else ("Logged In" if info["valid"] else "Logged Out"),
        "logged_in": info["valid"],
        "locked": False,
        "session_path": info["path"],
        "session_valid": info["valid"],
        "session_expires_at": info.get("expires_at"),
        "session_size_bytes": _dir_size(config.PLAYWRIGHT_USER_DATA_DIR)
        if config.PLAYWRIGHT_USER_DATA_DIR.exists() else 0,
        "reason": info["reason"],
        "is_default": True,
    }


def refresh(account_id: int) -> dict:
    """Re-read disk and write the truth back onto the account record."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")

    info = inspect(account_id)
    if info.get("status_unknown"):
        # Chrome has the cookie file locked, so disk can't answer. Keep what we
        # already knew: overwriting it with False would erase the live-page
        # evidence accounts._public() falls back to, turning an open, logged-in
        # window into a "Logged Out" card.
        db.update_account(account_id, {"session_last_used": _now()})
        result = describe(account)
        if account.get("logged_in"):
            # describe() re-reads the same locked file and shrugs too, but the
            # record still holds what the live page check last saw.
            result.update(
                status="Logged In",
                logged_in=True,
                session_valid=True,
                reason="Profile is open in Chrome — reporting the last known live session.",
            )
        return result

    changes = {"logged_in": info["valid"], "session_last_used": _now()}
    if info["valid"]:
        # first time we've seen a session for this account
        if not account.get("session_created_at"):
            changes["session_created_at"] = _now()
        changes["locked"] = True          # a live session locks the account
        # Cache what we just read, so a later locked read (Chrome open on this
        # profile) can still show expiry/size/cookies instead of blanks.
        changes["session_expires_at"] = info["expires_at"]
        changes["session_size_bytes"] = info["size_bytes"]
        changes["session_cookies"] = info["cookies"]
    else:
        changes["locked"] = False
        changes["session_created_at"] = None
        changes["session_expires_at"] = None
        changes["session_cookies"] = 0
    db.update_account(account_id, changes)
    return describe({**account, **changes})


def sweep() -> dict:
    """Re-check every account. Run at startup: sessions expire while we're off."""
    checked, live = 0, 0
    for account in db.list_accounts():
        try:
            info = refresh(account["id"])
            checked += 1
            live += bool(info["logged_in"])
        except Exception as exc:  # noqa: BLE001 — one bad profile must not stop boot
            activity.log("sessions", f"Sweep failed for {account['id']}: {exc}", "warn")
    activity.log("sessions", f"Session sweep: {live}/{checked} accounts logged in", "ok")
    return {"checked": checked, "logged_in": live}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(config.ROOT_DIR))
    except ValueError:
        return str(path)


def _remove_tree(path: Path) -> dict:
    """Delete a profile and PROVE it's gone.

    rmtree(ignore_errors=True) is a trap here: Windows refuses to delete files
    a running Chrome holds open, and the error is swallowed — so "cleared" is
    reported while the session sits on disk, still logged in. Retry for locks
    that are about to release, then check and admit failure.
    """
    entry = {"path": _rel(path), "existed": path.exists(), "removed": True, "error": None}
    if not path.exists():
        return entry

    last = None
    for attempt in range(4):
        try:
            shutil.rmtree(path)
        except Exception as exc:  # noqa: BLE001 — a lock we may outlive
            last = str(exc)
            time.sleep(0.4 * (attempt + 1))
        if not path.exists():
            return entry

    entry["removed"] = False
    entry["error"] = last or "still on disk"
    return entry


def clear(account_id: int) -> dict:
    """Delete the stored session and unlock the account."""
    account = db.get_account(account_id)
    if not account:
        raise KeyError(f"No account {account_id}")

    result = _remove_tree(profile_dir(account_id))
    db.update_account(
        account_id,
        {
            "logged_in": False,
            "locked": False,
            "session_created_at": None,
            "session_last_used": _now(),
            "last_result": "session_cleared",
        },
    )
    who = account.get("label") or account["login"]
    activity.log(
        "sessions",
        f"Cleared session for {who}" if result["removed"]
        else f"COULD NOT clear {who}: {result['error']}",
        "ok" if result["removed"] else "error",
    )
    return {"account_id": account_id, "who": who, "status": "Logged Out", **result}


def clear_all() -> dict:
    """Wipe every stored session — including the default profile.

    The default profile (PLAYWRIGHT_USER_DATA_DIR) is what "Open Chrome" uses
    when no account is active, and it collects its own Instagram login. Leaving
    it behind is why a "cleared" app could still open already logged in.
    """
    results = [clear(a["id"]) for a in db.list_accounts()]

    # the default profile — not an account, but absolutely a stored session
    default = _remove_tree(config.PLAYWRIGHT_USER_DATA_DIR)
    default["who"] = "default profile (no account)"
    results.append(default)

    # anything under profiles/ with no account behind it
    keep = {f"acct-{a['id']}" for a in db.list_accounts()}
    if PROFILES_DIR.exists():
        for child in sorted(PROFILES_DIR.iterdir()):
            if child.is_dir() and child.name not in keep:
                orphan = _remove_tree(child)
                orphan["who"] = f"orphan {child.name}"
                results.append(orphan)

    failed = [r for r in results if not r["removed"]]
    activity.log(
        "sessions",
        f"Cleared {len(results) - len(failed)}/{len(results)} sessions"
        + (f" — {len(failed)} LOCKED" if failed else ""),
        "warn" if failed else "ok",
    )
    return {
        "ok": not failed,
        "cleared": len([r for r in results if r["removed"] and r["existed"]]),
        "already_empty": len([r for r in results if not r["existed"]]),
        "failed": len(failed),
        "results": results,
        "detail": (
            "All sessions removed. Opening Chrome now lands on a logged-out Instagram."
            if not failed
            else "Some profiles are still locked by a running Chrome. Close every "
                 "Chrome window opened by this app and clear again."
        ),
    }


def is_locked(account_id: int) -> bool:
    account = db.get_account(account_id)
    return bool(account and account.get("locked")) and inspect(account_id)["valid"]


def unlock(account_id: int) -> dict:
    db.update_account(account_id, {"locked": False})
    return {"account_id": account_id, "locked": False}


def touch(account_id: int) -> None:
    """Mark the session as just used."""
    db.update_account(account_id, {"session_last_used": _now()})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
