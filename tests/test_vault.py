"""OR-70: every stored credential encrypted with a master key kept outside the data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from sqlmodel import Session

from orchestrator import vault
from orchestrator.cli import cli
from orchestrator.config import get_settings
from orchestrator.connections import store
from orchestrator.connections.model import Connection
from orchestrator.connections.service import export_shared
from orchestrator.db import session as session_module
from orchestrator.db.models import SETTING_PUSH_VAPID, Setting
from orchestrator.db.session import get_engine
from orchestrator.deploy import webhook
from orchestrator.notify.push import get_vapid
from orchestrator.web.app import create_app

TOKEN = "PVEAPIToken-super-secret-1234"
KEY = "ptr_live_api_key_abcdef"


@pytest.fixture
def db() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        yield s


def _conns() -> list[Connection]:
    return [
        Connection(
            id="home",
            kind="portainer",
            settings={"url": "https://10.0.0.12:9443"},
            secrets={"api_key": KEY},
        )
    ]


def _plain_file(path: Path) -> None:
    """A connections.yaml as an install from before OR-70 wrote it: secrets in plain text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.dump_connections(_conns()))


# -- values --------------------------------------------------------------------------------------


def test_a_value_round_trips_and_names_its_key() -> None:
    v = vault.current()
    sealed = v.encrypt(TOKEN)
    assert sealed.startswith(f"enc:v1:{v.key.id}:") and TOKEN not in sealed
    assert v.decrypt(sealed) == TOKEN
    assert v.encrypt(TOKEN) != sealed  # a fresh nonce every time
    assert v.decrypt("plain from before") == "plain from before"


def test_the_key_file_is_made_once_and_kept_private() -> None:
    path = vault.key_path(get_settings())
    first = vault.current().key.id
    assert path.is_file() and oct(path.stat().st_mode & 0o777) == "0o400"
    vault.use(None)
    assert vault.current().key.id == first  # read back, not made again


# -- connections.yaml -----------------------------------------------------------------------------


def test_connections_yaml_holds_no_plain_secret() -> None:
    path = store.path_for(get_settings())
    store.write_file(path, _conns())
    store.write_file(path, _conns())  # a second write keeps a backup copy
    text = path.read_text()
    assert KEY not in text and "enc:v1:" in text
    assert store.read_file(path)[0].secrets["api_key"] == KEY
    backups = list((path.parent / "connections-backups").glob("*"))
    assert backups and all(KEY not in b.read_text() for b in backups)


def test_an_export_for_another_orchestrator_is_plain(db: Session) -> None:
    store.write_file(
        store.path_for(get_settings()), [c.model_copy(update={"scope": "shared"}) for c in _conns()]
    )
    body = export_shared(get_settings(), with_secrets=True)
    assert KEY in body and "enc:v1:" not in body  # the other side has its own key


# -- migrating plain text ----------------------------------------------------------------------------


def test_startup_encrypts_everything_left_from_before(db: Session) -> None:
    settings = get_settings()
    path = store.path_for(settings)
    _plain_file(path)
    backups = path.parent / "connections-backups"
    backups.mkdir()
    (backups / "connections.yaml.20260101T000000Z").write_text(path.read_text())
    db.add(Setting(key="deploy.webhook.example-app", value={"secret": TOKEN}))
    db.add(
        Setting(
            key=SETTING_PUSH_VAPID,
            value={"private_pem": "-----BEGIN PRIVATE KEY-----x", "public_key": "pub"},
        )
    )
    db.commit()

    report = vault.startup(settings)
    assert report.changed and report.backups_rewritten == 1

    assert KEY not in path.read_text()
    assert all(KEY not in b.read_text() for b in backups.iterdir())
    db.expire_all()
    assert vault.is_encrypted(db.get(Setting, "deploy.webhook.example-app").value["secret"])  # type: ignore[union-attr]
    assert webhook.reveal_secret(db, "example-app") == TOKEN
    assert vault.recorded_key_id(db) == vault.current().key.id
    # The database file itself: the plain values are gone, free pages included (VACUUM).
    raw = Path(str(get_engine().url.database)).read_bytes()
    assert TOKEN.encode() not in raw

    again = vault.startup(settings)
    assert not again.changed  # nothing left to do


def test_status_counts_what_is_still_plain(db: Session) -> None:
    _plain_file(store.path_for(get_settings()))
    db.add(Setting(key="deploy.webhook.a", value={"secret": TOKEN}))
    db.commit()
    report = vault.status(get_settings(), db)
    assert report.connections_plain == 1 and report.db_plain == 1 and report.plain == 2


def test_new_secrets_are_stored_encrypted(db: Session) -> None:
    key = webhook.rotate_secret(db, "example-app")
    stored = db.get(Setting, "deploy.webhook.example-app").value["secret"]  # type: ignore[union-attr]
    assert vault.is_encrypted(stored) and key not in stored
    assert webhook.reveal_secret(db, "example-app") == key
    _vapid, public = get_vapid(db)
    pem = db.get(Setting, SETTING_PUSH_VAPID).value["private_pem"]  # type: ignore[union-attr]
    assert vault.is_encrypted(pem) and get_vapid(db)[1] == public


# -- a missing or wrong key ------------------------------------------------------------------------


def test_a_missing_key_stops_the_start_instead_of_blank_credentials(db: Session) -> None:
    settings = get_settings()
    vault.startup(settings)
    vault.key_path(settings).unlink()
    vault.use(None)
    with pytest.raises(vault.VaultError, match="master key is missing"):
        vault.boot(settings)
    assert not vault.key_path(settings).exists()  # and no new key was made in its place

    with pytest.raises(RuntimeError, match="will not start"):
        create_app()


def test_a_wrong_key_is_refused(db: Session) -> None:
    settings = get_settings()
    vault.startup(settings)
    path = vault.key_path(settings)
    path.chmod(0o600)
    vault.write_key(path, b"x" * 32)
    vault.use(None)
    with pytest.raises(vault.VaultError, match="wrong master key"):
        vault.boot(settings)


# -- rotation and the CLI ------------------------------------------------------------------------------


def test_rotation_re_encrypts_everything_under_the_new_key(db: Session) -> None:
    settings = get_settings()
    store.write_file(store.path_for(settings), _conns())
    secret = webhook.rotate_secret(db, "example-app")
    vault.startup(settings)
    old = vault.current().key.id

    out = CliRunner().invoke(cli, ["secrets", "rotate", "--yes"])
    assert out.exit_code == 0, out.output
    new = vault.current().key.id
    assert new != old

    text = store.path_for(settings).read_text()
    assert f"enc:v1:{new}:" in text and f"enc:v1:{old}:" not in text
    db.expire_all()
    assert webhook.reveal_secret(db, "example-app") == secret
    assert store.read_file(store.path_for(settings))[0].secrets["api_key"] == KEY
    assert not vault.key_path(settings).with_name("master.key.previous").exists()


def test_the_cli_reports_and_exports(db: Session) -> None:
    _plain_file(store.path_for(get_settings()))
    runner = CliRunner()
    out = runner.invoke(cli, ["secrets", "status"]).output
    assert "1 plain" in out and "secrets encrypt" in out

    assert runner.invoke(cli, ["secrets", "encrypt"]).exit_code == 0
    assert "Every stored secret is encrypted" in runner.invoke(cli, ["secrets", "status"]).output

    exported = runner.invoke(cli, ["secrets", "export-key", "--yes"]).output
    key = vault.current().key
    import base64

    assert base64.b64encode(key.raw).decode() in exported


# -- the session cookie key -----------------------------------------------------------------------------


def test_the_session_key_is_derived_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import config as config_module

    assert vault.session_secret(get_settings()) == "test-only"  # set explicitly (conftest)
    monkeypatch.delenv("SESSION_SECRET")
    config_module.reset_settings_cache()
    derived = vault.session_secret(get_settings())
    assert derived not in ("", "dev-only-change-me") and len(derived) == 64
    assert vault.session_secret(get_settings()) == derived  # stable


# -- going back to an older version ------------------------------------------------------------------


def test_decrypt_writes_everything_back_for_a_downgrade(db: Session) -> None:
    settings = get_settings()
    store.write_file(store.path_for(settings), _conns())
    secret = webhook.rotate_secret(db, "example-app")
    vault.startup(settings)

    out = CliRunner().invoke(cli, ["secrets", "decrypt", "--yes"])
    assert out.exit_code == 0, out.output
    assert KEY in store.path_for(settings).read_text()
    db.expire_all()
    assert db.get(Setting, "deploy.webhook.example-app").value["secret"] == secret  # type: ignore[union-attr]
    assert vault.recorded_key_id(db) is None


def test_plain_text_from_before_waits_until_an_update_cannot_roll_back(db: Session) -> None:
    from fastapi.testclient import TestClient

    path = store.path_for(get_settings())
    _plain_file(path)
    with TestClient(create_app()):
        # SECRETS_ENCRYPT_DELAY_S (300 s): an update that fails its health check is rolled back
        # to a version that reads plain text only, so nothing old is rewritten yet...
        assert KEY in path.read_text()
        # ...but the key is recorded at once, so losing it is noticed from now on.
        with Session(get_engine()) as s:
            assert vault.recorded_key_id(s) == vault.current().key.id
