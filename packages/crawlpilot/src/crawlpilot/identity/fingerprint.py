"""Per-identity browser fingerprint -- a port of Pulsar's `Fingerprint` /
`FingerprintParameters` schema (`pulsar-common/.../fingerprint/Fingerprint.kt`,
`FingerprintParameters.kt`) with the value choices from its `stealth.js`.

The load-bearing rule Pulsar enforces: every parameter block must be mutually
consistent -- the UA's platform, the screen size, the hardware, the WebGL
renderer, and the timezone all describe *one plausible machine*. A spoofed
WebGL vendor that disagrees with the UA is a stronger bot tell than no spoof at
all. So this module never mixes blocks: a fingerprint is drawn whole from one
`_DevicePreset` family, and pinned for the identity's life (deterministic on the
identity slug) so cookies/IP/fingerprint stay coherent across visits.

Pulsar's rich randomiser lives in a closed "pro" ServiceLoader plugin that
isn't in the open tree, so this generator is authored fresh from the schema --
a small set of real, internally-consistent device families rather than a full
randomiser. Add families as needed; keep each one internally consistent.

Patchright already neutralises the `navigator.webdriver` / `window.chrome`
shims at the CDP layer, so `init_script()` ports only the *value choices*
(WebGL strings, hardwareConcurrency, languages) that Patchright does not set --
applied via `add_init_script`, with the `toString`-native guard so the patched
getters don't themselves become a tell.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from crawlpilot.config import DEFAULT_CHROME_VERSION


@dataclass(frozen=True)
class ScreenParameters:
    width: int
    height: int
    avail_width: int
    avail_height: int
    color_depth: int = 24
    device_pixel_ratio: float = 1.0


@dataclass(frozen=True)
class HardwareParameters:
    hardware_concurrency: int
    device_memory: int
    platform: str
    vendor: str = "Google Inc."
    max_touch_points: int = 0
    product_sub: str = "20030107"


@dataclass(frozen=True)
class WebGLParameters:
    vendor: str
    renderer: str
    unmasked_vendor: str
    unmasked_renderer: str
    shading_language_version: str = "WebGL GLSL ES 1.0"
    max_texture_size: int = 16384


@dataclass(frozen=True)
class GeoTimeParameters:
    timezone_id: str
    offset_minutes: int
    locale: str
    languages: tuple[str, ...]


@dataclass(frozen=True)
class Fingerprint:
    user_agent: str
    screen: ScreenParameters
    hardware: HardwareParameters
    webgl: WebGLParameters
    geo: GeoTimeParameters
    canvas_seed: str
    # Client-Hint block, pinned to the same Chrome version as the UA string so
    # UA / Sec-CH-UA / navigator.userAgentData all describe one build.
    chrome_full_version: str
    chrome_major: str
    ch_platform: str
    ch_platform_version: str
    ch_architecture: str

    def client_hint_headers(self) -> dict[str, str]:
        """The always-sent (low-entropy) Client-Hint request headers, pinned to
        this fingerprint's Chrome major + platform. Applied via the context's
        extra HTTP headers so the wire-level Sec-CH-UA agrees with the spoofed
        UA header and with navigator.userAgentData (patched in init_script) --
        the trio Akamai cross-checks. These three are exactly the hints Chrome
        sends by default (no Accept-CH negotiation needed)."""

        return {
            "sec-ch-ua": _sec_ch_ua(self.chrome_major),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{self.ch_platform}"',
        }

    def context_kwargs(self) -> dict[str, str]:
        """Patchright `launch_persistent_context` kwargs that Playwright applies
        natively (UA + locale + timezone). Screen/DPR are pinned in JS via
        `init_script` (patching `window.screen`/`devicePixelRatio`), not CDP
        device-metrics -- the driver runs `no_viewport=True` with a real window,
        which `Emulation.setDeviceMetricsOverride` would override."""

        return {
            "user_agent": self.user_agent,
            "locale": self.geo.locale,
            "timezone_id": self.geo.timezone_id,
        }

    def launch_args(self) -> list[str]:
        """Curated Chrome hardening flags that Patchright/Playwright does NOT
        already pass (verified against its `chromiumSwitches`). We deliberately
        omit everything Patchright owns -- above all
        `--disable-blink-features=AutomationControlled`, which Patchright adds
        itself only if no `--disable-blink-features` is present, so we never pass
        one. `--window-size` matches the pinned screen so a headful window's
        geometry agrees with the spoofed `window.screen`. Ported from
        `Options.kt:69-127` / `BrowserSettings.kt:548-571`, filtered by Phase 0."""

        return [
            f"--window-size={self.screen.width},{self.screen.height}",
            "--window-position=0,0",
            "--mute-audio",
            "--disable-client-side-phishing-detection",
            # NOT `--disable-default-apps`. Scrapling classes it with
            # `--enable-automation` and `--disable-extensions` as a flag that
            # marks the browser as driven (`engines/constants.py::HARMFUL_ARGS`)
            # -- a stock Chrome has its default apps, and saying otherwise is a
            # difference with no upside. It was here for tidiness, which is not
            # worth a signal.
            "--metrics-recording-only",
            "--safebrowsing-disable-auto-update",
        ]

    def init_script(self) -> str:
        """JS injected via `add_init_script` to pin the values Patchright does
        not set: WebGL unmasked vendor/renderer, hardwareConcurrency,
        deviceMemory, platform, languages. The `toString` guard keeps the
        patched getters reporting `[native code]` (stealth.js's core trick)."""

        cfg = {
            "hardwareConcurrency": self.hardware.hardware_concurrency,
            "deviceMemory": self.hardware.device_memory,
            "platform": self.hardware.platform,
            "languages": list(self.geo.languages),
            "webglVendor": self.webgl.unmasked_vendor,
            "webglRenderer": self.webgl.unmasked_renderer,
            # window.screen / devicePixelRatio, pinned to the device family so a
            # headless (or differently-sized-host) context reports the same
            # display geometry as the --window-size launch flag and the WebGL/UA
            # blocks describe. window.screen otherwise leaks the real host size.
            "screen": {
                "width": self.screen.width,
                "height": self.screen.height,
                "availWidth": self.screen.avail_width,
                "availHeight": self.screen.avail_height,
                "colorDepth": self.screen.color_depth,
                "devicePixelRatio": self.screen.device_pixel_ratio,
            },
            # navigator.userAgentData, kept coherent with the spoofed UA and the
            # Sec-CH-UA headers (client_hint_headers). Patchright leaves this at
            # the *real* Chrome's values, which disagree with our pinned UA -- the
            # mismatch WAFs flag. getHighEntropyValues echoes only requested hints.
            "uaData": {
                "brands": _brands(self.chrome_major),
                "mobile": False,
                "platform": self.ch_platform,
                "architecture": self.ch_architecture,
                "bitness": "64",
                "model": "",
                "platformVersion": self.ch_platform_version,
                "uaFullVersion": self.chrome_full_version,
                "fullVersionList": _full_version_list(self.chrome_full_version, self.chrome_major),
                "wow64": False,
            },
            # Per-identity canvas/audio noise seed. Derived from the identity
            # digest (`generate`), so the *same* identity always draws the same
            # canvas -- a fingerprint that changes between page loads is a
            # louder signal than one that never changes at all.
            "canvasSeed": _seed_int(self.canvas_seed),
            "maxTouchPoints": self.hardware.max_touch_points,
            "vendor": self.hardware.vendor,
            "productSub": self.hardware.product_sub,
        }
        return _INIT_SCRIPT_TEMPLATE.replace("__CFG__", json.dumps(cfg))


# --- Coherent device families. Screen + hardware + WebGL + geo are chosen
# together so the whole bundle describes one real machine (Fingerprint.kt's
# consistency contract). WebGL strings are the real ANGLE renderer names from
# FingerprintParameters.kt:283-329; the headless leaks they replace are
# "Google Inc." / "Google SwiftShader".

# The Chrome version claimed across *every* UA-derived surface: the UA string,
# the Sec-CH-UA request headers, and navigator.userAgentData. `channel="chrome"`
# launches the real system Chrome, so pinning a stale version here (the old
# hardcoded 120) while the browser -- and its native Sec-CH-UA / userAgentData --
# reported a much newer build was a hard, deterministic bot tell for WAFs
# (Akamai) that cross-check UA against Client Hints. Keep this aligned with the
# deployed Chrome via AGENTPILOT_CHROME_VERSION; the default tracks a recent
# stable so a fresh install and the pinned value already agree.
# Chrome freezes the UA string's minor/build/patch to `.0.0.0` (the UA-reduction
# rollout); only the major is real there. The full version rides on the
# high-entropy Client Hints (uaFullVersion / fullVersionList) instead. The
# concrete version is supplied per call by `config.FingerprintConfig`, not read
# from the environment here -- reading it at module scope froze the value on
# first import, so a caller that was not the process owner could not change it.
def _ua_version(full_version: str) -> str:
    return f"{full_version.split('.', 1)[0]}.0.0.0"


# Sec-CH-UA GREASE brand -- computed, not hardcoded.
#
# This used to be a fixed `"Not_A Brand";v="24"`, on the assumption that "WAFs
# check the version, not the brand order". Both halves were wrong. Chrome derives
# the GREASE brand, its version *and* the brand order from the major version
# (`GetGreasedUserAgentBrandVersion` in Chromium's user_agent_utils.cc), so the
# fixed value was right for no current release and disagreed with the real
# `navigator.userAgentData` on every one. MEASURED, byte for byte, against the
# live browsers:
#
#   151 -> "Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"
#   153 -> "Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"
_GREASEY_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASED_VERSIONS = ("8", "99", "24")
_BRAND_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def _grease(major: str) -> tuple[str, str, tuple[int, int, int]]:
    """(brand, version, order) for Chrome `major`, seeded exactly as Chromium seeds it."""

    seed = int(major)
    brand = (
        f"Not{_GREASEY_CHARS[seed % len(_GREASEY_CHARS)]}A"
        f"{_GREASEY_CHARS[(seed + 1) % len(_GREASEY_CHARS)]}Brand"
    )
    return brand, _GREASED_VERSIONS[seed % len(_GREASED_VERSIONS)], _BRAND_ORDERS[seed % 6]


def _ordered(major: str, grease_version: str, chromium: str, chrome: str) -> list[dict[str, str]]:
    brand, _, order = _grease(major)
    slots: list[dict[str, str]] = [{}, {}, {}]
    slots[order[0]] = {"brand": brand, "version": grease_version}
    slots[order[1]] = {"brand": "Chromium", "version": chromium}
    slots[order[2]] = {"brand": "Google Chrome", "version": chrome}
    return slots


def _brands(major: str) -> list[dict[str, str]]:
    """Low-entropy brand list shared by Sec-CH-UA and userAgentData.brands."""
    _, version, _ = _grease(major)
    return _ordered(major, version, major, major)


def _full_version_list(full: str, major: str) -> list[dict[str, str]]:
    """High-entropy fullVersionList: same brands and order, full dotted versions."""
    _, version, _ = _grease(major)
    return _ordered(major, f"{version}.0.0.0", full, full)


def _seed_int(canvas_seed: str) -> int:
    """The hex `canvas_seed` as a 32-bit int for the init script's xorshift.

    `canvas_seed` was computed and stored from the identity digest but read by
    nothing until the canvas/audio noise landed -- this is what finally uses it.
    """

    return int(canvas_seed[:8], 16)


def _sec_ch_ua(major: str) -> str:
    """The `Sec-CH-UA` header value (always-sent, low-entropy)."""
    return ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in _brands(major))


@dataclass(frozen=True)
class _DevicePreset:
    user_agent: str
    screen: ScreenParameters
    hardware: HardwareParameters
    webgl: WebGLParameters
    geo: GeoTimeParameters
    # Client-Hint geometry. `ch_platform` is the Sec-CH-UA-Platform /
    # userAgentData.platform token ("Windows"/"macOS"/"Linux") -- note this is
    # *not* navigator.platform ("Win32"/"MacIntel"/...), which the hardware block
    # carries separately. `ch_architecture` must agree with the WebGL/hardware
    # family ("arm" for Apple Silicon, "x86" otherwise).
    ch_platform: str
    ch_platform_version: str
    ch_architecture: str


_WINDOWS_INTEL_US = _DevicePreset(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
    ),
    screen=ScreenParameters(1920, 1080, 1920, 1040, color_depth=24, device_pixel_ratio=1.0),
    hardware=HardwareParameters(8, 8, platform="Win32"),
    webgl=WebGLParameters(
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        unmasked_vendor="Intel Inc.",
        unmasked_renderer="Intel(R) UHD Graphics",
    ),
    geo=GeoTimeParameters("America/New_York", 300, "en-US", ("en-US", "en")),
    ch_platform="Windows",
    ch_platform_version="15.0.0",
    ch_architecture="x86",
)

_MAC_APPLE_UK = _DevicePreset(
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
    ),
    screen=ScreenParameters(2560, 1600, 2560, 1495, color_depth=30, device_pixel_ratio=2.0),
    # `vendor` stays the default `Google Inc.`: that is what Chrome reports on
    # every OS. "Apple Computer, Inc." is Safari's value, and claiming it from a
    # Chrome UA was a contradiction SannySoft and CreepJS both printed.
    hardware=HardwareParameters(8, 16, platform="MacIntel"),
    webgl=WebGLParameters(
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, Apple M1, OpenGL 4.1)",
        unmasked_vendor="Apple Inc.",
        unmasked_renderer="Apple M1",
    ),
    geo=GeoTimeParameters("Europe/London", 0, "en-GB", ("en-GB", "en")),
    ch_platform="macOS",
    ch_platform_version="14.5.0",
    ch_architecture="arm",
)

_LINUX_NVIDIA_IN = _DevicePreset(
    user_agent=(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
    ),
    screen=ScreenParameters(1366, 768, 1366, 728, color_depth=24, device_pixel_ratio=1.0),
    hardware=HardwareParameters(4, 8, platform="Linux x86_64"),
    webgl=WebGLParameters(
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        unmasked_vendor="Google Inc. (NVIDIA)",
        unmasked_renderer="NVIDIA GeForce GTX 1650",
    ),
    geo=GeoTimeParameters("Asia/Kolkata", -330, "en-IN", ("en-IN", "en")),
    ch_platform="Linux",
    ch_platform_version="6.5.0",
    ch_architecture="x86",
)

_PRESETS: tuple[_DevicePreset, ...] = (_WINDOWS_INTEL_US, _MAC_APPLE_UK, _LINUX_NVIDIA_IN)
_PRESETS_BY_REGION: dict[str, _DevicePreset] = {
    "US": _WINDOWS_INTEL_US,
    "GB": _MAC_APPLE_UK,
    "UK": _MAC_APPLE_UK,
    "IN": _LINUX_NVIDIA_IN,
}


def geo_for_region(region: str | None) -> GeoTimeParameters | None:
    """The timezone/locale block for an egress country, or `None` if unknown.

    Deliberately independent of the device preset: the geo must follow where the
    traffic exits, never which device an identity happens to hash to.
    """

    if not region:
        return None
    preset = _PRESETS_BY_REGION.get(region.upper())
    return preset.geo if preset else None


def generate(
    identity_slug: str,
    *,
    region: str | None = None,
    chrome_version: str = DEFAULT_CHROME_VERSION,
) -> Fingerprint:
    """Deterministically pin one coherent fingerprint to an identity. Same slug
    -> same fingerprint for life (the pinning contract). When `region` is given
    (e.g. from the proxy exit-IP country), prefer a family whose timezone/locale
    matches, so egress geo and fingerprint geo agree; otherwise pick a stable
    family from the slug hash."""

    digest = hashlib.sha256(identity_slug.encode()).hexdigest()
    if region and region.upper() in _PRESETS_BY_REGION:
        preset = _PRESETS_BY_REGION[region.upper()]
    else:
        preset = _PRESETS[int(digest[:8], 16) % len(_PRESETS)]
    # A per-identity canvas seed drives deterministic canvas noise (CanvasParameters).
    canvas_seed = digest[8:24]
    return Fingerprint(
        user_agent=preset.user_agent.format(chrome_ver=_ua_version(chrome_version)),
        screen=preset.screen,
        hardware=preset.hardware,
        webgl=preset.webgl,
        geo=preset.geo,
        canvas_seed=canvas_seed,
        chrome_full_version=chrome_version,
        chrome_major=chrome_version.split(".", 1)[0],
        ch_platform=preset.ch_platform,
        ch_platform_version=preset.ch_platform_version,
        ch_architecture=preset.ch_architecture,
    )


_INIT_SCRIPT_TEMPLATE = """
(() => {
  const cfg = __CFG__;
  const nativeToString = Function.prototype.toString;
  const patched = new WeakSet();
  const define = (obj, prop, value) => {
    try {
      const getter = () => value;
      patched.add(getter);
      Object.defineProperty(obj, prop, { get: getter, configurable: true });
    } catch (e) {}
  };
  // navigator scalars
  define(navigator, 'hardwareConcurrency', cfg.hardwareConcurrency);
  define(navigator, 'deviceMemory', cfg.deviceMemory);
  define(navigator, 'platform', cfg.platform);
  try {
    Object.defineProperty(navigator, 'languages', { get: () => cfg.languages, configurable: true });
  } catch (e) {}
  // WebGL unmasked vendor/renderer (37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL)
  const patchGL = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    const wrapped = function (p) {
      if (p === 37445) return cfg.webglVendor;
      if (p === 37446) return cfg.webglRenderer;
      return orig.call(this, p);
    };
    // Register with the toString guard below, exactly like `define()` does for
    // every getter it installs. Without this,
    // `WebGLRenderingContext.prototype.getParameter.toString()` returns this
    // function's literal source instead of "[native code]" -- a hard,
    // trivially-scriptable tell that defeats the very spoof it is guarding,
    // and one of the first things a commercial sensor checks.
    patched.add(wrapped);
    proto.getParameter = wrapped;
  };
  try { patchGL(WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patchGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}
  // window.screen / devicePixelRatio -- pin display geometry to the device family.
  const scr = cfg.screen;
  if (scr) {
    const defScreen = (prop, value) => {
      try { Object.defineProperty(window.screen, prop, { get: () => value, configurable: true }); }
      catch (e) {}
    };
    defScreen('width', scr.width);
    defScreen('height', scr.height);
    defScreen('availWidth', scr.availWidth);
    defScreen('availHeight', scr.availHeight);
    defScreen('colorDepth', scr.colorDepth);
    defScreen('pixelDepth', scr.colorDepth);
    try {
      Object.defineProperty(window, 'devicePixelRatio', {
        get: () => scr.devicePixelRatio, configurable: true,
      });
    } catch (e) {}
  }
  // navigator.userAgentData -- pin brands/platform/high-entropy to the same
  // Chrome build as the UA string + Sec-CH-UA headers. getHighEntropyValues
  // returns only the hints the caller asked for (Chrome's contract).
  const uad = cfg.uaData;
  if (uad) {
    const high = {
      architecture: uad.architecture,
      bitness: uad.bitness,
      model: uad.model,
      platformVersion: uad.platformVersion,
      uaFullVersion: uad.uaFullVersion,
      fullVersionList: uad.fullVersionList,
      wow64: uad.wow64,
    };
    const low = { brands: uad.brands, mobile: uad.mobile, platform: uad.platform };
    const getHighEntropyValues = function (hints) {
      const out = Object.assign({}, low);
      (hints || []).forEach((h) => { if (h in high) out[h] = high[h]; });
      return Promise.resolve(out);
    };
    patched.add(getHighEntropyValues);
    const data = Object.assign({}, low, {
      getHighEntropyValues,
      toJSON: () => Object.assign({}, low),
    });
    patched.add(data.toJSON);
    try {
      Object.defineProperty(navigator, 'userAgentData', { get: () => data, configurable: true });
    } catch (e) {}
  }
  // navigator scalars that the dataclass already carried but nothing applied.
  define(navigator, 'maxTouchPoints', cfg.maxTouchPoints);
  define(navigator, 'vendor', cfg.vendor);
  define(navigator, 'productSub', cfg.productSub);
  // navigator.plugins / mimeTypes. Headless Chrome reports an *empty*
  // PluginArray; a real desktop Chrome always carries the built-in PDF
  // entries, so emptiness is itself the tell. Cross-referenced the way Chrome
  // does it (each plugin's item(0) is its mimeType and vice versa).
  try {
    const specs = [
      ['PDF Viewer', 'internal-pdf-viewer'],
      ['Chrome PDF Viewer', 'internal-pdf-viewer'],
      ['Chromium PDF Viewer', 'internal-pdf-viewer'],
      ['Microsoft Edge PDF Viewer', 'internal-pdf-viewer'],
      ['WebKit built-in PDF', 'internal-pdf-viewer'],
    ];
    const mimeSpecs = [
      ['application/pdf', 'pdf'],
      ['text/pdf', 'pdf'],
    ];
    const mimes = mimeSpecs.map(([type, suffixes]) =>
      Object.create(MimeType.prototype, {
        type: { value: type, enumerable: true },
        suffixes: { value: suffixes, enumerable: true },
        description: { value: 'Portable Document Format', enumerable: true },
      }));
    const plugins = specs.map(([name, filename]) => {
      const p = Object.create(Plugin.prototype, {
        name: { value: name, enumerable: true },
        filename: { value: filename, enumerable: true },
        description: { value: 'Portable Document Format', enumerable: true },
        length: { value: mimes.length, enumerable: true },
      });
      mimes.forEach((m, i) => Object.defineProperty(p, i, { value: m, enumerable: true }));
      Object.defineProperty(p, 'item', { value: (i) => mimes[i] || null });
      Object.defineProperty(p, 'namedItem', {
        value: (n) => mimes.find((m) => m.type === n) || null });
      return p;
    });
    mimes.forEach((m) => Object.defineProperty(m, 'enabledPlugin', { value: plugins[0] }));
    const arrayLike = (items, proto, key) => {
      const obj = Object.create(proto);
      items.forEach((it, i) => Object.defineProperty(obj, i, { value: it, enumerable: true }));
      Object.defineProperty(obj, 'length', { value: items.length });
      Object.defineProperty(obj, 'item', { value: (i) => items[i] || null });
      Object.defineProperty(obj, 'namedItem', {
        value: (n) => items.find((it) => it[key] === n) || null });
      Object.defineProperty(obj, 'refresh', { value: () => undefined });
      return obj;
    };
    define(navigator, 'plugins', arrayLike(plugins, PluginArray.prototype, 'name'));
    define(navigator, 'mimeTypes', arrayLike(mimes, MimeTypeArray.prototype, 'type'));
  } catch (e) {}
  // WebRTC: force it to respect the proxy, or rather to stop routing around it.
  //
  // This is the one patch here that closes a LEAK rather than smoothing a
  // difference. Everything else makes the browser look more ordinary; this
  // stops it volunteering the answer. A page that opens an RTCPeerConnection
  // and reads the ICE candidates gets the host's real local and public
  // addresses straight from the OS network stack -- WebRTC never goes through
  // an HTTP proxy -- so a single JS call undoes the entire per-identity proxy
  // pin that `identity/proxy_pinning.py` works to maintain.
  //
  // Deleting RTCPeerConnection outright is its own tell: it exists in every
  // real desktop Chrome and a number of ordinary sites feature-detect it. So
  // the constructor stays, and the candidates it would emit are dropped --
  // which is indistinguishable from a browser behind a symmetric NAT that
  // gathered nothing, a completely unremarkable state.
  try {
    const Native = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (Native) {
      const Guarded = function (...args) {
        const pc = new Native(...args);
        const origGather = pc.createDataChannel && pc.createDataChannel.bind(pc);
        if (origGather) {
          const guardedChannel = function (...a) { return origGather(...a); };
          patched.add(guardedChannel);
          pc.createDataChannel = guardedChannel;
        }
        // `icecandidate` is where the addresses surface. Swallowing the
        // event's candidates leaves gathering to complete normally -- the
        // null end-of-candidates event still fires, so callers waiting on it
        // do not hang.
        pc.addEventListener('icecandidate', (event) => {
          if (event && event.candidate && event.candidate.candidate) {
            event.stopImmediatePropagation();
          }
        }, true);
        return pc;
      };
      Guarded.prototype = Native.prototype;
      patched.add(Guarded);
      window.RTCPeerConnection = Guarded;
      if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Guarded;
    }
  } catch (e) {}
  // permissions.query: headless Chrome answers 'denied' for notifications
  // while Notification.permission says 'default' -- a self-contradiction no
  // real browser produces.
  try {
    const origQuery = Permissions.prototype.query;
    const query = function (parameters) {
      if (parameters && parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return origQuery.call(this, parameters);
    };
    patched.add(query);
    Permissions.prototype.query = query;
  } catch (e) {}
  // Canvas + audio noise, seeded per identity. Deterministic on purpose: a
  // fingerprint that differs on every read is a louder signal than a stable
  // one, so this perturbs by a fixed-per-identity amount rather than randomly.
  try {
    let seed = cfg.canvasSeed >>> 0;
    const nextNoise = () => {
      // xorshift32 -- small, dependency-free, and reproducible from the seed.
      seed ^= seed << 13; seed >>>= 0;
      seed ^= seed >> 17;
      seed ^= seed << 5;  seed >>>= 0;
      return seed % 3;  // 0..2, i.e. at most one LSB step per channel
    };
    const perturb = (data) => {
      for (let i = 0; i < data.length; i += 4) {
        // Alpha (i+3) is deliberately untouched: nudging it changes visible
        // transparency and breaks pages that composite the canvas.
        data[i] = Math.min(255, Math.max(0, data[i] + nextNoise() - 1));
        data[i + 1] = Math.min(255, Math.max(0, data[i + 1] + nextNoise() - 1));
        data[i + 2] = Math.min(255, Math.max(0, data[i + 2] + nextNoise() - 1));
      }
    };
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const getImageData = function (...args) {
      const out = origGetImageData.apply(this, args);
      const localSeed = seed;
      perturb(out.data);
      seed = localSeed;  // same input -> same output within a document
      return out;
    };
    patched.add(getImageData);
    CanvasRenderingContext2D.prototype.getImageData = getImageData;

    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const toDataURL = function (...args) {
      try {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
          const img = origGetImageData.call(ctx, 0, 0, this.width, this.height);
          const localSeed = seed;
          perturb(img.data);
          seed = localSeed;
          ctx.putImageData(img, 0, 0);
        }
      } catch (e) {}
      return origToDataURL.apply(this, args);
    };
    patched.add(toDataURL);
    HTMLCanvasElement.prototype.toDataURL = toDataURL;
  } catch (e) {}
  try {
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    const getChannelData = function (channel) {
      const out = origGetChannelData.call(this, channel);
      // A fixed, inaudible offset keyed to the identity -- enough to move the
      // fingerprint hash, far below anything that affects playback.
      const delta = ((cfg.canvasSeed % 1000) + 1) * 1e-8;
      for (let i = 0; i < out.length; i += 100) out[i] = out[i] + delta;
      return out;
    };
    patched.add(getChannelData);
    AudioBuffer.prototype.getChannelData = getChannelData;
  } catch (e) {}
  // Error.prepareStackTrace lockdown (Browser4's hand-appended CDP defence,
  // stealth.js:121-130). Non-writable + non-configurable means a page cannot
  // install a getter to observe how an Error is serialized, which is the
  // classic `Runtime.enable` + console.debug side channel.
  try {
    Object.defineProperty(Error, 'prepareStackTrace', {
      writable: false, configurable: false, value: undefined,
    });
  } catch (e) {}
  // Keep toString native for patched getters (stealth.js core trick).
  const toStringProxy = new Proxy(nativeToString, {
    apply(target, thisArg, args) {
      if (patched.has(thisArg)) return 'function () { [native code] }';
      return Reflect.apply(target, thisArg, args);
    },
  });
  // eslint-disable-next-line no-extend-native
  Function.prototype.toString = toStringProxy;
})();
"""
