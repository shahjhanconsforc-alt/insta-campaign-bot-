from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import accounts, db, sessions

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountBody(BaseModel):
    label: str = ""
    login: str = ""
    password: str = ""


class LoginBody(BaseModel):
    ids: list[int] = []


@router.get("")
async def index():
    return {"accounts": accounts.list_accounts()}


@router.post("")
async def create(body: AccountBody):
    try:
        return {"account": accounts.add_account(body.label, body.login, body.password)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{account_id}/secret")
async def secret(account_id: int):
    """Backs the eye icon. Kept off the list response so passwords aren't sitting
    in the page until actually asked for."""
    try:
        return {"password": accounts.reveal(account_id)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{account_id}")
async def delete(account_id: int):
    if not accounts.remove_account(account_id):
        raise HTTPException(404, f"No account {account_id}")
    return {"deleted": account_id}


@router.post("/{account_id}/open")
async def open_one(account_id: int):
    try:
        return await accounts.open_account(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/login")
async def login(body: LoginBody):
    if not body.ids:
        raise HTTPException(400, "Select at least one account first.")
    return await accounts.login_accounts(body.ids)


# ─── Sessions ────────────────────────────────────────────────────────────


@router.get("/sessions")
async def sessions_index():
    """Every stored session, read from disk — the source of truth. Includes the
    default profile, which is not an account but does hold a login."""
    return {
        "sessions": [sessions.describe(a) for a in db.list_accounts()],
        "default_profile": sessions.default_profile(),
    }


@router.post("/sessions/check")
async def sessions_check_all():
    """Check Login Status — every stored session, straight off disk.

    Reads each profile's cookie store; no browser is launched and no login is
    attempted. A valid `sessionid` means Logged In; anything else (no profile,
    no cookie, expired) is Logged Out.
    """
    result = sessions.sweep()
    return {**result, "accounts": accounts.list_accounts()}


@router.post("/sessions/sweep")
async def sessions_sweep():
    return sessions.sweep()


@router.post("/sessions/clear")
async def sessions_clear_all():
    """Delete every stored session — account profiles AND the default profile.
    Accounts and credentials are kept."""
    import asyncio

    from backend.automation import playwright_runner as browser

    # Chrome holds its profile's files open, so deletion silently fails while
    # it's alive. Close it and give the process a moment to actually exit.
    await browser.close_browser()
    await asyncio.sleep(1.2)

    result = sessions.clear_all()
    if not result["ok"]:
        # say so loudly rather than report a clear that didn't happen
        raise HTTPException(409, result["detail"])
    return result


@router.get("/{account_id}/session")
async def session_of(account_id: int):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, f"No account {account_id}")
    return sessions.describe(account)


@router.post("/{account_id}/session/check")
async def session_check(account_id: int):
    """Re-read one account's stored session from disk. No browser."""
    try:
        sessions.refresh(account_id)
        return accounts._public(db.get_account(account_id))  # noqa: SLF001
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{account_id}/session/close")
async def session_close(account_id: int):
    """Close just this account's window, leaving its session on disk."""
    from backend.automation import playwright_runner as browser

    return await browser.close_browser(profile=sessions.profile_dir(account_id))


@router.post("/{account_id}/logout")
async def logout(account_id: int):
    """Log out: close the window and delete the stored session."""
    from backend.automation import playwright_runner as browser

    await browser.close_browser(profile=sessions.profile_dir(account_id))
    import asyncio

    await asyncio.sleep(1.0)          # let Chrome release the profile's files
    try:
        return sessions.clear(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{account_id}/session")
async def session_clear(account_id: int):
    from backend.automation import playwright_runner as browser

    try:
        if browser.current_profile() == sessions.profile_dir(account_id):
            await browser.close_browser()
        return sessions.clear(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{account_id}/session/unlock")
async def session_unlock(account_id: int):
    return sessions.unlock(account_id)


@router.post("/{account_id}/new-login")
async def new_login(account_id: int):
    """Drop the stored session, then log in fresh and re-lock."""
    try:
        return await accounts.new_login(account_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
