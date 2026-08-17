"""Clone naming: the client names every target volume, no generated suffix."""

import pytest

from netapp_migration.core.engine import MigrationEngine
from netapp_migration.core.preflight import PreflightChecker
from netapp_migration.models import PreflightFailed
from netapp_migration.security import csvio

from conftest import cascade_ready


def failing(report):
    return {c.code for c in report.failures}


# =============================================================================
# CSV parsing
# =============================================================================

def test_volume_map_csv():
    mapping = csvio.parse_volume_map_csv(
        "qtree,volume\nq_fin,vol_finance\nq_hr,vol_rh\n")
    assert mapping == {"q_fin": "vol_finance", "q_hr": "vol_rh"}


def test_volume_map_csv_rejects_duplicate_volume():
    with pytest.raises(ValueError) as err:
        csvio.parse_volume_map_csv(
            "qtree,volume\nq_fin,vol_same\nq_hr,vol_same\n")
    assert "already used" in str(err.value)


def test_volume_map_csv_rejects_missing_name():
    with pytest.raises(ValueError) as err:
        csvio.parse_volume_map_csv("qtree,volume\nq_fin,\n")
    assert "line 2" in str(err.value)


def test_volume_map_csv_tolerates_bom_and_semicolons():
    mapping = csvio.parse_volume_map_csv("﻿qtree;volume\nq_fin;vol_fin\n")
    assert mapping == {"q_fin": "vol_fin"}


# =============================================================================
# Pre-flight
# =============================================================================

def _ready_job(store, params, client):
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def test_missing_mapping_is_refused(client, params, logger, store):
    job = _ready_job(store, params, client)
    report = PreflightChecker(client, params, logger).for_test(job, ["q_fin"])
    assert "VOLUME_MAP_MISSING" in failing(report)


def test_illegal_volume_name_is_refused(client, params, logger, store):
    job = _ready_job(store, params, client)
    report = PreflightChecker(client, params, logger).for_test(
        job, ["q_fin"], volume_map={"q_fin": "vol-with-dashes"})
    assert "VOLUME_NAME_ILLEGAL" in failing(report)


def test_duplicate_target_names_are_refused(client, params, logger, store):
    job = _ready_job(store, params, client)
    report = PreflightChecker(client, params, logger).for_test(
        job, ["q_fin", "q_hr"],
        volume_map={"q_fin": "vol_same", "q_hr": "vol_same"})
    assert "VOLUME_MAP_DUPLICATE" in failing(report)


def test_name_already_taken_is_refused(client, params, logger, store):
    job = _ready_job(store, params, client)
    client.add_volume("PRD", "svm_dest", "vol_fin")
    report = PreflightChecker(client, params, logger).for_test(
        job, ["q_fin"], volume_map={"q_fin": "vol_fin"})
    assert "VOLUME_NAME_TAKEN" in failing(report)


def test_valid_mapping_passes(client, params, logger, store):
    job = _ready_job(store, params, client)
    report = PreflightChecker(client, params, logger).for_test(
        job, ["q_fin", "q_hr"],
        volume_map={"q_fin": "vol_finance", "q_hr": "vol_rh"})
    assert report.ok, report.summary()


# =============================================================================
# Engine
# =============================================================================

def test_test_creates_the_named_volumes(client, params, store, logger):
    engine = MigrationEngine(client, params, store, logger)
    job = _ready_job(store, params, client)
    result = engine.test("q_fin,q_hr", job=job,
                         volume_map={"q_fin": "vol_finance", "q_hr": "vol_rh"})

    assert result["clone_volumes"] == ["vol_finance", "vol_rh"]
    assert "create_clone PRD svm_dest:vol_finance" in client.calls
    assert "create_clone DRC svm_dr:vol_rh" in client.calls
    # no generated suffix anywhere
    assert not any("v_q_fin_" in call for call in client.calls)
    # the mapping is recorded so the promotion can reuse it
    assert store.load(job["job_id"])["volume_map"] == {
        "q_fin": "vol_finance", "q_hr": "vol_rh"}


def test_engine_refuses_an_unmapped_qtree(client, params, store, logger):
    engine = MigrationEngine(client, params, store, logger)
    job = _ready_job(store, params, client)
    with pytest.raises(PreflightFailed):
        engine.test("q_fin,q_hr", job=job, volume_map={"q_fin": "vol_finance"})
    assert not any(c.startswith("create_clone") for c in client.calls)


def test_promotion_reuses_the_recorded_names(client, params, store, logger):
    engine = MigrationEngine(client, params, store, logger)
    job = _ready_job(store, params, client)
    engine.test("q_fin", job=job, volume_map={"q_fin": "vol_finance"})
    client.calls.clear()

    result = engine.clone("q_fin", job=job)
    assert result["promoted"] is True
    assert result["clone_volumes"] == ["vol_finance"]
    # promotion only moves; it never recreates a clone
    assert not any(c.startswith("create_clone") for c in client.calls)
    assert any(c.startswith("volume_move") and "vol_finance" in c
               for c in client.calls)


def test_full_clone_without_test_env(client, params, store, logger):
    engine = MigrationEngine(client, params, store, logger)
    job = _ready_job(store, params, client)
    result = engine.clone("q_ops", job=job, volume_map={"q_ops": "vol_ops"})
    assert result["clone_volumes"] == ["vol_ops"]
    assert "create_clone PRD svm_dest:vol_ops" in client.calls
