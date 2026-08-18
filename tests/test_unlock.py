"""The local unlock channel: how a service-managed API receives its token.

Regression cover for the failure this channel exists to remove — a service
started under systemd parked forever on a console read, reporting "active"
while nothing was ever bound to its port.
"""

import os
import socket
import stat
import time

import pytest

from netapp_migration.interfaces.api.unlock import (UnlockListener,
                                                    default_socket_path,
                                                    request_unlock)
from netapp_migration.security.tokens import TokenStore

GLOBAL = "GlobalToken-unlock-1"


@pytest.fixture()
def store(tmp_path):
    store = TokenStore(str(tmp_path / "tokens.enc"))
    store.initialise(GLOBAL)
    store.upsert("q_fin", ["test", "clone"], "NEW_TOKEN")
    store.lock()
    return store


@pytest.fixture()
def listener(store, tmp_path):
    listener = UnlockListener(store, str(tmp_path / "unlock.sock"))
    listener.bind()
    listener.start()
    yield listener
    listener.stop()


# =============================================================================
# Nominal exchange
# =============================================================================

def test_correct_token_unlocks_the_store(store, listener):
    assert not store.unlocked

    answer = request_unlock(listener.path, GLOBAL)

    assert answer.startswith("OK")
    assert "1 delegated token" in answer
    assert store.unlocked, "the API becomes usable through the same object"


def test_second_unlock_is_a_no_op(store, listener):
    request_unlock(listener.path, GLOBAL)
    assert request_unlock(listener.path, GLOBAL) == "OK already unlocked"


def test_listener_survives_a_refusal(store, listener):
    assert request_unlock(listener.path, "wrong-token").startswith("ERR")
    assert not store.unlocked

    # A typo must not take the channel down: the admin retries.
    assert request_unlock(listener.path, GLOBAL).startswith("OK")
    assert store.unlocked


# =============================================================================
# Refusals
# =============================================================================

def test_wrong_token_is_refused_and_explained(store, listener):
    answer = request_unlock(listener.path, "not-the-global-token")
    assert answer.startswith("ERR")
    assert len(answer) > 4, "the refusal carries a reason, not just a code"
    assert not store.unlocked


def test_empty_token_is_refused(store, listener):
    assert request_unlock(listener.path, "") == "ERR empty token"
    assert not store.unlocked


def test_oversized_token_is_refused_without_reading_it_all(store, listener):
    answer = request_unlock(listener.path, "x" * 9000)
    assert answer == "ERR token too long"
    assert not store.unlocked


# =============================================================================
# Exposure
# =============================================================================

def test_socket_is_private_to_its_owner(listener):
    mode = stat.S_IMODE(os.stat(listener.path).st_mode)
    assert mode == 0o600, f"unlock socket is {oct(mode)}, must be 0600"


def test_socket_is_removed_on_stop(store, tmp_path):
    listener = UnlockListener(store, str(tmp_path / "gone.sock"))
    listener.bind()
    listener.start()
    assert os.path.exists(listener.path)

    listener.stop()
    for _ in range(50):                      # the accept loop wakes up ≤1s
        if not os.path.exists(listener.path):
            break
        time.sleep(0.02)
    assert not os.path.exists(listener.path)


def test_stale_socket_does_not_block_a_restart(store, tmp_path):
    path = str(tmp_path / "stale.sock")
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(path)                        # as a killed process would leave it
    orphan.close()
    assert os.path.exists(path)

    listener = UnlockListener(store, path)
    listener.bind()                          # must not raise EADDRINUSE
    listener.start()
    try:
        assert request_unlock(path, GLOBAL).startswith("OK")
    finally:
        listener.stop()


def test_default_path_sits_beside_the_token_store(tmp_path, monkeypatch):
    monkeypatch.delenv("NETAPP_UNLOCK_SOCKET", raising=False)
    store_path = str(tmp_path / "etc" / "netapp_tokens.enc")
    assert default_socket_path(store_path) == str(tmp_path / "etc" /
                                                  "unlock.sock")


def test_environment_overrides_the_default_path(tmp_path, monkeypatch):
    monkeypatch.setenv("NETAPP_UNLOCK_SOCKET", "/run/nm/u.sock")
    assert default_socket_path(str(tmp_path / "t.enc")) == "/run/nm/u.sock"


# =============================================================================
# CLI wiring
# =============================================================================

def test_cli_unlocks_a_running_api(store, listener, monkeypatch, capsys):
    from netapp_migration.interfaces import cli

    code = cli.main(["--action", "api-unlock",
                     "--unlock-socket", listener.path,
                     "--token", GLOBAL])

    assert code == 0
    assert "unlocked" in capsys.readouterr().out
    assert store.unlocked


def test_cli_says_so_when_the_api_is_not_running(tmp_path, capsys):
    from netapp_migration.interfaces import cli

    code = cli.main(["--action", "api-unlock",
                     "--unlock-socket", str(tmp_path / "absent.sock"),
                     "--token", GLOBAL])

    assert code == 1
    error = capsys.readouterr().err
    assert "no unlock socket" in error
    assert "--start-locked" in error, "the message must say how to fix it"


def test_cli_reports_a_refused_token(store, listener, capsys):
    from netapp_migration.interfaces import cli

    code = cli.main(["--action", "api-unlock",
                     "--unlock-socket", listener.path,
                     "--token", "wrong"])

    assert code == 1
    assert capsys.readouterr().err.startswith("ERROR:")
    assert not store.unlocked
