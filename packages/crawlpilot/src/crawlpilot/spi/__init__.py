"""The contracts: driver-agnostic types and Protocols everything else is written against.

Pure data and interfaces -- dataclasses, `Protocol`s, exceptions. No engine, no
I/O, no framework. That is what lets the driver, the session layer, the platform
and a remote client all depend on the same vocabulary without depending on each
other.

`spi` is a namespace of modules rather than a flat set of names, and `__all__`
below names the **modules** that are published, because that is how it is
actually imported:

    from crawlpilot.spi import actions, errors
    from crawlpilot.spi.errors import NavigationTimeout

Modules absent from this list (`hashing`, `health`, `storage_state`,
`geometry`) are internal to the fusion and session machinery. They are reachable
-- Python has no way to stop you -- but they are not part of what a consumer may
rely on, and `tests/test_project_boundary.py` fails a build that reaches for one.
"""

from crawlpilot.spi import (
    actions,
    cdp,
    dom_tree,
    driver,
    egress,
    errors,
    identity,
    lease,
    proxy,
    scrape,
    streaming,
)

__all__ = [
    "actions",
    "cdp",
    "dom_tree",
    "driver",
    "egress",
    "errors",
    "identity",
    "lease",
    "proxy",
    "scrape",
    "streaming",
]
