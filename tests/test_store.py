from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path

import pytest

from harness_mcp.config import AppConfig, ModelConfig, ProjectConfig, frozen_execution, normalize_request, request_fingerprint
from harness_mcp.errors import HarnessError
from harness_mcp.job_store import JobStore


@pytest.fixture
def store(tmp_path: Path):
    value = JobStore(tmp_path / "data")
    yield value
    value.close()


def request(request_id: str, prompt: str = "review") -> dict:
    return {"project_id": "demo", "prompt": prompt, "client_request_id": request_id, "relative_paths": None, "timeout_seconds": 600}


def submit(store: JobStore, request_id: str, prompt: str = "review", execution=None):
    payload = request(request_id, prompt)
    return store.submit(payload, request_fingerprint(payload), execution or {"model": {"credential_source": "env", "credential_env": "TEST_KEY"}}, 5)


def test_one_active_plus_five_waiting_and_idempotency_precedes_capacity(store):
    jobs = [submit(store, f"req-{i}")[0] for i in range(6)]
    assert jobs[0]["status"] == "preparing"
    assert [j["status"] for j in jobs[1:]] == ["queued"] * 5
    with pytest.raises(HarnessError) as exc:
        submit(store, "overflow")
    assert exc.value.code == "QUEUE_FULL"
    replay, created = submit(store, "req-5", execution={"model": {"model": "changed-admin-config"}})
    assert not created and replay["job_id"] == jobs[-1]["job_id"]
    assert replay["execution"] == jobs[-1]["execution"]
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6


def test_idempotency_conflict_precedes_capacity_and_does_not_change_original(store):
    original = submit(store, "req")[0]
    for i in range(5):
        submit(store, f"waiting-{i}")
    with pytest.raises(HarnessError) as exc:
        submit(store, "req", "different prompt")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert store.get(original["job_id"])["request"]["prompt"] == "review"


def test_pending_backends_preserve_legacy_docker_and_frozen_desktop(store):
    old = submit(store, "old")[0]
    native = submit(store, "native", execution={"executor": {"backend": "desktop"}})[0]
    assert store.pending_backends() == {"docker", "desktop"}
    store.finish(old["job_id"], "failed")
    assert store.pending_backends() == {"desktop"}
    store.finish(native["job_id"], "cancelled")
    assert not store.pending_backends()


def test_fifo_promotes_exactly_one_waiting_job_and_accepts_new_waiter(store):
    jobs = [submit(store, f"req-{i}")[0] for i in range(6)]
    store.finish(jobs[0]["job_id"], "succeeded")
    later = submit(store, "later")[0]
    assert later["status"] == "queued"
    assert store.get(jobs[1]["job_id"])["status"] == "preparing"
    for expected in jobs[1:] + [later]:
        active = store.next_job()
        assert active["job_id"] == expected["job_id"]
        assert active["status"] == "preparing"
        store.finish(active["job_id"], "succeeded")
    assert store.next_job() is None
    assert not store.has_pending()


def test_concurrent_same_request_creates_one_record(store):
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: submit(store, "same-request"), range(32)))
    assert len({job["job_id"] for job, _ in values}) == 1
    assert sum(created for _, created in values) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_queued_cancel_is_terminal_idempotent_and_frees_capacity(store):
    jobs = [submit(store, f"req-{i}")[0] for i in range(6)]
    cancelled = store.request_cancel(jobs[2]["job_id"])
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"] is not None
    assert store.request_cancel(jobs[2]["job_id"]) == cancelled
    assert store.finish(jobs[2]["job_id"], "succeeded")["status"] == "cancelled"
    assert submit(store, "replacement")[0]["status"] == "queued"


@pytest.mark.parametrize("reason", ["cancelled", "timed_out", "interrupted"])
def test_running_stop_request_takes_priority_over_late_success(store, reason):
    job = submit(store, "req")[0]
    assert store.set_running(job["job_id"], {"snapshot_id": "id"})
    stopping = store.request_cancel(job["job_id"], reason)
    assert stopping["status"] == "cancelling" and stopping["finished_at"] is None
    assert not store.set_running(job["job_id"], {})
    terminal = store.finish(job["job_id"], "succeeded", report_id="actual-report-digest")
    assert terminal["status"] == reason
    assert terminal["finished_at"] is not None
    assert terminal["report_id"] == "actual-report-digest"


def test_terminal_job_cannot_be_overwritten_by_error_or_later_activity(store):
    job = submit(store, "req")[0]
    done = store.finish(job["job_id"], "failed", error={"code": "MODEL_FAILED", "message": "safe"})
    store.activity(job["job_id"], "late event")
    store.termination_failed(job["job_id"])
    assert store.finish(job["job_id"], "succeeded") == done
    assert store.get(job["job_id"]) == done


def test_termination_failure_blocks_new_work_and_scheduler_but_not_idempotent_retry(store):
    active = submit(store, "active")[0]
    queued = submit(store, "waiting")[0]
    store.request_cancel(active["job_id"])
    store.termination_failed(active["job_id"])
    assert store.get(active["job_id"])["status"] == "cancelling"
    assert store.get(active["job_id"])["termination_failed"]
    assert store.next_job() is None
    with pytest.raises(HarnessError) as exc:
        submit(store, "new")
    assert exc.value.code == "EXECUTION_BLOCKED"
    replay, created = submit(store, "waiting")
    assert not created and replay["job_id"] == queued["job_id"]
    store.finish(active["job_id"], "cancelled")
    assert store.next_job()["job_id"] == queued["job_id"]


def test_termination_failure_without_prior_cancel_can_reach_terminal_after_stop_confirmed(store):
    job = submit(store, "active")[0]
    store.set_running(job["job_id"], {})
    store.termination_failed(job["job_id"])
    stopped = store.finish(job["job_id"], "failed")
    assert stopped["status"] in {"failed", "interrupted", "cancelled"}
    assert not stopped["termination_failed"]


def test_restart_marks_all_old_nonterminal_interrupted_without_replay(tmp_path):
    data = tmp_path / "state"
    previous = JobStore(data)
    jobs = [submit(previous, f"req-{i}")[0] for i in range(4)]
    succeeded = previous.finish(jobs[0]["job_id"], "succeeded", report_id="report-kept")
    previous.next_job()
    owner = previous.owner_id
    previous.close()
    restarted = JobStore(data)
    try:
        assert restarted.owner_id == owner
        restarted.recover_interrupted(stopped=True)
        assert restarted.get(succeeded["job_id"])["status"] == "succeeded"
        assert restarted.get(succeeded["job_id"])["report_id"] == "report-kept"
        assert {restarted.get(job["job_id"])["status"] for job in jobs[1:]} == {"interrupted"}
        assert restarted.next_job() is None
        assert not restarted.has_pending()
        replay, created = submit(restarted, "req-1")
        assert not created and replay["status"] == "interrupted"
    finally:
        restarted.close()


def test_restart_stop_failure_preserves_block_and_does_not_replay(store):
    active = submit(store, "active")[0]
    waiting = submit(store, "waiting")[0]
    store.recover_interrupted(stopped=False)
    assert store.next_job() is None
    for job in (active, waiting):
        restored = store.get(job["job_id"])
        assert restored["status"] == "cancelling" and restored["termination_failed"]
        assert restored["cancel_reason"] == "interrupted"
    with pytest.raises(HarnessError) as exc:
        submit(store, "new")
    assert exc.value.code == "EXECUTION_BLOCKED"


def test_model_credentials_never_appear_in_sqlite_or_wal(tmp_path, monkeypatch):
    secret = "test-local-secret-must-not-be-in-job-database"
    monkeypatch.setenv("TEST_HARNESS_SECRET", secret)
    config = AppConfig(tmp_path / "state", {"demo": ProjectConfig("demo", tmp_path)}, model=ModelConfig(credential_source="env", credential_env="TEST_HARNESS_SECRET"))
    value = JobStore(config.data_dir)
    try:
        payload = normalize_request(config, "demo", "review", "request")
        execution = frozen_execution(config, "demo")
        job, _ = value.submit(payload, request_fingerprint(payload), execution, 5)
        assert secret not in json.dumps(job)
        for path in config.data_dir.glob("jobs.sqlite3*"):
            assert secret.encode() not in path.read_bytes()
    finally:
        value.close()


def test_sql_parameters_are_not_interpolated_into_queries(store):
    with pytest.raises(HarnessError) as exc:
        store.get("' OR 1=1 --")
    assert exc.value.code == "JOB_NOT_FOUND"
    assert submit(store, "valid")[1]
