"""Persistent browser session with our own cookie persistence.

M0 findings baked in (see _research/RESEARCH.md):
- the site's auth session cookie is session-scoped by design; it dies with
  every browser close, so WE persist all context cookies to a jar and
  re-inject on every launch;
- when the jar goes stale, /.auth/login/aad?prompt=none does a silent SSO
  round-trip using the (also jar-persisted) Microsoft identity cookies;
  the final fallback is the account picker — one click, no password.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page, Playwright

from ..config import CHAT_URL, Config

log = logging.getLogger("mai2srt.browser")

AUTH_PROBE_JS = ("async () => { const r = await fetch('/.auth/me'); "
                 "return r.status; }")

#: playwright channel -> (exe names for PATH lookup, well-known install paths)
#: "chromium-system" is not a playwright channel: it is the distro's own
#: chromium package (Linux), used only as a last resort when playwright's
#: bundled chromium has not been downloaded.
if sys.platform == "win32":
    CHANNEL_HINTS = {
        "chrome": (["chrome.exe"], [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]),
        "msedge": (["msedge.exe"], [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]),
    }
elif sys.platform == "darwin":
    CHANNEL_HINTS = {
        "chrome": (["google-chrome"], [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]),
        "msedge": (["microsoft-edge"], [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]),
    }
else:
    CHANNEL_HINTS = {
        "chrome": (["google-chrome", "google-chrome-stable", "chrome"], [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome",
        ]),
        "msedge": (["microsoft-edge", "microsoft-edge-stable", "msedge"], [
            "/opt/microsoft/msedge/msedge",
            "/usr/bin/microsoft-edge",
        ]),
        # /snap/bin/chromium is deliberately absent: a snap-confined browser
        # cannot read the dot-directory profile under the home folder
        "chromium-system": (["chromium", "chromium-browser"], [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]),
    }

#: channels playwright itself understands via ``channel=``; everything else
#: found on disk is launched through ``executable_path``
_PLAYWRIGHT_CHANNELS = ("chrome", "msedge")


def find_channel_exe(channel: str) -> str | None:
    """Locate a real browser executable for a playwright channel."""
    hint = CHANNEL_HINTS.get(channel)
    if not hint:
        return None
    names, paths = hint
    for exe in names:
        hit = shutil.which(exe)
        if hit and "/snap/" not in hit:
            return hit
    for p in paths:
        if Path(p).exists():
            return p
    return None


def resolve_channel(cfg: Config) -> str | None:
    """Which browser to drive; None = playwright's bundled chromium.

    Preference from config.json ("auto" | "chrome" | "msedge" | "chromium").
    A specific preference still falls back gracefully: other real browsers
    first, bundled chromium last.
    """
    from ..config import browser_pref
    pref = browser_pref(cfg)
    if pref == "chromium":
        return None
    order = ["chrome", "msedge"] if pref == "auto" else [pref]
    order += [c for c in ("chrome", "msedge") if c not in order]
    for ch in order:
        if find_channel_exe(ch):
            return ch
    # Linux: no Chrome/Edge, and playwright's own chromium was never
    # downloaded -> the distro's chromium package beats failing at launch
    if ("chromium-system" in CHANNEL_HINTS and find_channel_exe("chromium-system")
            and not _bundled_chromium_present()):
        return "chromium-system"
    return None


def _bundled_chromium_present() -> bool:
    from ..preflight import bundled_chromium_present
    return bundled_chromium_present()


def display_available() -> bool:
    """Can a visible browser window open here? Always True off Linux; on
    Linux a window needs an X11 or Wayland display."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


NO_DISPLAY_MESSAGE = (
    "没有可用的图形显示（DISPLAY / WAYLAND_DISPLAY 均未设置），无法打开浏览器登录。"
    "请在有桌面的机器上执行 `mai2srt login`，然后用 `mai2srt session export <file>` "
    "导出登录态，在本机用 `mai2srt session import <file>` 导入 "
    "(no display: sign in on a desktop machine, then export/import the session)")


class NoDisplayError(RuntimeError):
    """A visible browser was requested on a Linux box with no display."""


async def launch(p: Playwright, cfg: Config, headless: bool = False) -> BrowserContext:
    """Persistent context (preferred real browser, else bundled) + jar restore.

    headless=False is for interactive sign-in (the user must see and click);
    headless=True is for transcription runs -- no window pops up. Auth then
    relies entirely on the cookie jar + silent SSO; if both fail the run
    aborts with a sign-in hint instead of waiting for invisible clicks.

    Two hard-won robustness rules:
    - profiles are PER-CHANNEL (<account-profile>/<channel>/): a directory
      written by one browser build (e.g. Chrome 141) can CHECK-crash another
      (e.g. bundled chromium 1194) on incompatible cache files -- observed
      live as exit 0x80000003 right after "Failed to open GraphiteDawnCache";
    - any failed launch QUARANTINES the suspect profile and retries once
      with a fresh one, so profile corruption is self-healing and never
      needs manual directory surgery. The cookie jar (browser-agnostic)
      still restores the session afterwards.
    """
    if not headless and not display_available():
        raise NoDisplayError(NO_DISPLAY_MESSAGE)
    channel = resolve_channel(cfg)
    base = cfg.profile_dir                      # per-account base dir
    candidates = ([channel] if channel else []) + [None]  # None = bundled
    tried: set[str] = set()
    last_error: Exception | None = None

    for cand in candidates:
        name = cand or "chromium"
        if name in tried:
            continue
        tried.add(name)
        profile = base / name
        kwargs = dict(
            user_data_dir=str(profile),
            headless=headless,
            viewport=None,
            args=["--disable-blink-features=AutomationControlled"]
            + ([] if headless else ["--start-maximized"]),
        )
        for attempt in (1, 2):
            try:
                if cand in _PLAYWRIGHT_CHANNELS and sys.platform == "win32":
                    ctx = await p.chromium.launch_persistent_context(
                        channel=cand, **kwargs)
                elif cand:
                    # Linux/macOS: the exact binary find_channel_exe located
                    # (playwright's channel lookup only knows the vendor's
                    # default install prefix)
                    ctx = await p.chromium.launch_persistent_context(
                        executable_path=find_channel_exe(cand), **kwargs)
                else:
                    ctx = await p.chromium.launch_persistent_context(**kwargs)
                log.info("browser channel: %s%s", name,
                         " (headless)" if headless else "")
                await _restore_jar(ctx, cfg)
                return ctx
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt == 1:
                    log.warning("%s launch failed (%s); retrying with a fresh "
                                "profile", name, str(e).splitlines()[0])
                    _quarantine_profile(profile)
                else:
                    log.error("%s launch failed even with a fresh profile: %s",
                              name, str(e).splitlines()[0])
    raise RuntimeError(
        f"no usable browser could be launched (last error: {last_error})")


def _quarantine_profile(profile: Path) -> None:
    """Move a suspect profile aside so the retry starts clean."""
    if not profile.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = profile.with_name(f"{profile.name}.broken-{stamp}")
    try:
        profile.rename(target)
        log.info("quarantined profile %s -> %s", profile.name, target.name)
    except OSError as e:
        log.warning("could not quarantine %s (%s); deleting instead", profile, e)
        shutil.rmtree(profile, ignore_errors=True)


async def _restore_jar(ctx: BrowserContext, cfg: Config) -> None:
    if cfg.cookie_jar.exists():
        try:
            jar = json.loads(cfg.cookie_jar.read_text(encoding="utf-8"))
            await ctx.add_cookies(jar)
            log.debug("cookie jar restored (%d cookies)", len(jar))
        except Exception as e:  # noqa: BLE001 — stale jar must not be fatal
            log.warning("cookie jar restore failed (ignored): %s", e)


async def save_cookie_jar(ctx: BrowserContext, cfg: Config) -> None:
    try:
        cookies = await ctx.cookies()
        cfg.cookie_jar.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
        log.debug("cookie jar saved (%d cookies)", len(cookies))
    except Exception as e:  # noqa: BLE001
        log.warning("cookie jar save failed (ignored): %s", e)


async def auth_status(page: Page) -> int | None:
    try:
        return await page.evaluate(AUTH_PROBE_JS)
    except Exception:  # noqa: BLE001 — page navigating
        return None


async def open_page(ctx: BrowserContext, url: str = CHAT_URL,
                    timeout_ms: int = 1_800_000) -> Page:
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(timeout_ms)
    await page.goto(url, wait_until="domcontentloaded")
    return page


def login_url(prompt: str) -> str:
    """Easy Auth entry point: prompt=none tries silent SSO, select_account
    lands the user DIRECTLY on the Microsoft account picker (no detour
    through the playground chat page first)."""
    from ..config import BASE_URL
    return (f"{BASE_URL}/.auth/login/aad?prompt={prompt}"
            f"&post_login_redirect_uri={quote('/chat')}")


async def _trigger_login(page: Page, prompt: str) -> bool:
    """Navigate the Easy Auth login redirect; True if signed in after."""
    try:
        await page.goto(login_url(prompt), wait_until="domcontentloaded",
                        timeout=120_000)
    except Exception as e:  # noqa: BLE001 — redirect chains can be flaky
        log.debug("login redirect hiccup (continuing): %s", e)
    for _ in range(15):
        if await auth_status(page) == 200:
            return True
        await asyncio.sleep(1)
    return False


async def wait_for_login(page: Page, cfg: Config, timeout_s: int = 600) -> int | None:
    """Interactive sign-in: the login page is already on screen (visible
    browser); poll until the user completes it, then persist the jar.

    Returns None when the user CLOSES the browser window first -- the close
    event is tracked explicitly; polling alone would confuse a dead context
    with a mid-redirect page (both probe as None) and burn the timeout."""
    ctx = page.context
    closed = asyncio.Event()
    ctx.on("close", lambda *_: closed.set())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if closed.is_set() or page.is_closed():
            log.info("browser window closed before sign-in completed")
            return None
        await asyncio.sleep(2)
        status = await auth_status(page)
        if status == 200:
            log.info("sign-in detected")
            await save_cookie_jar(page.context, cfg)
            return status
    log.error("still not signed in after %ss", timeout_s)
    return await auth_status(page)


async def ensure_auth(page: Page, cfg: Config, timeout_s: int = 600,
                      interactive: bool = True) -> int | None:
    """jar session -> silent SSO -> (interactive only) account picker.

    Non-interactive callers (headless transcription) stop after silent SSO:
    there is no window to click in, so a stale session must fail fast with
    a sign-in hint instead of burning the timeout.
    """
    status = await auth_status(page)
    if status == 200:
        await save_cookie_jar(page.context, cfg)
        return status

    log.info("not signed in; trying silent SSO (prompt=none)")
    if await _trigger_login(page, "none"):
        log.info("silent SSO ok")
        await save_cookie_jar(page.context, cfg)
        return 200

    if not interactive:
        log.error("session expired and silent SSO failed; "
                  "sign in again via the Settings page / CLI login")
        return await auth_status(page)

    log.info("silent SSO unavailable; account picker opened in the browser - "
             "click your account (password only if asked)")
    await _trigger_login(page, "select_account")
    return await wait_for_login(page, cfg, timeout_s)
