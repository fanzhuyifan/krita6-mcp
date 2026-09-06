"""Thread-safe at-most-once dispatch within a live plugin instance."""

from collections import deque
import copy
import hashlib
import json
import threading
import time

from .protocol import BridgeError, MUTATIONS, validate_request

_TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


class OperationLedger:
    def __init__(
        self,
        instance_id,
        queue_limit=32,
        mutation_limit=10000,
        clock=time.monotonic,
        *,
        read_limit=256,
        result_ttl=300,
        read_ttl=60,
        result_bytes_limit=16 * 1024 * 1024,
    ):
        if (
            min(queue_limit, mutation_limit, read_limit, result_bytes_limit) < 1
            or min(result_ttl, read_ttl) <= 0
        ):
            raise ValueError("Ledger bounds must be positive")
        self.instance_id = instance_id
        self.queue_limit, self.mutation_limit = queue_limit, mutation_limit
        self.read_limit, self.result_ttl, self.read_ttl = read_limit, result_ttl, read_ttl
        self.result_bytes_limit = result_bytes_limit
        self._clock, self._lock = clock, threading.RLock()
        self._entries, self._queue = {}, deque()
        self._draining = False
        self._sequence = 0
        self._result_bytes = 0

    def _snapshot(self, entry):
        return copy.deepcopy(entry["snapshot"])

    def _terminal(self, entry, state, now, error=None):
        s = entry["snapshot"]
        s["state"], s["error"] = state, error
        entry["completed"] = now
        entry["request"] = None

    def _release_result_bytes(self, entry):
        self._result_bytes -= entry.pop("result_bytes", 0)

    def _expire_result(self, entry):
        self._release_result_bytes(entry)
        entry["snapshot"]["result"] = None
        entry["snapshot"]["error"] = {
            "code": "RESULT_EXPIRED",
            "message": "Result body expired; recorded state and effect remain authoritative",
        }
        entry["result_expired"] = True

    def _bound_results(self):
        # Read and mutation bodies share this byte budget. Compact tombstones
        # remain bounded by their respective entry counts, never by eviction.
        while self._result_bytes > self.result_bytes_limit:
            oldest = min(
                (entry for entry in self._entries.values() if entry.get("result_bytes")),
                key=lambda entry: entry["completed"],
            )
            self._expire_result(oldest)

    def _maintain(self):
        now = self._clock()
        for op in list(self._queue):
            e = self._entries[op]
            if e["snapshot"]["state"] == "queued" and now >= e["deadline"]:
                self._terminal(
                    e,
                    "expired",
                    now,
                    {"code": "QUEUE_EXPIRED", "message": "Queue deadline elapsed before dispatch"},
                )
        self._queue = deque(
            op for op in self._queue if self._entries[op]["snapshot"]["state"] == "queued"
        )
        for op, e in list(self._entries.items()):
            if e["snapshot"]["state"] not in _TERMINAL:
                continue
            age = now - e["completed"]
            if not e["mutation"] and age >= self.read_ttl:
                self._release_result_bytes(e)
                del self._entries[op]
            elif e["mutation"] and age >= self.result_ttl and not e.get("result_expired"):
                self._expire_result(e)

    def admit(self, request):
        request = validate_request(request, self.instance_id)
        payload = {k: request[k] for k in ("command", "target", "params")}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()
        op = request["operation_id"]
        with self._lock:
            self._maintain()
            if op in self._entries:
                e = self._entries[op]
                if e["digest"] != digest:
                    raise BridgeError(
                        "OPERATION_ID_CONFLICT", "Operation ID already identifies different work"
                    )
                return self._snapshot(e)
            if self._draining:
                raise BridgeError("BRIDGE_DRAINING", "Bridge is stopping and rejects new work")
            if len(self._queue) >= self.queue_limit:
                raise BridgeError("QUEUE_FULL", "GUI command queue is full")
            mutation = request["command"] in MUTATIONS
            if (
                mutation
                and sum(e["mutation"] for e in self._entries.values()) >= self.mutation_limit
            ):
                raise BridgeError(
                    "LEDGER_FULL",
                    "Mutation ledger is full; start a new bridge session after draining",
                )
            if not mutation:
                reads = [(k, e) for k, e in self._entries.items() if not e["mutation"]]
                if len(reads) >= self.read_limit:
                    completed = [(k, e) for k, e in reads if e["snapshot"]["state"] in _TERMINAL]
                    if not completed:
                        raise BridgeError("READ_CACHE_FULL", "Read operation cache is full")
                    oldest = min(completed, key=lambda item: item[1]["completed"])[0]
                    self._release_result_bytes(self._entries[oldest])
                    del self._entries[oldest]
            s = {
                "instance_id": self.instance_id,
                "operation_id": op,
                "command": request["command"],
                "state": "queued",
                "effect": "none",
                "result": None,
                "error": None,
            }
            self._entries[op] = {
                "snapshot": s,
                "request": request,
                "digest": digest,
                "mutation": mutation,
                "deadline": self._clock() + request["queue_timeout_ms"] / 1000,
            }
            self._queue.append(op)
            return self._snapshot(self._entries[op])

    def take_next(self):
        with self._lock:
            self._maintain()
            # Even callers outside the GUI executor cannot overlap native commands.
            if any(
                e["snapshot"]["state"] in {"running", "cancel_requested"}
                for e in self._entries.values()
            ):
                return None
            if not self._queue:
                return None
            e = self._entries[self._queue.popleft()]
            e["snapshot"]["state"] = "running"
            # Dispatch hands control to the host; until completion its effect is
            # uncertain even if the client stops waiting or requests cancellation.
            e["snapshot"]["effect"] = "unknown" if e["mutation"] else "none"
            return copy.deepcopy(e["request"])

    def finish(self, operation_id, result=None, error=None, effect="applied"):
        if effect not in {"none", "applied", "partial", "unknown"}:
            raise ValueError("Unknown effect")
        if result is not None and not isinstance(result, dict):
            raise ValueError("Result must be an object")
        if error is not None and (
            not isinstance(error, dict)
            or set(error) != {"code", "message"}
            or any(not isinstance(v, str) for v in error.values())
        ):
            raise ValueError("Error must contain code and message")
        # Round-trip ensures no host wrappers cross threads, and bounds stored responses.
        encoded = json.dumps({"result": result, "error": error}, allow_nan=False).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("Operation result exceeds 1 MiB")
        plain = json.loads(encoded)
        with self._lock:
            e = self._lookup(operation_id)
            if e["snapshot"]["state"] not in {"running", "cancel_requested"}:
                raise BridgeError(
                    "INVALID_OPERATION_STATE", "Only dispatched operations can finish"
                )
            self._terminal(e, "failed" if error else "succeeded", self._clock(), plain["error"])
            e["snapshot"]["result"] = plain["result"]
            e["snapshot"]["effect"] = effect if e["mutation"] else "none"
            e["result_bytes"] = len(encoded)
            self._result_bytes += len(encoded)
            self._bound_results()
            if e["mutation"]:
                self._sequence += 1
            return self._snapshot(e)

    def _lookup(self, operation_id):
        if operation_id not in self._entries:
            raise BridgeError("OPERATION_NOT_FOUND", "Operation is unknown in this instance")
        return self._entries[operation_id]

    def get(self, operation_id):
        with self._lock:
            self._maintain()
            return self._snapshot(self._lookup(operation_id))

    def cancel(self, operation_id):
        with self._lock:
            self._maintain()
            e = self._lookup(operation_id)
            state = e["snapshot"]["state"]
            if state == "queued":
                self._terminal(e, "cancelled", self._clock())
                self._queue.remove(operation_id)
            elif state == "running":
                e["snapshot"]["state"] = "cancel_requested"
            return self._snapshot(e)

    def drain(self):
        with self._lock:
            self._draining = True
            for op in list(self._queue):
                self.cancel(op)

    def is_idle(self):
        with self._lock:
            self._maintain()
            return not any(e["snapshot"]["state"] not in _TERMINAL for e in self._entries.values())

    def status(self):
        with self._lock:
            self._maintain()
            states = [e["snapshot"]["state"] for e in self._entries.values()]
            return {
                "draining": self._draining,
                "queued": states.count("queued"),
                "running": states.count("running") + states.count("cancel_requested"),
                "queue_limit": self.queue_limit,
                "mutation_limit": self.mutation_limit,
                "mutations": sum(e["mutation"] for e in self._entries.values()),
                "read_entries": sum(not e["mutation"] for e in self._entries.values()),
                "result_bytes": self._result_bytes,
                "result_bytes_limit": self.result_bytes_limit,
                "bridge_sequence": self._sequence,
            }
