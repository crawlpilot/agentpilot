"""`agentpilot.identity.vault` -- encryption-at-rest round-trip and tamper/foreign
-key resilience. Trigger semantics (restore-only-on-fresh-dir,
checkpoint-on-release) are the *caller's* responsibility (see the module
docstring), so they're exercised in `test_seam_e2e.py`-level integration,
not here."""

from __future__ import annotations

from cryptography.fernet import Fernet

from agentpilot.identity.vault import Vault
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.storage_state import LocalStorageEntry, OriginState, StorageState

IDENTITY = IdentityKey(tenant="acme", domain="example.com", name="alice")

STATE = StorageState(
    cookies=[{"name": "sid", "value": "abc123", "domain": "example.com"}],
    origins=[
        OriginState(
            origin="https://example.com",
            local_storage=[LocalStorageEntry(name="k", value="v")],
        ),
        OriginState(
            origin="https://other.example.com",
            local_storage=[LocalStorageEntry(name="k2", value="v2")],
        ),
    ],
)


def test_save_load_round_trips_multi_origin_state(tmp_path) -> None:
    vault = Vault(tmp_path, Fernet.generate_key())
    vault.save(IDENTITY, STATE)

    loaded = vault.load(IDENTITY)
    assert loaded is not None
    assert loaded.cookies == STATE.cookies
    assert {o.origin for o in loaded.origins} == {o.origin for o in STATE.origins}


def test_load_missing_identity_returns_none(tmp_path) -> None:
    vault = Vault(tmp_path, Fernet.generate_key())
    assert vault.load(IDENTITY) is None


def test_file_is_actually_encrypted_not_plaintext_json(tmp_path) -> None:
    vault = Vault(tmp_path, Fernet.generate_key())
    vault.save(IDENTITY, STATE)

    raw = (tmp_path / f"{IDENTITY.slug()}.json.enc").read_bytes()
    assert b"abc123" not in raw
    assert b"sid" not in raw


def test_wrong_key_cannot_decrypt_another_tenants_ciphertext(tmp_path) -> None:
    vault_a = Vault(tmp_path, Fernet.generate_key())
    vault_a.save(IDENTITY, STATE)

    vault_b = Vault(tmp_path, Fernet.generate_key())
    assert vault_b.load(IDENTITY) is None


def test_corrupted_file_returns_none_not_a_crash(tmp_path) -> None:
    vault = Vault(tmp_path, Fernet.generate_key())
    vault.save(IDENTITY, STATE)

    path = tmp_path / f"{IDENTITY.slug()}.json.enc"
    path.write_bytes(b"not-even-valid-fernet-ciphertext")

    assert vault.load(IDENTITY) is None
