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
        enabled: Iterable[str] | None = None,
        host_api_version: str = API_VERSION,
    ) -> None:
        """`disabled` and `enabled` are the two shapes of the same decision, and
        both are needed because they answer different questions.

        `disabled` is an operator's: "this deployment has a bad extension
        installed, turn it off". It names exceptions to a default of *all*.

        `enabled` is a caller's: "run this request with only these". It is an
        allowlist, so `None` (the default) means the deployment's normal set and
        an empty sequence means none at all -- a distinction a caller needs and
        which `disabled` cannot express, since there is no list of "everything
        except everything".

        The second exists because an `Extension` is *code* and cannot cross a
        network. A remote caller cannot hand the worker an object, so site
        knowledge arrives as a `pip install` into the worker image and the
        caller selects among what is there **by name**. This is the parameter
        that selection lands on.
        """

        self.browse = BrowseHooks()
        self.content = ContentHooks()
        self.blocks = BlockHooks()
        self.tools = browser_tools()
        """Built-in browser verbs, plus whatever `ToolMount` extensions add in
        their own namespace."""

        self._loaded: list[ExtensionManifest] = []
        self._disabled = set(disabled)
        self._enabled = None if enabled is None else set(enabled)
        self._host_api_version = host_api_version
        for extension in extensions:
            self.register(extension)

    # ------------------------------------------------------------ loading

    @property
    def loaded(self) -> tuple[str, ...]:
        return tuple(m.name for m in self._loaded)

    @property
    def manifests(self) -> tuple[ExtensionManifest, ...]:
        """The full manifests, not just their names.

        `loaded` answers "is X on?", which is what logging and assertions want.
        A caller choosing *between* extensions needs the version and description
        too -- that is what `GET /v1/capabilities` publishes, and selecting by
        name is not much use without a way to see what the names mean.
        """

        return tuple(self._loaded)

    def register(self, extension: Extension) -> bool:
        """Returns whether the extension was loaded."""

        manifest = getattr(extension, "manifest", None)
        if manifest is None:
            log.warning("extension.no_manifest", extension=type(extension).__name__)
            return False

        # An allowlist, when the caller gave one, wins over every default --
        # including `default_enabled`, which is the extension author's opinion
        # about deployments in general and not about this request.
        if self._enabled is not None and manifest.name not in self._enabled:
            log.info("extension.not_selected", extension=manifest.name)
            return False

        # Disable-by-config: an operator can turn one off without uninstalling.
        # Checked after the allowlist so an operator's "off" still wins over a
        # caller asking for it by name.
        if manifest.name in self._disabled:
            log.info("extension.disabled", extension=manifest.name)
            return False

        if self._enabled is None and not manifest.default_enabled:
            log.info("extension.not_default_enabled", extension=manifest.name)
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
