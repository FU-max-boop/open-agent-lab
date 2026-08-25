"""Verify one bounded, non-scoring live-provider route probe."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from harbor.models.trajectories.trajectory import Trajectory

from .codex_runtime import CODEX_RUNTIME_SPEC_SHA256
from .experiment_contract import (
    CODEX_PROVIDER_RETRY_POLICY,
    EXPERIMENT_ID,
    LIVE_ROUTE_PROBE_AGENT,
    LIVE_ROUTE_PROBE_AGENT_IMPORT,
    LIVE_ROUTE_PROBE_COMMAND,
    LIVE_ROUTE_PROBE_LIMITS,
    LIVE_ROUTE_PROBE_TASK,
    PILOT_RELAY_TTL_SECONDS,
    RELAY_CLAIM_FIELDS,
    ZAI_ROUTE_PROBE_OUTPUT_BUDGET,
    canonical_digest,
    canonical_json,
    digest_bytes,
    is_digest,
    is_strict_int,
    live_route_probe_variant,
    provider_control_window,
    relay_claim_name,
    same_json,
)
from .paired_results import (
    CompletedJob,
    IntegrityError,
    LiveRouteRun,
    RelayEvidence,
)

_CAP_FIELDS = {
    "schemaVersion",
    "proofClass",
    "experimentId",
    "provider",
    "model",
    "preflightSha256",
    "providerCredentialSha256",
    "providerControl",
    "verification",
}
_MAX_INPUT_BYTES = 64 * 1024
_MAX_SCANNED_FILE_BYTES = 64 * 1024 * 1024
_MAX_SCANNED_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_SCANNED_FILES = 4096
_MAX_SCANNED_DIRS = 1024
_MAX_SCAN_DEPTH = 32
_RELAY_START_MARGIN_SECONDS = 60


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise IntegrityError(f"{label} must be an array")
    return value


def _iso(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise IntegrityError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IntegrityError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise IntegrityError(f"{label} must include a timezone")
    return parsed


def _loads(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid JSON in {label}: {error}") from error


def _bounded_file(path: Path, label: str) -> bytes:
    """Read one bounded regular file without following the final symlink."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise IntegrityError(f"{label} must be a readable regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= _MAX_INPUT_BYTES
        ):
            raise IntegrityError(f"{label} must be a non-empty bounded regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(_MAX_INPUT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not data or len(data) > _MAX_INPUT_BYTES:
        raise IntegrityError(f"{label} must be a non-empty bounded regular file")
    return data


def _credential_bytes(path: Path, label: str) -> bytes:
    credentials = _bounded_file(path, label)
    if not credentials.isascii():
        raise IntegrityError(f"{label} must contain only ASCII bytes")
    normalized = credentials.strip()
    if len(normalized) < 32 or any(byte < 0x21 or byte > 0x7E for byte in normalized):
        raise IntegrityError(f"{label} must contain at least 32 visible ASCII bytes")
    return credentials


def _policy_layout(path: Path, provider: str, suffix: str) -> tuple[Path, Path]:
    raw = path.expanduser()
    if not raw.is_absolute():
        raise IntegrityError("authorization paths must be absolute")
    absolute = Path(os.path.abspath(raw))
    parent = absolute.parent
    try:
        run_dir = parent.parent.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise IntegrityError("authorization directory is unavailable") from error
    if (
        absolute.name != f"{provider}{suffix}"
        or parent.name != "authorizations"
        or parent.is_symlink()
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) & 0o077
        or resolved_parent != run_dir / "authorizations"
    ):
        raise IntegrityError("authorization path is outside the prepared run")
    return absolute, run_dir


def _cap_attestation(
    path: Path, provider: str, model: str, preflight_sha256: str
) -> tuple[dict[str, Any], str]:
    raw = _bounded_file(path, "provider authorization")
    try:
        value = _loads(raw.decode(), "provider authorization")
    except (UnicodeError, ValueError) as error:
        raise IntegrityError("provider authorization is invalid") from error
    cap = _mapping(value, "provider authorization")
    if raw != canonical_json(cap):
        raise IntegrityError("provider authorization must be canonical JSON")
    if (
        set(cap) != _CAP_FIELDS
        or not is_strict_int(cap.get("schemaVersion"))
        or cap["schemaVersion"] != 2
        or cap.get("proofClass") != "live-route-provider-authorization-v2"
        or cap.get("experimentId") != EXPERIMENT_ID
        or cap.get("provider") != provider
        or cap.get("model") != model
        or cap.get("preflightSha256") != preflight_sha256
        or not is_digest(cap.get("providerCredentialSha256"))
        or cap.get("verification") != "operator_attested"
    ):
        raise IntegrityError("provider authorization policy drifted")
    try:
        control, observed, expires = provider_control_window(
            cap.get("providerControl"), provider
        )
    except (TypeError, ValueError) as error:
        raise IntegrityError("provider authorization policy drifted") from error
    now = datetime.now(timezone.utc)
    if not observed <= now <= expires:
        raise IntegrityError("provider authorization is not currently valid")
    cap["providerControl"] = control
    return cap, digest_bytes(raw)


def _relay_window(expires_at: Any, ttl_seconds: int) -> None:
    expires = (
        expires_at
        if isinstance(expires_at, datetime)
        else _iso(expires_at, "authorization expiry")
    )
    required = datetime.now(timezone.utc) + timedelta(
        seconds=ttl_seconds + _RELAY_START_MARGIN_SECONDS
    )
    if expires < required:
        raise IntegrityError("authorization does not cover the full relay lifetime")


def _claim_slot(
    run_dir: Path,
    provider: str,
    role: str,
    policy_sha256: str,
    job_dir: Path,
    job_id: UUID,
    trial_lock_sha256: str,
) -> Path:
    if (
        role not in {"probe", "pilot"}
        or not is_digest(policy_sha256)
        or not is_digest(trial_lock_sha256)
    ):
        raise IntegrityError("authorization slot identity is invalid")
    claim = (
        run_dir / "authorizations" / relay_claim_name(provider, role, trial_lock_sha256)
    )
    record = {
        "schemaVersion": 1,
        "proofClass": f"{role}-relay-slot-claim-v1",
        "provider": provider,
        "policySha256": policy_sha256,
        "jobId": str(job_id),
        "jobDir": str(job_dir),
        "trialLockSha256": trial_lock_sha256,
        "claimedAt": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    try:
        _publish(claim, record)
    except IntegrityError as error:
        raise IntegrityError("authorization slot was already claimed") from error
    return claim


def _claimed_slot(
    run_dir: Path,
    provider: str,
    role: str,
    policy_path: Path,
    expected_policy_sha256: str,
    job_dir: Path,
    job_id: UUID,
    trial_lock_sha256: str,
    *,
    not_before: datetime,
    before: datetime,
) -> str:
    claim_path = (
        run_dir / "authorizations" / relay_claim_name(provider, role, trial_lock_sha256)
    )
    try:
        info = claim_path.lstat()
    except OSError as error:
        raise IntegrityError("required authorization claim is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise IntegrityError("authorization claim must be a private regular file")
    raw = _bounded_file(claim_path, "authorization claim")
    try:
        claim = _mapping(_loads(raw.decode(), "authorization claim"), "claim")
    except (UnicodeError, ValueError) as error:
        raise IntegrityError("authorization claim is invalid") from error
    claimed_at = _iso(claim.get("claimedAt"), "authorization claimedAt")
    active_policy_sha256 = digest_bytes(
        _bounded_file(policy_path, f"{role} authorization policy")
    )
    expected = {
        "schemaVersion": 1,
        "proofClass": f"{role}-relay-slot-claim-v1",
        "provider": provider,
        "policySha256": expected_policy_sha256,
        "jobId": str(job_id),
        "jobDir": str(job_dir),
        "trialLockSha256": trial_lock_sha256,
        "claimedAt": claim.get("claimedAt"),
    }
    if (
        set(claim) != RELAY_CLAIM_FIELDS
        or raw != canonical_json(claim)
        or not is_strict_int(claim.get("schemaVersion"))
        or active_policy_sha256 != expected_policy_sha256
        or not same_json(claim, expected)
        or not not_before <= claimed_at <= before
    ):
        raise IntegrityError("authorization claim differs from the active trial")
    return digest_bytes(raw)


def validate_probe_cap(
    path: Path,
    provider: str,
    model: str,
    binding: dict[str, Any],
    credential_file: Path,
    active_trial_dir: Path,
) -> dict[str, Any]:
    """Validate provider control immediately before the probe relay is opened."""
    cap_path, run_dir = _policy_layout(path, provider, ".cap.json")
    if (
        (run := LiveRouteRun.open(run_dir, provider)).model != model
        or binding != run.binding
        or run.probe.get("relayImageSha256") != binding.get("relay_image_sha256")
    ):
        raise IntegrityError("provider authorization run binding drifted")
    cap, cap_sha256 = _cap_attestation(
        cap_path, provider, model, binding["preflight_sha256"]
    )
    credential_sha256 = digest_bytes(
        _credential_bytes(credential_file, "provider credential")
    )
    if cap.get("providerCredentialSha256") != credential_sha256:
        raise IntegrityError("authorization belongs to another provider credential")
    _relay_window(
        cap["providerControl"]["expiresAt"], LIVE_ROUTE_PROBE_LIMITS["ttlSeconds"]
    )
    job_id, trial_lock_sha256 = run.probe_job.claim_active_trial(active_trial_dir)
    _claim_slot(
        run_dir,
        provider,
        "probe",
        cap_sha256,
        run.probe_job.job_dir,
        job_id,
        trial_lock_sha256,
    )
    return cap


def _agent_identity(agent: Any, run: LiveRouteRun) -> None:
    value = _mapping(agent, "probe agent")
    kwargs = _mapping(value.get("kwargs"), "probe agent kwargs")
    if (
        value.get("name") != LIVE_ROUTE_PROBE_AGENT
        or value.get("import_path") != LIVE_ROUTE_PROBE_AGENT_IMPORT
        or value.get("model_name") != f"{run.provider}/{run.model}"
        or kwargs.get("version") != run.codex_version
        or kwargs.get("reasoning_effort") != run.reasoning
        or kwargs.get("enable_verify_instruction_v1") is not False
        or kwargs.get("run_binding") != run.binding
    ):
        raise IntegrityError("live-route probe agent identity drifted")


def _probe_trial(
    run: LiveRouteRun,
    completed: CompletedJob,
) -> tuple[Path, dict[str, Any], dict[str, Any], datetime, datetime, datetime]:
    trial_dir, result, lock = completed.single_trial()
    job_dir = run.probe_job.job_dir
    task_binding = run.probe_task_binding
    task = _mapping(lock.get("task"), "trial task lock")
    result_config = _mapping(result.get("config"), "trial result config")
    result_task = _mapping(result_config.get("task"), "trial result task")
    task_path = job_dir.parents[2] / "tasks" / "live-route-probe"
    if (
        task.get("name") != "live-route-probe"
        or task.get("type") != "local"
        or task.get("digest") != task_binding["taskDigest"]
        or task.get("source") != LIVE_ROUTE_PROBE_TASK
        or Path(str(task.get("path"))).resolve() != task_path.resolve()
        or result.get("task_name") != LIVE_ROUTE_PROBE_TASK
        or result.get("source") != LIVE_ROUTE_PROBE_TASK
        or result.get("task_checksum") != task_binding["taskChecksum"]
        or result_task.get("source") != LIVE_ROUTE_PROBE_TASK
        or Path(str(result_task.get("path"))).resolve() != task_path.resolve()
        or result.get("verifier_result") is not None
        or result.get("exception_info") is not None
        or result.get("step_results") is not None
    ):
        raise IntegrityError("live-route probe task or result drifted")
    expected_task_id = json.loads(
        completed.config.tasks[0].get_task_id().model_dump_json()
    )
    if not same_json(result.get("task_id"), expected_task_id):
        raise IntegrityError("live-route probe task ID drifted")
    locked_agent = lock.get("agent")
    result_agent = result_config.get("agent")
    expected_locked_agent = json.loads(
        completed.config.agents[0].model_dump_json(exclude_none=True)
    )
    expected_result_agent = json.loads(completed.config.agents[0].model_dump_json())
    _agent_identity(locked_agent, run)
    _agent_identity(result_agent, run)
    if (
        not same_json(locked_agent, expected_locked_agent)
        or not same_json(result_agent, expected_result_agent)
        or result.get("trial_name") != trial_dir.name
        or result.get("trial_uri") != trial_dir.resolve().as_uri()
        or lock.get("install_only") is not False
        or result_config.get("install_only") is not False
        or _mapping(lock.get("verifier"), "trial verifier").get("disable") is not True
        or _mapping(result_config.get("verifier"), "result verifier").get("disable")
        is not True
    ):
        raise IntegrityError("live-route verifier must remain disabled")
    agent_info = _mapping(result.get("agent_info"), "agent_info")
    if (
        agent_info.get("name") != LIVE_ROUTE_PROBE_AGENT
        or agent_info.get("version") != run.codex_version
        or not same_json(
            agent_info.get("model_info"),
            {"name": run.model, "provider": run.provider},
        )
        or str(result.get("id")) == ""
        or str(result_config.get("job_id")) != str(completed.result.id)
    ):
        raise IntegrityError("runtime probe agent identity drifted")
    agent_started = _iso(
        _mapping(result.get("agent_execution"), "agent execution").get("started_at"),
        "agent execution start",
    )
    agent_finished = _iso(
        _mapping(result.get("agent_execution"), "agent execution").get("finished_at"),
        "agent execution finish",
    )
    trial_finished = _iso(result.get("finished_at"), "trial finish")
    if not agent_started < agent_finished <= trial_finished:
        raise IntegrityError("live-route probe timing drifted")
    return trial_dir, result, lock, agent_started, agent_finished, trial_finished


def _trajectory(
    completed: CompletedJob, trial_dir: Path, run: LiveRouteRun
) -> tuple[dict[str, Any], str]:
    value = _mapping(
        completed.artifact_json(
            trial_dir,
            "agent/trajectory.json",
            "probe trajectory",
            max_bytes=64 * 1024 * 1024,
        ),
        "probe trajectory",
    )
    try:
        Trajectory.model_validate_json(canonical_json(value))
    except ValueError as error:
        raise IntegrityError("probe trajectory is invalid ATIF") from error
    agent = _mapping(value.get("agent"), "trajectory agent")
    session = value.get("session_id")
    if (
        value.get("schema_version") != "ATIF-v1.7"
        or not isinstance(session, str)
        or not session
        or agent.get("name") != "codex"
        or agent.get("version") != run.codex_version
        or agent.get("model_name") != run.model
        or value.get("continued_trajectory_ref") is not None
        or value.get("subagent_trajectories") not in (None, [])
    ):
        raise IntegrityError("probe trajectory identity drifted")
    steps = [
        _mapping(raw_step, "trajectory step")
        for raw_step in _sequence(value.get("steps"), "trajectory steps")
    ]
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
    results: list[dict[str, Any]] = []
    agent_messages: list[str] = []
    for step in steps:
        raw_calls = step.get("tool_calls")
        if raw_calls is not None:
            calls.extend(
                (step, _mapping(call, "trajectory tool call"))
                for call in _sequence(raw_calls, "trajectory tool calls")
            )
        observation = step.get("observation")
        if observation is not None:
            results.extend(
                _mapping(result, "trajectory observation")
                for result in _sequence(
                    _mapping(observation, "trajectory observation").get("results"),
                    "trajectory observation results",
                )
            )
        message = step.get("message")
        if step.get("source") == "agent" and isinstance(message, str) and message:
            agent_messages.append(message)
    if len(calls) != 1:
        raise IntegrityError("probe must contain exactly one fixed exec_command call")
    call_step, call = calls[0]
    call_id = call.get("tool_call_id")
    extra = _mapping(call_step.get("extra") or {}, "trajectory tool metadata")
    details = _mapping(extra.get("tool_call_details") or {}, "tool call details")
    detail = _mapping(details.get(call_id) or {}, "tool call detail")
    status = detail.get("status", extra.get("status"))
    metadata = _mapping(
        detail.get("metadata", extra.get("tool_metadata")) or {},
        "tool execution metadata",
    )
    final_response_ok = run.provider == "zai" or (
        agent_messages == ["LIVE_ROUTE_PROBE_OK"]
        and steps[-1].get("source") == "agent"
        and steps[-1].get("message") == "LIVE_ROUTE_PROBE_OK"
    )
    if (
        not isinstance(call_id, str)
        or not call_id
        or len(call_id.encode()) > 256
        or call.get("function_name") != "exec_command"
        or call.get("arguments") != {"cmd": LIVE_ROUTE_PROBE_COMMAND}
        or len(results) != 1
        or results[0].get("source_call_id") != call_id
        or not isinstance(results[0].get("content"), str)
        or not results[0]["content"]
        or status != "completed"
        or not is_strict_int(metadata.get("exit_code"))
        or metadata["exit_code"] != 0
        or not final_response_ok
    ):
        raise IntegrityError("probe tool lifecycle or final response drifted")
    return value, session


def _output_budget_probe(
    records: list[dict[str, Any]], marker: dict[str, Any], provider: str, model: str
) -> dict[str, Any] | None:
    accounting = _mapping(
        marker.get("outputTokenAccounting"), "relay output-token accounting"
    )
    if provider == "deepseek":
        if (
            marker.get("budgetClass") != "unmetered_route_probe"
            or marker.get("accountingMode") != "none"
            or marker.get("slotOutputTokenLimit") is not None
            or accounting.get("state") != "unmetered"
        ):
            raise IntegrityError("DeepSeek route-probe budget policy drifted")
        return None
    limits = dict(ZAI_ROUTE_PROBE_OUTPUT_BUDGET)
    allocations = tuple(limits["roundOutputTokenLimits"])
    requests = [
        item for item in records if item.get("event") == "transport.responses.request"
    ]
    closed = [
        item for item in records if item.get("event") == "transport.responses.closed"
    ]
    expected_terminals = (
        ("response.completed", (None, "completed"), None),
        ("response.incomplete", (None, "incomplete"), "max_output_tokens"),
    )
    rounds: list[dict[str, Any]] = []
    total_reported = 0
    for index, (request, terminal, allocation) in enumerate(
        zip(requests, closed, allocations, strict=True), start=1
    ):
        usage = _mapping(terminal.get("usage"), f"round {index} relay usage")
        output_tokens = usage.get("output_tokens")
        event, statuses, reason = expected_terminals[index - 1]
        if (
            request.get("effectiveMaxOutputTokens") != allocation
            or not is_strict_int(output_tokens)
            or not 0 <= output_tokens <= allocation
            or terminal.get("terminalEvent") != event
            or terminal.get("terminalStatus") not in statuses
            or terminal.get("incompleteReason") != reason
        ):
            raise IntegrityError("ZAI truncation-probe round drifted")
        total_reported += output_tokens
        rounds.append(
            {
                "ordinal": index,
                "effectiveMaxOutputTokens": allocation,
                "reportedOutputTokens": output_tokens,
                "burnedOutputBudgetTokens": allocation,
                "terminalEvent": event,
                "terminalStatus": terminal.get("terminalStatus"),
                "incompleteReason": reason,
            }
        )
    if (
        len(requests) != 2
        or len(closed) != 2
        or marker.get("budgetClass") != "zai_route_probe"
        or marker.get("accountingMode") != "fixed_round_allocations"
        or marker.get("slotOutputTokenLimit") != limits["slotOutputTokenLimit"]
        or marker.get("rejectedRequests") != {}
        or accounting.get("state") != "probe_conformant"
        or accounting.get("reportedOutputTokens") != total_reported
        or accounting.get("conservativeOutputTokenUpperBound") != total_reported
        or accounting.get("unusedOutputTokensBurned")
        != limits["slotOutputTokenLimit"] - total_reported
    ):
        raise IntegrityError("ZAI truncation-probe accounting drifted")
    return {
        "schemaVersion": 1,
        "proofClass": "empirical-responses-truncation-v1",
        "evidenceScope": "exact_observed_date_model_endpoint",
        "observedAt": closed[-1]["at"],
        "endpoint": "https://api.z.ai/api/v1/responses",
        "model": model,
        "protocol": "openai_responses",
        "accountingMode": "fixed_round_allocations",
        "burnedAccounting": "reserved_budget_retired_not_usage",
        "slotOutputTokenLimit": limits["slotOutputTokenLimit"],
        "minimumRequestedRound2OutputTokens": limits[
            "minimumRequestedRound2OutputTokens"
        ],
        "rounds": rounds,
        "totalReportedOutputTokens": total_reported,
        "totalBurnedOutputBudgetTokens": sum(allocations),
        "noThirdRequest": True,
    }


def _relay_and_metadata(
    trial_dir: Path,
    result: dict[str, Any],
    run: LiveRouteRun,
    agent_started: datetime,
    agent_finished: datetime,
    trajectory: dict[str, Any],
    trajectory_session: str,
) -> tuple[dict[str, Any], dict[str, int | None], dict[str, Any] | None]:
    evidence = RelayEvidence.complete(
        trial_dir,
        run.provider,
        run.model,
        run.binding,
        agent_started,
        agent_finished,
    )
    records, marker = evidence.records, evidence.seal
    if (
        len(records) != 6
        or [item.get("event") for item in records]
        != [
            "transport.responses.request",
            "transport.responses.headers",
            "transport.responses.closed",
        ]
        * 2
        or marker.get("rejectedRequests") != {}
        or any(
            item.get("requestedModel") != run.model
            for item in records
            if item.get("event") == "transport.responses.request"
        )
    ):
        raise IntegrityError("live-route relay publication gate failed")
    closed = [item for item in records if item["event"] == "transport.responses.closed"]
    provider_ids = [item.get("providerRequestId") for item in closed]
    response_ids = [item.get("responseId") for item in closed]
    if (
        len(closed) != 2
        or any(
            not isinstance(item, str) or not item
            for item in provider_ids + response_ids
        )
        or len(set(provider_ids)) != 2
        or len(set(response_ids)) != 2
    ):
        raise IntegrityError("live-route provider response identity is incomplete")
    agent_result = _mapping(result.get("agent_result"), "agent result")
    expected_variant = live_route_probe_variant(run.provider, effect_verified=True)
    expected_harbor = {
        "schema_version": 1,
        "harbor_context_id": str(result["id"]),
        "harbor_session_id": f"{result['trial_name']}__agent",
        "trajectory_session_id": trajectory_session,
        "relay_instance_id": marker["relayInstanceId"],
        "relay_build_id": marker["buildId"],
        "relay_marker_sha256": marker["markerSha256"],
        "codex_runtime_spec_sha256": CODEX_RUNTIME_SPEC_SHA256,
        "provider_id": run.provider,
        "requested_model": run.model,
        "variant_id": "live-route-probe-v1",
        "requested_developer_instructions_sha256": None,
        "run_binding": run.binding,
    }
    evidence.validate_embedded(
        agent_result,
        trajectory,
        expected_variant,
        expected_harbor,
        allow_missing_agent_totals_and_metrics=run.provider == "zai",
    )
    return (
        evidence.verified,
        evidence.usage,
        _output_budget_probe(records, marker, run.provider, run.model),
    )


def _cleanup(
    run: LiveRouteRun,
    completed: CompletedJob,
    trial_dir: Path,
    result: dict[str, Any],
    agent_finished: datetime,
    credential_sha256: str,
) -> dict[str, Any]:
    receipt = _mapping(
        completed.artifact_json(
            trial_dir, "environment-cleanup.json", "environment cleanup receipt"
        ),
        "environment cleanup receipt",
    )
    task = run.probe_task_binding
    stopped = _iso(receipt.get("stoppedAt"), "cleanup stoppedAt")
    finished = _iso(result.get("finished_at"), "trial finishedAt")
    session, project = run.probe_job.environment_identity(trial_dir)
    binding = run.binding
    if (
        set(receipt)
        != {
            "schemaVersion",
            "experimentId",
            "replicationId",
            "sourceRevision",
            "experimentManifestSha256",
            "preflightSha256",
            "runBindingSha256",
            "relayImageSha256",
            "providerCredentialSha256",
            "fullComposeSha256",
            "taskId",
            "taskDigest",
            "taskChecksum",
            "sessionId",
            "projectName",
            "stoppedAt",
        }
        or not is_strict_int(receipt.get("schemaVersion"))
        or receipt.get("schemaVersion") != 1
        or receipt.get("experimentId") != EXPERIMENT_ID
        or receipt.get("replicationId") != binding["replication_id"]
        or receipt.get("sourceRevision") != binding["source_revision"]
        or receipt.get("experimentManifestSha256")
        != binding["experiment_manifest_sha256"]
        or receipt.get("preflightSha256") != binding["preflight_sha256"]
        or receipt.get("runBindingSha256") != canonical_digest(binding)
        or receipt.get("relayImageSha256") != binding["relay_image_sha256"]
        or receipt.get("providerCredentialSha256") != credential_sha256
        or not is_digest(receipt.get("fullComposeSha256"))
        or receipt.get("taskId") != LIVE_ROUTE_PROBE_TASK
        or receipt.get("taskDigest") != task["taskDigest"]
        or receipt.get("taskChecksum") != task["taskChecksum"]
        or receipt.get("sessionId") != session
        or receipt.get("projectName") != project
        or not agent_finished <= stopped <= finished
    ):
        raise IntegrityError("live-route environment cleanup receipt drifted")
    return receipt


def _scan_job(job_dir: Path, credentials: bytes) -> dict[str, int]:
    needles = {credentials, credentials.strip()}
    needles.discard(b"")
    files = 0
    total = 0
    directories = 0

    def scan(directory: int, label: str, depth: int) -> None:
        nonlocal directories, files, total
        directories += 1
        if directories > _MAX_SCANNED_DIRS or depth > _MAX_SCAN_DEPTH:
            raise IntegrityError("live-route job directory scan exceeds its bound")
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                child = f"{label}/{entry.name}"
                if stat.S_ISDIR(info.st_mode):
                    descriptor = os.open(
                        entry.name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory,
                    )
                    try:
                        scan(descriptor, child, depth + 1)
                    finally:
                        os.close(descriptor)
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > _MAX_SCANNED_FILE_BYTES
                ):
                    raise IntegrityError(f"unsafe or oversized job artifact: {child}")
                files += 1
                total += info.st_size
                if files > _MAX_SCANNED_FILES or total > _MAX_SCANNED_TOTAL_BYTES:
                    raise IntegrityError(
                        "live-route job artifact scan exceeds its bound"
                    )
                descriptor = os.open(
                    entry.name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory,
                )
                try:
                    observed = os.fstat(descriptor)
                    if (
                        observed.st_ino != info.st_ino
                        or observed.st_size != info.st_size
                    ):
                        raise IntegrityError(
                            "job artifact changed during credential scan"
                        )
                    tail = b""
                    while chunk := os.read(descriptor, 1024 * 1024):
                        sample = tail + chunk
                        if any(needle in sample for needle in needles):
                            raise IntegrityError(
                                "provider credential leaked into the job directory"
                            )
                        tail = sample[-(_MAX_INPUT_BYTES - 1) :]
                finally:
                    os.close(descriptor)

    root = os.open(
        job_dir,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        scan(root, job_dir.name, 0)
    finally:
        os.close(root)
    return {"files": files, "bytes": total, "directories": directories}


def _publish(path: Path, receipt: dict[str, Any]) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise IntegrityError("receipt output parent must be a real directory")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        data = canonical_json(receipt)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
            try:
                path.unlink()
            except OSError:
                pass
        raise IntegrityError("receipt output must be new and writable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_probe(
    run_dir: Path | str,
    provider: str,
    credential_file: Path | str,
    cap_attestation_file: Path | str,
    output: Path | str | None = None,
) -> dict[str, Any]:
    """Verify a scoped live route and, only then, authorize a benchmark start."""
    run_dir = Path(run_dir).expanduser().resolve(strict=True)
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise IntegrityError("prepared run directory is unavailable")
    credentials = _credential_bytes(
        Path(credential_file).expanduser(), "credential file"
    )
    run = LiveRouteRun.open(run_dir, provider)
    cap_path, cap_run_dir = _policy_layout(
        Path(cap_attestation_file), provider, ".cap.json"
    )
    if cap_run_dir != run_dir:
        raise IntegrityError("provider authorization belongs to another prepared run")
    cap, cap_sha256 = _cap_attestation(
        cap_path,
        provider,
        run.model,
        canonical_digest(run.preflight),
    )
    completed = run.probe_job.validate_completion()
    trial_dir, result, lock, agent_started, agent_finished, trial_finished = (
        _probe_trial(run, completed)
    )
    control = _mapping(cap["providerControl"], "provider control")
    cap_observed = _iso(control["observedAt"], "provider control observedAt")
    cap_expires = _iso(control["expiresAt"], "provider control expiresAt")
    verified_at = datetime.now(timezone.utc)
    if not (
        cap_observed <= agent_started < agent_finished <= trial_finished <= cap_expires
        and verified_at <= cap_expires
    ):
        raise IntegrityError("provider authorization does not cover the probe window")
    claim_sha256 = _claimed_slot(
        run_dir,
        provider,
        "probe",
        cap_path,
        cap_sha256,
        run.probe_job.job_dir,
        completed.result.id,
        canonical_digest(lock),
        not_before=cap_observed,
        before=agent_started,
    )
    trajectory, session = _trajectory(completed, trial_dir, run)
    verified, totals, output_budget_probe = _relay_and_metadata(
        trial_dir,
        result,
        run,
        agent_started,
        agent_finished,
        trajectory,
        session,
    )
    credential_sha256 = digest_bytes(credentials)
    if cap.get("providerCredentialSha256") != credential_sha256:
        raise IntegrityError("authorization belongs to another provider credential")
    cleanup = _cleanup(
        run, completed, trial_dir, result, agent_finished, credential_sha256
    )
    scan = _scan_job(run.probe_job.job_dir, credentials)
    closed = [
        item
        for item in verified["records"]
        if item["event"] == "transport.responses.closed"
    ]
    output_path: Path | None = None
    if output is not None:
        output_path, output_run_dir = _policy_layout(Path(output), provider, ".json")
        if output_run_dir != run_dir:
            raise IntegrityError("authorization output belongs to another prepared run")
    receipt = {
        "schemaVersion": 3,
        "proofClass": "live-route-probe-v3",
        "provider": provider,
        "model": run.model,
        "sourceRevision": run.preflight["sourceRevision"],
        "preflightSha256": run.binding["preflight_sha256"],
        "runBindingSha256": canonical_digest(run.binding),
        "configSha256": run.probe["configSha256"],
        "composeSha256": run.probe["composeSha256"],
        "fullComposeSha256": cleanup["fullComposeSha256"],
        "providerCredentialSha256": credential_sha256,
        "probeClaimSha256": claim_sha256,
        "relayMarkerSha256": verified["seal"]["markerSha256"],
        "relayChainHead": verified["chain_head"],
        "providerRequestIdsSha256": canonical_digest(
            [item["providerRequestId"] for item in closed]
        ),
        "responseIdsSha256": canonical_digest([item["responseId"] for item in closed]),
        "requestCount": 2,
        "usage": totals,
        "outputBudgetProbe": output_budget_probe,
        "credentialLeakScan": {"ok": True, **scan},
        "providerControl": control,
        "codexProviderRetryPolicy": dict(CODEX_PROVIDER_RETRY_POLICY),
        "harborTrialRetries": completed.config.retry.max_retries,
        "pilotJob": {
            key: run.pilot[key]
            for key in (
                "armOrder",
                "config",
                "configSha256",
                "jobDir",
                "compose",
                "composeSha256",
            )
        },
        "probeStartedAt": agent_started.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "probeFinishedAt": trial_finished.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "authorizationExpiresAt": control["expiresAt"],
        "liveProviderRouteObserved": True,
        "liveProviderConformance": False,
        "empiricalResponseTruncationObserved": provider == "zai",
        "benchmarkTaskInstructionUsed": False,
        "benchmarkRewardUsed": False,
        "providerControlVerification": cap["verification"],
        "benchmarkStartAuthorized": output_path is not None,
        "verifiedAt": verified_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
    }
    if output_path is not None:
        _publish(output_path, receipt)
    return receipt


def validate_pilot_authorization(
    path: Path,
    provider: str,
    model: str,
    binding: dict[str, Any],
    credential_file: Path,
    active_trial_dir: Path,
) -> dict[str, Any]:
    """Rebuild a probe proof before the production relay accepts a request."""
    receipt_path, run_dir = _policy_layout(path, provider, ".json")
    info = receipt_path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise IntegrityError("pilot authorization must be a private regular file")
    raw = _bounded_file(receipt_path, "pilot authorization")
    try:
        receipt = _mapping(
            _loads(raw.decode(), "pilot authorization"), "pilot authorization"
        )
    except (UnicodeError, ValueError) as error:
        raise IntegrityError("pilot authorization is invalid") from error
    if raw != canonical_json(receipt):
        raise IntegrityError("pilot authorization must be canonical JSON")
    fresh = verify_probe(
        run_dir,
        provider,
        credential_file,
        run_dir / "authorizations" / f"{provider}.cap.json",
    )
    expected = {
        **fresh,
        "benchmarkStartAuthorized": True,
        "verifiedAt": receipt.get("verifiedAt"),
    }
    verified = _iso(receipt.get("verifiedAt"), "authorization verifiedAt")
    finished = _iso(receipt.get("probeFinishedAt"), "probe finishedAt")
    expires = _iso(receipt.get("authorizationExpiresAt"), "authorization expiresAt")
    if (
        not same_json(receipt, expected)
        or receipt.get("model") != model
        or receipt.get("runBindingSha256") != canonical_digest(binding)
        or not finished <= verified <= datetime.now(timezone.utc) <= expires
    ):
        raise IntegrityError("pilot authorization is stale or bound to another run")
    _relay_window(expires, PILOT_RELAY_TTL_SECONDS)
    pilot = LiveRouteRun.open(run_dir, provider).pilot_job()
    job_id, trial_lock_sha256 = pilot.claim_active_trial(active_trial_dir)
    _claim_slot(
        run_dir,
        provider,
        "pilot",
        digest_bytes(raw),
        pilot.job_dir,
        job_id,
        trial_lock_sha256,
    )
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--provider", required=True, choices=LiveRouteRun.providers())
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--cap-attestation-file", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        receipt = verify_probe(
            arguments.run_dir,
            arguments.provider,
            arguments.credential_file,
            arguments.cap_attestation_file,
            arguments.output,
        )
    except (IntegrityError, OSError, TypeError, ValueError) as error:
        print(f"live-route probe rejected: {error}", file=sys.stderr)
        return 1
    if arguments.output is None:
        sys.stdout.buffer.write(canonical_json(receipt) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
