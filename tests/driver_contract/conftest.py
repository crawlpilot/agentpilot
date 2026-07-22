from __future__ import annotations

import pytest
import pytest_asyncio

from baas.driver.patchright_driver import PatchrightDriver
from baas.driver.process_launcher import ProcessLauncher
from baas.spi.egress import EgressPolicy
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef


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
