"""Playwright runner — owns one long-lived, real Chrome window.

Launched as a *persistent context*, not a throwaway automation session:

  * real Chrome (channel="chrome"), with a real on-disk profile, so logins
    survive across runs — log into Instagram once, stay logged in;
  * no "Chrome is being controlled by automated test software" infobar;
  * navigator.webdriver is undefined rather than true;
  * an optional residential/mobile proxy (config.PROXY_SERVER), set once at
    launch and held for the whole session — a datacenter IP is the biggest
    bot-detection tell, and changing IP mid-session reads as a suspicious login.

The context is kept in module state so it stays open across HTTP requests:
click "Open Chrome" once, then navigate/screenshot against the same window.
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from backend import activity, config

SHOTS_DIR = config.BACKEND_DIR / "data" / "screenshots"

# "scheme://…" — the trailing slashes matter: without them "localhost:4000" and
# "example.com:8080" parse as scheme "localhost"/"example.com" rather than host:port.
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# Schemes that legitimately carry no "//".
_BARE_SCHEMES = ("about:", "data:", "mailto:", "blob:", "javascript:", "chrome:", "view-source:")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class BadUrl(ValueError):
    """Raised for input that can't be turned into a navigable URL."""


def normalize_url(url: str | None) -> str:
    """Turn user input into something Chrome will actually navigate to.

    Chrome's DevTools protocol needs an absolute URL: bare "www.google.com" is
    read as a relative path and rejected with "Cannot navigate to invalid URL".
    """
    raw = (url or "").strip().strip('"').strip("'")
    if not raw:
        raise BadUrl("No URL given. Type an address such as https://www.google.com.")
    # A URL can't hold a raw space, so an inner space is always a typo:
    # "www. instagram.com" means "www.instagram.com". Text that isn't a URL at
    # all ("hello world" -> "helloworld") still has no dot and is rejected below.
    raw = re.sub(r"\s+", "", raw)
    if raw.startswith("//"):  # protocol-relative
        return f"https:{raw}"
    if _SCHEME.match(raw) or raw.lower().startswith(_BARE_SCHEMES):
        return raw

    host = re.split(r"[/?#]", raw, maxsplit=1)[0].split("@")[-1]
    hostname, _, port = host.partition(":")
    if port and not port.isdigit():  # "foo:bar" is neither host:port nor a scheme
        raise BadUrl(f"{raw!r} is not a URL. Try https://{raw}")
    looks_like_host = (
        "." in hostname and " " not in hostname and not hostname.endswith(".")
    ) or hostname in ("localhost",) or _IPV4.match(hostname)

    if not looks_like_host:
        raise BadUrl(
            f"{raw!r} is not a URL. Enter a full address such as "
            "https://www.google.com (or just www.google.com)."
        )
    return f"https://{raw}"

# Chrome flags that mark a session as automated. Removing --enable-automation
# drops the infobar; the blink feature drops navigator.webdriver.
_AUTOMATION_ARGS = ["--enable-automation"]
# Playwright adds --no-sandbox by default. On a desktop we drop it too (a normal
# Chrome runs sandboxed), but as root — e.g. inside a container — Chromium refuses
# to start WITH the sandbox ("Chromium sandboxing failed"), so keep the default there.
if not (os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0):
    _AUTOMATION_ARGS.append("--no-sandbox")
_IS_LINUX = sys.platform.startswith("linux")


def _check_display() -> None:
    """A headed Chrome on Linux needs a desktop session to draw on.

    Started from SSH, a systemd service or a container, DISPLAY is unset and
    Chrome dies with "Missing X server or $DISPLAY" — fail early with a hint."""
    if config.PLAYWRIGHT_HEADLESS or not _IS_LINUX:
        return
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return
    raise RuntimeError(
        "PLAYWRIGHT_HEADLESS=false but no display is available (DISPLAY / "
        "WAYLAND_DISPLAY unset). Start the server from a terminal INSIDE the "
        "Ubuntu desktop session (not over SSH, Docker or a system service), or "
        "export DISPLAY=:0 and XAUTHORITY=/run/user/<uid>/gdm/Xauthority for the "
        "logged-in desktop user. Set PLAYWRIGHT_HEADLESS=true to run hidden."
    )
_CLEAN_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
    "--start-maximized",
]

_lock = asyncio.Lock()
_playwright: Playwright | None = None       # one instance hosts every window

# One window per profile, all alive at once — several accounts can sit logged in
# side by side. Keyed by profile path; a profile is locked to one Chrome
# process, so one window per profile is also the hard limit.
_windows: dict[str, dict] = {}
_active: str | None = None                  # key of the window last used


def _key(profile: Path) -> str:
    return str(Path(profile).resolve()).lower()


# ── Session sync with MinIO ───────────────────────────────────────
# Account profiles live under data/profiles/ and their logged-in state is backed
# up to MinIO. Only those are synced — the default chrome-profile isn't an
# account and has no backup.
_PROFILES_DIR = config.BACKEND_DIR / "data" / "profiles"


def _is_account_profile(path: Path) -> bool:
    try:
        return Path(path).resolve().parent == _PROFILES_DIR.resolve()
    except Exception:  # noqa: BLE001
        return False


def _has_session(path: Path) -> bool:
    """True if the profile already holds a login (its cookie DB exists)."""
    return (Path(path) / "Default" / "Network" / "Cookies").is_file()


def _maybe_restore_session(wanted: Path) -> None:
    """Before launch: if an account profile has no local login but MinIO holds a
    backup, restore it so the window opens already logged in. Best-effort — any
    failure (MinIO down, no backup) just leaves a fresh profile, exactly as
    before this existed."""
    if not _is_account_profile(wanted) or _has_session(wanted):
        return
    try:
        from services.storage import session_storage
        name = Path(wanted).name
        if any(it["name"] == f"session-{name}.zip" for it in session_storage.list_sessions()):
            session_storage.restore(name, overwrite=True)  # profile is empty
            activity.log("browser", f"Restored {name}'s session from MinIO", "ok")
    except Exception as exc:  # noqa: BLE001 — never block a launch on the sync
        activity.log("browser", f"Session restore skipped for {Path(wanted).name}: {exc}", "warn")


def _backup_session(profile: Path) -> None:
    """After a window closes (files unlocked): back the profile up to MinIO.
    Best-effort and off the critical path — a failed backup logs and moves on."""
    if not _is_account_profile(profile):
        return
    try:
        from services.storage import session_storage
        session_storage.backup(Path(profile).name)
    except Exception as exc:  # noqa: BLE001
        activity.log("browser", f"Session backup skipped for {Path(profile).name}: {exc}", "warn")


def _win(profile: Path | None = None) -> dict | None:
    """The window for a profile, or the active one."""
    key = _key(profile) if profile else _active
    win = _windows.get(key) if key else None
    if win and (win["page"].is_closed() or not win["context"].pages):
        _windows.pop(key, None)
        return None
    return win


def is_open(profile: Path | None = None) -> bool:
    return _win(profile) is not None


def open_windows() -> list[dict]:
    """Every live window — what the UI shows when several accounts are up."""
    out = []
    for key in list(_windows):
        win = _win(Path(key))
        if win:
            out.append({"profile": _rel(win["profile"]), "url": win["page"].url,
                        "active": key == _active})
    return out


def _forget(key: str) -> None:
    """Drop a window the user closed by hand."""
    global _active
    _windows.pop(key, None)
    if _active == key:
        _active = next(iter(_windows), None)
    activity.log("browser", f"Chrome window closed ({len(_windows)} left)", "info")


async def open_browser(url: str | None = None, profile: Path | None = None) -> dict:
    """Launch Chrome if it isn't up, then navigate. Idempotent.

    With no url (and no PLAYWRIGHT_START_URL) the window opens blank and stays
    blank — we never navigate. The URL box is what sends it somewhere.

    `profile` selects which on-disk Chrome profile to use — one per Instagram
    account, so their sessions don't overwrite each other. Each profile gets its
    OWN window and they all stay up together: opening account 2 no longer closes
    account 1. (One window per profile is the ceiling — Chrome locks a profile
    to a single process.)
    """
    global _playwright, _active
    requested = (url or "").strip() or config.PLAYWRIGHT_START_URL
    # raises BadUrl before we bother launching
    target = normalize_url(requested) if requested else None
    wanted = Path(profile) if profile else config.PLAYWRIGHT_USER_DATA_DIR
    key = _key(wanted)

    async with _lock:
        existing = _win(wanted)
        if existing:                       # this account's window is already up
            _active = key
            if target:
                await existing["page"].goto(target, wait_until="domcontentloaded")
            return {
                "launched": False,
                "reused": True,
                "profile": _rel(wanted),
                "windows": len(_windows),
                "url": existing["page"].url,
                "title": await existing["page"].title(),
            }

        if _playwright is None:
            _playwright = await async_playwright().start()

        # Pull this account's logged-in session down from MinIO if it isn't here
        # locally yet, so a wiped/cleared profile comes back logged in.
        _maybe_restore_session(wanted)

        wanted.mkdir(parents=True, exist_ok=True)
        clean = config.PLAYWRIGHT_CLEAN_LAUNCH
        # Optional residential/mobile proxy so we're not on a flagged datacenter IP.
        # One proxy for the whole session (no mid-session rotation — that reads as a
        # suspicious login on Instagram). None when unset, which Playwright ignores.
        proxy = config.playwright_proxy()
        _check_display()
        try:
            context = await _playwright.chromium.launch_persistent_context(
                user_data_dir=str(wanted),
                headless=config.PLAYWRIGHT_HEADLESS,
                channel=config.PLAYWRIGHT_CHANNEL or None,
                slow_mo=config.PLAYWRIGHT_SLOWMO,
                no_viewport=True,
                proxy=proxy,
                args=_CLEAN_ARGS if clean else ["--start-maximized"],
                ignore_default_args=_AUTOMATION_ARGS if clean else [],
            )
        except Exception as exc:
            raise RuntimeError(_launch_hint(exc)) from exc

        # A persistent context opens with one blank tab already — reuse it so we
        # don't leave an orphan about:blank window next to the real one.
        page = context.pages[0] if context.pages else await context.new_page()
        _windows[key] = {"profile": wanted, "context": context, "page": page}
        _active = key
        context.on("close", lambda *_: _forget(key))

        activity.log(
            "browser",
            f"Chrome launched ({config.PLAYWRIGHT_CHANNEL}, profile {wanted.name}"
            + (f", proxy {_proxy_label(proxy)}" if proxy else "")
            + f") — {len(_windows)} window(s) open",
            "ok",
        )
        if target:
            await page.goto(target, wait_until="domcontentloaded")

        return {
            "launched": True,
            "reused": False,
            "blank": target is None,
            "headless": config.PLAYWRIGHT_HEADLESS,
            "channel": config.PLAYWRIGHT_CHANNEL,
            "clean_launch": clean,
            "proxy": _proxy_label(proxy),
            "profile": _rel(wanted),
            "windows": len(_windows),
            "url": page.url,
            "title": await page.title(),
        }


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(config.ROOT_DIR))
    except ValueError:
        return str(path)


def _proxy_label(proxy: dict | None) -> str:
    """The proxy's server (host:port) for logs/status — NEVER the credentials.
    Username/password live in their own keys, so returning `server` can't leak
    them. Empty string when no proxy is configured."""
    return (proxy or {}).get("server", "") if proxy else ""


def _launch_hint(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    channel = config.PLAYWRIGHT_CHANNEL
    hint = f"Could not launch Chrome (channel={channel!r}): {text}\n"
    if "missing x server" in low or "$display" in low or "cannot open display" in low:
        hint += (
            "No display: a headed Chrome needs the desktop session. Start the server "
            "from a terminal inside the Ubuntu desktop (not SSH/Docker/systemd), or "
            "export DISPLAY=:0 for the logged-in user. Or set PLAYWRIGHT_HEADLESS=true."
        )
    elif "executable doesn't exist" in low or "please run the following command" in low:
        if channel == "chromium" or not channel:
            hint += (
                "Playwright's bundled Chromium is not installed for this Python. Run:\n"
                "  python -m playwright install chromium\n"
                "  sudo python -m playwright install-deps chromium   (Linux system libs)"
            )
        else:
            hint += (
                f'Google Chrome ({channel}) is not installed. Run "python -m playwright '
                f'install {channel}", install Chrome, or set PLAYWRIGHT_CHANNEL=chromium '
                "in .env and run \"python -m playwright install chromium\"."
            )
    elif "without --no-sandbox" in low or "running as root" in low:
        hint += (
            "Chrome refuses to run as root without a sandbox. Run the server as a "
            "normal desktop user instead of root/sudo."
        )
    elif "shared libraries" in low or "cannot open shared object" in low:
        hint += (
            "Missing Linux system libraries. Run: sudo python -m playwright install-deps chromium"
        )
    elif "channel" in low:
        hint += (
            f'Unknown channel {channel!r}. Use PLAYWRIGHT_CHANNEL=chrome (installed Google '
            "Chrome) or PLAYWRIGHT_CHANNEL=chromium (Playwright's bundled build)."
        )
    elif "profile" in low or "lock" in low or "singletonlock" in low:
        hint += (
            "That profile looks locked — another Chrome is already using it. Close it, "
            "or point PLAYWRIGHT_USER_DATA_DIR at a different folder in .env."
        )
    else:
        hint += "Check that Chrome is installed and no other process holds the profile."
    return hint


def current_profile() -> Path:
    """The profile of the window last used (or the default)."""
    win = _win()
    return win["profile"] if win else config.PLAYWRIGHT_USER_DATA_DIR


def page_count(profile: Path | None = None) -> int:
    win = _win(profile)
    return len(win["context"].pages) if win else 0


def current_page() -> Page:
    win = _win()
    if not win:
        raise RuntimeError("No Chrome window is open.")
    return win["page"]


def page_for(profile: Path) -> Page:
    """That profile's Page, by profile — never the 'active' one.

    This is what makes two accounts safe to drive at once. current_page() hands
    back whichever window was touched last, so with two jobs running the second
    one to start would steal the first one's page mid-run and type into the
    wrong account's window. Pass a profile and you always get that profile.
    """
    win = _win(Path(profile))
    if not win:
        raise RuntimeError("No Chrome window is open for this profile.")
    return win["page"]


def sync_active_page(profile: Path | None = None) -> dict:
    """Re-point a window at its newest tab, no waiting. Defaults to the active
    one; pass a profile to be explicit when several accounts are running.

    Chrome's new-tab page swaps renderers when it navigates, which can leave
    our handle on the old chrome://new-tab-page object — so status and
    screenshots would describe a tab that is no longer on screen.
    """
    win = _win(profile)
    if not win:
        return {"tabs": 0, "url": None}
    if win["context"].pages:
        win["page"] = win["context"].pages[-1]
    return {"tabs": len(win["context"].pages), "url": win["page"].url}


async def adopt_newest_page(timeout: float = 5.0, profile: Path | None = None) -> dict:
    """Point a window at its newest tab. Defaults to the active one.

    A tab opened by clicking Chrome's + is invisible to us otherwise: the
    context gains a page but our handle still holds the old one, so a
    screenshot or a status read would describe the wrong tab.
    """
    win = _win(profile)
    if not win:
        raise RuntimeError("No browser window is open.")
    context = win["context"]

    deadline = asyncio.get_event_loop().time() + timeout
    known = len(context.pages)
    while asyncio.get_event_loop().time() < deadline:
        if context.pages and context.pages[-1] is not win["page"]:
            break
        await asyncio.sleep(0.15)

    if context.pages:
        win["page"] = context.pages[-1]
    return {"tabs": len(context.pages), "was": known, "url": win["page"].url}


async def require_page(profile: Path | None = None) -> Page:
    """That profile's Page, launching its window if it isn't up. Other accounts'
    windows are left alone."""
    global _active
    wanted = Path(profile) if profile else config.PLAYWRIGHT_USER_DATA_DIR
    win = _win(wanted)
    if not win:
        await open_browser(None, profile=wanted)
        win = _win(wanted)
    _active = _key(wanted)
    return win["page"]


async def goto(url: str) -> dict:
    target = normalize_url(url)
    win = _win()
    if not win:
        return await open_browser(target)
    async with _lock:
        await win["page"].goto(target, wait_until="domcontentloaded")
        return {"url": win["page"].url, "title": await win["page"].title()}


async def screenshot() -> dict:
    win = _win()
    if not win:
        raise RuntimeError("No browser window is open. Click 'Open Chrome' first.")
    async with _lock:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = SHOTS_DIR / f"shot-{stamp}.png"
        await win["page"].screenshot(path=str(path), full_page=False)
        return {
            "file": str(path.relative_to(config.ROOT_DIR)),
            "served_at": f"/shots/{path.name}",
            "url": win["page"].url,
        }


async def status() -> dict:
    win = _win()
    base = {
        "open": False,
        "headless": config.PLAYWRIGHT_HEADLESS,
        "channel": config.PLAYWRIGHT_CHANNEL,
        "clean_launch": config.PLAYWRIGHT_CLEAN_LAUNCH,
        "proxy": _proxy_label(config.playwright_proxy()),
        "profile": _rel(current_profile()),
        "windows": len(_windows),
        "all_windows": open_windows(),
    }
    if not win:
        return base
    return {**base, "open": True, "url": win["page"].url, "title": await win["page"].title()}


async def reload_windows() -> dict:
    """Reload every open window, leaving them open and logged in.

    What Stop uses instead of closing: a stopped job may have left a half-typed
    message or a staged image in a composer, and a reload clears that back to a
    clean conversation without costing the session — which is the expensive
    thing to rebuild.
    """
    reloaded, failed = [], []
    for key in list(_windows):
        win = _windows.get(key)
        if not win:
            continue
        try:
            await win["page"].reload(wait_until="domcontentloaded", timeout=15000)
            reloaded.append(_rel(win["profile"]))
        except Exception as exc:  # noqa: BLE001 — one bad window mustn't stop the rest
            failed.append(f"{_rel(win['profile'])}: {exc}")
    return {"reloaded": reloaded, "failed": failed, "windows_open": len(_windows)}


async def close_browser(profile: Path | None = None) -> dict:
    """Close one account's window, or every window when given nothing.

    Windows are never closed on failure — a login that went wrong is exactly the
    one you want left on screen to look at.
    """
    global _active, _playwright
    async with _lock:
        keys = [_key(profile)] if profile else list(_windows)
        closed = []
        for key in keys:
            win = _windows.pop(key, None)
            if not win:
                continue
            closed.append(_rel(win["profile"]))
            try:
                await win["context"].close()
            except Exception:  # noqa: BLE001 — teardown must not mask the real error
                pass
            # Chrome has released the profile's files now, so back its login up
            # to MinIO — this captures whatever the session did before closing.
            _backup_session(win["profile"])
            if _active == key:
                _active = None

        _active = _active or next(iter(_windows), None)
        if not _windows and _playwright:      # last one out turns off the lights
            try:
                await _playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            _playwright = None
        return {"closed": bool(closed), "windows_closed": closed, "windows_left": len(_windows)}
