"""Instagram session helpers.

The important bit is NOT the auto-login — it's the persistent Chrome profile in
playwright_runner. Once a session exists on disk, Instagram stops asking. So the
flow is: check if we're already logged in, and only touch the form if we aren't.

Selectors cover two form variants Instagram serves:
  * classic:  input[name="username"] / input[name="password"] / button[type=submit]
  * FB-style: input[name="email"]    / input[name="pass"]     / input[type=submit]
"""
import asyncio
import random

from backend import config, settings
from backend.automation import cursor, typing

HOME = "https://www.instagram.com/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"

USER_SEL = 'input[name="username"], input[name="email"]'
PASS_SEL = 'input[name="password"], input[name="pass"]'
SUBMIT_SEL = 'button[type="submit"], input[type="submit"]'
SUBMIT_FALLBACK = 'div[role="button"]:has-text("Log in")'
COOKIE_SEL = (
    'button:has-text("Allow all cookies"), button:has-text("Decline optional cookies"), '
    'div[role="button"]:has-text("Allow all cookies"), '
    'div[role="button"]:has-text("Decline optional cookies")'
)


async def type_like_human(locator, text: str, cfg: dict | None = None) -> dict:
    """Type `text` using the Config panel's settings. See automation/typing.py."""
    return await typing.type_text(locator, text, cfg or settings.typing())


async def _dismiss_cookies(page) -> bool:
    """The cookie banner covers the form; it isn't always present."""
    try:
        button = page.locator(COOKIE_SEL).first
        if await button.count() and await button.is_visible():
            await button.click(timeout=4000)
            await page.wait_for_timeout(800)
            return True
    except Exception:  # noqa: BLE001 — never fail login over a banner
        pass
    return False


SESSION_COOKIE = "sessionid"


async def _has_session_cookie(page) -> bool:
    """True when Instagram has set a non-empty `sessionid` cookie.

    This IS Instagram's 'logged in', read live from the browser — the same
    cookie sessions.inspect() reads off disk. It's the authoritative signal:
    immune to which page is on screen and to hidden form elements, so it doesn't
    trip on the '/accounts/onetap/' ("Save your login info?") interstitial the
    way a DOM check does.
    """
    try:
        cookies = await page.context.cookies("https://www.instagram.com/")
    except Exception:  # noqa: BLE001
        return False
    return any(
        c.get("name") == SESSION_COOKIE and (c.get("value") or "").strip()
        for c in cookies
    )


async def is_logged_in(page) -> bool:
    """Logged in == a sessionid cookie exists. The login-form check is only a
    fallback for the moment before that cookie is set.

    Was: "logged out == a login form is on screen". That read the ABSENCE of a
    form as success, which broke on the post-login 'Save your login info?' page
    and on any hidden username/email input Instagram leaves in the DOM — the
    account showed 'Logged Out' seconds after a real login.
    """
    if await _has_session_cookie(page):
        return True
    # No session cookie yet — fall back to the form. A login/signup URL or a
    # username field on screen means we're still logged out.
    if "/accounts/login" in page.url or "/accounts/emailsignup" in page.url:
        return False
    try:
        return await page.locator(USER_SEL).count() == 0
    except Exception:  # noqa: BLE001
        return False


async def status(page) -> dict:
    await page.goto(HOME, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    logged_in = await is_logged_in(page)
    return {
        "logged_in": logged_in,
        "url": page.url,
        "profile": str(config.PLAYWRIGHT_USER_DATA_DIR.relative_to(config.ROOT_DIR)),
        "hint": (
            "Session is stored in the Chrome profile — it survives restarts."
            if logged_in
            else "Not logged in. Log in once (by hand or with the button) and it will stick."
        ),
    }


async def fill_credentials(page, username: str, password: str) -> dict:
    """Type credentials into the login form and stop — no submit.

    Corrections (delete-and-retype) stay ON here, unlike the address bar: this
    form has no inline autocomplete to fight with, and the React inputs want
    real key events anyway.
    """
    if not username or not password:
        raise ValueError("Both a username and a password are needed.")

    if await is_logged_in(page):
        return {"filled": False, "reason": "already_logged_in",
                "detail": "Already logged in — no login form to fill."}

    await _dismiss_cookies(page)
    cfg = settings.typing()

    user_box = page.locator(USER_SEL).first
    pass_box = page.locator(PASS_SEL).first
    try:
        await user_box.wait_for(state="visible", timeout=15000)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "No Instagram login form on this page. Is the browser on "
            "instagram.com, and are you logged out?"
        ) from exc

    await user_box.fill("")
    user_stats = await typing.type_text(user_box, username, cfg)
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await pass_box.fill("")
    pass_stats = await typing.type_text(pass_box, password, cfg)

    # what actually landed in the boxes, not what we meant to type
    typed_user = await user_box.input_value()
    ok = typed_user == username and len(await pass_box.input_value()) == len(password)
    return {
        "filled": True,
        "verified": ok,
        "username_keystrokes": user_stats["keystrokes"],
        "password_keystrokes": pass_stats["keystrokes"],
        "backspaces": user_stats["backspaces"] + pass_stats["backspaces"],
        "detail": "Credentials typed. Press Log in yourself, or use Accounts → Log in selected."
        if ok
        else "Typed, but the fields don't match what was sent — check the form.",
    }


async def login(page, username: str | None = None, password: str | None = None) -> dict:
    """Log in only if needed. Returns a dict describing what happened."""
    user = (username or config.IG_USERNAME).strip()
    pwd = password or config.IG_PASSWORD

    await page.goto(HOME, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    if await is_logged_in(page):
        return {
            "logged_in": True,
            "already": True,
            "action": "none",
            "detail": "Session restored from the Chrome profile — no login needed.",
        }

    if not user or not pwd:
        return {
            "logged_in": False,
            "already": False,
            "action": "manual",
            "detail": (
                "No IG_USERNAME / IG_PASSWORD in .env. Log in by hand in the Chrome "
                "window that's open — the profile will remember it from then on."
            ),
        }

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    await _dismiss_cookies(page)

    cfg = settings.typing()  # read once, so both fields type the same way
    try:
        user_box = page.locator(USER_SEL).first
        pass_box = page.locator(PASS_SEL).first
        await user_box.wait_for(state="visible", timeout=15000)
        await type_like_human(user_box, user, cfg)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await type_like_human(pass_box, pwd, cfg)
        await asyncio.sleep(random.uniform(0.3, 0.9))
    except Exception as exc:  # noqa: BLE001
        return {
            "logged_in": False,
            "action": "failed",
            "detail": f"Could not find the login form ({exc}). Instagram may have "
            "changed its markup — log in by hand this once.",
        }

    return await click_login(page)


async def _submit_locator(page):
    """The Log in button, however this build of the page spells it."""
    submit = page.locator(SUBMIT_SEL).first
    if not await submit.count():
        submit = page.locator(SUBMIT_FALLBACK).first
    if not await submit.count():
        raise RuntimeError(
            "No Log in button on this page. Open the login form first — "
            "step 3 types the URL, step 4 fills it in."
        )
    return submit


async def _await_outcome(page, seconds: int) -> dict | None:
    """Poll until the form resolves into home, a challenge, or an error.
    None means it never moved."""
    for _ in range(seconds):
        await asyncio.sleep(1)
        outcome = await _classify(page)
        if outcome:
            return outcome
    return None


async def click_login(page, method: str = "inspect", wait: int = 30) -> dict:
    """Press Log in on the form already on screen, and report what it became.

    Split out of login() so the Automation panel can type the credentials and
    press the button as two separate, watchable steps. Types nothing itself —
    whatever is in the boxes is what gets submitted.

    Two ways to press it:
      * "cursor"  — the real mouse, via PyAutoGUI. What a person's click looks
        like, and it can miss: the window may be covered, scrolled, or moved.
      * "inspect" — Playwright's own click, aimed by DOM selector. Cannot miss,
        but the event is synthetic and never touches the OS.

    A cursor click that lands on nothing leaves the page sitting there, so
    silence is a MISS, not a slow login — it raises, and the caller retries.
    """
    await _dismiss_cookies(page)
    submit = await _submit_locator(page)

    if method == "cursor":
        aim = await cursor.click_element(page, submit)
    else:
        await submit.click(timeout=15000)
        aim = {"method": "inspect"}

    outcome = await _await_outcome(page, wait)
    if outcome:
        return {**outcome, **aim}

    if method == "cursor":
        raise RuntimeError(
            f"Pointer clicked at {aim['clicked_at']['x']},{aim['clicked_at']['y']} but the "
            f"form hasn't moved in {wait}s — the click missed the Log in button. Is the "
            "Chrome window covered by another one?"
        )
    return {
        "logged_in": await is_logged_in(page),
        "action": "timeout",
        "detail": f"Login did not resolve in {wait}s. Check the Chrome window.",
        **aim,
    }


# Instagram's "those credentials are wrong" copy. It moved out of the old
# #slfErrorAlert container into a plain, obfuscated-class <div> — so the TEXT is
# the stable signal, not the markup. Lower-cased; the first two are the current
# ones the user sees. ONLY these count as a login failure.
LOGIN_ERROR_PHRASES = (
    "the login information you entered is incorrect",   # + "Find your account and log in."
    "the password you entered is incorrect",
    "your password was incorrect",
    "sorry, your password was incorrect",
    "the username you entered doesn't belong",
    "incorrect username or password",
    "couldn't log you in",
)


async def _login_error(page) -> str | None:
    """The visible 'login failed' message, or None. ONLY the specific login-
    rejection copy above counts. A generic role=alert on the logged-in page (a
    notifications prompt, a toast, a cookie banner, …) is NOT a login error — the
    old code returned ANY alert text, so a real cursor login was read as 'Failed'.
    Checked before success so a real rejection can't be mistaken for logged in."""
    def _matches(text: str) -> bool:
        low = text.lower()
        return any(phrase in low for phrase in LOGIN_ERROR_PHRASES)

    # Alert containers — but only when their text is actually a rejection phrase.
    for sel in ("#slfErrorAlert", 'div[role="alert"]'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                text = (await loc.inner_text()).strip()
                if text and _matches(text):
                    return text[:200]
        except Exception:  # noqa: BLE001
            pass
    # Otherwise, the rejection phrase anywhere in the visible page text. These
    # phrases only ever appear on a failed login, so this won't match home.
    # inner_text() returns only what's visible, so a hidden signup form is safe.
    try:
        visible = (await page.locator("body").inner_text()).lower()
    except Exception:  # noqa: BLE001
        return None
    for phrase in LOGIN_ERROR_PHRASES:
        if phrase in visible:
            return phrase
    return None


async def _classify(page) -> dict | None:
    """Map the post-submit page onto an outcome, or None to keep waiting."""
    url = page.url
    if "two_factor" in url or await page.locator('input[name="verificationCode"]').count():
        return {
            "logged_in": False,
            "action": "2fa_required",
            "detail": "Instagram wants a 2FA code. Enter it in the Chrome window — "
            "once you're through, the profile keeps the session.",
        }
    if "challenge" in url:
        return {
            "logged_in": False,
            "action": "challenge",
            "detail": "Instagram flagged this as a suspicious login and wants "
            "verification. Finish it in the Chrome window.",
        }
    # A visible rejection wins over everything below — check it before success so
    # a stale cookie can't paper over "your password is wrong".
    error = await _login_error(page)
    if error:
        return {"logged_in": False, "action": "rejected", "detail": error[:200]}
    if await is_logged_in(page):
        return {
            "logged_in": True,
            "already": False,
            "action": "logged_in",
            "detail": "Logged in. The session is saved in the Chrome profile — "
            "you won't be asked again.",
        }
    return None
