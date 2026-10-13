"""Real cursor + keyboard control of Chrome's address bar — Linux/X11 backend.

`cursor.py` drives Chrome through Windows UI Automation (pywinauto). On a Linux
desktop (or a container whose DISPLAY points at a desktop, e.g. the webtop
display shared over /tmp/.X11-unix) there is no UIA tree, so this module does
the same job with X11 tooling:

  * our Chrome window is found by WM_CLASS — Chromium puts the --user-data-dir
    path in the window's class instance, so each account's window is unambiguous;
  * the pointer and keys are driven with `xdotool` (XTEST), i.e. real OS input;
  * the address bar is focused with Ctrl+L and read back through the clipboard
    (Ctrl+A, Ctrl+C, `xclip`), since X11 has no accessible "value" to query;
  * page elements are clicked at their true screen position: the viewport's
    screen origin is measured by moving the pointer and reading the mousemove
    the page receives, so no guessing about toolbar heights or window frames.

Requires: xdotool, xclip, x11-utils (xprop) and a DISPLAY. `cursor.py` swaps
its public functions for these on non-Windows platforms.
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from backend import activity, settings
from backend.automation import typing
from backend.automation.cursor import (
    CursorError,
    _prefix_ok,
    _progress,
    _same_url,
    profile_for,
)

NEW_TAB_ATTEMPTS = 3


# ─── xdotool / X11 plumbing ────────────────────────────────────────────────

def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":1")
    return env


def _deps() -> None:
    missing = [tool for tool in ("xdotool", "xclip", "xprop") if shutil.which(tool) is None]
    if missing:
        raise CursorError(
            "Desktop control on Linux needs " + ", ".join(missing)
            + " in the container (apt-get install xdotool xclip x11-utils)."
        )
    if not _env().get("DISPLAY"):
        raise CursorError("Desktop control needs DISPLAY to point at the desktop's X server.")


def _run(*args: str, timeout: float = 10.0) -> str:
    try:
        out = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, env=_env(), check=False
        )
    except FileNotFoundError as exc:
        raise CursorError(f"{args[0]} is not installed in this environment.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CursorError(f"{' '.join(args[:2])} timed out — is the X display reachable?") from exc
    return out.stdout


def _xdo(*args: str, timeout: float = 10.0) -> str:
    return _run("xdotool", *args, timeout=timeout)


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


# ─── our Chrome window ────────────────────────────────────────────────────

class _Win:
    """A Chromium toplevel window of ours, by X window id."""

    def __init__(self, wid: str, profile: Path):
        self.wid = wid
        self.profile = profile

    def geometry(self) -> dict:
        shell = _xdo("getwindowgeometry", "--shell", self.wid)
        geo = {}
        for line in shell.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                try:
                    geo[k.strip().lower()] = int(v.strip())
                except ValueError:
                    pass
        if not {"x", "y", "width", "height"} <= geo.keys():
            raise CursorError("Could not read the Chrome window geometry from X.")
        return geo

    def focus(self) -> None:
        _xdo("windowactivate", "--sync", self.wid, timeout=6.0)
        _xdo("windowraise", self.wid)
        _sleep(0.15)

    def is_minimized(self) -> bool:
        state = _run("xprop", "-id", self.wid, "_NET_WM_STATE")
        return "_NET_WM_STATE_HIDDEN" in state

    def restore(self) -> None:
        _xdo("windowmap", self.wid)
        self.focus()


def _wm_class(wid: str) -> str:
    return _run("xprop", "-id", wid, "WM_CLASS")


def _find_window(profile: Path) -> _Win:
    """Our Chrome window for this profile: the visible Chromium toplevel whose
    WM_CLASS carries this profile's path. Prefers the largest one (the browser
    frame, not a popup)."""
    _deps()
    wanted = str(Path(profile).resolve())
    wids = _xdo("search", "--onlyvisible", "--class", "[Cc]hrom").split()
    matches: list[tuple[int, str]] = []
    for wid in wids:
        try:
            klass = _wm_class(wid)
        except CursorError:
            continue
        if wanted in klass:
            try:
                geo = _Win(wid, profile).geometry()
                matches.append((geo["width"] * geo["height"], wid))
            except CursorError:
                continue
    if matches:
        matches.sort(reverse=True)
        return _Win(matches[0][1], profile)
    if wids:
        raise CursorError(
            "A Chrome window is on the desktop but none belongs to this account's "
            f"profile ({wanted}). Open that account's window first."
        )
    raise CursorError(
        "No Chrome window on the desktop. Click 'Open Chrome window' first "
        "(and PLAYWRIGHT_HEADLESS must be false — a hidden window has no address bar)."
    )


# ─── pointer ──────────────────────────────────────────────────────────────

def _pointer() -> tuple[int, int]:
    out = _xdo("getmouselocation", "--shell")
    pos = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            pos[k.strip()] = v.strip()
    try:
        return int(pos["X"]), int(pos["Y"])
    except (KeyError, ValueError):
        return 0, 0


def _glide(x: int, y: int, duration: float) -> None:
    """Move the real pointer to (x, y) along a slightly curved, eased path so
    the motion resembles a hand on a mouse instead of a teleport."""
    x0, y0 = _pointer()
    dist = math.hypot(x - x0, y - y0)
    steps = max(12, min(48, int(dist / 12)))
    # one random control point gives a gentle arc
    cx = (x0 + x) / 2 + random.uniform(-0.25, 0.25) * (y - y0)
    cy = (y0 + y) / 2 + random.uniform(-0.25, 0.25) * (x0 - x)
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 3                       # ease-out cubic
        px = (1 - e) ** 2 * x0 + 2 * (1 - e) * e * cx + e ** 2 * x
        py = (1 - e) ** 2 * y0 + 2 * (1 - e) * e * cy + e ** 2 * y
        # no --sync: under Xvfb it waits for a motion event that may never come
        _xdo("mousemove", str(int(round(px))), str(int(round(py))))
        _sleep(duration / steps)
    _xdo("mousemove", str(x), str(y))
    _sleep(0.03)


def _click() -> None:
    _xdo("click", "1")


def _key(*keys: str) -> None:
    _xdo("key", "--clearmodifiers", *keys)


def _type_char(ch: str) -> None:
    # `type` handles unicode and shifted characters for us
    _xdo("type", "--delay", "0", "--", ch)


def _clipboard() -> str:
    try:
        return _run("xclip", "-o", "-selection", "clipboard", timeout=3.0).strip()
    except CursorError:
        return ""


def _jitter_inside(left: int, top: int, right: int, bottom: int) -> tuple[int, int]:
    cx, cy = (left + right) // 2, (top + bottom) // 2
    jx, jy = max(2, (right - left) // 6), max(2, (bottom - top) // 6)
    x = min(max(cx + random.randint(-jx, jx), left + 3), right - 3)
    y = min(max(cy + random.randint(-jy, jy), top + 3), bottom - 3)
    return x, y


# ─── viewport ↔ screen mapping ────────────────────────────────────────────

_PROBE_INSTALL = """
() => {
  if (!window.__cursorProbe) {
    window.__cursorProbe = {x: null, y: null, n: 0};
    document.addEventListener('mousemove', (e) => {
      window.__cursorProbe = {x: e.clientX, y: e.clientY, n: (window.__cursorProbe.n || 0) + 1};
    }, true);
  }
  return {iw: window.innerWidth, ih: window.innerHeight};
}
"""


async def _viewport_origin(page, win: _Win) -> dict:
    """Where the page's viewport sits on the X screen.

    Estimate from the window geometry (viewport is the bottom part of the
    client window, below tabs + toolbar), then MEASURE: move the pointer to the
    estimated viewport centre and read the clientX/Y the page saw. The
    difference corrects for window frames, toolbar height and DPI in one go.
    """
    size = await page.evaluate(_PROBE_INSTALL)
    geo = await asyncio.to_thread(win.geometry)
    iw, ih = int(size["iw"]), int(size["ih"])
    est_left = geo["x"] + max(0, (geo["width"] - iw) // 2)
    est_top = geo["y"] + max(0, geo["height"] - ih)

    # two probe moves, slightly apart, so we read a fresh event
    tx, ty = est_left + iw // 2, est_top + ih // 2
    await asyncio.to_thread(_xdo, "mousemove", str(tx + 7), str(ty + 5))
    await asyncio.sleep(0.12)
    await asyncio.to_thread(_xdo, "mousemove", str(tx), str(ty))
    await asyncio.sleep(0.25)
    probe = await page.evaluate("() => window.__cursorProbe")
    left, top, measured = est_left, est_top, False
    if probe and probe.get("x") is not None:
        left = tx - int(probe["x"])
        top = ty - int(probe["y"])
        measured = True
    return {"left": left, "top": top, "iw": iw, "ih": ih, "measured": measured, "window": geo}


# ─── public API (same shape as cursor.py) ─────────────────────────────────

def focus_window(profile) -> dict:
    """Bring our Chrome window to the front, restoring it if minimised."""
    win = _find_window(Path(profile))
    try:
        if win.is_minimized():
            win.restore()
    except CursorError:
        pass
    try:
        win.focus()
    except CursorError as exc:
        raise CursorError(f"Couldn't bring the Chrome window to the front ({exc}).") from exc
    return {"focused": True, "window": win.wid}


def _new_tab_blocking(profile: Path) -> dict:
    win = _find_window(profile)
    win.focus()
    _sleep(random.uniform(0.2, 0.4))
    _key("ctrl+t")
    _sleep(random.uniform(0.4, 0.8))
    return {"method": "ctrl+t", "window": win.wid}


async def new_tab(account_id: int | None = None) -> dict:
    """Open a tab with a real Ctrl+T on our window, then hand the new tab to
    Playwright so everything downstream targets it."""
    from backend.automation import playwright_runner as browser

    profile = profile_for(account_id)
    if not browser.is_open(profile):
        raise CursorError("No Chrome window is open for that account. Open it first.")

    before = browser.page_count(profile)
    activity.log("cursor", "Opening a new tab with Ctrl+T on the real keyboard")
    last = None
    for attempt in range(1, NEW_TAB_ATTEMPTS + 1):
        try:
            last = await asyncio.to_thread(_new_tab_blocking, profile)
        except CursorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CursorError(f"Could not open a new tab: {exc}") from exc
        adopted = await browser.adopt_newest_page(timeout=3.0, profile=profile)
        if adopted["tabs"] > before:
            activity.log("cursor", f"New tab open ({adopted['tabs']} tabs)", "ok")
            return {**last, **adopted, "attempts": attempt}
        await asyncio.sleep(0.5 + 0.25 * attempt)
    raise CursorError(
        f"Pressed Ctrl+T {NEW_TAB_ATTEMPTS} times but no tab opened. Is the Chrome "
        "window visible and focused on the desktop?"
    )


def _click_screen_point(x: int, y: int, duration: float) -> dict:
    _glide(x, y, duration)
    _sleep(random.uniform(0.05, 0.15))
    _click()
    _sleep(random.uniform(0.3, 0.6))
    return {"clicked_at": {"x": x, "y": y}}


async def click_element(page, locator) -> dict:
    """Click a page element (the Log in button) with the REAL pointer: scroll it
    into view, find its viewport box, map that to the screen with a measured
    viewport origin, glide the pointer there and click."""
    from backend.automation import playwright_runner as browser

    if not browser.is_open():
        raise CursorError("No Chrome window is open. Open Chrome first.")
    profile = browser.current_profile()

    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    box = await locator.bounding_box()
    if not box:
        raise CursorError("The element has no on-screen box (hidden or detached). Is the login form visible?")

    win = await asyncio.to_thread(_find_window, Path(profile))
    await asyncio.to_thread(win.focus)
    origin = await _viewport_origin(page, win)
    left = origin["left"] + int(box["x"])
    top = origin["top"] + int(box["y"])
    right = left + int(box["width"])
    bottom = top + int(box["height"])
    x, y = _jitter_inside(left, top, right, bottom)

    activity.log("cursor", "Moving pointer to the Log in button")
    try:
        result = await asyncio.to_thread(_click_screen_point, x, y, random.uniform(0.3, 0.7))
    except CursorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CursorError(f"Desktop control failed: {exc}") from exc
    activity.log("cursor", f"Clicked the Log in button at {result['clicked_at']}", "ok")
    return {
        "method": "cursor",
        **result,
        "button": {"left": left, "top": top, "right": right, "bottom": bottom},
        "viewport_measured": origin["measured"],
    }


def _omnibox_text() -> str:
    """What the address bar reads right now: select all, copy, read clipboard.
    Leaves the text selected, which is fine — Enter navigates to it and typing
    replaces it."""
    _key("ctrl+a")
    _sleep(0.08)
    _key("ctrl+c")
    _sleep(0.12)
    return _clipboard()


def _type_events(url: str, events: list[dict]) -> str:
    intended = ""
    idx = 0
    for event in events:
        if event["key"] == typing.BACKSPACE:
            _key("BackSpace")
            intended = intended[:-1]
        else:
            _type_char(event["key"])
            intended += event["key"]
            idx += 1
        _sleep(event["delay_ms"] / 1000)
        # No per-key read-back on X11 (it would disturb the selection); mirror
        # what we typed so the UI still animates.
        _progress(chrome=intended, done=idx, matched=_same_url(intended, url))
    return intended


def _drive(url: str, cfg: dict, profile: Path, origin: dict | None) -> dict:
    """Blocking: focus our window, walk the pointer to the address bar, focus it
    with Ctrl+L (an ordinary keyboard shortcut), type the URL, verify, Enter."""
    win = _find_window(profile)
    win.focus()
    _sleep(random.uniform(0.2, 0.4))

    geo = origin["window"] if origin else win.geometry()
    # The omnibox is the toolbar row just above the viewport; with a measured
    # viewport origin we aim ~26px above it, else estimate from the geometry.
    if origin and origin.get("measured"):
        bar_y = origin["top"] - 26
        bar_left = origin["left"] + 120
        bar_right = origin["left"] + max(300, origin["iw"] - 140)
    else:
        bar_y = geo["y"] + 86
        bar_left = geo["x"] + 120
        bar_right = geo["x"] + max(300, geo["width"] - 140)
    x = random.randint(bar_left, bar_left + max(40, (bar_right - bar_left) // 3))
    y = bar_y + random.randint(-6, 6)
    _glide(x, y, random.uniform(0.35, 0.75))
    _click()
    clicked_at = {"x": x, "y": y}
    _sleep(0.2)
    _key("ctrl+l")          # guarantees the caret is in the address bar
    _sleep(0.15)
    _key("ctrl+a")

    plan_cfg = {**cfg, "remove_enabled": False}
    events = typing.plan_keystrokes(url, plan_cfg)
    _progress(active=True, target=url, chrome="", done=0,
              total=sum(1 for e in events if e["key"] != typing.BACKSPACE),
              matched=False, diverged_at=None)
    try:
        _type_events(url, events)
        actual = _omnibox_text()
        if not _same_url(actual, url):
            if _prefix_ok(actual, url) and actual.lower().startswith(url.lower().split("://")[-1][:8]):
                pass  # autocomplete added a suffix to a correct prefix: still our URL
            else:
                _key("ctrl+a")
                _sleep(0.2)
                _type_events(url, events)
                actual = _omnibox_text()
                if not _same_url(actual, url) and not _prefix_ok(actual, url):
                    raise CursorError(
                        f"Address bar reads {actual!r} but we meant {url!r}. Not pressing "
                        "Enter — Chrome's autocomplete probably interfered."
                    )
        _progress(chrome=actual, matched=True)
    finally:
        _progress(active=False)

    _sleep(random.uniform(0.2, 0.5))
    _key("Return")
    return {
        "clicked_at": clicked_at,
        "clicked": True,
        "already_focused": False,
        "omnibox": {"left": bar_left, "top": bar_y - 12, "right": bar_right, "bottom": bar_y + 12},
        "omnibox_read_back": actual,
        "verified": True,
        **typing.summarize(url, events),
    }


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

    origin = None
    try:
        page = browser.page_for(Path(profile))
        win = await asyncio.to_thread(_find_window, Path(profile))
        origin = await _viewport_origin(page, win)
    except Exception:  # noqa: BLE001 — fall back to the geometry estimate
        origin = None

    try:
        result = await asyncio.to_thread(_drive, url, cfg, Path(profile), origin)
    except CursorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CursorError(f"Desktop control failed: {exc}") from exc

    activity.log(
        "cursor",
        f"Typed {url} into the address bar ({result['keystrokes']} keystrokes)",
        "ok",
    )
    return {"url": url, "viewport_measured": bool(origin and origin.get("measured")), **result}


def type_progress() -> dict:  # pragma: no cover - identical to cursor.type_progress
    from backend.automation import cursor

    return cursor.type_progress()
