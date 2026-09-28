"""Find a browser to drive.

Pure and Playwright-free -- given a filesystem, it answers "which binary, or
which Playwright channel". That makes the whole resolution order unit-testable
without installing a browser, which matters because this is the code path a new
user hits first and the one most likely to fail on a machine nobody tested on.

Until now there was no resolution order at all: `channel="chrome"` was hardcoded
at the single `launch_persistent_context` call, so crawlpilot could drive exactly
one browser -- real Google Chrome, installed where Playwright's registry expects
it. Two consequences, both bad for a library someone installs on their laptop:
a user with Chromium, Brave or Edge, or a Chrome in an unusual place, had no way
to say so; and arm64 had no path at all, because `patchright install chrome`
publishes no arm64 build while the bundled Chromium runs there fine.

The order is browser-use's (`browser/chrome.py:37-68`,
`watchdogs/local_browser_watchdog.py:220-358`), which is itself the order users
expect: what you asked for, then what you have, then what we can fall back on.

**Why Chrome stays the default over bundled Chromium.** Chromium is a bot tell in
its own right -- the UA, the missing Widevine/codecs, `navigator.plugins` -- and
the pinned fingerprint (`identity/fingerprint.py`) pins a *Chrome* version. So
real Chrome is still tier 3 and Chromium tier 4: preferred when present,
available when not, rather than the only option or no option.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Playwright channels we pass straight through rather than resolving ourselves.
# Playwright owns the registry that maps these to binaries, and it knows about
# installs we would have to guess at.
KNOWN_CHANNELS = frozenset(
    {
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "chromium",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
    }
)

_MACOS_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)

_LINUX_CHROME_COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)

_WINDOWS_CHROME_PATHS = (
    r"{ProgramFiles}\Google\Chrome\Application\chrome.exe",
    r"{ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    r"{LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
    r"{ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    r"{ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
)

# Where Playwright/Patchright unpack what `install` downloads. Honouring
# PLAYWRIGHT_BROWSERS_PATH matters in containers, which routinely relocate it to
# keep the browser in a cached image layer.
_BROWSER_CACHE_ENV = "PLAYWRIGHT_BROWSERS_PATH"

_CACHE_ROOTS = {
    "Darwin": "~/Library/Caches/ms-playwright",
    "Linux": "~/.cache/ms-playwright",
}

# Globs relative to a `chromium-*` / `chrome-*` build directory in the cache.
#
# Globs rather than literal paths because the layout carries the architecture and
# the product name: `chrome-mac-arm64/Google Chrome for Testing.app/...` on an
# Apple Silicon machine, `chrome-mac/Chromium.app/...` on an older build,
# `chrome-linux64/` on some Linux builds. Enumerating those combinations is how
# this quietly stops finding a browser after an upstream rename.
_CACHE_GLOBS = {
    "Darwin": ("chrome-*/*.app/Contents/MacOS/*",),
    "Linux": ("chrome-*/chrome", "chrome-*/headless_shell"),
    "Windows": ("chrome-*/chrome.exe",),
}

INSTALL_HINT = (
    "No browser found. Install one with `patchright install chrome` (real Google "
    "Chrome, x86_64 only) or `patchright install chromium` (bundled Chromium, "
    "also works on arm64), or point crawlpilot at a binary you already have with "
    "Browser(executable_path=...). To drive a browser running elsewhere instead, "
    "pass Browser(cdp_url=...)."
)


class BrowserNotFound(RuntimeError):
    """No browser could be located, and none was named."""


@dataclass(frozen=True)
class Launch:
    """How to start a browser: exactly one of these is set.

    `channel` hands the decision to Playwright's own registry; `executable_path`
    names a binary directly. Keeping them distinct rather than collapsing to a
    path preserves the channel machinery Playwright uses for real Chrome
    installs, which is more reliable than us guessing at paths.
    """

    channel: str | None = None
    executable_path: str | None = None
    source: str = "unknown"
    """Which tier answered, for the log line. A user debugging "why is it using
    *that* browser" needs to see which rung matched."""

    def __post_init__(self) -> None:
        if bool(self.channel) == bool(self.executable_path):
            raise ValueError("a Launch names exactly one of channel / executable_path")


_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")


@lru_cache(maxsize=8)
def browser_version(executable_path: str | None = None) -> str | None:
    """The full version of the browser that will actually launch, or None.

    Asked of the binary itself (`--version`) rather than of a pinned constant,
    because the pinned constant is what goes stale. `config.DEFAULT_CHROME_VERSION`
    says it "must track the Chrome actually deployed", and on this machine it
    read `131.0.6778.86` while the browser launching was `151.0.7922.137` --
    twenty majors apart, on every request, in the UA and Sec-CH-UA of every
    identity. That is precisely the cross-check the constant's own docstring
    warns about, and it drifts again on the next Chrome release because keeping
    it accurate is a human's job.

    Cached: this shells out, and the answer cannot change while the process
    lives. `None` when the binary cannot be asked -- a channel launch names a
    registry entry rather than a path, a container may not permit exec -- and
    the caller keeps its configured value in that case.
    """

    if not executable_path:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - our own resolved browser binary
            [executable_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = _VERSION_RE.search(f"{out.stdout} {out.stderr}")
    return found.group(1) if found else None


_UA_PLATFORM = {
    "Darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
    "Windows": "Windows NT 10.0; Win64; x64",
}
"""The frozen platform token a reduced desktop Chrome UA carries per OS. Frozen
by Chrome's UA reduction -- an Apple Silicon Mac still says `Intel Mac OS X
10_15_7`, which is exactly what makes it safe to hardcode."""


def headful_user_agent(executable_path: str | None) -> str | None:
    """The UA this browser would send with a window, for a launch that has none.

    Headless Chrome sends `HeadlessChrome/<major>` where a windowed one sends
    `Chrome/<major>`, and nothing else in the reduced UA differs. That token alone
    is an edge-level 403 at Akamai (measured on cos.com). So when a launch ends up
    headless, the driver sets this with `--user-agent` -- a launch switch, not the
    Playwright `user_agent=` kwarg, because the kwarg synthesises client-hint
    metadata and Workers then report empty `userAgentData` brands (measured, via
    CreepJS's WorkerGlobalScope card). The switch changes the UA string only.

    `None` when the major version or the OS cannot be determined; the caller then
    leaves the browser's own UA alone rather than guessing a version.
    """

    version = browser_version(executable_path)
    token = _UA_PLATFORM.get(platform.system())
    if version is None or token is None:
        return None
    major = version.split(".", 1)[0]
    return (
        f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def resolve_browser(
    *,
    executable_path: str | Path | None = None,
    channel: str | None = None,
) -> Launch:
    """Decide which browser to launch, most-specific first.

    Raises `BrowserNotFound` -- with the install command in the message -- rather
    than falling through to a Playwright error that names neither what was tried
    nor what to do about it.
    """

    if executable_path is not None:
        path = Path(executable_path).expanduser()
        if not path.exists():
            raise BrowserNotFound(
                f"executable_path {str(path)!r} does not exist. {INSTALL_HINT}"
            )
        return Launch(executable_path=str(path), source="executable_path")

    if channel is not None:
        if channel not in KNOWN_CHANNELS:
            raise BrowserNotFound(
                f"unknown browser channel {channel!r}. "
                f"Known channels: {', '.join(sorted(KNOWN_CHANNELS))}."
            )
        return Launch(channel=channel, source="channel")

    system_browser = find_system_browser()
    if system_browser is not None:
        return Launch(executable_path=system_browser, source="system")

    bundled = find_bundled_chromium()
    if bundled is not None:
        return Launch(executable_path=bundled, source="bundled")

    raise BrowserNotFound(INSTALL_HINT)


def find_system_browser() -> str | None:
    """A Chrome-family browser installed the ordinary way, or `None`.

    Ordered by preference, not by discovery: Chrome first because the pinned
    fingerprint describes Chrome, then Chromium, then the other Chromium-family
    browsers that Playwright can drive.
    """

    system = platform.system()

    if system == "Darwin":
        return next((p for p in _MACOS_CHROME_PATHS if Path(p).exists()), None)

    if system == "Windows":
        for template in _WINDOWS_CHROME_PATHS:
            candidate = _expand_windows(template)
            if candidate and Path(candidate).exists():
                return candidate
        return None

    # Linux and anything else with a PATH.
    for command in _LINUX_CHROME_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    return None


def find_bundled_chromium() -> str | None:
    """A Chromium unpacked by `patchright install` / `playwright install`.

    Several versions accumulate in the cache over time; the highest-sorting
    directory wins, which is the newest build -- the same choice Playwright
    itself makes.
    """

    root = _cache_root()
    if root is None or not root.is_dir():
        return None

    globs = _CACHE_GLOBS.get(platform.system(), _CACHE_GLOBS["Linux"])

    builds = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith(("chromium-", "chrome-"))),
        key=_build_sort_key,
        reverse=True,
    )
    for build in builds:
        for pattern in globs:
            for candidate in sorted(build.glob(pattern)):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


def _build_sort_key(path: Path) -> tuple[int, str]:
    """Sort `chromium-1228` above `chromium-999` -- the revision is a number, and
    sorting these as strings would prefer the older build once it reaches four
    digits. Falls back to the name for anything unnumbered."""

    _, _, revision = path.name.rpartition("-")
    return (int(revision), path.name) if revision.isdigit() else (-1, path.name)


def _cache_root() -> Path | None:
    override = os.environ.get(_BROWSER_CACHE_ENV)
    if override:
        # "0" is Playwright's "keep browsers next to the package" mode, which we
        # cannot locate from here -- treat it as "no cache to search".
        return None if override == "0" else Path(override).expanduser()
    default = _CACHE_ROOTS.get(platform.system())
    if default is not None:
        return Path(default).expanduser()
    local_appdata = os.environ.get("LOCALAPPDATA")
    return Path(local_appdata) / "ms-playwright" if local_appdata else None


def _expand_windows(template: str) -> str | None:
    try:
        return template.format(**os.environ)
    except KeyError:
        return None  # the referenced environment variable is not set
