from __future__ import annotations

import pytest
import pytest_asyncio

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.driver.process_launcher import ProcessLauncher
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef


@pytest_asyncio.fixture
async def launcher():
    launcher = ProcessLauncher()
    yield launcher
    await launcher.close()


@pytest.fixture
def driver(launcher: ProcessLauncher) -> PatchrightDriver:
    return PatchrightDriver(launcher)


@pytest_asyncio.fixture
async def open_ctx(driver: PatchrightDriver, tmp_path) -> ContextRef:
    identity = IdentityKey(tenant="t", domain="example.com", name="test")
    ctx = await driver.open(
        identity, tmp_path / "profile", None, headful=False, egress=EgressPolicy()
    )
    yield ctx
    await driver.close(ctx)


@pytest_asyncio.fixture
async def cdp_ctx(driver: PatchrightDriver, tmp_path) -> ContextRef:
    identity = IdentityKey(tenant="t", domain="example.com", name="cdp-test")
    ctx = await driver.open(
        identity,
        tmp_path / "profile",
        None,
        headful=False,
        egress=EgressPolicy(),
        enable_cdp=True,
    )
    yield ctx
    await driver.close(ctx)
