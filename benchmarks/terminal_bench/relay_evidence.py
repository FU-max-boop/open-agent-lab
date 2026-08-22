"""Validate sealed native-Responses relay evidence for publication."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_BUILD_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def relay_metadata(journal_path: Path, seal_path: Path) -> dict[str, Any]:
    if journal_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Provider metadata exceeds the publication limit.")
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for line_number, line in enumerate(journal_path.read_text().splitlines(), 1):
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(
            record.get("eventSha256"), str
        ):
            raise TypeError(f"Invalid provider metadata record at line {line_number}.")
        event_hash = record.pop("eventSha256")
        canonical = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        actual = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        if record.get("previousEventSha256") != previous or actual != event_hash:
            raise ValueError(f"Provider metadata chain mismatch at line {line_number}.")
        record["eventSha256"] = event_hash
        records.append(record)
        previous = event_hash
    if not records:
        raise ValueError("Provider metadata is empty.")
    if len(records) % 3:
        raise ValueError("Provider metadata has an incomplete lifecycle.")

    run_id = records[0].get("runId")
    instance_id = records[0].get("relayInstanceId")
    provider_id = records[0].get("providerId")
    build_id = records[0].get("buildId")
    if not all(
        isinstance(value, str) and value
        for value in (run_id, instance_id, provider_id, build_id)
    ):
        raise ValueError("Provider metadata identity is missing.")
    request_ids: set[str] = set()
    requested_model = records[0].get("requestedModel")
    if not isinstance(requested_model, str) or not requested_model:
        raise ValueError("Requested model identity is missing.")
    expected_events = (
        "transport.responses.request",
        "transport.responses.headers",
        "transport.responses.closed",
    )
    for offset in range(0, len(records), 3):
        ordinal = offset // 3 + 1
        group = records[offset : offset + 3]
        request_id = group[0].get("relayRequestId")
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id in request_ids
        ):
            raise ValueError(f"Invalid request identity at ordinal {ordinal}.")
        request_ids.add(request_id)
        if (
            group[0].get("requestedModel") != requested_model
            or group[1].get("status") != group[2].get("status")
            or group[1].get("providerRequestId") != group[2].get("providerRequestId")
        ):
            raise ValueError(f"Conflicting lifecycle metadata at ordinal {ordinal}.")
        for event_offset, record in enumerate(group):
            if (
                not _is_integer(record.get("schemaVersion"))
                or record.get("schemaVersion") != 1
                or record.get("relayVersion") != "native-responses-relay-v1"
                or record.get("event") != expected_events[event_offset]
                or not _is_integer(record.get("ordinal"))
                or record.get("ordinal") != ordinal
                or record.get("runId") != run_id
                or record.get("relayInstanceId") != instance_id
                or record.get("providerId") != provider_id
                or record.get("buildId") != build_id
                or record.get("relayRequestId") != request_id
            ):
                raise ValueError(f"Invalid lifecycle at ordinal {ordinal}.")

    marker = json.loads(seal_path.read_text())
    if not isinstance(marker, dict) or not isinstance(marker.get("markerSha256"), str):
        raise TypeError("Invalid provider metadata seal.")
    marker_hash = marker.pop("markerSha256")
    canonical_marker = json.dumps(
        marker,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    actual_marker_hash = (
        "sha256:" + hashlib.sha256(canonical_marker.encode()).hexdigest()
    )
    rejected_requests = marker.get("rejectedRequests")
    if (
        marker_hash != actual_marker_hash
        or not _is_integer(marker.get("schemaVersion"))
        or marker.get("schemaVersion") != 1
        or marker.get("state") != "sealed"
        or marker.get("relayVersion") != "native-responses-relay-v1"
        or marker.get("runId") != run_id
        or marker.get("relayInstanceId") != instance_id
        or marker.get("providerId") != provider_id
        or marker.get("buildId") != build_id
        or not isinstance(marker.get("sealedAt"), str)
        or not marker.get("sealedAt")
        or not _is_integer(marker.get("eventCount"))
        or marker.get("eventCount") != len(records)
        or marker.get("chainHead") != previous
        or marker.get("expectedModel") != requested_model
        or not isinstance(rejected_requests, dict)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in rejected_requests.values()
        )
    ):
        raise ValueError("Provider metadata seal mismatch.")
    marker["markerSha256"] = marker_hash

    requested = {
        record["ordinal"]: record.get("requestedModel")
        for record in records
        if record.get("event") == "transport.responses.request"
    }
    reasons: set[str] = set()
    if not _BUILD_ID.fullmatch(build_id):
        reasons.add("unverifiable_relay_build")
    if any(rejected_requests.values()):
        reasons.add("relay_rejected_requests")
    completed = 0
    for record in records:
        status = record.get("status")
        successful_http = _is_integer(status) and 200 <= status < 300
        if (
            record.get("event") == "transport.responses.headers"
            and successful_http
            and not record.get("providerRequestId")
        ):
            reasons.add("provider_request_id_missing")
        if record.get("event") != "transport.responses.closed":
            continue
        if not successful_http or record.get("transportState") != "completed":
            reasons.add("provider_request_incomplete_or_failed")
            continue
        completed += 1
        if record.get("parseErrors") != 0 or record.get("metadataConflicts"):
            reasons.add("provider_metadata_inconsistent")
        if record.get("modelConsistency") != "consistent":
            reasons.add("returned_model_missing_or_conflicting")
        if record.get("returnedModel") != requested.get(record.get("ordinal")):
            reasons.add("returned_model_mismatch")
        if not record.get("responseId"):
            reasons.add("response_id_missing")
        usage = record.get("usage")
        if not isinstance(usage, dict) or any(
            not isinstance(usage.get(field), int)
            or isinstance(usage.get(field), bool)
            or usage[field] < 0
            for field in ("input_tokens", "output_tokens", "total_tokens")
        ):
            reasons.add("usage_missing_or_invalid")
        if record.get("terminalEvent") != "response.completed":
            reasons.add("terminal_event_missing")
    if completed == 0:
        reasons.add("no_completed_response")
    return {
        "schema_version": 1,
        "event_count": len(records),
        "chain_head": previous,
        "seal": marker,
        "publication_gate": {"ok": not reasons, "reasons": sorted(reasons)},
        "records": records,
    }
