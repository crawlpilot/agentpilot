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
from dataclasses import dataclass, field


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

    def context_kwargs(self) -> dict:
        """Patchright `launch_persistent_context` kwargs that Playwright applies
        natively (UA + locale + timezone + viewport). Screen/DPR go through CDP
        `Emulation.setDeviceMetricsOverride` at the driver, not here."""

        return {
            "user_agent": self.user_agent,
            "locale": self.geo.locale,
            "timezone_id": self.geo.timezone_id,
        }

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
        }
        return _INIT_SCRIPT_TEMPLATE.replace("__CFG__", json.dumps(cfg))


# --- Coherent device families. Screen + hardware + WebGL + geo are chosen
# together so the whole bundle describes one real machine (Fingerprint.kt's
# consistency contract). WebGL strings are the real ANGLE renderer names from
# FingerprintParameters.kt:283-329; the headless leaks they replace are
# "Google Inc." / "Google SwiftShader".
_CHROME_VER = "120.0.0.0"


@dataclass(frozen=True)
class _DevicePreset:
    user_agent: str
    screen: ScreenParameters
    hardware: HardwareParameters
    webgl: WebGLParameters
    geo: GeoTimeParameters


_WINDOWS_INTEL_US = _DevicePreset(
    user_agent=(
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROME_VER} Safari/537.36"
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
)

_MAC_APPLE_UK = _DevicePreset(
    user_agent=(
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROME_VER} Safari/537.36"
    ),
    screen=ScreenParameters(2560, 1600, 2560, 1495, color_depth=30, device_pixel_ratio=2.0),
    hardware=HardwareParameters(8, 16, platform="MacIntel", vendor="Apple Computer, Inc."),
    webgl=WebGLParameters(
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, Apple M1, OpenGL 4.1)",
        unmasked_vendor="Apple Inc.",
        unmasked_renderer="Apple M1",
    ),
    geo=GeoTimeParameters("Europe/London", 0, "en-GB", ("en-GB", "en")),
)

_LINUX_NVIDIA_IN = _DevicePreset(
    user_agent=(
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROME_VER} Safari/537.36"
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
)

_PRESETS: tuple[_DevicePreset, ...] = (_WINDOWS_INTEL_US, _MAC_APPLE_UK, _LINUX_NVIDIA_IN)
_PRESETS_BY_REGION: dict[str, _DevicePreset] = {
    "US": _WINDOWS_INTEL_US,
    "GB": _MAC_APPLE_UK,
    "UK": _MAC_APPLE_UK,
    "IN": _LINUX_NVIDIA_IN,
}


def generate(identity_slug: str, *, region: str | None = None) -> Fingerprint:
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
        user_agent=preset.user_agent,
        screen=preset.screen,
        hardware=preset.hardware,
        webgl=preset.webgl,
        geo=preset.geo,
        canvas_seed=canvas_seed,
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
    proto.getParameter = wrapped;
  };
  try { patchGL(WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patchGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}
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
