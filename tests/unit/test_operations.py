from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError


def request(op="op-1", command="create_document", **changes):
    value = {
        "bridge_protocol": 1,
        "instance_id": "instance-a",
        "operation_id": op,
        "command": command,
        "params": {"width": 64, "height": 64, "name": "Scratch"}
        if command == "create_document"
        else {},
    }
    value.update(changes)
    return value


class Clock:
    now = 10.0

    def __call__(self):
        return self.now


def assert_code(code, callback):
    with pytest.raises(BridgeError) as error:
        callback()
    assert error.value.code == code


def test_retry_deduplicates_inflight_completed_and_expired_results():
    clock = Clock()
    ledger = OperationLedger("instance-a", clock=clock, result_ttl=1)
    body = request()
    assert ledger.admit(body)["state"] == "queued"
    assert ledger.admit({**body, "queue_timeout_ms": 999})["state"] == "queued"
    assert ledger.take_next()["operation_id"] == "op-1"
    assert ledger.admit(body)["state"] == "running"
    assert ledger.admit(body)["effect"] == "unknown"
    result = {"document_id": "doc-1", "details": {"a": 1}}
    ledger.finish("op-1", result=result)
    result["details"]["a"] = 4
    assert ledger.admit(body)["result"]["details"]["a"] == 1
    clock.now += 2
    expired = ledger.admit(body)
    assert expired["state"] == "succeeded"
    assert expired["effect"] == "applied"
    assert expired["result"] is None
    assert expired["error"]["code"] == "RESULT_EXPIRED"
    assert ledger.take_next() is None
    assert ledger.status()["mutations"] == 1
    assert_code(
        "OPERATION_ID_CONFLICT",
        lambda: ledger.admit(request(params={"width": 32, "height": 32, "name": "changed"})),
    )


def test_queue_full_does_not_reserve_identity():
    ledger = OperationLedger("instance-a", queue_limit=1)
    ledger.admit(request("first"))
    assert_code("QUEUE_FULL", lambda: ledger.admit(request("second")))
    ledger.cancel("first")
    assert (
        ledger.admit(request("second", params={"width": 12, "height": 12, "name": "new payload"}))[
            "state"
        ]
        == "queued"
    )
    assert ledger.take_next()["operation_id"] == "second"


def test_cancelled_and_expired_entries_never_dispatch():
    clock = Clock()
    ledger = OperationLedger("instance-a", clock=clock)
    ledger.admit(request("cancelled"))
    ledger.admit(request("expired", queue_timeout_ms=100))
    assert ledger.cancel("cancelled")["state"] == "cancelled"
    clock.now += 0.2
    assert ledger.take_next() is None
    assert ledger.get("expired")["state"] == "expired"
    assert ledger.get("expired")["effect"] == "none"
    assert ledger.admit(request("cancelled"))["state"] == "cancelled"
    assert ledger.is_idle()


def test_running_cancellation_records_intent_without_claiming_rollback():
    ledger = OperationLedger("instance-a")
    ledger.admit(request())
    ledger.take_next()
    assert ledger.cancel("op-1")["state"] == "cancel_requested"
    assert ledger.get("op-1")["effect"] == "unknown"
    result = ledger.finish(
        "op-1",
        error={"code": "HOST_ERROR", "message": "Native completion uncertain"},
        effect="unknown",
    )
    assert result["state"] == "failed"
    assert result["effect"] == "unknown"
    assert ledger.cancel("op-1") == result


def test_duplicate_admission_is_atomic_under_contention():
    ledger = OperationLedger("instance-a")
    with ThreadPoolExecutor(max_workers=16) as workers:
        snapshots = list(workers.map(lambda _: ledger.admit(request()), range(100)))
    assert all(s["state"] == "queued" for s in snapshots)
    assert ledger.status()["queued"] == ledger.status()["mutations"] == 1
    assert ledger.take_next()["operation_id"] == "op-1"
    assert ledger.take_next() is None


def test_cancel_and_dispatch_share_an_atomic_transition():
    for _ in range(60):
        ledger = OperationLedger("instance-a")
        ledger.admit(request())
        barrier = threading.Barrier(2)

        def dispatch():
            barrier.wait()
            return ledger.take_next()

        def cancel():
            barrier.wait()
            return ledger.cancel("op-1")

        with ThreadPoolExecutor(max_workers=2) as workers:
            dispatched, cancelled = workers.submit(dispatch), workers.submit(cancel)
            dispatched, cancelled = dispatched.result(), cancelled.result()
        if cancelled["state"] == "cancelled":
            assert dispatched is None
        else:
            assert cancelled["state"] == "cancel_requested"
            assert dispatched["operation_id"] == "op-1"


def test_full_mutation_ledger_preserves_status_cancel_and_bounded_reads():
    clock = Clock()
    ledger = OperationLedger("instance-a", mutation_limit=1, read_limit=1, clock=clock, read_ttl=1)
    ledger.admit(request())
    ledger.take_next()
    ledger.finish("op-1")
    assert_code("LEDGER_FULL", lambda: ledger.admit(request("second")))
    assert ledger.get("op-1")["state"] == "succeeded"
    for op in ("read-1", "read-2"):
        ledger.admit(request(op, command="list_documents"))
        ledger.take_next()
        assert ledger.finish(op, result={"documents": []})["effect"] == "none"
    assert_code("OPERATION_NOT_FOUND", lambda: ledger.get("read-1"))
    assert ledger.status()["read_entries"] == 1
    clock.now += 2
    assert_code("OPERATION_NOT_FOUND", lambda: ledger.get("read-2"))
    assert ledger.get("op-1")["state"] == "succeeded"


def test_read_and_mutation_ids_cannot_collide_while_cached():
    ledger = OperationLedger("instance-a")
    ledger.admit(request(command="list_documents"))
    assert_code("OPERATION_ID_CONFLICT", lambda: ledger.admit(request()))


def test_drain_cancels_queue_preserves_running_and_original_retry():
    ledger = OperationLedger("instance-a")
    ledger.admit(request("running"))
    ledger.take_next()
    ledger.admit(request("queued"))
    ledger.drain()
    assert ledger.get("queued")["state"] == "cancelled"
    assert not ledger.is_idle()
    assert ledger.admit(request("running"))["state"] == "running"
    assert_code("BRIDGE_DRAINING", lambda: ledger.admit(request("new")))
    ledger.finish("running")
    assert ledger.is_idle()


def test_finish_never_retains_host_wrappers_or_unbounded_payloads():
    ledger = OperationLedger("instance-a")
    ledger.admit(request())
    assert_code("INVALID_OPERATION_STATE", lambda: ledger.finish("op-1"))
    ledger.take_next()
    with pytest.raises(TypeError):
        ledger.finish("op-1", result={"wrapper": object()})
    with pytest.raises(ValueError):
        ledger.finish("op-1", result={"large": "x" * (1024 * 1024)})
    assert (
        ledger.finish(
            "op-1", error={"code": "RESULT_ERROR", "message": "No safe result"}, effect="unknown"
        )["effect"]
        == "unknown"
    )


def test_cancel_admit_churn_does_not_accumulate_phantom_queue_entries():
    ledger = OperationLedger("instance-a", queue_limit=1)
    for number in range(100):
        op = "op-" + str(number)
        ledger.admit(request(op))
        ledger.cancel(op)
    assert ledger.status()["queued"] == 0
    assert ledger.take_next() is None
