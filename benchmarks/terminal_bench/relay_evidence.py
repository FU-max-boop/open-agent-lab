"""Validate sealed native-Responses relay evidence for publication."""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_BUILD_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_EMPTY_SHA256 = (
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_BODY_BYTES = 64 * 1024 * 1024
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
_MAX_SEAL_BYTES = 64 * 1024
_MODEL_CONFLICTS = {
    "model",
    "response_id",
    "system_fingerprint",
    "terminal_event",
    "usage",
}
_TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}
_FAILED_TRANSPORT_ERRORS = {
    "expired",
    "response_too_large",
    "upstream_aborted",
    "upstream_body_missing",
    "upstream_compressed",
    "upstream_connect_timeout",
    "upstream_failure",
    "upstream_idle_timeout",
    "upstream_redirect",
}
_USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
}
_MODEL_SOURCE = re.compile(
    r"^event\.(?P<event>.+)\.response\."
    r"(?P<kind>model|headers\.openai-model)\.(?P<index>[1-9][0-9]*)$"
)
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_COMMON_FIELDS = {
    "schemaVersion",
    "relayVersion",
    "runId",
    "relayInstanceId",
    "providerId",
    "buildId",
    "event",
    "ordinal",
    "relayRequestId",
    "at",
    "previousEventSha256",
    "eventSha256",
}
_EVENT_FIELDS = {
    "transport.responses.request": _COMMON_FIELDS
    | {"requestedModel", "requestBytes", "requestSha256", "clientRequestId", "stream"},
    "transport.responses.headers": _COMMON_FIELDS
    | {"status", "providerRequestId", "modelHeader", "headersMs"},
    "transport.responses.closed": _COMMON_FIELDS
    | {
        "transportState",
        "errorCategory",
        "status",
        "providerRequestId",
        "responseBytes",
        "responseSha256",
        "durationMs",
        "firstByteMs",
        "responseId",
        "returnedModel",
        "modelConsistency",
        "modelSources",
        "systemFingerprint",
        "terminalEvent",
        "usage",
        "metadataConflicts",
        "parseErrors",
    },
}
_SEAL_FIELDS = {
    "schemaVersion",
    "state",
    "relayVersion",
    "runId",
    "relayInstanceId",
    "providerId",
    "buildId",
    "expectedModel",
    "sealedAt",
    "rejectedRequests",
    "eventCount",
    "chainHead",
    "markerSha256",
}
_REJECTION_CODES = {
    "client_disconnected",
    "client_disconnected_after_close",
    "concurrency_exceeded",
    "expired",
    "invalid_json",
    "model_mismatch",
    "not_found",
    "relay_sealed",
    "request_quota_exceeded",
    "request_too_large",
    "unsupported_content_type",
    "unsupported_response_mode",
    "upstream_failure",
}


def _is_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) <= _MAX_SAFE_INTEGER
    )


def _bounded_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-16-le", errors="surrogatepass")) // 2 <= 512
        and not any(ord(character) < 32 for character in value)
    )


def _optional_text(value: object) -> bool:
    return value is None or _bounded_text(value)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return parsed


def _status(value: object) -> bool:
    return value is None or (_is_integer(value) and 200 <= value <= 599)


def _model_source_parts(key: object) -> tuple[str, str, int] | None:
    if key == "http.openai-model.0":
        return "http", "header", 0
    if not isinstance(key, str):
        return None
    matched = _MODEL_SOURCE.fullmatch(key)
    if matched is None or not _bounded_text(matched.group("event")):
        return None
    return matched.group("event"), matched.group("kind"), int(matched.group("index"))


def _model_sources(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > 16:
        return False
    parsed = [(_model_source_parts(key), model) for key, model in value.items()]
    if any(parts is None or not _bounded_text(model) for parts, model in parsed):
        return False
    indexed: dict[int, set[str]] = {}
    for parts, _model in parsed:
        assert parts is not None
        if parts[2] > 0:
            indexed.setdefault(parts[2], set()).add(parts[0])
    return all(len(events) == 1 for events in indexed.values())


def _usage(value: object) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value) <= _USAGE_FIELDS
        and all(_is_integer(item) and item >= 0 for item in value.values())
    )


def _request_payload(record: dict[str, Any]) -> bool:
    return all(
        (
            _bounded_text(record.get("requestedModel")),
            _is_integer(record.get("requestBytes")),
            record.get("requestBytes", -1) >= 0,
            record.get("requestBytes", _MAX_BODY_BYTES + 1) <= _MAX_BODY_BYTES,
            bool(_BUILD_ID.fullmatch(str(record.get("requestSha256", "")))),
            _optional_text(record.get("clientRequestId")),
            record.get("stream") is True,
        )
    )


def _headers_payload(record: dict[str, Any]) -> bool:
    return all(
        (
            _status(record.get("status")),
            _optional_text(record.get("providerRequestId")),
            _optional_text(record.get("modelHeader")),
            record.get("headersMs") is None
            or (
                _is_integer(record.get("headersMs"))
                and record.get("headersMs", -1) >= 0
            ),
        )
    )


def _closed_payload(record: dict[str, Any]) -> bool:
    conflicts = record.get("metadataConflicts")
    state = record.get("transportState")
    error = record.get("errorCategory")
    state_matches_error = (
        (state == "completed" and error is None)
        or (state == "aborted" and error == "client_disconnected")
        or (state == "failed" and error in _FAILED_TRANSPORT_ERRORS)
    )
    return all(
        (
            state in {"completed", "failed", "aborted"},
            _optional_text(error),
            state_matches_error,
            _status(record.get("status")),
            _optional_text(record.get("providerRequestId")),
            _is_integer(record.get("responseBytes")),
            record.get("responseBytes", -1) >= 0,
            record.get("responseBytes", _MAX_BODY_BYTES + 1) <= _MAX_BODY_BYTES,
            bool(_BUILD_ID.fullmatch(str(record.get("responseSha256", "")))),
            _is_integer(record.get("durationMs")),
            record.get("durationMs", -1) >= 0,
            record.get("firstByteMs") is None
            or (
                _is_integer(record.get("firstByteMs"))
                and record.get("firstByteMs", -1) >= 0
            ),
            _optional_text(record.get("responseId")),
            _optional_text(record.get("returnedModel")),
            record.get("modelConsistency") in {"consistent", "conflict", "missing"},
            _model_sources(record.get("modelSources")),
            _optional_text(record.get("systemFingerprint")),
            record.get("terminalEvent") is None
            or record.get("terminalEvent") in _TERMINAL_EVENTS,
            _usage(record.get("usage")),
            isinstance(conflicts, list),
            len(conflicts) == len(set(conflicts))
            if isinstance(conflicts, list)
            else False,
            set(conflicts) <= _MODEL_CONFLICTS
            if isinstance(conflicts, list)
            else False,
            _is_integer(record.get("parseErrors")),
            record.get("parseErrors", -1) >= 0,
        )
    )


def _valid_event_payload(record: dict[str, Any]) -> bool:
    validators = {
        "transport.responses.request": _request_payload,
        "transport.responses.headers": _headers_payload,
        "transport.responses.closed": _closed_payload,
    }
    validator = validators.get(record.get("event"))
    return validator is not None and validator(record)


def _minimum_terminal_sse_bytes(record: dict[str, Any]) -> int:
    terminal_event = record["terminalEvent"]
    if terminal_event is None:
        return 0
    response: dict[str, Any] = {}
    for field, source in (
        ("id", "responseId"),
        ("model", "returnedModel"),
        ("system_fingerprint", "systemFingerprint"),
    ):
        if record[source] is not None:
            response[field] = record[source]
    if record["usage"] is not None:
        response["usage"] = {field: 0 for field in record["usage"]}
    minimal = json.dumps(
        {"type": terminal_event, "response": response},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return len(f"data:{minimal}".encode())


def _minimum_request_bytes(model: str) -> int:
    minimal = json.dumps(
        {"model": model, "store": False, "stream": True},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return len(minimal.encode())


def _valid_transport_measurements(group: list[dict[str, Any]]) -> bool:
    request, headers, closed = group
    response_bytes = closed["responseBytes"]
    first_byte = closed["firstByteMs"]
    headers_ms = headers["headersMs"]
    duration = closed["durationMs"]
    status = headers["status"]
    request_at, headers_at, closed_at = (
        _timestamp(record.get("at")) for record in group
    )
    timestamps_valid = all(
        value is not None for value in (request_at, headers_at, closed_at)
    )
    if timestamps_valid:
        assert (
            request_at is not None and headers_at is not None and closed_at is not None
        )
        timestamps_valid = (
            request_at <= headers_at <= closed_at
            and closed_at - request_at == timedelta(milliseconds=duration)
            and (
                headers_ms is None
                or headers_at - request_at == timedelta(milliseconds=headers_ms)
            )
        )
    zero_bytes_valid = response_bytes != 0 or (
        closed["responseSha256"] == _EMPTY_SHA256
        and not any(
            parts[2] > 0
            for key in closed["modelSources"]
            if (parts := _model_source_parts(key)) is not None
        )
        and all(
            closed[field] is None
            for field in (
                "responseId",
                "returnedModel",
                "systemFingerprint",
                "terminalEvent",
                "usage",
            )
        )
        and closed["metadataConflicts"] == []
        and closed["parseErrors"] == 0
    )
    status_semantics_valid = (
        status is None
        or (status not in {204, 205} and not 300 <= status < 400)
        or (
            response_bytes == 0
            and first_byte is None
            and closed["transportState"] == "failed"
            and closed["errorCategory"]
            == ("upstream_redirect" if 300 <= status < 400 else "upstream_body_missing")
        )
    )
    error = closed["errorCategory"]
    error_stage_valid = (
        (error == "upstream_connect_timeout" and status is None)
        or (error == "upstream_redirect" and status is not None and 300 <= status < 400)
        or (
            error
            in {
                "response_too_large",
                "upstream_body_missing",
                "upstream_compressed",
                "upstream_idle_timeout",
            }
            and status is not None
        )
        or error
        not in {
            "upstream_connect_timeout",
            "upstream_redirect",
            "response_too_large",
            "upstream_body_missing",
            "upstream_compressed",
            "upstream_idle_timeout",
        }
    )
    model_source_indices = [
        parts[2]
        for key in closed["modelSources"]
        if (parts := _model_source_parts(key)) is not None
    ]
    return all(
        (
            timestamps_valid,
            request["requestBytes"]
            >= _minimum_request_bytes(request["requestedModel"]),
            request["requestSha256"] != _EMPTY_SHA256,
            zero_bytes_valid,
            status_semantics_valid,
            error_stage_valid,
            response_bytes >= _minimum_terminal_sse_bytes(closed),
            closed["parseErrors"] <= response_bytes,
            all(index <= response_bytes for index in model_source_indices),
            response_bytes == 0 or first_byte is not None,
            response_bytes == 0 or closed["responseSha256"] != _EMPTY_SHA256,
            first_byte is None or first_byte <= duration,
            headers_ms is None or headers_ms <= duration,
            first_byte is None or headers_ms is None or headers_ms <= first_byte,
            (headers["status"] is None) == (headers_ms is None),
            headers["status"] is not None
            or (
                headers["providerRequestId"] is None
                and headers["modelHeader"] is None
                and closed["providerRequestId"] is None
            ),
            headers["status"] is not None or response_bytes == 0,
            closed["transportState"] != "completed"
            or (
                headers["status"] is not None
                and (first_byte is None) == (response_bytes == 0)
            ),
        )
    )


def _canonical(value: object) -> str:
    # JavaScript's canonicalJson follows JCS and sorts by UTF-16 code units.
    def javascript_order(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: javascript_order(item[key])
                for key in sorted(item, key=lambda key: key.encode("utf-16-be"))
            }
        if isinstance(item, list):
            return [javascript_order(child) for child in item]
        return item

    return json.dumps(
        javascript_order(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _canonical_hash(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key.")
        value[key] = item
    return value


def _parse_canonical_json(text: str, label: str) -> object:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
        )
        if text != _canonical(value):
            raise ValueError
    except (json.JSONDecodeError, UnicodeEncodeError, ValueError):
        raise ValueError(f"{label} is not valid canonical JSON.") from None
    return value


def _decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} is not valid UTF-8.") from None


def _read_limited(path: Path, limit: int, label: str) -> bytes:
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{label} exceeds the publication limit.")
    return data


def _read_records(journal_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    journal = _read_limited(journal_path, _MAX_JOURNAL_BYTES, "Provider metadata")
    text = _decode_utf8(journal, "Provider metadata")
    if text and (not text.endswith("\n") or text.endswith("\n\n")):
        raise ValueError("Provider metadata must end with exactly one newline.")
    records: list[dict[str, Any]] = []
    previous: str | None = None
    lines = text[:-1].split("\n") if text else []
    for line_number, line in enumerate(lines, 1):
        record = _parse_canonical_json(line, "Provider metadata record")
        if not isinstance(record, dict) or not isinstance(
            record.get("eventSha256"), str
        ):
            raise TypeError(f"Invalid provider metadata record at line {line_number}.")
        event = record.get("event")
        if not isinstance(event, str) or set(record) != _EVENT_FIELDS.get(event):
            raise ValueError(f"Unknown provider metadata field at line {line_number}.")
        event_hash = record.pop("eventSha256")
        if (
            record.get("previousEventSha256") != previous
            or _canonical_hash(record) != event_hash
        ):
            raise ValueError(f"Provider metadata chain mismatch at line {line_number}.")
        record["eventSha256"] = event_hash
        records.append(record)
        previous = event_hash
    if len(records) % 3:
        raise ValueError("Provider metadata has an incomplete lifecycle.")
    return records, previous


def _read_marker(seal_path: Path) -> tuple[dict[str, Any], str, str]:
    data = _read_limited(seal_path, _MAX_SEAL_BYTES, "Provider metadata seal")
    text = _decode_utf8(data, "Provider metadata seal")
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise ValueError("Provider metadata seal must end with exactly one newline.")
    marker = _parse_canonical_json(text[:-1], "Provider metadata seal")
    if not isinstance(marker, dict) or not isinstance(marker.get("markerSha256"), str):
        raise TypeError("Invalid provider metadata seal.")
    if set(marker) != _SEAL_FIELDS:
        raise ValueError("Unknown provider metadata seal field.")
    marker_hash = marker.pop("markerSha256")
    return marker, marker_hash, _canonical_hash(marker)


def _identity(
    records: list[dict[str, Any]], marker: dict[str, Any]
) -> tuple[str, str, str, str, str]:
    source = records[0] if records else marker
    values = (
        source.get("runId"),
        source.get("relayInstanceId"),
        source.get("providerId"),
        source.get("buildId"),
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("Provider metadata identity is missing.")
    if not _RUN_ID.fullmatch(values[0]) or not _UUID4.fullmatch(values[1]):
        raise ValueError("Provider metadata identity is invalid.")
    requested_model = (
        records[0].get("requestedModel") if records else marker.get("expectedModel")
    )
    if not isinstance(requested_model, str) or not requested_model:
        raise ValueError("Requested model identity is missing.")
    run_id, instance_id, provider_id, build_id = values
    return run_id, instance_id, provider_id, build_id, requested_model


def _valid_lifecycle_record(
    record: dict[str, Any],
    *,
    expected_event: str,
    ordinal: int,
    request_id: str,
    identity: tuple[str, str, str, str],
) -> bool:
    run_id, instance_id, provider_id, build_id = identity
    return all(
        (
            _is_integer(record.get("schemaVersion")),
            record.get("schemaVersion") == 1,
            record.get("relayVersion") == "native-responses-relay-v1",
            record.get("event") == expected_event,
            _is_integer(record.get("ordinal")),
            record.get("ordinal") == ordinal,
            record.get("runId") == run_id,
            record.get("relayInstanceId") == instance_id,
            record.get("providerId") == provider_id,
            record.get("buildId") == build_id,
            record.get("relayRequestId") == request_id,
            bool(_UUID4.fullmatch(request_id)),
        )
    )


def _validate_lifecycles(
    records: list[dict[str, Any]],
    identity: tuple[str, str, str, str],
    requested_model: str,
) -> None:
    expected_events = (
        "transport.responses.request",
        "transport.responses.headers",
        "transport.responses.closed",
    )
    request_ids: set[str] = set()
    provider_response_identities: list[tuple[str, str | None, str | None]] = []
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
        if not all(
            _valid_lifecycle_record(
                record,
                expected_event=expected_events[event_offset],
                ordinal=ordinal,
                request_id=request_id,
                identity=identity,
            )
            for event_offset, record in enumerate(group)
        ) or not all(_valid_event_payload(record) for record in group):
            raise ValueError(f"Invalid lifecycle at ordinal {ordinal}.")
        if not _valid_transport_measurements(group):
            raise ValueError(f"Impossible transport measurements at ordinal {ordinal}.")
        provider_response_identities.append(
            (identity[2], group[2]["providerRequestId"], group[2]["responseId"])
        )
    conflict = _provider_response_identity_error(provider_response_identities)
    if conflict is not None:
        raise ValueError(conflict)


def _provider_response_identity_error(
    identities: list[tuple[str, str | None, str | None]],
) -> str | None:
    response_ids = [
        (provider, response)
        for provider, _request, response in identities
        if response is not None
    ]
    if len(set(response_ids)) != len(response_ids):
        return "response IDs must be unique within each provider"
    request_counts = Counter(
        (provider, request)
        for provider, request, _response in identities
        if request is not None
    )
    if any(
        response is None
        and request is not None
        and request_counts[(provider, request)] > 1
        for provider, request, response in identities
    ):
        return "fallback provider request IDs must be unique within each provider"
    return None


def _validate_marker(
    marker: dict[str, Any],
    marker_hash: str,
    actual_marker_hash: str,
    identity: tuple[str, str, str, str],
    requested_model: str,
    event_count: int,
    chain_head: str | None,
) -> dict[str, int]:
    run_id, instance_id, provider_id, build_id = identity
    rejected_requests = marker.get("rejectedRequests")
    rejected_valid = (
        isinstance(rejected_requests, dict)
        and set(rejected_requests) <= _REJECTION_CODES
        and all(
            _is_integer(value) and value > 0 for value in rejected_requests.values()
        )
        and rejected_requests.get("client_disconnected_after_close", 0)
        <= event_count // 3
    )
    valid = all(
        (
            marker_hash == actual_marker_hash,
            _is_integer(marker.get("schemaVersion")),
            marker.get("schemaVersion") == 1,
            marker.get("state") == "sealed",
            marker.get("relayVersion") == "native-responses-relay-v1",
            marker.get("runId") == run_id,
            marker.get("relayInstanceId") == instance_id,
            marker.get("providerId") == provider_id,
            marker.get("buildId") == build_id,
            _timestamp(marker.get("sealedAt")) is not None,
            _is_integer(marker.get("eventCount")),
            marker.get("eventCount") == event_count,
            marker.get("chainHead") == chain_head,
            marker.get("expectedModel") == requested_model,
            rejected_valid,
        )
    )
    if not valid:
        raise ValueError("Provider metadata seal mismatch.")
    marker["markerSha256"] = marker_hash
    return rejected_requests


def _model_reasons(record: dict[str, Any], requested_model: str) -> set[str]:
    reasons: set[str] = set()
    consistency = record.get("modelConsistency")
    returned_model = record.get("returnedModel")
    if consistency == "conflict":
        reasons.add("returned_model_conflict")
    elif consistency == "missing":
        reasons.add("returned_model_missing")
    elif consistency != "consistent":
        reasons.add("returned_model_state_invalid")
    if returned_model is None:
        reasons.add("returned_model_missing")
    if returned_model is not None and returned_model != requested_model:
        reasons.add("returned_model_mismatch")
    return reasons


def _raw_model_reasons(
    headers: dict[str, Any], closed: dict[str, Any], requested_model: str
) -> set[str]:
    reasons: set[str] = set()
    sources = closed["modelSources"]
    header = headers.get("modelHeader")
    header_key = "http.openai-model.0"
    models = set(sources.values())
    expected_consistency = (
        "missing" if not models else "consistent" if len(models) == 1 else "conflict"
    )
    returned_model = closed.get("returnedModel")
    terminal_event = closed.get("terminalEvent")
    conflicts = set(closed["metadataConflicts"])
    terminal_groups = {
        (parts[0], parts[2])
        for key in sources
        if (parts := _model_source_parts(key)) is not None
        and parts[0] in _TERMINAL_EVENTS
    }
    terminal_models = [
        model
        for key, model in sources.items()
        if (parts := _model_source_parts(key)) is not None
        and parts[0] == terminal_event
        and parts[1] == "model"
    ]
    header_matches = (header is None and header_key not in sources) or (
        header is not None and sources.get(header_key) == header
    )
    returned_matches = (
        not terminal_models
        if returned_model is None
        else expected_consistency == "consistent"
        and terminal_models == [returned_model]
    )
    if terminal_event is None:
        terminal_shape_matches = (
            closed.get("responseId") is None
            and returned_model is None
            and closed.get("usage") is None
            and (not terminal_groups or "terminal_event" in conflicts)
        )
    else:
        terminal_shape_matches = (
            terminal_event in _TERMINAL_EVENTS
            and "terminal_event" not in conflicts
            and len(terminal_groups) <= 1
            and all(event == terminal_event for event, _index in terminal_groups)
        )
    if (
        not header_matches
        or closed.get("modelConsistency") != expected_consistency
        or not returned_matches
        or ("model" in conflicts) != (len(models) > 1)
        or not terminal_shape_matches
    ):
        reasons.add("provider_metadata_inconsistent")
    if any(model != requested_model for model in models):
        reasons.add("returned_model_mismatch")
    return reasons


def _closed_record_reasons(record: dict[str, Any]) -> set[str]:
    reasons: set[str] = set()
    if not _bounded_text(record.get("responseId")):
        reasons.add("response_id_missing")
    usage = record.get("usage")
    if not isinstance(usage, dict) or any(
        not _is_integer(usage.get(field)) or usage[field] < 0
        for field in ("input_tokens", "output_tokens", "total_tokens")
    ):
        reasons.add("usage_missing_or_invalid")
    if record.get("terminalEvent") != "response.completed":
        reasons.add("terminal_event_missing")
    return reasons


def _incomplete_metadata_reasons(record: dict[str, Any]) -> set[str]:
    conflicts = record.get("metadataConflicts")
    if isinstance(conflicts, list) and "model" in conflicts:
        return {"provider_metadata_inconsistent"}
    if record.get("parseErrors") != 0 or conflicts:
        return {"provider_metadata_unreliable"}
    return set()


def _transport_reasons(
    records: list[dict[str, Any]], requested_model: str
) -> tuple[set[str], int]:
    reasons: set[str] = set()
    completed = 0
    for offset in range(0, len(records), 3):
        headers, record = records[offset + 1], records[offset + 2]
        status = headers.get("status")
        successful_http = _is_integer(status) and 200 <= status < 300
        if successful_http and not _bounded_text(headers.get("providerRequestId")):
            reasons.add("provider_request_id_missing")
        reasons.update(_model_reasons(record, requested_model))
        reasons.update(_raw_model_reasons(headers, record, requested_model))
        reasons.update(_incomplete_metadata_reasons(record))
        if not successful_http or record.get("transportState") != "completed":
            reasons.add("provider_request_incomplete_or_failed")
            continue
        completed += 1
        reasons.update(_closed_record_reasons(record))
    return reasons, completed


def _publication_reasons(
    records: list[dict[str, Any]],
    rejected_requests: dict[str, int],
    provider_id: str,
    build_id: str,
    requested_model: str,
) -> set[str]:
    reasons, completed = _transport_reasons(records, requested_model)
    if provider_id == "synthetic-fixture":
        reasons.add("synthetic_provider")
    if not _BUILD_ID.fullmatch(build_id):
        reasons.add("unverifiable_relay_build")
    if rejected_requests.get("model_mismatch", 0) > 0:
        reasons.add("requested_model_mismatch")
    # Clients may close after terminal SSE instead of waiting for EOF.
    if any(
        count
        for code, count in rejected_requests.items()
        if code != "client_disconnected_after_close"
    ):
        reasons.add("relay_rejected_requests")
    if completed == 0:
        reasons.add("no_completed_response")
    return reasons


def relay_metadata(
    journal_path: Path, seal_path: Path, *, allow_empty: bool = False
) -> dict[str, Any]:
    records, chain_head = _read_records(journal_path)
    if not records and not allow_empty:
        raise ValueError("Provider metadata is empty.")
    marker, marker_hash, actual_marker_hash = _read_marker(seal_path)
    run_id, instance_id, provider_id, build_id, requested_model = _identity(
        records, marker
    )
    identity = run_id, instance_id, provider_id, build_id
    _validate_lifecycles(records, identity, requested_model)
    rejected_requests = _validate_marker(
        marker,
        marker_hash,
        actual_marker_hash,
        identity,
        requested_model,
        len(records),
        chain_head,
    )
    reasons = _publication_reasons(
        records, rejected_requests, provider_id, build_id, requested_model
    )
    return {
        "schema_version": 1,
        "event_count": len(records),
        "chain_head": chain_head,
        "seal": marker,
        "publication_gate": {"ok": not reasons, "reasons": sorted(reasons)},
        "records": records,
    }
