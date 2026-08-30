"""The per-`Browser` extension registry.

**Scoped to an instance, never global, never populated at import time.** That is
the specific defect this replaces: `block_detect` called
`install_default_site_checkers()` at module import, so merely importing the
content pipeline mutated a module-level list with three retailers' policy, and
behaviour depended on import order.

Wiring an extension is individually isolated, the same discipline Browser4's
`PluginManager` applies to each mount: a `configure_*` that raises disables that
one surface with a warning rather than failing construction.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import structlog

from crawlpilot.extensions.manifest import (
    API_VERSION,
    Compatibility,
    ExtensionManifest,
    check_compatibility,
)
from crawlpilot.extensions.mounts import (
    BlockHooks,
    BlockMount,
    BrowseHooks,
    BrowseMount,
    ContentHooks,
    ContentMount,
    Extension,
    ToolMount,
)
from crawlpilot.tools import browser_tools

log = structlog.get_logger(__name__)

ENTRY_POINT_GROUP = "crawlpilot.extensions"


class ExtensionRegistry:
    def __init__(
        self,
        extensions: Sequence[Extension] = (),
        *,
        disabled: Iterable[str] = (),
        host_api_version: str = API_VERSION,
    ) -> None:
        self.browse = BrowseHooks()
        self.content = ContentHooks()
        self.blocks = BlockHooks()
        self.tools = browser_tools()
        """Built-in browser verbs, plus whatever `ToolMount` extensions add in
        their own namespace."""

        self._loaded: list[ExtensionManifest] = []
        self._disabled = set(disabled)
        self._host_api_version = host_api_version
        for extension in extensions:
            self.register(extension)

    # ------------------------------------------------------------ loading

    @property
    def loaded(self) -> tuple[str, ...]:
        return tuple(m.name for m in self._loaded)

    def register(self, extension: Extension) -> bool:
        """Returns whether the extension was loaded."""

        manifest = getattr(extension, "manifest", None)
        if manifest is None:
            log.warning("extension.no_manifest", extension=type(extension).__name__)
            return False

        # Disable-by-config: an operator can turn one off without uninstalling.
        if manifest.name in self._disabled or not manifest.default_enabled:
            log.info("extension.disabled", extension=manifest.name)
            return False

        verdict = check_compatibility(manifest, self._host_api_version)
        if verdict.compatibility is Compatibility.REFUSE:
            log.error("extension.refused", extension=manifest.name, reason=verdict.reason)
            return False
        if verdict.compatibility is Compatibility.LOAD_WITH_WARNING:
            log.warning("extension.compat_warning", extension=manifest.name, reason=verdict.reason)

        wired = self._wire(extension, manifest)
        self._loaded.append(manifest)
        log.info(
            "extension.loaded",
            extension=manifest.name,
            version=manifest.version,
            source=type(extension).__module__,
            mounts=wired,
        )
        return True

    def _wire(self, extension: Extension, manifest: ExtensionManifest) -> list[str]:
        wired: list[str] = []
        if isinstance(extension, ToolMount):
            try:
                for spec in extension.tools():
                    self.tools.register(spec, namespace=manifest.name)
                wired.append("tools")
            except Exception as exc:  # noqa: BLE001 -- one bad mount, not a dead registry
                log.warning(
                    "extension.mount_failed",
                    extension=manifest.name,
                    mount="tools",
                    error=str(exc),
                )

        for mount, hooks, attr in (
            (BrowseMount, self.browse, "configure_browse"),
            (ContentMount, self.content, "configure_content"),
            (BlockMount, self.blocks, "configure_blocks"),
        ):
            if not isinstance(extension, mount):
                continue
            before = _chain_sizes(hooks)
            try:
                getattr(extension, attr)(hooks)
            except Exception as exc:  # noqa: BLE001 -- one bad mount, not a dead registry
                log.warning(
                    "extension.mount_failed",
                    extension=manifest.name,
                    mount=attr,
                    error=str(exc),
                )
                continue
            _label_new_handlers(hooks, before, manifest.name)
            wired.append(attr)
        return wired


def _chain_sizes(hooks: object) -> dict[str, int]:
    return {k: len(v) for k, v in vars(hooks).items()}


def _label_new_handlers(hooks: object, before: dict[str, int], extension: str) -> None:
    """Attribute the handlers this extension just added, so hook metrics and
    failure logs name the right one. Done here rather than asking extensions to
    pass their own name to every `add_last` call."""

    for key, chain in vars(hooks).items():
        for handler in chain.handlers[before.get(key, 0) :]:
            handler.extension = extension


def discover_extensions(group: str = ENTRY_POINT_GROUP) -> list[Extension]:
    """Load extensions advertised by installed packages.

    **Opt-in**: nothing calls this at import. A caller that wants third-party
    extensions asks for them; a caller that does not gets exactly the ones it
    passed. Each entry point is loaded in isolation so one broken package cannot
    prevent the rest from loading.
    """

    from importlib.metadata import entry_points  # noqa: PLC0415

    found: list[Extension] = []
    for ep in entry_points(group=group):
        try:
            factory = ep.load()
            found.append(factory() if callable(factory) else factory)
        except Exception as exc:  # noqa: BLE001
            log.warning("extension.discovery_failed", entry_point=ep.name, error=str(exc))
    return found
