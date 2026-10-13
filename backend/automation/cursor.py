"""Real cursor + keyboard control of Chrome's address bar.

Why this exists: Playwright cannot touch the omnibox. It drives the *page* —
the address bar is browser chrome, outside the DOM. `page.mouse` is
viewport-relative and Ctrl+L via Playwright lands in the page. So typing a URL
into the real address bar means OS-level input.

Consequences worth knowing:
  * it moves your actual mouse pointer and takes focus — you cannot use the
    machine while it runs;
  * Windows only (pywinauto/UIA);
  * the Chrome window must be visible (PLAYWRIGHT_HEADLESS=false).
"""
import asyncio
import random
import threading

from backend import activity, settings
from backend.automation import typing

OMNIBOX = "Address and search bar"
NEW_TAB = "New Tab"

# The + click misses often enough to be worth several goes: a just-launched
# window eats the first click, and the button slides along the tab strip as
# tabs open, so a stale aim lands on a tab instead.
NEW_TAB_ATTEMPTS = 5

# Live progress of the address-bar typing, so the UI can show each character
# landing in Chrome as it's typed. Written from the worker thread that types,
# read from the /type-progress endpoint on the event loop — hence the lock.
_type_lock = threading.Lock()
_type_state = {
    "active": False,     # a type is in flight right now
    "target": "",        # the URL we're aiming to type
    "chrome": "",        # what the real address bar reads this instant
    "done": 0,           # characters entered so far
    "total": 0,          # characters to enter
    "matched": False,    # does the address bar now equal the target URL?
    "diverged_at": None, # index of the first character that didn't land, if any
}


def _progress(**changes) -> None:
    with _type_lock:
        _type_state.update(changes)


def type_progress() -> dict:
    """A snapshot of the live typing state for the polling endpoint."""
    with _type_lock:
        return dict(_type_state)


class CursorError(RuntimeError):
    pass


def _deps():
    try:
        import pyautogui
        from pywinauto import Desktop
    except ImportError as exc:  # noqa: BLE001
        raise CursorError(
            f"Desktop control needs pywinauto + pyautogui ({exc}). "
            "Install with: pip install -r requirements.txt"
        ) from exc
    pyautogui.FAILSAFE = True  # slam the pointer into a corner to abort
    return pyautogui, Desktop


def _our_chrome_pids(profile) -> set[int]:
    """PIDs of the Chrome we launched, identified by our profile dir on their
    command line. Without this we could drive the user's *personal* Chrome —
    typing a URL into whatever window happened to be open."""
    import psutil

    needle = str(profile).lower()
    pids = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() != "chrome.exe":
                continue
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
            if f"--user-data-dir={needle}" in cmdline or needle in cmdline:
                pids.add(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _find_omnibox(profile):
    """The address bar of *our* Chrome window.

    Two filters matter here:
      * Electron apps (VS Code!) also report class Chrome_WidgetWin, so the
        address bar's presence is the real test of "this is a browser";
      * the window's PID must belong to the Chrome we launched, or we would
        happily type into the user's own browser.
    """
    _, Desktop = _deps()
    pids = _our_chrome_pids(profile)
    if not pids:
        raise CursorError(
            "Our Chrome isn't running. Click 'Open Chrome window' first "
            "(and PLAYWRIGHT_HEADLESS must be false — a hidden window has no address bar)."
        )

    for win in Desktop(backend="uia").windows():
        try:
            if "Chrome_WidgetWin" not in (win.class_name() or ""):
                continue
            if win.element_info.process_id not in pids:
                continue  # someone else's Chrome — leave it alone
            for edit in win.descendants(control_type="Edit"):
                if edit.element_info.name == OMNIBOX:
                    return win, edit
        except Exception:  # noqa: BLE001 — unreadable windows are not ours
            continue

    raise CursorError(
        "Found our Chrome process but no address bar on it. Is the window "
        "minimised, or running headless?"
    )


def _click_new_tab(profile) -> dict:
    """Blocking: move the real pointer onto Chrome's + and click it."""
    pyautogui, _ = _deps()
    win, _edit = _find_omnibox(profile)
    try:
        win.set_focus()
    except Exception:  # noqa: BLE001
        pass

    # A window that has only just launched swallows the first click. Give it a
    # beat before aiming at the +.
    _sleep(0.6)

    button = None
    for ctrl in win.descendants(control_type="Button"):
        try:
            if ctrl.element_info.name == NEW_TAB:
                button = ctrl
                break
        except Exception:  # noqa: BLE001
            continue
    if button is None:
        raise CursorError("Chrome's + (New Tab) button wasn't found on our window.")

    # Aim at the middle of the button, jittered only slightly. The rect runs to
    # the top of the window, so picking anywhere inside it can land above the
    # visible + — on the title bar, or on nothing.
    rect = button.rectangle()
    cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    jx, jy = max(2, rect.width() // 6), max(2, rect.height() // 6)
    x = min(max(cx + random.randint(-jx, jx), rect.left + 3), rect.right - 3)
    y = min(max(cy + random.randint(-jy, jy), rect.top + 3), rect.bottom - 3)

    pyautogui.moveTo(x, y, duration=random.uniform(0.25, 0.6), tween=pyautogui.easeOutQuad)
    pyautogui.click()
    _sleep(random.uniform(0.4, 0.8))
    return {
        "clicked_at": {"x": x, "y": y},
        "button": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
    }


def profile_for(account_id: int | None):
    """Which Chrome profile a cursor action should drive.

    With an account, its own profile — so picking account 2 in the panel drives
    account 2's window even if another account's window was touched more
    recently. Without one, whatever window is active (the old behaviour).
    """
    from backend.automation import playwright_runner as browser

    if account_id is None:
        return browser.current_profile()
    from backend import sessions

    return sessions.profile_dir(account_id)


async def new_tab(account_id: int | None = None) -> dict:
    """Open a tab by clicking Chrome's + with the real cursor, then hand the
    new tab to Playwright so everything downstream targets it."""
    from backend.automation import playwright_runner as browser

    profile = profile_for(account_id)
    if not browser.is_open(profile):
        raise CursorError("No Chrome window is open for that account. Open it first.")

    before = browser.page_count(profile)
    activity.log("cursor", "Clicking Chrome's + for a new tab")

    # Verify rather than assume, and keep trying. The click is genuinely flaky:
    # a window that has just launched swallows the first one, and the + shifts
    # along the tab strip as tabs open, so a stale position misses it entirely.
    # Every attempt re-finds the button from scratch rather than reusing
    # coordinates, and a miss is silent — without the tab-count check we would
    # carry on and type the URL into the old tab.
    last = None
    for attempt in range(1, NEW_TAB_ATTEMPTS + 1):
        try:
            last = await asyncio.to_thread(_click_new_tab, profile)
        except CursorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CursorError(f"Could not click the + button: {exc}") from exc

        adopted = await browser.adopt_newest_page(timeout=3.0, profile=profile)
        if adopted["tabs"] > before:
            if attempt > 1:
                activity.log("cursor", f"+ clicked on attempt {attempt}", "ok")
            activity.log("cursor", f"New tab open ({adopted['tabs']} tabs)", "ok")
            return {**last, **adopted, "attempts": attempt}

        if attempt < NEW_TAB_ATTEMPTS:
            activity.log(
                "cursor",
                f"+ click {attempt}/{NEW_TAB_ATTEMPTS} didn't register — re-locating and retrying",
                "warn",
            )
            await asyncio.sleep(0.5 + 0.25 * attempt)   # back off a little each time

    raise CursorError(
        f"Clicked + {NEW_TAB_ATTEMPTS} times but no tab opened. Is the Chrome "
        "window visible, not minimised, and not covered by another window?"
    )


# The Log in button's accessible name. Instagram renders a real
# <button type="submit">Log in</button>, which Chrome exposes to Windows' UIA
# tree exactly like the New Tab button — so we can find it the same way and read
# its true SCREEN rectangle, instead of translating web-page coordinates by hand
# (which never lined up with the pointer's coordinate space).
LOGIN_NAMES = ("log in", "login", "log in ")


def _find_login_button(win):
    """The Log in submit button on our Chrome window, from the UIA tree.

    Same approach as the + button: walk the window's controls and match by
    accessible name. Prefer a visible, exact "Log in" — skip the "Log in with
    Facebook" style links, which merely contain the words.
    """
    fallback = None
    for ctrl in win.descendants(control_type="Button"):
        try:
            name = (ctrl.element_info.name or "").strip().lower()
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        exact = name in LOGIN_NAMES
        loose = "log in" in name and "facebook" not in name and "google" not in name
        if not (exact or loose):
            continue
        try:
            if ctrl.is_visible() and exact:
                return ctrl                    # the one we want
        except Exception:  # noqa: BLE001
            pass
        fallback = fallback or ctrl
    return fallback


def _click_login_cursor(profile) -> dict:
    """Blocking: raise our Chrome, walk the pointer onto the Log in button,
    click it. Mirrors _click_new_tab — UIA gives the real screen rect, so no
    coordinate translation and no DPI guesswork."""
    pyautogui, _ = _deps()
    win, _edit = _find_omnibox(profile)
    try:
        win.set_focus()
    except Exception:  # noqa: BLE001 — focus can fail but the click still lands
        pass
    _sleep(0.5)  # a window that just came forward swallows the first click

    button = _find_login_button(win)
    if button is None:
        raise CursorError(
            "Couldn't find the Log in button on the page. Is the login form "
            "actually on screen (step 3), and the window visible, not minimised?"
        )

    # Aim for the middle of the button, jittered slightly but kept well inside
    # its edges — the same care _click_new_tab takes with the +.
    rect = button.rectangle()
    cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    jx, jy = max(2, rect.width() // 6), max(2, rect.height() // 6)
    x = min(max(cx + random.randint(-jx, jx), rect.left + 3), rect.right - 3)
    y = min(max(cy + random.randint(-jy, jy), rect.top + 3), rect.bottom - 3)

    pyautogui.moveTo(x, y, duration=random.uniform(0.3, 0.7), tween=pyautogui.easeOutQuad)
    pyautogui.click()
    _sleep(random.uniform(0.4, 0.8))
    return {
        "method": "cursor",
        "clicked_at": {"x": x, "y": y},
        "button": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
    }


async def click_element(page, locator) -> dict:
    """Click the Log in button with the REAL pointer instead of Playwright's.

    Playwright's click is dispatched inside the renderer — it always lands, and
    is exactly the sort of synthetic event a bot check can tell apart from a
    person. This walks the actual mouse there and presses it, so what Instagram
    sees is a genuine OS click. It can also miss, which is the trade.

    The `locator` is used to scroll the button into view (so UIA sees it on
    screen); the click itself is aimed by UIA, like the New Tab button.
    """
    from backend.automation import playwright_runner as browser

    if not browser.is_open():
        raise CursorError("No Chrome window is open. Open Chrome first.")

    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001 — UIA can still find it; don't fail here
        pass

    activity.log("cursor", "Moving pointer to the Log in button")
    try:
        result = await asyncio.to_thread(_click_login_cursor, browser.current_profile())
    except CursorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CursorError(f"Desktop control failed: {exc}") from exc

    activity.log("cursor", f"Clicked the Log in button at {result['clicked_at']}", "ok")
    return result

    activity.log("cursor", f"Clicked at {result['clicked_at']}", "ok")
    return result


def _drive(url: str, cfg: dict, profile) -> dict:
    """Blocking: move the pointer, click, type. Runs in a worker thread."""
    pyautogui, _ = _deps()
    win, edit = _find_omnibox(profile)

    try:
        win.set_focus()
    except Exception:  # noqa: BLE001 — focus can fail but the click still lands
        pass

    rect = edit.rectangle()
    pyautogui.PAUSE = 0

    # Chrome parks the caret in the address bar on a new tab, so clicking is
    # pointless there — just start typing. Only travel to the box when the
    # caret is somewhere else (e.g. on a loaded page).
    already_focused = _focused(edit)

    clicked_at = None
    if already_focused:
        _sleep(random.uniform(0.15, 0.35))
        pyautogui.hotkey("ctrl", "a")   # replace anything already in there
    else:
        # aim somewhere human inside the box, not dead centre every time
        x = random.randint(rect.left + 40, rect.left + max(41, rect.width() // 3))
        y = random.randint(rect.top + 8, rect.bottom - 8)
        # travel there in an arc rather than teleporting
        pyautogui.moveTo(x, y, duration=random.uniform(0.35, 0.75), tween=pyautogui.easeOutQuad)
        pyautogui.click()
        clicked_at = {"x": x, "y": y}
        _sleep(0.25)
        # A click that doesn't take focus leaves us typing into the void — the
        # keystrokes go to the page and the omnibox keeps the old URL. Check,
        # and fall back to asking UIA for focus directly.
        if not _focused(edit):
            pyautogui.click()
            _sleep(0.25)
            if not _focused(edit):
                try:
                    edit.set_focus()
                    _sleep(0.2)
                except Exception as exc:  # noqa: BLE001
                    raise CursorError(
                        f"Clicked the address bar but it never took focus ({exc})."
                    ) from exc
        pyautogui.hotkey("ctrl", "a")

    # No delete-and-retype here, whatever the Config panel says. The omnibox
    # inline-autocompletes and keeps the completion SELECTED, so a backspace
    # eats the selection instead of the last character and you end up
    # navigating somewhere else entirely. (Seen once as a cert warning on a
    # host we never asked for.) Corrections are for the login form, which has
    # no such behaviour.
    plan_cfg = {**cfg, "remove_enabled": False}
    events = typing.plan_keystrokes(url, plan_cfg)

    _progress(active=True, target=url, chrome="", done=0,
              total=sum(1 for e in events if e["key"] != typing.BACKSPACE),
              matched=False, diverged_at=None)
    try:
        actual = _type_verified(pyautogui, edit, url, events)
        if not _same_url(actual, url):
            # one full clear-and-retype, then give up rather than press Enter on
            # a mystery URL. Per-character checks already told us WHERE it drifted.
            pyautogui.hotkey("ctrl", "a")
            _sleep(0.2)
            actual = _type_verified(pyautogui, edit, url, events)
            if not _same_url(actual, url):
                raise CursorError(
                    f"Address bar reads {actual!r} but we meant {url!r}. Not pressing "
                    "Enter — Chrome's autocomplete probably interfered."
                )
        _progress(matched=True)
    finally:
        _progress(active=False)

    _sleep(random.uniform(0.2, 0.5))
    pyautogui.press("enter")

    return {
        "clicked_at": clicked_at,
        "clicked": clicked_at is not None,
        "already_focused": already_focused,
        "omnibox": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
        "omnibox_read_back": actual,
        "verified": True,
        **typing.summarize(url, events),
    }


def _type_verified(pyautogui, edit, url, events) -> str:
    """Type the URL one character at a time, checking each one lands.

    After every keystroke we read the real address bar and confirm what we've
    typed SO FAR is still a prefix of it. Chrome's inline autocomplete adds a
    suffix (typing "www.insta" shows "www.instagram.com"), so a prefix test —
    not equality — is the right check; a character that failed to register
    shortens the prefix and trips it. The live state is published after each
    keystroke so the UI can mirror the address bar as it fills in.

    Returns the final address-bar text. Records the first divergence but does
    not stop — the caller decides whether the end result is good enough.
    """
    intended = ""
    actual = ""
    diverged_at = None
    idx = 0
    for event in events:
        if event["key"] == typing.BACKSPACE:
            pyautogui.press("backspace")
            intended = intended[:-1]
        else:
            pyautogui.write(event["key"])
            intended += event["key"]
            idx += 1
        _sleep(event["delay_ms"] / 1000)

        actual = _omnibox_text(edit, settle=0.06)
        if not _prefix_ok(actual, intended) and diverged_at is None:
            diverged_at = idx
        _progress(chrome=actual, done=idx, diverged_at=diverged_at,
                  matched=_same_url(actual, url))
    return actual


def _prefix_ok(actual: str, intended: str) -> bool:
    """Is `intended` (what we've typed so far) the leading part of the address
    bar? Compared bare, so the scheme Chrome hides and its autocomplete suffix
    don't count against us."""

    def bare(value: str) -> str:
        value = value.strip().lower()
        for scheme in ("https://", "http://"):
            if value.startswith(scheme):
                value = value[len(scheme):]
        return value

    a, i = bare(actual), bare(intended)
    return not i or a.startswith(i) or i.startswith(a)


def focus_window(profile) -> dict:
    """Bring our Chrome window to the front, restoring it if minimised, so a
    retry always happens on a window that's actually visible on screen."""
    _deps()
    win, _edit = _find_omnibox(profile)
    try:
        if win.is_minimized():
            win.restore()
    except Exception:  # noqa: BLE001 — not all backends expose is_minimized
        pass
    try:
        win.set_focus()
    except Exception as exc:  # noqa: BLE001
        raise CursorError(f"Couldn't bring the Chrome window to the front ({exc}).") from exc
    return {"focused": True}


def _focused(edit) -> bool:
    """Does the address bar hold the caret? If we can't tell, assume not and
    click — a needless click is harmless, typing into the void is not."""
    try:
        return bool(edit.has_keyboard_focus())
    except Exception:  # noqa: BLE001
        return False


def _omnibox_text(edit, settle: float = 0.25) -> str:
    # A short settle lets the UIA value catch up with the keystroke. Per-char
    # reads pass a small one — 0.25s × every character would crawl.
    if settle:
        _sleep(settle)
    try:
        return (edit.get_value() or "").strip()
    except Exception:  # noqa: BLE001
        try:
            return (edit.window_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""


def _same_url(actual: str, wanted: str) -> str:
    """Chrome shows the omnibox its own way — it hides https:// and may add a
    trailing slash — so compare on the bare host+path."""

    def bare(value: str) -> str:
        value = value.strip().lower()
        for scheme in ("https://", "http://"):
            if value.startswith(scheme):
                value = value[len(scheme):]
        return value.rstrip("/")

    a, w = bare(actual), bare(wanted)
    return bool(a) and (a == w or a == f"www.{w}" or f"www.{a}" == w)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


async def type_url(url: str, account_id: int | None = None) -> dict:
    """Move the real cursor to Chrome's address bar, click, type `url`, Enter."""
    from backend.automation import playwright_runner as browser

    profile = profile_for(account_id)
    if not browser.is_open(profile):
        raise CursorError(
            "No Chrome window is open for that account. Click 'Open Chrome window' first."
        )

    cfg = settings.typing()
    activity.log("cursor", f"Moving pointer to the address bar to type {url}")
    try:
        # off the event loop: this blocks for seconds and would freeze the server
        result = await asyncio.to_thread(_drive, url, cfg, profile)
    except CursorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CursorError(f"Desktop control failed: {exc}") from exc

    activity.log(
        "cursor",
        f"Typed {url} into the address bar ({result['keystrokes']} keystrokes)",
        "ok",
    )
    return {"url": url, **result}


# ─── Linux / X11 ───────────────────────────────────────────────────────────
# pywinauto is Windows-only. On Linux (a desktop, or a container whose DISPLAY
# is a desktop's X server) the same public API is provided by cursor_x11, which
# drives Chrome with xdotool/xclip instead of UI Automation. Routes keep calling
# `cursor.<fn>`; only the implementation changes.
import sys as _sys  # noqa: E402

if not _sys.platform.startswith("win"):
    from backend.automation import cursor_x11 as _x11  # noqa: E402

    new_tab = _x11.new_tab
    click_element = _x11.click_element
    type_url = _x11.type_url
    focus_window = _x11.focus_window
