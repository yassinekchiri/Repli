"""Token store, scopes, and their enforcement on the API."""

import json

import pytest
from fastapi.testclient import TestClient

from netapp_migration.models import AuthError, ForbiddenError, Principal
from netapp_migration.security import csvio
from netapp_migration.security.tokens import TokenStore

from test_api import CREATE_BODY, GLOBAL_TOKEN, api            # noqa: F401
from conftest import cascade_ready


SCOPE_CSV = """qtree,token,actions,label
q_fin,NEW_TOKEN,"test,clone,acl",Finance
q_hr,NEW_TOKEN,test,HR
"""


# =============================================================================
# Encrypted store
# =============================================================================

def test_store_is_encrypted_and_leaks_nothing(tmp_path):
    path = str(tmp_path / "t.enc")
    store = TokenStore(path)
    store.initialise("GlobalToken12345")
    issued = store.upsert("q_fin", ["test", "clone"], "NEW_TOKEN")["token"]

    raw = open(path).read()
    assert issued not in raw, "a delegated token must never hit the disk"
    assert "GlobalToken12345" not in raw, "the global token is never stored"
    assert "q_fin" not in raw, "the payload as a whole is encrypted"
    assert json.loads(raw)["format"] == "netapp-migration-tokens"


def test_store_file_is_private(tmp_path):
    import os
    import stat
    path = str(tmp_path / "t.enc")
    TokenStore(path).initialise("GlobalToken12345")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_wrong_global_token_cannot_open(tmp_path):
    path = str(tmp_path / "t.enc")
    TokenStore(path).initialise("GlobalToken12345")
    with pytest.raises(AuthError) as err:
        TokenStore(path).unlock("NotTheRightOne")
    assert "invalid global token" in err.value.message


def test_store_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.enc")
    first = TokenStore(path)
    first.initialise("GlobalToken12345")
    token = first.upsert("q_fin", ["test"], "NEW_TOKEN")["token"]

    reopened = TokenStore(path)                    # simulates an API restart
    assert not reopened.unlocked
    with pytest.raises(AuthError):
        reopened.authenticate(token)               # locked -> refused
    reopened.unlock("GlobalToken12345")
    assert reopened.authenticate(token).qtrees == ["q_fin"]


def test_global_token_authenticates_as_super_admin(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    assert store.authenticate("GlobalToken12345").is_super_admin


def test_minimum_global_token_length(tmp_path):
    with pytest.raises(AuthError):
        TokenStore(str(tmp_path / "t.enc")).initialise("short")


# =============================================================================
# CSV import
# =============================================================================

def test_new_token_generates_one_per_line(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    rows = csvio.parse_scope_csv(SCOPE_CSV)
    results = [store.upsert(r["qtree"], r["actions"], r["token"], r["label"])
               for r in rows]
    assert all(r["token"].startswith("mtk_") for r in results)
    assert results[0]["token"] != results[1]["token"]
    assert {s.qtrees[0] for s in store.list_scopes()} == {"q_fin", "q_hr"}


def test_existing_token_extends_its_scope(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    token = store.upsert("q_fin", ["test"], "NEW_TOKEN")["token"]
    outcome = store.upsert("q_ops", ["test", "acl"], token)
    assert outcome["status"] == "updated"
    assert outcome["token"] == "", "an existing token is never re-emitted"
    scope = store.get_scope(outcome["token_id"])
    assert scope.qtrees == ["q_fin", "q_ops"]


def test_unknown_token_in_csv_is_refused(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    with pytest.raises(AuthError) as err:
        store.upsert("q_fin", ["test"], "mtk_thisisnotaknowntoken")
    assert "NEW_TOKEN" in err.value.hint


def test_super_only_actions_cannot_be_delegated(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    with pytest.raises(AuthError) as err:
        store.upsert("q_fin", ["create"], "NEW_TOKEN")
    assert "super admin" in err.value.hint


def test_csv_errors_point_at_the_line():
    with pytest.raises(ValueError) as err:
        csvio.parse_scope_csv("qtree,token,actions\nq_fin,NEW_TOKEN,\n")
    assert "line 2" in str(err.value)

    with pytest.raises(ValueError) as err:
        csvio.parse_scope_csv("qtree,actions\nq_fin,test\n")
    assert "token" in str(err.value)


def test_scope_csv_accepts_semicolons():
    rows = csvio.parse_scope_csv("qtree;token;actions\nq_fin;NEW_TOKEN;test\n")
    assert rows[0]["qtree"] == "q_fin"


# =============================================================================
# Scope enforcement
# =============================================================================

def test_scope_boundaries():
    p = Principal(qtrees=["q_fin"], actions=["test", "clone"], label="Fin")
    p.authorise("test", ["q_fin"])                      # allowed
    with pytest.raises(ForbiddenError):
        p.authorise("test", ["q_hr"])                   # other qtree
    with pytest.raises(ForbiddenError):
        p.authorise("acl", ["q_fin"])                   # action not granted
    with pytest.raises(ForbiddenError):
        p.authorise("create")                           # super-admin only


def test_scope_can_be_changed_dynamically(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    outcome = store.upsert("q_fin", ["test"], "NEW_TOKEN")
    token, token_id = outcome["token"], outcome["token_id"]

    store.set_scope(token_id, qtrees=["q_fin", "q_ops"],
                    actions=["test", "clone"])
    principal = store.authenticate(token)               # same token
    assert principal.qtrees == ["q_fin", "q_ops"]
    principal.authorise("clone", ["q_ops"])


def test_revoked_token_stops_working(tmp_path):
    store = TokenStore(str(tmp_path / "t.enc"))
    store.initialise("GlobalToken12345")
    outcome = store.upsert("q_fin", ["test"], "NEW_TOKEN")
    store.revoke(outcome["token_id"])
    with pytest.raises(AuthError):
        store.authenticate(outcome["token"])


# =============================================================================
# API enforcement
# =============================================================================

def _scoped_token(http, csv_text=SCOPE_CSV):
    answer = http.post("/api/v1/auth/scopes/import",
                       json={"csv": csv_text}).json()
    rows = csvio.parse_scope_csv(answer["csv"])
    return {r["qtree"]: r["token"] for r in rows}


def test_api_refuses_without_token(api):
    http, _, _, _ = api
    http.headers.pop("Authorization")
    assert http.get("/api/v1/migrations").status_code == 401


def test_api_refuses_unknown_token(api):
    http, _, _, _ = api
    http.headers.update({"Authorization": "Bearer mtk_nope"})
    r = http.get("/api/v1/migrations")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthenticated"


def test_health_stays_public(api):
    http, _, _, _ = api
    http.headers.pop("Authorization")
    body = http.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["auth"] == {"initialised": True, "unlocked": True}


def test_locked_store_answers_503(api):
    http, _, _, tokens = api
    tokens.lock()                                    # simulates a restart
    r = http.get("/api/v1/migrations")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "locked"
    assert "global token" in r.json()["detail"]["hint"]


def test_whoami_reports_the_scope(api):
    http, _, _, _ = api
    tokens = _scoped_token(http)
    http.headers.update({"Authorization": f"Bearer {tokens['q_fin']}"})
    body = http.get("/api/v1/auth/whoami").json()
    assert body["super_admin"] is False
    assert body["qtrees"] == ["q_fin"]
    assert set(body["actions"]) == {"acl", "clone", "test"}


def test_scoped_token_cannot_create(api):
    http, _, fake, _ = api
    tokens = _scoped_token(http)
    http.headers.update({"Authorization": f"Bearer {tokens['q_fin']}"})
    r = http.post("/api/v1/migrations", json=CREATE_BODY)
    assert r.status_code == 403
    assert fake.calls == []


def test_scoped_token_cannot_touch_another_qtree(api):
    http, store, fake, _ = api
    from netapp_migration.models import MigrationParams
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)

    tokens = _scoped_token(http)
    http.headers.update({"Authorization": f"Bearer {tokens['q_hr']}"})
    r = http.post(f"/api/v1/migrations/{job['job_id']}/test",
                  json={"qtrees": "q_fin", "volume_map": {"q_fin": "vol_fin"}})
    assert r.status_code == 403
    assert "q_fin" in r.json()["detail"]["message"]
    assert fake.calls == []


def test_scoped_token_may_run_its_own_qtree(api):
    http, store, fake, _ = api
    from netapp_migration.models import MigrationParams
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)

    tokens = _scoped_token(http)
    http.headers.update({"Authorization": f"Bearer {tokens['q_hr']}"})
    r = http.post(f"/api/v1/migrations/{job['job_id']}/preflight/test",
                  json={"qtrees": "q_hr", "volume_map": {"q_hr": "vol_rh"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_scoped_token_cannot_administer_tokens(api):
    http, _, _, _ = api
    tokens = _scoped_token(http)
    http.headers.update({"Authorization": f"Bearer {tokens['q_fin']}"})
    assert http.get("/api/v1/auth/scopes").status_code == 403
    assert http.post("/api/v1/auth/scopes/import",
                     json={"csv": SCOPE_CSV}).status_code == 403


def test_scope_update_through_the_api(api):
    http, _, _, _ = api
    issued = _scoped_token(http)
    listed = http.get("/api/v1/auth/scopes").json()
    token_id = next(s["token_id"] for s in listed if s["qtrees"] == ["q_hr"])

    http.patch(f"/api/v1/auth/scopes/{token_id}",
               json={"qtrees": "q_hr,q_ops", "actions": "test,acl"})

    http.headers.update({"Authorization": f"Bearer {issued['q_hr']}"})
    body = http.get("/api/v1/auth/whoami").json()
    assert body["qtrees"] == ["q_hr", "q_ops"]
    assert set(body["actions"]) == {"acl", "test"}


def test_revocation_through_the_api(api):
    http, _, _, _ = api
    issued = _scoped_token(http)
    listed = http.get("/api/v1/auth/scopes").json()
    token_id = next(s["token_id"] for s in listed if s["qtrees"] == ["q_fin"])
    assert http.delete(f"/api/v1/auth/scopes/{token_id}").status_code == 204

    http.headers.update({"Authorization": f"Bearer {issued['q_fin']}"})
    assert http.get("/api/v1/auth/whoami").status_code == 401


def test_import_answer_carries_the_generated_tokens(api):
    http, _, _, _ = api
    answer = http.post("/api/v1/auth/scopes/import",
                       json={"csv": SCOPE_CSV}).json()
    assert answer["created"] == 2
    rows = csvio.parse_scope_csv(answer["csv"])
    assert all(r["token"].startswith("mtk_") for r in rows)
    # the listing itself never exposes a token
    assert all("token" not in entry for entry in answer["tokens"])


def test_invalid_scope_csv_is_reported_with_its_line(api):
    http, _, _, _ = api
    r = http.post("/api/v1/auth/scopes/import",
                  json={"csv": "qtree,token,actions\nq_fin,NEW_TOKEN,create\n"})
    assert r.status_code == 422
    assert "line 2" in r.json()["detail"]["message"]
