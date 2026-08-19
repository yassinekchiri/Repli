"""No failure may reach the caller as a bare traceback.

Regression cover for a real incident: a `creds.json` with an unescaped
character made POST /api/v1/migrations answer 500 with an ASGI traceback in
the log and nothing usable in the response. Configuration mistakes are the
operator's to fix, so they must be named, located, and hinted at.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from netapp_migration.config import CredentialsResolver
from netapp_migration.models import ConfigError

from test_api import CREATE_BODY, GLOBAL_TOKEN, api            # noqa: F401


# =============================================================================
# The resolver
# =============================================================================

def test_malformed_json_is_located_not_dumped(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    # Exactly the shape of the reported incident: a quote inside the password.
    path.write_text('{\n  "defaults": {\n'
                    '    "username": "mutrepli",\n'
                    '    "password": "pa"ss",\n'
                    '    "verify_ssl": false\n  }\n}\n')
    monkeypatch.setenv("NETAPP_MIGRATION_CONFIG", str(path))

    with pytest.raises(ConfigError) as raised:
        CredentialsResolver()

    assert "not valid JSON" in raised.value.message
    assert "line 4" in raised.value.message, "the operator needs the location"
    assert str(path) in raised.value.message
    assert "json.tool" in raised.value.hint


def test_missing_file_says_where_it_looked(tmp_path, monkeypatch):
    absent = tmp_path / "nowhere" / "creds.json"
    monkeypatch.setenv("NETAPP_MIGRATION_CONFIG", str(absent))

    with pytest.raises(ConfigError) as raised:
        CredentialsResolver()

    assert str(absent) in raised.value.message
    assert "NETAPP_MIGRATION_CONFIG" in raised.value.hint


def test_json_that_is_not_an_object_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    path.write_text('["mutrepli", "secret"]')
    monkeypatch.setenv("NETAPP_MIGRATION_CONFIG", str(path))

    with pytest.raises(ConfigError) as raised:
        CredentialsResolver()
    assert "JSON object" in raised.value.message


def test_a_valid_file_still_loads(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({
        "defaults": {"username": "mutrepli", "password": 'pa"ss\\word',
                     "verify_ssl": False},
        "clusters": {"CLU1": {}}}))
    monkeypatch.setenv("NETAPP_MIGRATION_CONFIG", str(path))

    credentials = CredentialsResolver()("CLU1")

    assert credentials.username == "mutrepli"
    assert credentials.password == 'pa"ss\\word', "escaping survives the round trip"
    assert credentials.verify_ssl is False


def test_no_config_file_configured_is_not_an_error(monkeypatch):
    """Credentials can come from the environment alone."""
    monkeypatch.delenv("NETAPP_MIGRATION_CONFIG", raising=False)
    monkeypatch.setenv("NETAPP_API_USER", "mutrepli")
    monkeypatch.setenv("NETAPP_API_PASSWORD", "secret")

    assert CredentialsResolver()("CLU1").username == "mutrepli"


# =============================================================================
# What the API answers
# =============================================================================

@pytest.fixture()
def unstubbed_api(tmp_path, monkeypatch):
    """The app with its REAL engine builder.

    The `api` fixture replaces _engine_for with one wired onto the fake
    estate — right for behaviour tests, useless here: the incident happened
    inside the real builder, when it read the credentials file.
    raise_server_exceptions=False so the handlers' responses are observable
    instead of being re-raised into the test.
    """
    import netapp_migration.interfaces.api.app as app_module
    from netapp_migration.core.jobs import JobStore
    from netapp_migration.security.tokens import TokenStore

    tokens = TokenStore(str(tmp_path / "tokens.enc"))
    tokens.initialise(GLOBAL_TOKEN)
    monkeypatch.setattr(app_module, "_store", JobStore(str(tmp_path / "jobs")))
    monkeypatch.setattr(app_module, "_tokens", tokens)
    monkeypatch.setattr(app_module, "_runs", {})

    http = TestClient(app_module.app, raise_server_exceptions=False)
    http.headers.update({"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    return http


def test_broken_creds_answers_503_not_500(unstubbed_api, tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    path.write_text('{"defaults": {"password": "oops",}}')     # trailing comma
    monkeypatch.setenv("NETAPP_MIGRATION_CONFIG", str(path))

    body = dict(CREATE_BODY)
    body["dry_run"] = False            # dry-run never reads the credentials

    response = unstubbed_api.post("/api/v1/migrations", json=body)

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "configuration"
    assert "not valid JSON" in detail["message"]
    assert detail["path"] == str(path)
    assert detail["hint"], "a configuration error must say what to do"


def test_unexpected_failures_answer_a_structured_500(unstubbed_api, monkeypatch):
    """Anything unforeseen still gets a stable shape and a log reference."""
    from netapp_migration.interfaces.api import app as app_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(app_module, "_ensure_feasible", explode)

    response = unstubbed_api.post("/api/v1/migrations", json=CREATE_BODY)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error"] == "internal"
    assert "RuntimeError" in detail["message"], "the type tells a bug from a typo"
    assert len(detail["reference"]) == 12, "quotable in a support request"
    assert detail["reference"] in detail["hint"]
