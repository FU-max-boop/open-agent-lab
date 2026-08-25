"""Publication-policy tests for retained relay evidence."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from benchmarks.terminal_bench.relay_evidence import (
    _EMPTY_SHA256,
    _REJECTION_CODES,
    _canonical,
    _canonical_hash,
    _minimum_terminal_sse_bytes,
    _publication_reasons,
    relay_metadata,
)

_BUILD_ID = f"sha256:{'0' * 64}"
_MODEL = "deepseek-v4-pro"
_RELAY_INSTANCE_ID = "00000000-0000-4000-8000-000000000001"
_RESPONSE_SHA256 = f"sha256:{'1' * 64}"
_REQUEST_SHA256 = f"sha256:{'2' * 64}"


def _at(milliseconds: int) -> str:
    return f"2026-08-25T00:00:{milliseconds // 1000:02d}.{milliseconds % 1000:03d}Z"


def _records(lifecycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, lifecycle in enumerate(lifecycles):
        ordinal = index + 1
        request_id = f"00000000-0000-4000-8000-{ordinal:012d}"
        requested = lifecycle.get("requested_max")
        effective = lifecycle.get("effective_max", 50_000)
        transport_state = lifecycle.get("transport_state", "completed")
        terminal_event = lifecycle.get("terminal_event", "response.completed")
        terminal_status = lifecycle.get(
            "terminal_status",
            terminal_event.removeprefix("response.")
            if isinstance(terminal_event, str)
            else None,
        )
        incomplete_reason = lifecycle.get(
            "incomplete_reason",
            "max_output_tokens" if terminal_event == "response.incomplete" else None,
        )
        output_tokens = lifecycle.get("output_tokens", 12)
        failed = transport_state == "failed"
        response_bytes = lifecycle.get("response_bytes", 0 if failed else 512)
        common = {
            "schemaVersion": 2,
            "relayVersion": "native-responses-relay-v2",
            "runId": "relay-evidence-test",
            "relayInstanceId": _RELAY_INSTANCE_ID,
            "providerId": "deepseek",
            "buildId": _BUILD_ID,
            "ordinal": ordinal,
            "relayRequestId": request_id,
        }
        records.extend(
            (
                {
                    **common,
                    "event": "transport.responses.request",
                    "at": _at(index * 3),
                    "requestedModel": _MODEL,
                    "requestBytes": 128,
                    "requestSha256": _REQUEST_SHA256,
                    "clientRequestId": None,
                    "stream": True,
                    "requestedMaxOutputTokens": requested,
                    "effectiveMaxOutputTokens": effective,
                },
                {
                    **common,
                    "event": "transport.responses.headers",
                    "at": _at(index * 3 + 1),
                    "status": None if failed else 200,
                    "providerRequestId": None if failed else f"provider-{ordinal}",
                    "modelHeader": None,
                    "headersMs": None if failed else 1,
                },
                {
                    **common,
                    "event": "transport.responses.closed",
                    "at": _at(index * 3 + 2),
                    "transportState": transport_state,
                    "errorCategory": "upstream_failure" if failed else None,
                    "status": None if failed else 200,
                    "providerRequestId": None if failed else f"provider-{ordinal}",
                    "responseBytes": response_bytes,
                    "responseSha256": _EMPTY_SHA256
                    if response_bytes == 0
                    else _RESPONSE_SHA256,
                    "durationMs": 2,
                    "firstByteMs": None if response_bytes == 0 else 1,
                    "responseId": None if failed else f"resp-{ordinal}",
                    "returnedModel": None if failed else _MODEL,
                    "modelConsistency": "missing" if failed else "consistent",
                    "modelSources": {}
                    if failed
                    else {f"event.{terminal_event}.response.model.1": _MODEL},
                    "systemFingerprint": None,
                    "terminalEvent": terminal_event,
                    "terminalStatus": terminal_status,
                    "incompleteReason": incomplete_reason,
                    "usage": None
                    if output_tokens is None
                    else {
                        "input_tokens": 7,
                        "output_tokens": output_tokens,
                        "total_tokens": 7 + output_tokens,
                    },
                    "metadataConflicts": [],
                    "parseErrors": 0,
                },
            )
        )
    return records


def _journal(records: list[dict[str, Any]]) -> str:
    previous: str | None = None
    lines: list[str] = []
    for record in records:
        body = {**record, "previousEventSha256": previous}
        body.pop("eventSha256", None)
        event_hash = _canonical_hash(body)
        lines.append(_canonical({**body, "eventSha256": event_hash}))
        previous = event_hash
    return f"{'\n'.join(lines)}\n" if lines else ""


def _seal(journal: str, overrides: dict[str, Any] | None = None) -> str:
    records = [json.loads(line) for line in journal.splitlines()]
    output_tokens = sum(
        closed["usage"]["output_tokens"]
        for closed in records[2::3]
        if isinstance(closed.get("usage"), dict)
    )
    body: dict[str, Any] = {
        "schemaVersion": 2,
        "state": "sealed",
        "relayVersion": "native-responses-relay-v2",
        "runId": "relay-evidence-test",
        "relayInstanceId": _RELAY_INSTANCE_ID,
        "providerId": "deepseek",
        "buildId": _BUILD_ID,
        "expectedModel": _MODEL,
        "sealedAt": "2026-08-25T00:01:00.000Z",
        "rejectedRequests": {},
        "budgetClass": "scored_slot",
        "accountingMode": "sealed_usage_debit",
        "slotOutputTokenLimit": 50_000,
        "outputTokenAccounting": {
            "state": "complete",
            "reportedOutputTokens": output_tokens,
            "conservativeOutputTokenUpperBound": output_tokens,
            "unusedOutputTokensBurned": 50_000 - output_tokens,
        },
        "eventCount": len(records),
        "chainHead": records[-1]["eventSha256"] if records else None,
    }
    body.update(overrides or {})
    return f"{_canonical({**body, 'markerSha256': _canonical_hash(body)})}\n"


def _budget(
    budget_class: str,
    state: str,
    reported: int | None,
    upper: int | None,
    burned: int,
) -> dict[str, Any]:
    accounting_mode, limit = {
        "scored_slot": ("sealed_usage_debit", 50_000),
        "zai_route_probe": ("fixed_round_allocations", 8_448),
        "unmetered_route_probe": ("none", None),
    }[budget_class]
    return {
        "budgetClass": budget_class,
        "accountingMode": accounting_mode,
        "slotOutputTokenLimit": limit,
        "outputTokenAccounting": {
            "state": state,
            "reportedOutputTokens": reported,
            "conservativeOutputTokenUpperBound": upper,
            "unusedOutputTokensBurned": burned,
        },
    }


def _verify(
    lifecycles: list[dict[str, Any]],
    seal_overrides: dict[str, Any] | None = None,
    *,
    record_mutation: tuple[int, dict[str, Any]] | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    records = _records(lifecycles)
    if record_mutation is not None:
        index, mutation = record_mutation
        records[index].update(mutation)
    journal = _journal(records)
    seal = _seal(journal, seal_overrides)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        journal_path = root / "provider-metadata.ndjson"
        seal_path = root / "provider-metadata.ndjson.sealed"
        journal_path.write_text(journal, encoding="utf-8")
        seal_path.write_text(seal, encoding="utf-8")
        return relay_metadata(journal_path, seal_path, allow_empty=allow_empty)


class RelayEvidenceV2Test(unittest.TestCase):
    def test_scored_completed_envelope_and_accounting_are_v2(self) -> None:
        verified = _verify([{}])
        self.assertEqual(verified["schema_version"], 2)
        self.assertEqual(verified["publication_gate"], {"ok": True, "reasons": []})
        self.assertEqual(
            verified["seal"]["outputTokenAccounting"],
            {
                "state": "complete",
                "reportedOutputTokens": 12,
                "conservativeOutputTokenUpperBound": 12,
                "unusedOutputTokensBurned": 49_988,
            },
        )
        continued = _verify(
            [
                {"output_tokens": 30_000},
                {"effective_max": 20_000, "output_tokens": 10_000},
            ]
        )
        self.assertEqual(
            continued["seal"]["outputTokenAccounting"],
            {
                "state": "complete",
                "reportedOutputTokens": 40_000,
                "conservativeOutputTokenUpperBound": 40_000,
                "unusedOutputTokensBurned": 10_000,
            },
        )
        invalid_max = _verify(
            [{}], {"rejectedRequests": {"invalid_max_output_tokens": 1}}
        )
        self.assertIn(
            "invalid_max_output_tokens", invalid_max["publication_gate"]["reasons"]
        )

    def test_request_maxima_and_terminal_tuple_are_type_exact(self) -> None:
        request_mutations = (
            {"requestedMaxOutputTokens": 0},
            {"requestedMaxOutputTokens": False},
            {"requestedMaxOutputTokens": 1.5},
            {"effectiveMaxOutputTokens": 0},
            {"effectiveMaxOutputTokens": False},
            {"effectiveMaxOutputTokens": None},
        )
        for mutation in request_mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                _verify([{}], record_mutation=(0, mutation))
        with self.assertRaises(ValueError):
            _verify([{"requested_max": 1_024, "effective_max": 1_025}])

        terminal_mutations = (
            {
                "terminalEvent": None,
                "terminalStatus": "completed",
                "incompleteReason": None,
            },
            {
                "terminalEvent": "response.completed",
                "terminalStatus": "failed",
                "incompleteReason": None,
            },
            {
                "terminalEvent": "response.completed",
                "terminalStatus": "completed",
                "incompleteReason": "max_output_tokens",
            },
            {
                "terminalEvent": "response.incomplete",
                "terminalStatus": "incomplete",
                "incompleteReason": None,
            },
            {
                "terminalEvent": "response.incomplete",
                "terminalStatus": "incomplete",
                "incompleteReason": "x" * 513,
            },
            {"terminalStatus": {}},
        )
        for mutation in terminal_mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                _verify([{}], record_mutation=(2, mutation))

    def test_scored_terminal_exact_exhaustion_and_poisoned_states(self) -> None:
        with self.assertRaises(ValueError):
            _verify(
                [
                    {
                        "terminal_event": "response.incomplete",
                        "incomplete_reason": "max_output_tokens",
                        "output_tokens": 10,
                    },
                    {"effective_max": 49_990, "output_tokens": 20},
                ],
                _budget("scored_slot", "complete", 30, 30, 49_970),
            )

        incomplete = _verify(
            [
                {
                    "requested_max": 1_000,
                    "effective_max": 1_000,
                    "terminal_event": "response.incomplete",
                    "incomplete_reason": "max_output_tokens",
                    "output_tokens": 900,
                }
            ],
            _budget("scored_slot", "budget_terminal", 900, 900, 49_100),
        )
        self.assertEqual(
            incomplete["publication_gate"],
            {"ok": False, "reasons": ["terminal_event_incomplete"]},
        )

        exhausted = _verify(
            [{"output_tokens": 50_000}],
            {
                **_budget("scored_slot", "exact_exhaustion", 50_000, 50_000, 0),
                "rejectedRequests": {"slot_output_budget_exhausted": 1},
            },
        )
        self.assertEqual(
            exhausted["publication_gate"],
            {
                "ok": False,
                "reasons": [
                    "relay_rejected_requests",
                    "slot_output_budget_exhausted",
                ],
            },
        )

        poisoned = _verify(
            [
                {
                    "requested_max": 10_000,
                    "effective_max": 10_000,
                    "transport_state": "failed",
                    "terminal_event": None,
                    "terminal_status": None,
                    "incomplete_reason": None,
                    "output_tokens": None,
                }
            ],
            _budget("scored_slot", "poisoned", None, 10_000, 40_000),
        )
        self.assertEqual(
            poisoned["seal"]["outputTokenAccounting"][
                "conservativeOutputTokenUpperBound"
            ],
            10_000,
        )
        runtime_poisoned = (
            {
                "transportState": "failed",
                "errorCategory": "upstream_idle_timeout",
            },
            {"usage": {"output_tokens": 12}},
            {
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 12,
                    "total_tokens": 999,
                }
            },
            {"metadataConflicts": ["usage"]},
            {
                "terminalEvent": None,
                "terminalStatus": None,
                "incompleteReason": None,
            },
        )
        for mutation in runtime_poisoned:
            with self.subTest(mutation=mutation):
                verified = _verify(
                    [{}],
                    _budget("scored_slot", "poisoned", None, 50_000, 0),
                    record_mutation=(2, mutation),
                )
                self.assertEqual(
                    verified["seal"]["outputTokenAccounting"][
                        "conservativeOutputTokenUpperBound"
                    ],
                    50_000,
                )
        empty_poison = _verify(
            [],
            _budget("scored_slot", "poisoned", None, 0, 50_000),
            allow_empty=True,
        )
        self.assertEqual(empty_poison["seal"]["eventCount"], 0)

    def test_zai_fixed_rounds_and_unmetered_policy_are_exact(self) -> None:
        first_round = _verify(
            [
                {
                    "requested_max": 1_024,
                    "effective_max": 8_192,
                    "output_tokens": 100,
                }
            ],
            _budget("zai_route_probe", "complete", 100, 100, 8_092),
        )
        self.assertEqual(first_round["seal"]["budgetClass"], "zai_route_probe")

        probe = _verify(
            [
                {"effective_max": 8_192, "output_tokens": 100},
                {
                    "requested_max": 1_024,
                    "effective_max": 256,
                    "terminal_event": "response.incomplete",
                    "incomplete_reason": "max_output_tokens",
                    "output_tokens": 12,
                },
            ],
            _budget("zai_route_probe", "probe_conformant", 112, 112, 8_336),
        )
        self.assertEqual(
            probe["seal"]["outputTokenAccounting"]["state"], "probe_conformant"
        )

        for requested, effective in ((None, None), (123, 123)):
            with self.subTest(requested=requested):
                unmetered = _verify(
                    [
                        {
                            "requested_max": requested,
                            "effective_max": effective,
                            "output_tokens": 12,
                        }
                    ],
                    _budget("unmetered_route_probe", "unmetered", 12, 12, 0),
                )
                self.assertEqual(unmetered["seal"]["accountingMode"], "none")

        empty = _verify(
            [],
            _budget("unmetered_route_probe", "unmetered", 0, 0, 0),
            allow_empty=True,
        )
        self.assertEqual(empty["event_count"], 0)

        unknown = _verify(
            [
                {
                    "requested_max": None,
                    "effective_max": None,
                    "transport_state": "failed",
                    "terminal_event": None,
                    "terminal_status": None,
                    "incomplete_reason": None,
                    "output_tokens": None,
                }
            ],
            _budget("unmetered_route_probe", "poisoned", None, None, 0),
        )
        self.assertEqual(unknown["seal"]["outputTokenAccounting"]["state"], "poisoned")
        settled_then_poisoned = _verify(
            [{"requested_max": None, "effective_max": None}],
            _budget("unmetered_route_probe", "poisoned", None, None, 0),
        )
        self.assertEqual(
            settled_then_poisoned["seal"]["outputTokenAccounting"]["state"],
            "poisoned",
        )
        with self.assertRaises(ValueError):
            _verify(
                [
                    {
                        "requested_max": None,
                        "effective_max": None,
                        "transport_state": "failed",
                        "terminal_event": None,
                        "terminal_status": None,
                        "incomplete_reason": None,
                        "output_tokens": None,
                    },
                    {"requested_max": None, "effective_max": None},
                ],
                _budget("unmetered_route_probe", "poisoned", None, None, 0),
            )

        with self.assertRaises(ValueError):
            _verify(
                [{"requested_max": 123, "effective_max": None}],
                _budget("unmetered_route_probe", "unmetered", 12, 12, 0),
            )

    def test_policy_and_accounting_mutations_fail_closed(self) -> None:
        mutations = (
            {"schemaVersion": 1},
            {"relayVersion": "native-responses-relay-v1"},
            {"accountingMode": "none"},
            {"slotOutputTokenLimit": 49_999},
            {"budgetClass": "fixture"},
            {
                "outputTokenAccounting": {
                    "state": "complete",
                    "reportedOutputTokens": 12,
                    "conservativeOutputTokenUpperBound": 13,
                    "unusedOutputTokensBurned": 49_988,
                }
            },
            {
                "outputTokenAccounting": {
                    "state": "poisoned",
                    "reportedOutputTokens": 0,
                    "conservativeOutputTokenUpperBound": 12,
                    "unusedOutputTokensBurned": 49_988,
                }
            },
            {
                "outputTokenAccounting": {
                    "state": "complete",
                    "reportedOutputTokens": False,
                    "conservativeOutputTokenUpperBound": False,
                    "unusedOutputTokensBurned": 50_000,
                }
            },
            {
                "outputTokenAccounting": {
                    "state": "complete",
                    "reportedOutputTokens": 12,
                    "conservativeOutputTokenUpperBound": 12,
                    "unusedOutputTokensBurned": 49_988,
                    "extra": 1,
                }
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                _verify([{}], mutation)

    def test_terminal_minimum_includes_status_and_incomplete_details(self) -> None:
        closed = _records(
            [
                {
                    "terminal_event": "response.incomplete",
                    "terminal_status": "incomplete",
                    "incomplete_reason": "max_output_tokens",
                }
            ]
        )[2]
        minimum = _minimum_terminal_sse_bytes(closed)
        without_new_fields = {
            **closed,
            "terminalStatus": None,
            "incompleteReason": None,
        }
        self.assertGreater(minimum, _minimum_terminal_sse_bytes(without_new_fields))
        with self.assertRaisesRegex(ValueError, "Impossible transport measurements"):
            _verify(
                [
                    {
                        "terminal_event": "response.incomplete",
                        "terminal_status": "incomplete",
                        "incomplete_reason": "max_output_tokens",
                    }
                ],
                _budget("scored_slot", "budget_terminal", 12, 12, 49_988),
                record_mutation=(2, {"responseBytes": minimum - 1}),
            )


class PublicationReasonsTest(unittest.TestCase):
    def _reasons(self, rejected_requests: dict[str, int]) -> set[str]:
        return _publication_reasons(
            [], rejected_requests, "deepseek", _BUILD_ID, _MODEL
        )

    def test_security_and_budget_rejections_get_dedicated_reasons(self) -> None:
        cases = (
            ({"model_mismatch": 1}, "requested_model_mismatch"),
            ({"upstream_secret_echo": 1}, "upstream_secret_echo"),
            ({"invalid_turn_state": 1}, "invalid_turn_state"),
            ({"slot_output_budget_exhausted": 1}, "slot_output_budget_exhausted"),
            ({"invalid_max_output_tokens": 1}, "invalid_max_output_tokens"),
        )
        self.assertTrue(
            {
                "invalid_turn_state",
                "slot_output_budget_exhausted",
                "invalid_max_output_tokens",
            }
            <= _REJECTION_CODES
        )
        for rejected_requests, dedicated_reason in cases:
            with self.subTest(rejected_requests=rejected_requests):
                reasons = self._reasons(rejected_requests)
                self.assertEqual(
                    reasons,
                    {
                        "no_completed_response",
                        "relay_rejected_requests",
                        dedicated_reason,
                    },
                )

    def test_other_rejections_keep_the_existing_publication_semantics(self) -> None:
        cases = (
            ({}, {"no_completed_response"}),
            ({"client_disconnected_after_close": 1}, {"no_completed_response"}),
            (
                {"invalid_json": 1},
                {"no_completed_response", "relay_rejected_requests"},
            ),
        )
        for rejected_requests, expected in cases:
            with self.subTest(rejected_requests=rejected_requests):
                self.assertEqual(self._reasons(rejected_requests), expected)


if __name__ == "__main__":
    unittest.main()
