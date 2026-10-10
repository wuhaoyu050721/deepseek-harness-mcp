"""完成事务、主对话路由、投递不确定性与恢复；不启动模型或外部消息。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import uuid

import pytest

from harness_mcp.errors import HarnessError
from harness_mcp.job_store import JobStore


@pytest.fixture
def store(tmp_path: Path):
    value = JobStore(tmp_path / "data")
    yield value
    value.close()


def origin(thread_id=None, *, pipe="codex-app-tools-test", turn_id=None):
    return {"thread_id": thread_id or str(uuid.uuid4()), "turn_id": turn_id or str(uuid.uuid4()),
            "host_id": "local", "pipe_path": "\\\\.\\pipe\\" + pipe}


def submit(store, request_id="test", *, route=None, project="demo", session_id=None, parallel=False):
    request = {"client_request_id": request_id, "project_id": project, "prompt": "api_key=never-store-this-in-an-event",
               "mode": "review", "timeout_seconds": 600}
    if session_id:
        request["session_id"] = session_id
    if parallel:
        return store.submit_parallel(request, request_id, {}, 5, 3, completion_origin=route)
    return store.submit(request, request_id, {}, 5, completion_origin=route)


def completed(store, route=None, request_id="test", status="succeeded"):
    route = route or origin()
    job, _ = submit(store, request_id, route=route)
    store.finish(job["job_id"], status, report_id="saved-report")
    return job, route, store.completion_get(job["job_id"])


@pytest.mark.parametrize("parallel", [False, True])
def test_acceptance_binds_origin_in_same_transaction_and_keeps_request_clean(store, parallel):
    route = origin()
    job, created = submit(store, route=route, parallel=parallel)
    assert created
    assert "pipe_path" not in json.dumps(job["request"])
    assert store.connection.execute("SELECT thread_id FROM completion_bindings WHERE job_id=?", (job["job_id"],)).fetchone()[0] == route["thread_id"]
    assert store.completion_get(job["job_id"]) is None
    store.finish(job["job_id"], "succeeded", report_id="saved-report")
    event = store.completion_get(job["job_id"])
    assert event["origin"] == route
    assert event["report_id"] == "saved-report"


def test_unbound_history_never_generates_completion_without_explicit_watch(store):
    job, _ = submit(store)
    store.finish(job["job_id"], "succeeded", report_id="saved-report")
    store.recover_interrupted(True)
    assert store.completion_pending() == []
    bound = store.bind_completion(job["job_id"], origin())
    assert bound["completion"]["status"] == "succeeded"
    assert bound["completion"]["report_id"] == "saved-report"
    assert len(store.completion_pending()) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "timed_out", "interrupted"])
def test_each_terminal_generates_one_safe_event_and_duplicate_finish_is_idempotent(store, status):
    job, route, event = completed(store, status=status)
    store.finish(job["job_id"], "failed", error={"code": "OTHER", "message": "sensitive-other-error"})
    store.bind_completion(job["job_id"], route)
    assert store.completion_get(job["job_id"]) == event
    assert event["status"] == status and event["state"] == "pending"
    serialized = json.dumps(event)
    assert "never-store-this" not in serialized and "sensitive-other-error" not in serialized
    assert "request_json" not in event and "error" not in event
    assert store.connection.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] == 1


def test_event_failure_rolls_back_terminal_job_write(store, monkeypatch):
    job, _ = submit(store, route=origin())
    previous = store.get(job["job_id"])
    def fail(cursor, job_id):
        raise RuntimeError("fixture insertion failure")
    monkeypatch.setattr(store, "_completion_event", fail)
    with pytest.raises(RuntimeError):
        store.finish(job["job_id"], "succeeded", report_id="saved-report")
    assert store.get(job["job_id"]) == previous
    assert store.connection.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] == 0


def test_binding_failure_rolls_back_submission_acceptance(store, monkeypatch):
    def fail(cursor, job_id, route):
        raise RuntimeError("fixture binding failure")
    monkeypatch.setattr(store, "_bind_completion", fail)
    with pytest.raises(RuntimeError):
        submit(store, route=origin())
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_queue_cancel_terminal_and_late_success_keep_cancellation(store):
    submit(store, "active")
    job, _ = submit(store, "queued", route=origin())
    assert job["status"] == "queued"
    store.request_cancel(job["job_id"])
    store.finish(job["job_id"], "succeeded", report_id="late-report")
    event = store.completion_get(job["job_id"])
    assert event["status"] == "cancelled" and event["report_id"] is None
    assert len(store.completion_pending()) == 1


@pytest.mark.parametrize("reason", ["cancelled", "timed_out", "interrupted"])
def test_running_stop_does_not_publish_before_confirmation_and_wins_late_success(store, reason):
    job, _ = submit(store, route=origin())
    store.set_running(job["job_id"], {})
    store.request_cancel(job["job_id"], reason)
    store.termination_failed(job["job_id"])
    assert store.completion_get(job["job_id"]) is None
    store.finish(job["job_id"], "succeeded", report_id="late-report")
    assert store.completion_get(job["job_id"])["status"] == reason


def test_restart_emits_only_bound_interrupted_jobs_and_reuses_events(tmp_path):
    data = tmp_path / "data"
    previous = JobStore(data)
    route = origin()
    finished, _, event = completed(previous, route, "finished")
    bound, _ = submit(previous, "bound", route=route)
    legacy, _ = submit(previous, "legacy")
    previous.close()
    restarted = JobStore(data)
    try:
        restarted.recover_interrupted(False)
        assert restarted.completion_get(bound["job_id"]) is None
        restarted.recover_interrupted(True)
        restarted.recover_interrupted(True)
        assert restarted.completion_get(finished["job_id"]) == event
        assert restarted.completion_get(bound["job_id"])["status"] == "interrupted"
        assert restarted.completion_get(legacy["job_id"]) is None
        assert len(restarted.completion_pending()) == 2
        replay, created = submit(restarted, "bound", route=route)
        assert not created and replay["status"] == "interrupted"
        assert restarted.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    finally:
        restarted.close()


@pytest.mark.parametrize("parallel", [False, True])
def test_origin_target_cannot_be_changed_by_idempotent_retry(store, parallel):
    job, _ = submit(store, route=origin(), parallel=parallel)
    with pytest.raises(HarnessError) as error:
        submit(store, route=origin(), parallel=parallel)
    assert error.value.code == "COMPLETION_ORIGIN_CONFLICT"
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert store.get(job["job_id"])["client_request_id"] == "test"


def test_same_target_can_refresh_pipe_but_preserves_original_turn_and_event(store):
    job, route, event = completed(store)
    changed = origin(route["thread_id"], pipe="new-desktop-pipe")
    replay = store.bind_completion(job["job_id"], changed)
    assert replay["origin"] == {**route, "pipe_path": changed["pipe_path"]}
    assert replay["completion"]["event_id"] == event["event_id"]
    assert replay["completion"]["origin"]["turn_id"] == route["turn_id"]


@pytest.mark.parametrize("change", [
    {"thread_id": "not-a-thread"}, {"turn_id": "line\nbreak"}, {"host_id": "remote"},
    {"pipe_path": "https://callback.example"}, {"pipe_path": "\\\\remote\\pipe\\codex"},
    {"pipe_path": "\\\\.\\pipe\\codex\\other"}, {"pipe_path": "\\\\.\\pipe\\codex:bad"},
    {"pipe_path": "\\\\.\\pipe\\codex\x00"},
])
def test_invalid_route_cannot_accept_a_new_model_job(store, change):
    with pytest.raises(HarnessError) as error:
        submit(store, route={**origin(), **change})
    assert error.value.code == "COMPLETION_ORIGIN_INVALID"
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_binding_requires_authorization_and_existing_job(store):
    job, _ = submit(store)
    with pytest.raises(HarnessError) as error:
        store.bind_completion(job["job_id"], origin(), authorization=False)
    assert error.value.code == "COMPLETION_AUTHORIZATION_REQUIRED"
    with pytest.raises(HarnessError) as error:
        store.bind_completion("missing", origin())
    assert error.value.code == "JOB_NOT_FOUND"


def test_concurrent_claims_have_single_owner_and_delivery_does_not_ack(store):
    job, route, event = completed(store)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda _: store.completion_claim(event["event_id"], now=100), range(24)))
    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 1 and claimed[0]["attempts"] == 1
    delivered = store.completion_delivered(event["event_id"], claimed[0]["lease_token"], now=110)
    assert delivered["state"] == "delivered" and delivered["acknowledged_at"] is None
    assert store.completion_delivered(event["event_id"], claimed[0]["lease_token"], now=111) == delivered
    ack = store.completion_ack(event["event_id"], route["thread_id"], now=120)
    assert ack["state"] == "acknowledged"
    assert store.completion_ack(event["event_id"], route["thread_id"], now=121) == ack
    assert store.completion_delivered(event["event_id"], claimed[0]["lease_token"], now=122) == ack


def test_expired_claim_is_uncertain_across_restart_and_never_blindly_retried(tmp_path):
    data = tmp_path / "data"
    previous = JobStore(data)
    job, _, event = completed(previous)
    claimed = previous.completion_claim(event["event_id"], lease_seconds=5, now=100)
    previous.close()
    restarted = JobStore(data)
    try:
        assert restarted.completion_pending(now=106) == []
        uncertain = restarted.completion_get(job["job_id"])
        assert uncertain["state"] == "uncertain" and uncertain["last_error_code"] == "DELIVERY_OUTCOME_UNKNOWN"
        assert restarted.completion_claim(event["event_id"], now=500) is None
        assert restarted.completion_pending(now=500) == []
        # 原发送方拿到确定接受回执后，仍可确认这个未改归属的投递。
        assert restarted.completion_delivered(event["event_id"], claimed["lease_token"], now=501)["state"] == "delivered"
    finally:
        restarted.close()


def test_known_not_sent_failure_retries_after_delay_and_rejects_stale_receipt(store):
    job, _, event = completed(store)
    first = store.completion_claim(event["event_id"], now=100)
    pending = store.completion_failed(event["event_id"], first["lease_token"], "PIPE_NOT_AVAILABLE", retry_after_seconds=5, now=101)
    assert pending["state"] == "pending"
    assert store.completion_pending(now=105) == []
    second = store.completion_claim(event["event_id"], now=106)
    assert second["attempts"] == 2 and second["lease_token"] != first["lease_token"]
    with pytest.raises(HarnessError) as error:
        store.completion_delivered(event["event_id"], first["lease_token"], now=107)
    assert error.value.code == "COMPLETION_LEASE_CONFLICT"
    assert store.completion_get(job["job_id"])["state"] == "claimed"


def test_lost_response_is_uncertain_even_before_lease_expires(store):
    _, _, event = completed(store)
    claimed = store.completion_claim(event["event_id"], now=100)
    failed = store.completion_failed(event["event_id"], claimed["lease_token"], "WAKE_RESPONSE_LOST", uncertain=True, now=101)
    assert failed["state"] == "uncertain"
    assert store.completion_pending(now=1000) == []
    assert store.completion_claim(event["event_id"], now=1000) is None


def test_main_ai_can_ack_before_notification_and_wrong_target_cannot_ack(store):
    _, route, event = completed(store)
    with pytest.raises(HarnessError) as error:
        store.completion_ack(event["event_id"], str(uuid.uuid4()))
    assert error.value.code == "COMPLETION_TARGET_MISMATCH"
    with pytest.raises(HarnessError) as error:
        store.completion_ack(event["event_id"], route["thread_id"], host_id="remote")
    assert error.value.code == "COMPLETION_TARGET_MISMATCH"
    assert store.completion_ack(event["event_id"], route["thread_id"])["state"] == "acknowledged"
    assert store.completion_pending() == []


def test_project_and_session_metadata_are_distinct_and_thread_filter_is_exact(store):
    first, second = origin(), origin()
    jobs = []
    for index, route in enumerate((first, first, second)):
        job, _ = submit(store, str(index), route=route, project=f"project-{index}", session_id=f"session-{index}", parallel=True)
        store.finish(job["job_id"], "succeeded")
        jobs.append(job)
    listed = store.completion_list(first["thread_id"])
    assert [item["project_id"] for item in listed] == ["project-1", "project-0"]
    assert [item["session_id"] for item in listed] == ["session-1", "session-0"]
    assert [item["job_id"] for item in store.completion_list(second["thread_id"])] == [jobs[2]["job_id"]]
    assert store.completion_list(first["thread_id"], host_id="remote") == []
    assert len(store.completion_list(first["thread_id"], limit=1)) == 1


def test_late_bind_and_finish_race_cannot_drop_or_duplicate_event(store):
    job, _ = submit(store)
    route = origin()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(store.bind_completion, job["job_id"], route),
                   executor.submit(store.finish, job["job_id"], "succeeded")]
        for future in futures:
            future.result()
    assert store.completion_get(job["job_id"])["status"] == "succeeded"
    assert store.connection.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] == 1


@pytest.mark.parametrize("event_id", [True, "1", 0, -1, 2**63])
def test_invalid_event_ids_raise_stable_error(store, event_id):
    with pytest.raises(HarnessError) as error:
        store.completion_claim(event_id)
    assert error.value.code == "COMPLETION_NOT_FOUND"


@pytest.mark.parametrize("value", [True, 0, 101, "20"])
def test_event_pages_are_bounded(store, value):
    with pytest.raises(HarnessError) as error:
        store.completion_pending(value)
    assert error.value.code == "INVALID_PAGE_SIZE"


def test_completion_wakeup_runs_on_new_event_and_pending_pipe_refresh_only(store):
    wakes = []
    store.completion_wakeup = lambda: wakes.append("wake")
    job, route, event = completed(store)
    assert len(wakes) == 1
    store.finish(job["job_id"], "succeeded")
    store.bind_completion(job["job_id"], route)
    assert len(wakes) == 1
    refreshed = {**route, "pipe_path": "\\\\.\\pipe\\restarted-codex"}
    store.bind_completion(job["job_id"], refreshed)
    assert len(wakes) == 2
    claimed = store.completion_claim(event["event_id"], now=100)
    store.completion_failed(event["event_id"], claimed["lease_token"], "NOT_SENT", now=101)
    assert len(wakes) == 2
    claimed = store.completion_claim(event["event_id"], now=106)
    store.completion_delivered(event["event_id"], claimed["lease_token"], now=107)
    store.completion_ack(event["event_id"], route["thread_id"], now=108)
    store.bind_completion(job["job_id"], route)
    assert len(wakes) == 2


def test_wakeup_callback_failure_cannot_lose_terminal_event_or_accepted_job(store):
    def failed_callback():
        raise RuntimeError("broken local notification bridge")
    store.completion_wakeup = failed_callback
    job, _, event = completed(store)
    assert event["status"] == "succeeded"
    assert store.get(job["job_id"])["status"] == "succeeded"
    assert len(store.completion_pending()) == 1


def test_queue_cancel_and_restart_completion_each_wake_once(store):
    submit(store, "active")
    first, _ = submit(store, "queued-first", route=origin())
    second, _ = submit(store, "queued-second", route=origin())
    wakes = []
    store.completion_wakeup = lambda: wakes.append("wake")
    store.request_cancel(first["job_id"])
    store.request_cancel(first["job_id"])
    store.recover_interrupted(True)
    store.recover_interrupted(True)
    assert len(wakes) == 2
    assert store.completion_get(second["job_id"])["status"] == "interrupted"


@pytest.mark.parametrize("authorization", [None, 1, "true"])
def test_non_boolean_authorization_does_not_bind(store, authorization):
    job, _ = submit(store)
    with pytest.raises(HarnessError) as error:
        store.bind_completion(job["job_id"], origin(), authorization)
    assert error.value.code == "COMPLETION_AUTHORIZATION_REQUIRED"


def test_missing_and_extra_origin_fields_are_rejected_before_acceptance(store):
    for route in ({}, {**origin(), "callback_url": "https://example.invalid"}):
        with pytest.raises(HarnessError) as error:
            submit(store, route=route)
        assert error.value.code == "COMPLETION_ORIGIN_INVALID"
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1])
def test_nonfinite_timestamps_rejected_before_state_change(store, value):
    job, _, event = completed(store)
    with pytest.raises(HarnessError) as error:
        store.completion_claim(event["event_id"], now=value)
    assert error.value.code == "COMPLETION_ARGUMENT_INVALID"
    assert store.completion_get(job["job_id"])["state"] == "pending"


def test_only_stable_error_code_is_persisted_in_delivery_failure(store):
    job, _, event = completed(store)
    claimed = store.completion_claim(event["event_id"], now=100)
    with pytest.raises(HarnessError) as error:
        store.completion_failed(event["event_id"], claimed["lease_token"], "raw api_key=value stack trace", now=101)
    assert error.value.code == "COMPLETION_ARGUMENT_INVALID"
    assert store.completion_get(job["job_id"])["last_error_code"] is None


@pytest.mark.parametrize("arguments", [{"state": []}, {"state": True}, {"host_id": {}}, {"thread_id": []}])
def test_invalid_list_filters_raise_stable_error(store, arguments):
    with pytest.raises(HarnessError) as error:
        store.completion_list(**{"thread_id": str(uuid.uuid4()), **arguments})
    assert error.value.code == "COMPLETION_ARGUMENT_INVALID"


def claude_origin(session_id=None, connection_id=None):
    return {"client": "claude_code", "connection_id": connection_id or str(uuid.uuid4()),
            "session_id": session_id or str(uuid.uuid4())}


def test_typed_origins_preserve_codex_contract_and_claude_identity():
    codex, claude = origin(), claude_origin()
    assert JobStore.normalized_origin(codex) == codex
    assert JobStore._completion_origin(codex) == codex
    assert JobStore.get_origin_identity(codex) == (codex["thread_id"], "local")
    assert JobStore.normalized_origin(claude) == claude
    assert JobStore._completion_origin(claude) == claude
    assert JobStore.get_origin_identity(claude) == (claude["session_id"], "claude_code")


@pytest.mark.parametrize("parallel", [False, True])
def test_claude_submission_binds_actual_connection_without_codex_fields(store, parallel):
    route = claude_origin()
    job, _ = submit(store, route=route, parallel=parallel)
    row = store.connection.execute("SELECT * FROM completion_bindings WHERE job_id=?", (job["job_id"],)).fetchone()
    assert row["client"] == "claude_code" and row["destination_id"] == route["session_id"]
    assert row["host_id"] == "claude_code" and row["pipe_path"] == "" and row["turn_id"] == ""
    assert json.loads(row["origin_json"]) == route
    assert "connection_id" not in json.dumps(job["request"])
    store.finish(job["job_id"], "succeeded")
    event = store.completion_get(job["job_id"])
    assert event["origin"] == route
    assert not {"client", "destination_id", "origin_json", "thread_id", "pipe_path"}.intersection(event)


def test_two_claude_frontends_are_isolated_even_with_same_actual_connection(store):
    shared_connection = str(uuid.uuid4())
    first, second = claude_origin(connection_id=shared_connection), claude_origin(connection_id=shared_connection)
    jobs = [submit(store, str(index), route=route, parallel=True)[0] for index, route in enumerate((first, second))]
    for job in jobs:
        store.finish(job["job_id"], "succeeded")
    assert [event["job_id"] for event in store.completion_list(first["session_id"], host_id="claude_code")] == [jobs[0]["job_id"]]
    assert [event["job_id"] for event in store.completion_list(second["session_id"], host_id="claude_code")] == [jobs[1]["job_id"]]
    event = store.completion_get(jobs[0]["job_id"])
    with pytest.raises(HarnessError) as error:
        store.completion_ack(event["event_id"], second["session_id"], host_id="claude_code")
    assert error.value.code == "COMPLETION_TARGET_MISMATCH"
    destination_id, namespace = JobStore.get_origin_identity(first)
    assert store.completion_ack(event["event_id"], destination_id, host_id=namespace)["state"] == "acknowledged"


def test_codex_and_claude_same_uuid_cannot_change_or_observe_each_others_target(store):
    shared_id = str(uuid.uuid4())
    codex, claude = origin(shared_id), claude_origin(session_id=shared_id)
    first, _ = submit(store, "codex", route=codex)
    second, _ = submit(store, "claude", route=claude)
    for job in (first, second):
        store.finish(job["job_id"], "succeeded")
    assert [event["job_id"] for event in store.completion_list(shared_id)] == [first["job_id"]]
    assert [event["job_id"] for event in store.completion_list(shared_id, host_id="claude_code")] == [second["job_id"]]
    assert store.completion_list(shared_id, host_id="codex") == []
    with pytest.raises(HarnessError) as error:
        store.bind_completion(first["job_id"], claude)
    assert error.value.code == "COMPLETION_ORIGIN_CONFLICT"
    with pytest.raises(HarnessError) as error:
        store.completion_ack(store.completion_get(second["job_id"])["event_id"], shared_id)
    assert error.value.code == "COMPLETION_TARGET_MISMATCH"


def test_claude_reconnect_can_refresh_only_the_same_frontend_session(store):
    first = claude_origin()
    job, _ = submit(store, route=first)
    store.finish(job["job_id"], "succeeded")
    event = store.completion_get(job["job_id"])
    wakes = []
    store.completion_wakeup = lambda: wakes.append("wake")
    refreshed = claude_origin(first["session_id"])
    replay = store.bind_completion(job["job_id"], refreshed)
    assert replay["origin"] == refreshed and replay["completion"]["event_id"] == event["event_id"]
    assert wakes == ["wake"]
    store.bind_completion(job["job_id"], refreshed)
    assert wakes == ["wake"]
    with pytest.raises(HarnessError) as error:
        store.bind_completion(job["job_id"], claude_origin(connection_id=refreshed["connection_id"]))
    assert error.value.code == "COMPLETION_ORIGIN_CONFLICT"
    assert store.completion_get(job["job_id"])["origin"] == refreshed


@pytest.mark.parametrize("state", ["delivered", "uncertain", "acknowledged"])
def test_claude_route_refresh_preserves_nonpending_delivery_state_without_replay(store, state):
    first = claude_origin()
    job, _ = submit(store, route=first)
    store.finish(job["job_id"], "succeeded")
    event = store.completion_get(job["job_id"])
    claim = store.completion_claim(event["event_id"], now=100)
    if state == "delivered":
        store.completion_delivered(event["event_id"], claim["lease_token"], now=101)
    elif state == "uncertain":
        store.completion_failed(event["event_id"], claim["lease_token"], "RESPONSE_LOST", uncertain=True, now=101)
    else:
        store.completion_ack(event["event_id"], first["session_id"], host_id="claude_code", now=101)
    before = store.completion_get(job["job_id"])
    wakes = []
    store.completion_wakeup = lambda: wakes.append("wake")
    refreshed = claude_origin(first["session_id"])
    store.bind_completion(job["job_id"], refreshed)
    after = store.completion_get(job["job_id"])
    assert after == {**before, "origin": refreshed}
    assert store.completion_pending(now=1000) == [] and wakes == []
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize("changes", [
    {"client": "claude_desktop"}, {"connection_id": "not-uuid"}, {"session_id": "not-uuid"},
    {"connection_id": None}, {"session_id": True}, {"pipe_path": "\\\\.\\pipe\\unexpected"},
    {"callback_url": "https://example.invalid"}, {"turn_id": str(uuid.uuid4())},
])
def test_invalid_or_mixed_claude_routes_cannot_accept_jobs(store, changes):
    with pytest.raises(HarnessError) as error:
        submit(store, route={**claude_origin(), **changes})
    assert error.value.code == "COMPLETION_ORIGIN_INVALID"
    assert store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_old_codex_binding_schema_migrates_without_changing_delivery_state_or_owner(tmp_path):
    data = tmp_path / "migration-data"
    previous = JobStore(data)
    route = origin()
    jobs, events = [], []
    for index, state in enumerate(("pending", "delivered", "uncertain", "acknowledged")):
        job, _, event = completed(previous, route, f"legacy-{index}")
        if state != "pending":
            claimed = previous.completion_claim(event["event_id"], now=100)
            if state == "delivered":
                previous.completion_delivered(event["event_id"], claimed["lease_token"], now=101)
            elif state == "uncertain":
                previous.completion_failed(event["event_id"], claimed["lease_token"], "OLD_RESPONSE_LOST", uncertain=True, now=101)
            else:
                previous.completion_ack(event["event_id"], route["thread_id"], now=101)
        jobs.append(job)
        events.append(previous.completion_get(job["job_id"]))
    owner = previous.owner_id
    # 重建上一版本的七列绑定表，保留真实 jobs/events/owner 来验证增量迁移。
    with previous.transaction() as cursor:
        cursor.execute("DROP INDEX completion_destination")
        cursor.execute("ALTER TABLE completion_bindings RENAME TO newer_bindings")
        cursor.execute("""CREATE TABLE completion_bindings (
            job_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            host_id TEXT NOT NULL, pipe_path TEXT NOT NULL, bound_at REAL NOT NULL,
            authorization INTEGER NOT NULL CHECK(authorization=1))""")
        cursor.execute("""INSERT INTO completion_bindings SELECT job_id,thread_id,turn_id,host_id,pipe_path,bound_at,
            authorization FROM newer_bindings""")
        cursor.execute("DROP TABLE newer_bindings")
    previous.close()
    reopened = JobStore(data)
    try:
        assert reopened.owner_id == owner
        assert [reopened.completion_get(job["job_id"]) for job in jobs] == events
        assert [event["job_id"] for event in reopened.completion_pending(now=1000)] == [jobs[0]["job_id"]]
        bindings = reopened.connection.execute("SELECT client,destination_id,origin_json FROM completion_bindings").fetchall()
        assert all(row["client"] == "codex" and row["destination_id"] == route["thread_id"] and json.loads(row["origin_json"]) == route for row in bindings)
        assert reopened.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4
    finally:
        reopened.close()
    # 迁移再次打开仍保持原事件，无补发或覆盖。
    twice = JobStore(data)
    try:
        assert [twice.completion_get(job["job_id"]) for job in jobs] == events
    finally:
        twice.close()


@pytest.mark.parametrize("delivery_status", ["accepted", "channel_written"])
def test_delivery_receipt_type_is_recorded_without_claiming_result_processed(store, delivery_status):
    route = claude_origin() if delivery_status == "channel_written" else origin()
    job, _, event = completed(store, route)
    claim = store.completion_claim(event["event_id"], now=100)
    delivered = store.completion_delivered(event["event_id"], claim["lease_token"], now=101, delivery_status=delivery_status)
    assert delivered["state"] == "delivered" and delivered["delivery_status"] == delivery_status
    assert delivered["acknowledged_at"] is None
    assert store.completion_delivered(event["event_id"], claim["lease_token"], now=102, delivery_status=delivery_status) == delivered
    identity, namespace = store.get_origin_identity(route)
    ack = store.completion_ack(event["event_id"], identity, host_id=namespace, now=103)
    assert ack["state"] == "acknowledged" and ack["delivery_status"] == delivery_status


@pytest.mark.parametrize("delivery_status", [None, True, [], "queued", "succeeded", "host_processed"])
def test_invalid_delivery_receipt_cannot_modify_claim(store, delivery_status):
    job, _, event = completed(store)
    claim = store.completion_claim(event["event_id"], now=100)
    with pytest.raises(HarnessError) as error:
        store.completion_delivered(event["event_id"], claim["lease_token"], now=101, delivery_status=delivery_status)
    assert error.value.code == "COMPLETION_ARGUMENT_INVALID"
    assert store.completion_get(job["job_id"]) == claim


def test_legacy_event_table_gains_nullable_receipt_without_changing_delivery_state(tmp_path):
    data = tmp_path / "legacy-events"
    previous = JobStore(data)
    job, _, event = completed(previous)
    claim = previous.completion_claim(event["event_id"], now=100)
    before = previous.completion_delivered(event["event_id"], claim["lease_token"], now=101)
    before["delivery_status"] = None
    with previous.transaction() as cursor:
        cursor.execute("DROP INDEX completion_pending")
        cursor.execute("ALTER TABLE completion_events RENAME TO events_with_receipt")
        cursor.execute("""CREATE TABLE completion_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
            lease_token TEXT, lease_until REAL, delivered_at REAL, uncertain_at REAL,
            acknowledged_at REAL, last_error_code TEXT)""")
        cursor.execute("""INSERT INTO completion_events SELECT event_id,job_id,state,created_at,attempts,next_attempt_at,
            lease_token,lease_until,delivered_at,uncertain_at,acknowledged_at,last_error_code FROM events_with_receipt""")
        cursor.execute("DROP TABLE events_with_receipt")
    previous.close()
    reopened = JobStore(data)
    try:
        assert reopened.completion_get(job["job_id"]) == before
        assert reopened.completion_pending(now=1000) == []
        assert reopened.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        reopened.close()
