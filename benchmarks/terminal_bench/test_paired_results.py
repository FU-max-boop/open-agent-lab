import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import yaml
from harbor.agents.installed.codex import Codex
from harbor.models.job.lock import JobLock
from harbor.models.job.result import JobResult, JobStats
from harbor.models.trial.result import TrialResult

from benchmarks.terminal_bench import paired_results as paired
from benchmarks.terminal_bench.experiment_contract import (
    ENVIRONMENT_IMPORT,
    EXPERIMENT_ID,
)
from benchmarks.terminal_bench.failure_classification import (
    CLASSIFIED_EXCEPTION_TYPES,
    classify_failure,
)
from benchmarks.terminal_bench.relay_evidence import relay_metadata


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(paired._canonical(value) + "\n")


def _write_private(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(paired.canonical_json(value))
    path.chmod(0o600)


def _write_trial_result(path: Path, value: dict[str, object]) -> None:
    result = TrialResult.model_validate(value)
    _write(path, json.loads(result.model_dump_json()))


def _refresh_job_result(job_dir: Path) -> None:
    job = JobResult.model_validate_json((job_dir / "result.json").read_text())
    trials = [
        TrialResult.model_validate_json((path / "result.json").read_text())
        for path in sorted(job_dir.iterdir())
        if path.is_dir()
    ]
    job.stats = JobStats.from_trial_results(
        trials,
        n_total_trials=job.n_total_trials,
        n_retries=job.stats.n_retries,
    )
    _write(
        job_dir / "result.json",
        json.loads(job.model_dump_json(exclude={"trial_results"})),
    )


def _hash(value: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(paired._canonical(value).encode()).hexdigest()


def _runtime_receipt() -> dict[str, object]:
    spec = paired.codex_runtime_spec()
    files = spec["files"]
    assert isinstance(files, list)
    entrypoint = next(
        entry
        for entry in files
        if isinstance(entry, dict)
        and f"{spec['installRoot']}/{entry['path']}" == spec["entrypoint"]
    )
    return {
        "schema_version": 1,
        "spec_sha256": paired.CODEX_RUNTIME_SPEC_SHA256,
        "files": len(files),
        "entrypoint_sha256": entrypoint["sha256"],
    }


def _prepare_fixture_runtime(
    archive: Path, destination: Path, spec: object
) -> dict[str, object]:
    del archive
    paired.validate_codex_runtime_spec(spec)
    destination.mkdir()
    return _runtime_receipt()


def _materialize_fixture_tasks(source: Path, temp: Path) -> dict[str, dict[str, str]]:
    del source
    for snapshot in paired._declared_task_snapshots().values():
        path = temp / snapshot["relativePath"]
        path.mkdir(parents=True)
        (path / "task.toml").write_text("[task]\n")
    return paired._declared_task_snapshots()


def _relay_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _relay(
    directory: Path,
    provider: str,
    model: str,
    build_id: str,
    identity: str,
    started: datetime,
    finished: datetime,
    *,
    empty: bool = False,
    returned_model: str | None = None,
    include_optional_usage: bool = True,
    transport_state: str = "completed",
    model_consistency: str = "consistent",
    request_count: int = 1,
    terminal_metadata: bool = True,
    parse_errors: int = 0,
    metadata_conflicts: tuple[str, ...] = (),
) -> dict[str, object]:
    first_request_at = started + timedelta(seconds=2)
    identity_digest = hashlib.sha256(identity.encode()).hexdigest()
    usage = {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    if include_optional_usage:
        usage.update({"cached_input_tokens": 0, "reasoning_output_tokens": 1})
    identity_fields = {
        "schemaVersion": 1,
        "relayVersion": "native-responses-relay-v1",
        "runId": f"run-{identity_digest}",
        "relayInstanceId": str(UUID(hex=identity_digest[:32], version=4)),
        "providerId": provider,
        "buildId": build_id,
    }
    records = []
    if not empty:
        for ordinal in range(1, request_count + 1):
            requested_at = first_request_at + timedelta(milliseconds=(ordinal - 1) * 3)
            response_id = f"response-{identity}-{ordinal}"
            terminal_response = {
                "id": response_id,
                "model": returned_model or model,
                "usage": usage,
            }
            frames = (
                [
                    "data:"
                    + paired._canonical(
                        {"type": "response.completed", "response": terminal_response}
                    )
                ]
                if terminal_metadata
                else []
            )
            frames.extend("data:{" for _ in range(parse_errors))
            response_body = "\n\n".join(frames) if frames else "{}"
            request_body = paired._canonical(
                {"model": model, "store": False, "stream": True}
            )
            common = {
                **identity_fields,
                "ordinal": ordinal,
                "relayRequestId": str(
                    UUID(
                        hex=hashlib.sha256(
                            f"{identity}:{ordinal}".encode()
                        ).hexdigest()[:32],
                        version=4,
                    )
                ),
            }
            records.extend(
                [
                    {
                        **common,
                        "at": _relay_time(requested_at),
                        "event": "transport.responses.request",
                        "requestedModel": model,
                        "requestBytes": len(request_body.encode()),
                        "requestSha256": "sha256:"
                        + hashlib.sha256(request_body.encode()).hexdigest(),
                        "clientRequestId": f"client-{identity}",
                        "stream": True,
                    },
                    {
                        **common,
                        "at": _relay_time(requested_at + timedelta(milliseconds=1)),
                        "event": "transport.responses.headers",
                        "status": 200,
                        "providerRequestId": f"provider-{identity}-{ordinal}",
                        "modelHeader": model,
                        "headersMs": 1,
                    },
                    {
                        **common,
                        "at": _relay_time(requested_at + timedelta(milliseconds=2)),
                        "event": "transport.responses.closed",
                        "status": 200,
                        "providerRequestId": f"provider-{identity}-{ordinal}",
                        "transportState": transport_state,
                        "errorCategory": (
                            None
                            if transport_state == "completed"
                            else "client_disconnected"
                            if transport_state == "aborted"
                            else "upstream_failure"
                        ),
                        "responseBytes": len(response_body.encode()),
                        "responseSha256": "sha256:"
                        + hashlib.sha256(response_body.encode()).hexdigest(),
                        "durationMs": 2,
                        "firstByteMs": 1,
                        "parseErrors": parse_errors,
                        "metadataConflicts": list(metadata_conflicts),
                        "modelConsistency": model_consistency,
                        "modelSources": {
                            "http.openai-model.0": model,
                            **(
                                {
                                    "event.response.completed.response.model.1": (
                                        returned_model or model
                                    )
                                }
                                if terminal_metadata
                                else {}
                            ),
                        },
                        "returnedModel": returned_model or model
                        if terminal_metadata
                        else None,
                        "responseId": response_id if terminal_metadata else None,
                        "systemFingerprint": None,
                        "terminalEvent": "response.completed"
                        if terminal_metadata
                        else None,
                        "usage": usage if terminal_metadata else None,
                    },
                ]
            )
    previous = None
    rendered = []
    for record in records:
        body = {**record, "previousEventSha256": previous}
        previous = _hash(body)
        rendered.append(paired._canonical({**body, "eventSha256": previous}))
    journal = "\n".join(rendered) + ("\n" if rendered else "")
    marker = {
        "schemaVersion": 1,
        "relayVersion": "native-responses-relay-v1",
        "runId": identity_fields["runId"],
        "relayInstanceId": identity_fields["relayInstanceId"],
        "providerId": provider,
        "buildId": identity_fields["buildId"],
        "state": "sealed",
        "expectedModel": model,
        "sealedAt": _relay_time(finished - timedelta(seconds=1)),
        "eventCount": len(records),
        "chainHead": previous,
        "rejectedRequests": {},
    }
    seal = paired._canonical({**marker, "markerSha256": _hash(marker)}) + "\n"
    host = directory / "artifacts" / "provider-evidence"
    generic = directory / "artifacts"
    host.mkdir(parents=True, exist_ok=True)
    (host / "provider-metadata.ndjson").write_text(journal)
    (host / "provider-metadata.ndjson.sealed").write_text(seal)
    (generic / "provider-metadata.ndjson").write_text(journal)
    (generic / "provider-metadata.ndjson.sealed").write_text(seal)
    _write(
        generic / "manifest.json",
        [
            {
                "source": "/logs/artifacts",
                "destination": "artifacts/logs/artifacts",
                "type": "directory",
                "status": "empty",
                "service": None,
            },
            *[
                {
                    "source": f"/var/lib/open-agent-lab/{name}",
                    "destination": f"artifacts/{name}",
                    "type": "file",
                    "status": "ok",
                    "service": "open-agent-lab-relay",
                }
                for name in (
                    "provider-metadata.ndjson",
                    "provider-metadata.ndjson.sealed",
                )
            ],
        ],
    )
    return relay_metadata(
        host / "provider-metadata.ndjson",
        host / "provider-metadata.ndjson.sealed",
        allow_empty=empty,
    )


def _agent(
    provider: str, model: str, variant: str, binding: dict[str, object]
) -> dict[str, object]:
    spec = paired._VARIANTS[variant]
    return {
        "name": spec["name"],
        "import_path": spec["import_path"],
        "model_name": f"{provider}/{model}",
        "kwargs": {
            "version": "0.149.0",
            "reasoning_effort": paired._PROVIDERS[provider]["reasoning"],
            "enable_verify_instruction_v1": spec["enabled"],
            "run_binding": binding,
        },
        "env": {
            "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
            paired.PILOT_RECEIPT_ENV: str(
                Path("/unused") / "authorizations" / f"{provider}.json"
            ),
        },
    }


def _trial_lock(
    provider: str,
    model: str,
    task: str,
    variant: str,
    binding: dict[str, object],
    task_path: Path,
    compose_path: Path,
    compose_sha256: str,
) -> dict[str, object]:
    compose = str(compose_path)
    spec = paired._VARIANTS[variant]
    runtime_root = task_path.parent.parent / paired.CODEX_RUNTIME_PREPARED_RELATIVE
    return {
        "schema_version": 2,
        "task": {
            "name": task.removeprefix("terminal-bench/"),
            "type": "local",
            "digest": paired._TASK_DIGESTS[task],
            "source": paired._DATASET,
            "path": str(task_path),
        },
        "install_only": False,
        "timeout_multiplier": 1.0,
        "agent": {
            "name": spec["name"],
            "import_path": spec["import_path"],
            "model_name": f"{provider}/{model}",
            "skills": [],
            "resume_trajectory": False,
            "extra_allowed_hosts": [],
            "kwargs": {
                "version": "0.149.0",
                "reasoning_effort": paired._PROVIDERS[provider]["reasoning"],
                "enable_verify_instruction_v1": spec["enabled"],
                "run_binding": binding,
            },
            "env": {
                "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                paired.PILOT_RECEIPT_ENV: str(
                    task_path.parent.parent / "authorizations" / f"{provider}.json"
                ),
            },
            "mcp_servers": [],
        },
        "bridge_inputs": {},
        "skills": [],
        "environment": {
            "type": "docker",
            "import_path": ENVIRONMENT_IMPORT,
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "mounts": [paired._codex_runtime_mount(runtime_root)],
            "extra_docker_compose": [compose],
            "kwargs": {
                "relay_compose_sha256": compose_sha256,
                "run_binding": binding,
            },
            "extra_allowed_hosts": [],
        },
        "extra_docker_compose": [
            {
                "path": compose,
                "digest": compose_sha256,
            }
        ],
        "verifier": {"disable": False, "environment_mode": "shared"},
    }


def _replace_relay(
    result: dict[str, object],
    verified: dict[str, object],
    *,
    trajectory_missing: bool = False,
) -> None:
    agent_result = result["agent_result"]
    assert isinstance(agent_result, dict)
    metadata = agent_result["metadata"]
    assert isinstance(metadata, dict)
    provider_data = metadata["open_agent_lab_provider"]
    assert isinstance(provider_data, dict)
    for key in (
        "event_count",
        "chain_head",
        "seal",
        "records",
        "publication_gate",
    ):
        provider_data[key] = verified[key]
    binding = provider_data["harbor_binding"]
    seal = verified["seal"]
    assert isinstance(binding, dict) and isinstance(seal, dict)
    binding["relay_instance_id"] = seal["relayInstanceId"]
    binding["relay_build_id"] = seal["buildId"]
    binding["relay_marker_sha256"] = seal["markerSha256"]
    if trajectory_missing:
        binding["trajectory_session_id"] = None
        gate = verified["publication_gate"]
        assert isinstance(gate, dict) and isinstance(gate["reasons"], list)
        reasons = sorted({*gate["reasons"], "trajectory_session_missing"})
        provider_data["publication_gate"] = {"ok": not reasons, "reasons": reasons}
    binding["binding_sha256"] = paired._digest(
        {key: value for key, value in binding.items() if key != "binding_sha256"}
    )


def _rewrite_relay(trial: Path, mutation: str) -> None:
    evidence = trial / "artifacts" / "provider-evidence"
    journal_path = evidence / "provider-metadata.ndjson"
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    if mutation == "wrong-header-model":
        records[1]["modelHeader"] = "unexpected-model"
    elif mutation == "wrong-source-model":
        records[2]["modelSources"]["event.response.completed.response.model.1"] = (
            "unexpected-model"
        )
    elif mutation == "numeric-provider-request-id":
        records[1]["providerRequestId"] = 7
        records[2]["providerRequestId"] = 7
    elif mutation == "utf16-oversized-provider-request-id":
        records[1]["providerRequestId"] = "🧪" * 300
        records[2]["providerRequestId"] = "🧪" * 300
    elif mutation == "numeric-response-id":
        records[2]["responseId"] = 7
    elif mutation == "non-stream-request":
        records[0]["stream"] = False
    elif mutation == "completed-error":
        records[2]["errorCategory"] = "forged_error"
    elif mutation == "completed-bodyless-status":
        records[1]["status"] = 204
        records[2]["status"] = 204
    elif mutation == "completed-redirect-status":
        records[1]["status"] = 302
        records[2]["status"] = 302
    elif mutation == "redirect-error-with-success-status":
        records[2].update(
            {"transportState": "failed", "errorCategory": "upstream_redirect"}
        )
    elif mutation == "connect-timeout-after-headers":
        records[2].update(
            {
                "transportState": "failed",
                "errorCategory": "upstream_connect_timeout",
            }
        )
    elif mutation == "body-missing-before-headers":
        empty_hash = (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        records[1].update(
            {
                "status": None,
                "providerRequestId": None,
                "modelHeader": None,
                "headersMs": None,
            }
        )
        records[2].update(
            {
                "status": None,
                "providerRequestId": None,
                "transportState": "failed",
                "errorCategory": "upstream_body_missing",
                "responseBytes": 0,
                "responseSha256": empty_hash,
                "firstByteMs": None,
                "responseId": None,
                "returnedModel": None,
                "modelConsistency": "missing",
                "modelSources": {},
                "systemFingerprint": None,
                "terminalEvent": None,
                "usage": None,
                "metadataConflicts": [],
                "parseErrors": 0,
            }
        )
    elif mutation == "aborted-nonclient-error":
        records[2].update(
            {"transportState": "aborted", "errorCategory": "upstream_aborted"}
        )
    elif mutation == "failed-client-error":
        records[2].update(
            {"transportState": "failed", "errorCategory": "client_disconnected"}
        )
    elif mutation == "informational-status":
        records[1]["status"] = 199
        records[2].update(
            {
                "status": 199,
                "transportState": "failed",
                "errorCategory": "upstream_failure",
            }
        )
    elif mutation == "noncanonical-time":
        records[0]["at"] = records[0]["at"].replace("Z", "+00:00")
    elif mutation == "extra-event-field":
        records[0]["unknown"] = True
    elif mutation == "unknown-model-state":
        records[2]["modelConsistency"] = "unknown"
    elif mutation == "missing-terminal-state":
        records[2].update(
            {
                "terminalEvent": None,
                "returnedModel": None,
                "responseId": None,
                "usage": None,
                "metadataConflicts": [],
                "parseErrors": 0,
            }
        )
    elif mutation == "second-terminal-source":
        records[2]["modelSources"][
            "event.response.failed.response.headers.openai-model.2"
        ] = records[0]["requestedModel"]
    elif mutation == "zero-response-contradiction":
        records[2]["responseBytes"] = 0
        records[2]["firstByteMs"] = 1
    elif mutation == "first-byte-after-duration":
        records[2]["durationMs"] = 1
        records[2]["firstByteMs"] = 2
    elif mutation == "headers-after-duration":
        records[1]["headersMs"] = 3
        records[2]["durationMs"] = 2
    elif mutation == "duration-timestamp-mismatch":
        records[2]["durationMs"] = 3
    elif mutation == "fractional-duration":
        records[2]["durationMs"] = 2.0
    elif mutation == "empty-request":
        records[0]["requestBytes"] = 0
    elif mutation == "undersized-request":
        records[0]["requestBytes"] = 2
        records[0]["requestSha256"] = (
            "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        )
    elif mutation == "oversized-request":
        records[0]["requestBytes"] = 64 * 1024 * 1024 + 1
    elif mutation == "oversized-response":
        records[2]["responseBytes"] = 64 * 1024 * 1024 + 1
    elif mutation == "parse-errors-exceed-response":
        records[2]["parseErrors"] = records[2]["responseBytes"] + 1
    elif mutation == "event-index-exceeds-response":
        model = records[2]["modelSources"].pop(
            "event.response.completed.response.model.1"
        )
        records[2]["modelSources"][
            f"event.response.completed.response.model.{records[2]['responseBytes'] + 1}"
        ] = model
    elif mutation == "empty-request-hash":
        records[0]["requestSha256"] = (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
    elif mutation == "empty-response-hash":
        records[2]["responseSha256"] = (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
    elif mutation == "completed-null-status":
        records[1].update(
            {"status": None, "providerRequestId": None, "headersMs": None}
        )
        records[2].update({"status": None, "providerRequestId": None})
    elif mutation == "null-status-header-metadata":
        empty_hash = (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        records[1].update({"status": None, "headersMs": None})
        records[2].update(
            {
                "status": None,
                "transportState": "aborted",
                "errorCategory": "client_disconnected",
                "responseBytes": 0,
                "responseSha256": empty_hash,
                "firstByteMs": None,
                "modelSources": {"http.openai-model.0": records[0]["requestedModel"]},
                "returnedModel": None,
                "responseId": None,
                "terminalEvent": None,
                "usage": None,
            }
        )
    elif mutation == "unsafe-usage":
        records[2]["usage"] = {
            "input_tokens": 2**60,
            "output_tokens": 1,
            "total_tokens": 2**60 + 1,
        }
    elif mutation == "duplicate-upstream-identities":
        if len(records) < 6:
            raise AssertionError("duplicate identity mutation requires two lifecycles")
        records[4]["providerRequestId"] = records[1]["providerRequestId"]
        records[5]["providerRequestId"] = records[2]["providerRequestId"]
        records[5]["responseId"] = records[2]["responseId"]
    elif mutation == "setup-time":
        result = json.loads((trial / "result.json").read_text())
        records[0]["at"] = _relay_time(
            datetime.fromisoformat(result["started_at"]) + timedelta(milliseconds=500)
        )
    elif mutation in {
        "verifier-seal",
        "invalid-sealed-at",
        "extra-seal-field",
        "impossible-rejections",
    }:
        pass
    elif mutation in {
        "garbage-model-source",
        "nonterminal-model-source",
        "terminal-header-model-source",
        "header-only-model-source",
    }:
        model = records[0]["requestedModel"]
        if mutation != "header-only-model-source":
            records[1]["modelHeader"] = None
        key = {
            "garbage-model-source": "garbage",
            "nonterminal-model-source": "event.response.created.response.model.1",
            "terminal-header-model-source": (
                "event.response.completed.response.headers.openai-model.1"
            ),
            "header-only-model-source": "http.openai-model.0",
        }[mutation]
        records[2]["modelSources"] = {key: model}
    else:
        raise AssertionError(mutation)
    previous = None
    rendered = []
    for record in records:
        record.pop("eventSha256")
        record["previousEventSha256"] = previous
        previous = _hash(record)
        rendered.append(paired._canonical({**record, "eventSha256": previous}))
    journal = "\n".join(rendered) + "\n"
    marker_path = evidence / "provider-metadata.ndjson.sealed"
    marker = json.loads(marker_path.read_text())
    marker.pop("markerSha256")
    marker["chainHead"] = previous
    if mutation == "verifier-seal":
        result = json.loads((trial / "result.json").read_text())
        marker["sealedAt"] = result["verifier"]["started_at"]
    elif mutation == "invalid-sealed-at":
        marker["sealedAt"] = "not-a-time"
    elif mutation == "extra-seal-field":
        marker["unknown"] = True
    elif mutation == "impossible-rejections":
        marker["rejectedRequests"] = {
            "unknown": 1,
            "invalid_json": 0,
            "client_disconnected_after_close": len(records) // 3 + 1,
        }
    seal = paired._canonical({**marker, "markerSha256": _hash(marker)}) + "\n"
    journal_path.write_text(journal)
    marker_path.write_text(seal)
    (trial / "artifacts" / journal_path.name).write_text(journal)
    (trial / "artifacts" / marker_path.name).write_text(seal)


def _failure_info(
    result: dict[str, object], exception_type: str = "AgentTimeoutError"
) -> dict[str, object]:
    agent_execution = result["agent_execution"]
    assert isinstance(agent_execution, dict)
    finished = datetime.fromisoformat(str(agent_execution["finished_at"]))
    return {
        "exception_type": exception_type,
        "exception_message": "agent execution failed",
        "exception_traceback": f"{exception_type}: failed",
        "occurred_at": (finished + timedelta(milliseconds=100)).isoformat(),
    }


def _live_probe_records(
    root: Path,
    binding: dict[str, object],
    images: dict[str, str],
    replication: str,
) -> list[dict[str, object]]:
    probes: list[dict[str, object]] = []
    for provider, profile in paired._PROVIDERS.items():
        compose_path = (
            root / "overlays" / f"relay.{provider}.live-route-probe.compose.yaml"
        )
        compose_path.parent.mkdir(exist_ok=True)
        compose_text = yaml.safe_dump(
            paired._pinned_overlay(
                paired._repo_root(),
                provider,
                images["production"],
                live_route_probe=True,
            ),
            sort_keys=False,
        )
        compose_path.write_text(compose_text)
        compose_sha256 = paired._digest_bytes(compose_text.encode())
        config_path = root / "live-route-probes" / f"{provider}.yaml"
        config_path.parent.mkdir(exist_ok=True)
        config_text = yaml.safe_dump(
            paired.live_route_probe_config(
                root,
                binding,
                provider,
                profile["model"],
                profile["reasoning"],
                compose_path,
                compose_sha256,
            ),
            sort_keys=False,
        )
        config_path.write_text(config_text)
        job_name = f"open-agent-lab-{replication}-{provider}-live-route-probe"
        probes.append(
            {
                "provider": provider,
                "model": profile["model"],
                "reasoning": profile["reasoning"],
                "task": paired.LIVE_ROUTE_PROBE_TASK,
                "config": f"live-route-probes/{provider}.yaml",
                "configSha256": paired._digest_bytes(config_text.encode()),
                "jobDir": f"live-route-jobs/{provider}/{job_name}",
                "compose": f"overlays/relay.{provider}.live-route-probe.compose.yaml",
                "composeSha256": compose_sha256,
                "relayImageSha256": images["production"],
                "limits": dict(paired.LIVE_ROUTE_PROBE_LIMITS),
            }
        )
    return probes


def _pilot_authorization(
    provider: str,
    model: str,
    binding: dict[str, object],
    probe: dict[str, object],
    pilot: dict[str, object],
    base_start: datetime,
    credential_sha256: str,
) -> dict[str, object]:
    def stamp(offset_ms: int) -> str:
        return (
            (base_start + timedelta(milliseconds=offset_ms))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    expires = base_start + timedelta(hours=5)
    return {
        "schemaVersion": 1,
        "proofClass": "live-route-probe-v1",
        "provider": provider,
        "model": model,
        "sourceRevision": binding["source_revision"],
        "preflightSha256": binding["preflight_sha256"],
        "runBindingSha256": paired._digest(binding),
        "configSha256": probe["configSha256"],
        "composeSha256": probe["composeSha256"],
        "fullComposeSha256": "sha256:" + "5" * 64,
        "providerCredentialSha256": credential_sha256,
        "probeClaimSha256": "sha256:" + "6" * 64,
        "relayMarkerSha256": "sha256:" + "7" * 64,
        "relayChainHead": "sha256:" + "8" * 64,
        "providerRequestIdsSha256": "sha256:" + "9" * 64,
        "responseIdsSha256": "sha256:" + "a" * 64,
        "requestCount": 2,
        "usage": {
            "input_tokens": 6,
            "cached_input_tokens": 0,
            "output_tokens": 4,
            "reasoning_output_tokens": 2,
            "total_tokens": 10,
        },
        "credentialLeakScan": {
            "ok": True,
            "files": 1,
            "bytes": 100,
            "directories": 1,
        },
        "spendCap": {
            "limitUsd": 2,
            "observedAt": stamp(0),
            "expiresAt": expires.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "evidenceSha256": "sha256:" + "b" * 64,
            "assertedBy": "fixture operator",
        },
        "pilotJob": {
            key: pilot[key]
            for key in (
                "armOrder",
                "config",
                "configSha256",
                "jobDir",
                "compose",
                "composeSha256",
            )
        },
        "probeStartedAt": stamp(100),
        "probeFinishedAt": stamp(200),
        "authorizationExpiresAt": expires.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "liveProviderRouteObserved": True,
        "liveProviderConformance": False,
        "benchmarkTaskInstructionUsed": False,
        "benchmarkRewardUsed": False,
        "spendCapVerification": "operator_attested",
        "benchmarkStartAuthorized": True,
        "verifiedAt": stamp(300),
    }


class RunFixture:
    def __init__(
        self,
        root: Path,
        replication: str,
        source: str = "a" * 40,
        *,
        created_at: str = "2026-08-22T00:00:00Z",
        trial_start: datetime | None = None,
    ) -> None:
        self.root = (root / replication).resolve()
        self.root.mkdir()
        manifest_path = paired._repo_root() / paired._MANIFEST
        manifest = json.loads(manifest_path.read_text())
        self.tasks = manifest["runtime"]["taskOrder"]
        manifest_sha = paired._digest_bytes(manifest_path.read_bytes())
        self.images = {
            "production": "sha256:" + "1" * 64,
            "providerFreeFixture": "sha256:" + "2" * 64,
        }
        self.image_tags = paired._relay_image_tags(self.root, source)
        self.task_snapshots = paired._declared_task_snapshots()
        for task in self.task_snapshots:
            task_path = self.root / self.task_snapshots[task]["relativePath"]
            task_path.mkdir(parents=True)
            (task_path / "task.toml").write_text("[task]\n")
        self.preflight = {
            "schemaVersion": 1,
            "experimentId": EXPERIMENT_ID,
            "replicationId": replication,
            "sourceRevision": source,
            "experimentManifestSha256": manifest_sha,
            "relayBuildSha256": manifest["relayBuildIds"]["production"],
            "relayImageSha256": self.images["production"],
            "taskSnapshotsSha256": paired._digest(self.task_snapshots),
            "cleanTree": True,
            "createdAt": created_at,
        }
        preflight_sha = paired._digest(self.preflight)
        self.binding = {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "replication_id": replication,
            "source_revision": source,
            "experiment_manifest_sha256": manifest_sha,
            "relay_build_sha256": self.preflight["relayBuildSha256"],
            "relay_image_sha256": self.preflight["relayImageSha256"],
            "task_snapshots_sha256": self.preflight["taskSnapshotsSha256"],
            "preflight_sha256": preflight_sha,
        }
        replication_spec = next(
            item for item in manifest["replications"] if item["id"] == replication
        )
        replication_offset = 0 if replication == "screen-v1" else 100_000
        base_start = trial_start or datetime(2026, 8, 22, tzinfo=timezone.utc)
        live_route_probes = _live_probe_records(
            self.root, self.binding, self.images, replication
        )
        probes_by_provider = {
            str(probe["provider"]): probe for probe in live_route_probes
        }
        providers = []
        (self.root / "authorizations").mkdir(mode=0o700)
        self.trials: dict[tuple[str, str, str], Path] = {}
        for provider, provider_spec in paired._PROVIDERS.items():
            model = provider_spec["model"]
            order = replication_spec["armOrderByProvider"][provider]
            job_name = f"open-agent-lab-{replication}-{provider}"
            job_dir = self.root / "jobs" / provider / job_name
            job_dir.mkdir(parents=True)
            config = yaml.safe_load(
                (paired._repo_root() / paired._TEMPLATES[provider]).read_text()
            )
            template_agents = {
                (
                    "verify-instruction-v1"
                    if agent["kwargs"]["enable_verify_instruction_v1"]
                    else "control-v1"
                ): agent
                for agent in config["agents"]
            }
            config["agents"] = [template_agents[variant] for variant in order]
            for agent in config["agents"]:
                agent["kwargs"]["run_binding"] = self.binding
                agent["env"] = {
                    "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                    paired.PILOT_RECEIPT_ENV: str(
                        self.root / "authorizations" / f"{provider}.json"
                    ),
                }
            config["job_name"] = job_name
            config["jobs_dir"] = str(job_dir.parent)
            config["datasets"] = []
            config["tasks"] = [
                {
                    "path": str(self.root / self.task_snapshots[task]["relativePath"]),
                    "source": paired._DATASET,
                }
                for task in self.tasks
            ]
            compose_path = self.root / "overlays" / f"relay.{provider}.compose.yaml"
            compose_path.parent.mkdir(exist_ok=True)
            compose_text = yaml.safe_dump(
                paired._pinned_overlay(
                    paired._repo_root(), provider, self.images["production"]
                ),
                sort_keys=False,
            )
            compose_path.write_text(compose_text)
            compose_sha256 = paired._digest_bytes(compose_text.encode())
            config["environment"]["extra_docker_compose"] = [str(compose_path)]
            config["environment"]["import_path"] = ENVIRONMENT_IMPORT
            config["environment"]["mounts"] = [
                paired._codex_runtime_mount(
                    self.root / paired.CODEX_RUNTIME_PREPARED_RELATIVE
                )
            ]
            config["environment"]["kwargs"] = {
                "relay_compose_sha256": compose_sha256,
                "run_binding": self.binding,
            }
            job_id = UUID(int=2000 if provider == "deepseek" else 2001)
            authorization_path = self.root / "authorizations" / f"{provider}.json"
            parsed_job_config = paired.JobConfig.model_validate(config)
            trial_results: list[TrialResult] = []
            config_path = self.root / "configs" / f"{provider}.yaml"
            config_path.parent.mkdir(exist_ok=True)
            rendered = yaml.safe_dump(config, sort_keys=False)
            config_path.write_text(rendered)
            pilot = {
                "provider": provider,
                "model": model,
                "armOrder": order,
                "config": f"configs/{provider}.yaml",
                "configSha256": paired._digest_bytes(rendered.encode()),
                "jobDir": f"jobs/{provider}/{job_name}",
                "compose": f"overlays/relay.{provider}.compose.yaml",
                "composeSha256": compose_sha256,
                "relayImageSha256": self.images["production"],
            }
            providers.append(pilot)
            credential_sha256 = paired._digest(
                {"provider": provider, "fixture": "credential"}
            )
            _write_private(
                authorization_path,
                _pilot_authorization(
                    provider,
                    model,
                    self.binding,
                    probes_by_provider[provider],
                    pilot,
                    base_start,
                    credential_sha256,
                ),
            )
            authorization_sha256 = paired._digest_bytes(authorization_path.read_bytes())
            locks = []
            ordinal = 0
            for task in self.tasks:
                for variant in order:
                    trial_dir = job_dir / f"trial-{ordinal:02d}-{variant}"
                    trial_dir.mkdir()
                    self.trials[(provider, task, variant)] = trial_dir
                    agent = _agent(provider, model, variant, self.binding)
                    lock = _trial_lock(
                        provider,
                        model,
                        task,
                        variant,
                        self.binding,
                        self.root / self.task_snapshots[task]["relativePath"],
                        compose_path,
                        compose_sha256,
                    )
                    locks.append(lock)
                    attempt_started = base_start + timedelta(seconds=ordinal * 20)
                    attempt_finished = attempt_started + timedelta(seconds=10)
                    identity = f"{replication}-{provider}-{ordinal}"
                    lock_sha256 = paired._digest(lock)
                    _write_private(
                        self.root
                        / "authorizations"
                        / paired.relay_claim_name(provider, "pilot", lock_sha256),
                        {
                            "schemaVersion": 1,
                            "proofClass": "pilot-relay-slot-claim-v1",
                            "provider": provider,
                            "policySha256": authorization_sha256,
                            "jobId": str(job_id),
                            "jobDir": str(job_dir),
                            "trialLockSha256": lock_sha256,
                            "claimedAt": (attempt_started + timedelta(milliseconds=500))
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z"),
                        },
                    )
                    verified = _relay(
                        trial_dir,
                        provider,
                        model,
                        self.binding["relay_build_sha256"],
                        identity,
                        attempt_started,
                        attempt_finished,
                    )
                    session = f"session-{identity}"
                    spec = paired._VARIANTS[variant]
                    trial_id = str(
                        UUID(
                            int=replication_offset
                            + ordinal
                            + (1 if provider == "deepseek" else 1000)
                        )
                    )
                    harbor_binding = {
                        "schema_version": 1,
                        "harbor_context_id": trial_id,
                        "harbor_session_id": f"{trial_dir.name}__agent",
                        "trajectory_session_id": session,
                        "relay_instance_id": verified["seal"]["relayInstanceId"],
                        "relay_build_id": verified["seal"]["buildId"],
                        "relay_marker_sha256": verified["seal"]["markerSha256"],
                        "codex_runtime_spec_sha256": (paired.CODEX_RUNTIME_SPEC_SHA256),
                        "provider_id": provider,
                        "requested_model": model,
                        "variant_id": variant,
                        "requested_developer_instructions_sha256": spec[
                            "instruction_sha256"
                        ],
                        "run_binding": self.binding,
                    }
                    harbor_binding["binding_sha256"] = paired._digest(harbor_binding)
                    provider_data = {
                        **verified,
                        "harbor_binding": harbor_binding,
                        "agent_variant": {
                            "schema_version": 1,
                            "variant_id": variant,
                            "developer_instruction_requested": spec["enabled"],
                            "requested_developer_instructions_sha256": spec[
                                "instruction_sha256"
                            ],
                        },
                    }
                    result_config, task_id = paired._expected_result_config(
                        trial_dir,
                        task,
                        variant,
                        parsed_job_config,
                        job_id,
                    )
                    result = {
                        "id": trial_id,
                        "trial_name": trial_dir.name,
                        "task_name": task,
                        "trial_uri": trial_dir.resolve().as_uri(),
                        "task_id": task_id,
                        "source": paired._DATASET,
                        "task_checksum": paired._TASK_RUNTIME_BINDINGS[task][
                            "taskChecksum"
                        ],
                        "config": result_config,
                        "agent_info": {
                            "name": spec["name"],
                            "version": "0.149.0",
                            "model_info": {"name": model, "provider": provider},
                        },
                        "agent_result": {
                            "n_input_tokens": 3,
                            "n_cache_tokens": 0,
                            "n_output_tokens": 2,
                            "metadata": {"open_agent_lab_provider": provider_data},
                        },
                        "verifier_result": {
                            "rewards": {
                                "reward": 0.6
                                if variant == "verify-instruction-v1"
                                else 0.4
                            }
                        },
                        "verifier_environment_mode": "shared",
                        "exception_info": None,
                        "started_at": attempt_started.isoformat(),
                        "finished_at": attempt_finished.isoformat(),
                        "agent_execution": {
                            "started_at": (
                                attempt_started + timedelta(seconds=1)
                            ).isoformat(),
                            "finished_at": (
                                attempt_finished - timedelta(seconds=1)
                            ).isoformat(),
                        },
                        "environment_setup": {
                            "started_at": attempt_started.isoformat(),
                            "finished_at": (
                                attempt_started + timedelta(milliseconds=200)
                            ).isoformat(),
                        },
                        "agent_setup": {
                            "started_at": (
                                attempt_started + timedelta(milliseconds=300)
                            ).isoformat(),
                            "finished_at": (
                                attempt_started + timedelta(milliseconds=800)
                            ).isoformat(),
                        },
                        "verifier": {
                            "started_at": (
                                attempt_finished - timedelta(milliseconds=800)
                            ).isoformat(),
                            "finished_at": (
                                attempt_finished - timedelta(milliseconds=200)
                            ).isoformat(),
                        },
                        "step_results": None,
                    }
                    trial_result = TrialResult.model_validate(result)
                    result = json.loads(trial_result.model_dump_json())
                    trial_results.append(trial_result)
                    trajectory = {
                        "schema_version": "ATIF-v1.7",
                        "session_id": session,
                        "agent": {
                            "name": "codex",
                            "version": "0.149.0",
                            "model_name": model,
                        },
                        "steps": [
                            {
                                "step_id": 1,
                                "source": "agent",
                                "message": "done",
                                "tool_calls": [
                                    {
                                        "tool_call_id": "call-1",
                                        "function_name": "exec_command",
                                        "arguments": {},
                                    }
                                ],
                            }
                        ],
                        "final_metrics": {
                            "total_prompt_tokens": 3,
                            "total_completion_tokens": 2,
                            "total_cached_tokens": None,
                            "total_steps": 1,
                            "extra": {
                                "reasoning_output_tokens": 1,
                                "total_tokens": 5,
                            },
                        },
                    }
                    _write(trial_dir / "lock.json", lock)
                    _write(trial_dir / "result.json", result)
                    _write(trial_dir / "agent" / "trajectory.json", trajectory)
                    _write(
                        trial_dir / "environment-cleanup.json",
                        {
                            "schemaVersion": 1,
                            "experimentId": EXPERIMENT_ID,
                            "replicationId": replication,
                            "sourceRevision": source,
                            "experimentManifestSha256": manifest_sha,
                            "preflightSha256": preflight_sha,
                            "runBindingSha256": paired._digest(self.binding),
                            "relayImageSha256": self.images["production"],
                            "providerCredentialSha256": credential_sha256,
                            "fullComposeSha256": paired._digest(
                                {
                                    "provider": provider,
                                    "task": task,
                                    "variant": variant,
                                }
                            ),
                            "taskId": task,
                            "taskDigest": paired._TASK_RUNTIME_BINDINGS[task][
                                "taskDigest"
                            ],
                            "taskChecksum": paired._TASK_RUNTIME_BINDINGS[task][
                                "taskChecksum"
                            ],
                            "sessionId": f"{trial_dir.name}__env",
                            "projectName": f"{trial_dir.name}__env",
                            "stoppedAt": (
                                attempt_finished - timedelta(milliseconds=100)
                            ).isoformat(),
                        },
                    )
                    ordinal += 1
            _write(
                job_dir / "config.json",
                paired.JobConfig.model_validate(config).model_dump(
                    mode="json", exclude_defaults=True
                ),
            )
            job_lock = JobLock.model_validate(
                {
                    "schema_version": 3,
                    "created_at": base_start,
                    "harbor": {"version": "0.22.0", "is_editable": False},
                    "n_concurrent_trials": 1,
                    "retry": parsed_job_config.retry,
                    "trials": locks,
                }
            )
            _write(
                job_dir / "lock.json",
                json.loads(job_lock.model_dump_json(exclude_none=True)),
            )
            _write(
                job_dir / "result.json",
                json.loads(
                    JobResult(
                        id=job_id,
                        started_at=base_start.replace(tzinfo=None),
                        updated_at=base_start.replace(
                            hour=1, minute=0, second=0, microsecond=0, tzinfo=None
                        ),
                        finished_at=base_start.replace(
                            hour=1, minute=0, second=0, microsecond=0, tzinfo=None
                        ),
                        n_total_trials=10,
                        stats=JobStats.from_trial_results(
                            trial_results, n_total_trials=10
                        ),
                    ).model_dump_json(exclude={"trial_results"})
                ),
            )
        _write(
            self.root / "run-record.json",
            {
                "schemaVersion": 1,
                "preflight": self.preflight,
                "preflightSha256": preflight_sha,
                "relayImages": self.images,
                "relayImageTags": self.image_tags,
                "taskSnapshots": self.task_snapshots,
                "codexRuntime": _runtime_receipt(),
                "providers": providers,
                "liveRouteProbes": live_route_probes,
            },
        )


class StrictInputTest(unittest.TestCase):
    def test_invalid_cli_summary_uses_the_current_schema(self) -> None:
        with (
            patch.object(
                paired,
                "summarize",
                side_effect=paired.IntegrityError("frozen input drifted"),
            ),
            patch("builtins.print") as emit,
        ):
            self.assertEqual(paired.main(["summarize", "/missing"]), 1)
        invalid = json.loads(emit.call_args.args[0])
        self.assertEqual(
            invalid,
            {
                "schemaVersion": 2,
                "experimentId": EXPERIMENT_ID,
                "integrityOk": False,
                "analysisComplete": False,
                "analysisStatus": "invalid",
                "promotion": {
                    "ok": False,
                    "status": "not_promotable",
                    "blockingReasons": ["frozen input drifted"],
                },
            },
        )

    def test_manifest_policy_has_one_frozen_authority(self) -> None:
        root = paired._repo_root()
        manifest = json.loads((root / paired._MANIFEST).read_text())
        self.assertEqual(paired._digest(manifest), paired._POLICY_SHA256)
        self.assertEqual(
            manifest["fileSha256"]["benchmarks/terminal_bench/paired_results.py"],
            paired._frozen_file_digest(
                root, "benchmarks/terminal_bench/paired_results.py"
            ),
        )
        self.assertEqual(manifest["taskRuntimeBindings"], paired._TASK_RUNTIME_BINDINGS)

    def test_scorable_exceptions_match_harbor_codex_failure_types(self) -> None:
        classified = {pattern.exception.__name__ for pattern in Codex.ERROR_PATTERNS}
        self.assertEqual(
            CLASSIFIED_EXCEPTION_TYPES,
            classified | {"AgentTimeoutError", "NonZeroAgentExitCodeError"},
        )

    def test_failure_classes_are_stable_and_unknown_types_fail_closed(self) -> None:
        expected = {
            "AgentTimeoutError": "agent_timeout",
            "NonZeroAgentExitCodeError": "agent_runtime",
            "AgentAuthenticationError": "provider_configuration",
            "ApiProviderResourceNotFoundError": "provider_configuration",
            "ModelNotFoundError": "provider_configuration",
            "ApiRateLimitError": "provider_quota",
            "ApiUsageLimitError": "provider_quota",
            "ApiInternalServerError": "provider_availability",
            "ApiOverloadedError": "provider_availability",
            "UnknownApiError": "unknown_api",
            "ApiConnectionClosedError": "provider_transport",
            "ApiResponseStalledError": "provider_timeout",
            "NetworkConnectionError": "provider_transport",
            "ContextWindowExceededError": "model_budget",
            "OutputTokenExceededError": "model_budget",
            "AgentSafetyRefusalError": "safety_refusal",
        }
        self.assertEqual(
            {name: classify_failure(name).value for name in expected}, expected
        )
        with self.assertRaisesRegex(ValueError, "not classified"):
            classify_failure("FutureHarborError")

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        for value in ('{"key":1,"key":2}', '{"key":NaN}', '{"key":Infinity}'):
            with self.subTest(value=value), self.assertRaises(paired.IntegrityError):
                paired._loads(value, "test")


class PrepareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_env = patch.dict(
            paired.os.environ,
            {paired.CODEX_ARCHIVE_ENV: "/tmp/open-agent-lab-test-codex.tgz"},
        )
        self.runtime_prepare = patch.object(
            paired, "prepare_tree", side_effect=_prepare_fixture_runtime
        )
        self.runtime_env.start()
        self.runtime_prepare.start()

    def tearDown(self) -> None:
        self.runtime_prepare.stop()
        self.runtime_env.stop()

    def test_runtime_preparation_requires_an_absolute_archive_path(self) -> None:
        spec = paired.codex_runtime_spec()
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            for value, message in ((None, "must name"), ("relative.tgz", "absolute")):
                with self.subTest(value=value):
                    if value is None:
                        paired.os.environ.pop(paired.CODEX_ARCHIVE_ENV)
                    else:
                        paired.os.environ[paired.CODEX_ARCHIVE_ENV] = value
                    with self.assertRaisesRegex(paired.IntegrityError, message):
                        paired._materialize_codex_runtime(workspace, spec)

    def test_task_materialization_uses_exact_exported_package_refs(self) -> None:
        observed: list[paired.PackageTaskId] = []
        options: list[tuple[bool, bool]] = []

        class Client:
            async def download_tasks(
                self,
                task_ids: list[paired.PackageTaskId],
                *,
                overwrite: bool,
                output_dir: Path,
                export: bool,
            ) -> SimpleNamespace:
                observed.extend(task_ids)
                options.append((overwrite, export))
                results = []
                for task_id in task_ids:
                    path = output_dir / task_id.name
                    path.mkdir()
                    (path / "task.toml").write_text("[task]\n")
                    results.append(SimpleNamespace(path=path))
                return SimpleNamespace(results=results)

        by_name = {
            paired._task_relative(task): binding
            for task, binding in paired._TASK_RUNTIME_BINDINGS.items()
        }
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(paired, "TaskClient", return_value=Client()),
            patch.object(
                paired,
                "_task_content_identity",
                side_effect=lambda path: (
                    by_name[path.name]["taskDigest"],
                    by_name[path.name]["taskChecksum"],
                ),
            ),
        ):
            snapshots = paired._materialize_task_snapshots(
                paired._repo_root(), Path(raw)
            )
        self.assertEqual(snapshots, paired._declared_task_snapshots())
        self.assertEqual(options, [(True, True)])
        self.assertEqual(
            [(item.org, item.name, item.ref) for item in observed],
            [
                (
                    "terminal-bench",
                    task.removeprefix("terminal-bench/"),
                    paired._TASK_DIGESTS[task],
                )
                for task in paired._TASKS
            ],
        )

    def test_materialized_source_is_a_clean_detached_clone(self) -> None:
        root = paired._repo_root()
        revision = paired._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        with tempfile.TemporaryDirectory() as raw:
            source = paired._materialize_revision(root, revision, Path(raw) / "source")
            self.assertEqual(paired._clean_revision(source), revision)
            self.assertTrue(
                (source / "benchmarks/terminal_bench/harbor_agent.py").is_file()
            )

    def test_relay_image_builds_require_exact_ids_and_embedded_build_identity(
        self,
    ) -> None:
        expected = {
            "production": "sha256:" + "a" * 64,
            "providerFreeFixture": "sha256:" + "b" * 64,
        }
        images = {
            "production": "sha256:" + "1" * 64,
            "fixture": "sha256:" + "2" * 64,
        }
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            snapshot = workspace / "source"
            snapshot.mkdir()
            (snapshot / ".dockerignore").write_text("*\n")
            tags = {
                "production": "open-agent-lab-prepared:test-production",
                "providerFreeFixture": "open-agent-lab-prepared:test-fixture",
            }

            def docker(*args: str, cwd: Path | None = None) -> str:
                del cwd
                if args[:2] == ("image", "ls"):
                    return ""
                if args[0] == "build":
                    target = args[args.index("--target") + 1]
                    iidfile = Path(args[args.index("--iidfile") + 1])
                    iidfile.write_text(images[target])
                    return ""
                if args[:2] == ("image", "inspect"):
                    return (
                        images["production"]
                        if args[-1] == tags["production"]
                        else images["fixture"]
                    )
                image = args[-2]
                return (
                    expected["production"]
                    if image == images["production"]
                    else expected["providerFreeFixture"]
                )

            with patch.object(paired, "_docker", side_effect=docker):
                created: set[str] = set()
                self.assertEqual(
                    paired._build_relay_images(
                        snapshot, expected, workspace, tags, created
                    ),
                    {
                        "production": images["production"],
                        "providerFreeFixture": images["fixture"],
                    },
                )
                self.assertEqual(created, set(images.values()))

            def invalid_id(*args: str, cwd: Path | None = None) -> str:
                del cwd
                if args[:2] == ("image", "ls"):
                    return ""
                if args[0] == "build":
                    Path(args[args.index("--iidfile") + 1]).write_text("relay:latest")
                return ""

            with (
                patch.object(paired, "_docker", side_effect=invalid_id),
                self.assertRaisesRegex(paired.IntegrityError, "image ID"),
            ):
                paired._build_relay_images(snapshot, expected, workspace, tags, set())

            def wrong_build(*args: str, cwd: Path | None = None) -> str:
                del cwd
                if args[:2] == ("image", "ls"):
                    return ""
                if args[0] == "build":
                    target = args[args.index("--target") + 1]
                    Path(args[args.index("--iidfile") + 1]).write_text(images[target])
                    return ""
                return "sha256:" + "c" * 64

            with (
                patch.object(paired, "_docker", side_effect=wrong_build),
                self.assertRaisesRegex(paired.IntegrityError, "build identity"),
            ):
                paired._build_relay_images(snapshot, expected, workspace, tags, set())

            builds: list[tuple[str, ...]] = []

            def retained_tag(*args: str, cwd: Path | None = None) -> str:
                del cwd
                if args[:2] == ("image", "ls") and "--filter" in args:
                    return "sha256:" + "9" * 64
                if args and args[0] == "build":
                    builds.append(args)
                return ""

            with (
                patch.object(paired, "_docker", side_effect=retained_tag),
                self.assertRaisesRegex(paired.IntegrityError, "already exists"),
            ):
                paired._build_relay_images(snapshot, expected, workspace, tags, set())
            self.assertEqual(builds, [])

    def test_prepare_binds_clean_source_and_mirror_order_without_overwrite(
        self,
    ) -> None:
        images = {
            "production": "sha256:" + "1" * 64,
            "providerFreeFixture": "sha256:" + "2" * 64,
        }
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(paired, "_clean_revision", return_value="a" * 40),
            patch.object(
                paired, "_materialize_revision", return_value=paired._repo_root()
            ),
            patch.object(
                paired,
                "_materialize_task_snapshots",
                side_effect=_materialize_fixture_tasks,
            ),
            patch.object(paired, "_build_relay_images", return_value=images),
        ):
            output = Path(raw) / "prepared"
            record_path = paired.prepare(output, "mirror-v1")
            record = json.loads(record_path.read_text())
            self.assertTrue(record["preflight"]["cleanTree"])
            self.assertEqual(record["preflight"]["sourceRevision"], "a" * 40)
            self.assertEqual(
                record["preflight"]["taskSnapshotsSha256"],
                paired._digest(record["taskSnapshots"]),
            )
            self.assertEqual(record["taskSnapshots"], paired._declared_task_snapshots())
            deepseek = yaml.safe_load((output / "configs/deepseek.yaml").read_text())
            self.assertEqual(deepseek["datasets"], [])
            self.assertEqual(
                deepseek["tasks"],
                [
                    {
                        "path": str(
                            output.resolve()
                            / record["taskSnapshots"][task]["relativePath"]
                        ),
                        "source": paired._DATASET,
                    }
                    for task in paired._TASKS
                ],
            )
            self.assertEqual(
                [
                    agent["kwargs"]["enable_verify_instruction_v1"]
                    for agent in deepseek["agents"]
                ],
                [True, False],
            )
            self.assertTrue(
                all(
                    agent["kwargs"]["run_binding"]["preflight_sha256"]
                    == record["preflightSha256"]
                    for agent in deepseek["agents"]
                )
            )
            fixture = yaml.safe_load((output / "fixtures/harbor-e2e.yaml").read_text())
            fixture_binding = fixture["agents"][0]["kwargs"]["run_binding"]
            fixture_preflight = json.loads(
                (output / "fixtures/preflight.json").read_text()
            )
            pinned = yaml.safe_load(
                (output / "overlays/relay.deepseek.compose.yaml").read_text()
            )["services"]["open-agent-lab-relay"]
            self.assertNotIn("build", pinned)
            self.assertEqual(pinned["image"], images["production"])
            self.assertEqual(pinned["pull_policy"], "never")
            self.assertEqual(
                deepseek["environment"]["extra_docker_compose"],
                [str(output.resolve() / "overlays/relay.deepseek.compose.yaml")],
            )
            expected_mount = paired._codex_runtime_mount(
                output.resolve() / paired.CODEX_RUNTIME_PREPARED_RELATIVE
            )
            self.assertEqual(deepseek["environment"]["mounts"], [expected_mount])
            self.assertEqual(fixture["environment"]["mounts"], [expected_mount])
            self.assertEqual(record["codexRuntime"], _runtime_receipt())
            self.assertTrue((output / paired.CODEX_RUNTIME_PREPARED_RELATIVE).is_dir())
            manifest = json.loads((paired._repo_root() / paired._MANIFEST).read_text())
            self.assertEqual(
                fixture_binding["relay_build_sha256"],
                manifest["relayBuildIds"]["providerFreeFixture"],
            )
            self.assertEqual(
                fixture_binding["relay_image_sha256"],
                images["providerFreeFixture"],
            )
            self.assertEqual(
                fixture_binding["preflight_sha256"],
                paired._digest(fixture_preflight),
            )
            self.assertEqual(
                fixture_preflight["relayBuildSha256"],
                manifest["relayBuildIds"]["providerFreeFixture"],
            )
            self.assertEqual(
                fixture_preflight["relayImageSha256"],
                images["providerFreeFixture"],
            )
            self.assertEqual(record["relayImages"], images)
            self.assertEqual(
                record["relayImageTags"], paired._relay_image_tags(output, "a" * 40)
            )
            self.assertTrue(paired._prepare_lock_path(output).is_file())
            self.assertNotIn("provider_free_fixture", fixture["agents"][0]["kwargs"])
            self.assertEqual(
                Path(fixture["jobs_dir"]),
                output.resolve() / "fixture-jobs/harbor-e2e",
            )
            with self.assertRaisesRegex(paired.IntegrityError, "must not exist"):
                paired.prepare(output)

    def test_prepare_rejects_a_dirty_tree(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(paired, "_git", return_value=" M result-aware-edit"),
            self.assertRaisesRegex(paired.IntegrityError, "clean Git worktree"),
        ):
            paired.prepare(Path(raw) / "prepared")

    def test_failed_prepare_discards_only_its_new_images(self) -> None:
        image_ids = {"sha256:" + "1" * 64, "sha256:" + "2" * 64}
        for fail_after_both in (False, True):
            with (
                self.subTest(fail_after_both=fail_after_both),
                tempfile.TemporaryDirectory() as raw,
            ):
                output = Path(raw) / "prepared"

                def build(
                    snapshot: Path,
                    expected: dict[str, str],
                    workspace: Path,
                    tags: dict[str, str],
                    created: set[str],
                    failure_after_both: bool = fail_after_both,
                ) -> dict[str, str]:
                    del snapshot, expected, workspace, tags
                    created.update(
                        image_ids if failure_after_both else {min(image_ids)}
                    )
                    if not failure_after_both:
                        raise paired.IntegrityError("second build failed")
                    return {
                        "production": min(image_ids),
                        "providerFreeFixture": max(image_ids),
                    }

                with (
                    patch.object(paired, "_clean_revision", return_value="a" * 40),
                    patch.object(
                        paired,
                        "_materialize_revision",
                        return_value=paired._repo_root(),
                    ),
                    patch.object(
                        paired,
                        "_materialize_task_snapshots",
                        side_effect=_materialize_fixture_tasks,
                    ),
                    patch.object(paired, "_build_relay_images", side_effect=build),
                    patch.object(
                        paired,
                        "_render_pinned_overlays",
                        side_effect=(
                            paired.IntegrityError("later prepare failed")
                            if fail_after_both
                            else None
                        ),
                    ),
                    patch.object(paired, "_discard_relay_images") as discard,
                    self.assertRaises(paired.IntegrityError),
                ):
                    paired.prepare(output)
                discard.assert_called_once_with(
                    paired._relay_image_tags(output, "a" * 40),
                    image_ids if fail_after_both else {min(image_ids)},
                )

    def test_prepare_lock_prevents_concurrent_tag_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "prepared"
            descriptor = paired._acquire_prepare_lock(output)
            try:
                with (
                    patch.object(paired, "_clean_revision") as revision,
                    self.assertRaisesRegex(
                        paired.IntegrityError, "already in progress"
                    ),
                ):
                    paired.prepare(output)
                revision.assert_not_called()
            finally:
                paired._release_prepare_lock(descriptor)
            recovered = paired._acquire_prepare_lock(output)
            paired._release_prepare_lock(recovered)

    def test_prepare_never_replaces_a_target_created_during_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "prepared"
            original = shutil.copytree

            def race(source: Path, target: Path, **kwargs: object) -> Path:
                Path(target).mkdir()
                (Path(target) / "sentinel").write_text("keep")
                return original(source, target, **kwargs)

            with (
                patch.object(paired, "_clean_revision", return_value="a" * 40),
                patch.object(
                    paired, "_materialize_revision", return_value=paired._repo_root()
                ),
                patch.object(
                    paired,
                    "_materialize_task_snapshots",
                    side_effect=_materialize_fixture_tasks,
                ),
                patch.object(
                    paired,
                    "_build_relay_images",
                    return_value={
                        "production": "sha256:" + "1" * 64,
                        "providerFreeFixture": "sha256:" + "2" * 64,
                    },
                ),
                patch.object(paired, "_discard_relay_images"),
                patch.object(paired.shutil, "copytree", side_effect=race),
                self.assertRaisesRegex(paired.IntegrityError, "non-overwriting"),
            ):
                paired.prepare(output)
            self.assertEqual((output / "sentinel").read_text(), "keep")
            self.assertFalse((output / "run-record.json").exists())


class PairedResultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        by_name = {
            paired._task_relative(task): binding
            for task, binding in paired._TASK_RUNTIME_BINDINGS.items()
        }
        self.task_identity = patch.object(
            paired,
            "_task_content_identity",
            side_effect=lambda path: (
                by_name[path.name]["taskDigest"],
                by_name[path.name]["taskChecksum"],
            ),
        )
        self.task_identity.start()
        self.runtime_verify_patch = patch.object(
            paired, "verify_tree", return_value=_runtime_receipt()
        )
        self.runtime_verify = self.runtime_verify_patch.start()

    def tearDown(self) -> None:
        self.runtime_verify_patch.stop()
        self.task_identity.stop()
        self.temporary.cleanup()

    def test_cleanup_removes_only_verified_run_owned_tags(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        removed: list[str] = []

        def docker(*args: str, cwd: Path | None = None) -> str:
            del cwd
            if args[:2] == ("image", "inspect"):
                identity = next(
                    key for key, tag in screen.image_tags.items() if tag == args[-1]
                )
                return screen.images[identity]
            if args[:2] == ("image", "rm"):
                removed.append(args[-1])
                return ""
            raise AssertionError(args)

        with patch.object(paired, "_docker", side_effect=docker):
            self.assertEqual(paired.cleanup_images(screen.root), screen.image_tags)
        self.assertEqual(removed, list(screen.image_tags.values()))

    def test_private_task_snapshots_are_rehashed_and_cannot_be_symlinks(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        task = screen.tasks[0]
        path = screen.root / screen.task_snapshots[task]["relativePath"]
        retained = screen.root / (path.name + "-retained")
        path.rename(retained)
        path.symlink_to(retained, target_is_directory=True)
        try:
            with self.assertRaisesRegex(paired.IntegrityError, "unsafe"):
                paired.summarize([screen.root])
        finally:
            path.unlink()
            retained.rename(path)
        with (
            patch.object(
                paired,
                "_task_content_identity",
                return_value=("sha256:" + "f" * 64, "f" * 64),
            ),
            self.assertRaisesRegex(paired.IntegrityError, "snapshot drifted"),
        ):
            paired.summarize([screen.root])

    def test_prepared_runtime_is_reverified_during_analysis(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        self.runtime_verify.side_effect = ValueError("runtime drift")
        with self.assertRaisesRegex(paired.IntegrityError, "runtime drifted"):
            paired.summarize([screen.root])
        self.runtime_verify.assert_called_once_with(
            screen.root / paired.CODEX_RUNTIME_PREPARED_RELATIVE,
            paired.codex_runtime_spec(),
        )

    def test_prepared_runtime_receipt_cannot_be_rewritten(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        record_path = screen.root / "run-record.json"
        record = json.loads(record_path.read_text())
        record["codexRuntime"]["files"] -= 1
        _write(record_path, record)
        with self.assertRaisesRegex(paired.IntegrityError, "receipt drifted"):
            paired.summarize([screen.root])

    def test_screen_is_valid_deterministic_and_never_promotable(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        first = paired.summarize([screen.root])
        second = paired.summarize([screen.root])
        self.assertEqual(first, second)
        self.assertTrue(first["integrityOk"])
        self.assertEqual(first["schemaVersion"], 2)
        self.assertFalse(first["analysisComplete"])
        self.assertEqual(first["analysisStatus"], "valid_incomplete")
        self.assertEqual(
            first["denominator"],
            {
                "attempts": 20,
                "erroredAttempts": 0,
                "pairs": 10,
                "tasksPerProvider": 5,
            },
        )
        self.assertEqual(first["promotion"]["status"], "not_promotable")
        self.assertIn(
            "mirrored_within_provider_replication_missing",
            first["promotion"]["blockingReasons"],
        )
        self.assertNotIn(str(self.root), paired._canonical(first))

    def test_each_attempt_requires_its_pre_execution_pilot_claim(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = next(iter(screen.trials.values()))
        lock = json.loads((trial / "lock.json").read_text())
        provider = lock["agent"]["model_name"].split("/", 1)[0]
        claim = (
            screen.root
            / "authorizations"
            / paired.relay_claim_name(provider, "pilot", paired._digest(lock))
        )
        claim.unlink()
        with self.assertRaisesRegex(
            paired.IntegrityError, "pilot authorization claim is unavailable"
        ):
            paired.summarize([screen.root])

    def test_pilot_claim_cannot_be_rebound_after_agent_start(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = next(iter(screen.trials.values()))
        lock = json.loads((trial / "lock.json").read_text())
        provider = lock["agent"]["model_name"].split("/", 1)[0]
        claim_path = (
            screen.root
            / "authorizations"
            / paired.relay_claim_name(provider, "pilot", paired._digest(lock))
        )
        claim = json.loads(claim_path.read_text())
        result = json.loads((trial / "result.json").read_text())
        agent_started = datetime.fromisoformat(result["agent_execution"]["started_at"])
        claim["claimedAt"] = (
            (agent_started + timedelta(milliseconds=1))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        _write_private(claim_path, claim)
        with self.assertRaisesRegex(
            paired.IntegrityError, "pilot authorization claim differs"
        ):
            paired.summarize([screen.root])

    def test_pilot_claim_cannot_bless_invalid_probe_receipts(self) -> None:
        for mutation in ("junk", "expired", "probe-config"):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                provider = "deepseek"
                authorization = screen.root / "authorizations" / f"{provider}.json"
                receipt = json.loads(authorization.read_text())
                if mutation == "junk":
                    receipt.pop("benchmarkStartAuthorized")
                elif mutation == "expired":
                    receipt["spendCap"]["expiresAt"] = "2026-08-22T00:00:05Z"
                    receipt["authorizationExpiresAt"] = "2026-08-22T00:00:05Z"
                else:
                    receipt["configSha256"] = "sha256:" + "f" * 64
                _write_private(authorization, receipt)
                policy_sha256 = paired._digest_bytes(authorization.read_bytes())
                for claim_path in (screen.root / "authorizations").glob(
                    f"{provider}.pilot.*.claim.json"
                ):
                    claim = json.loads(claim_path.read_text())
                    claim["policySha256"] = policy_sha256
                    _write_private(claim_path, claim)
                with self.assertRaisesRegex(
                    paired.IntegrityError, "authorization receipt"
                ):
                    paired.summarize([screen.root])

    def test_cleanup_receipt_binds_the_authorized_provider_credential(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = next(iter(screen.trials.values()))
        cleanup_path = trial / "environment-cleanup.json"
        cleanup = json.loads(cleanup_path.read_text())
        cleanup["providerCredentialSha256"] = "sha256:" + "f" * 64
        _write(cleanup_path, cleanup)
        with self.assertRaisesRegex(
            paired.IntegrityError, "environment cleanup receipt"
        ):
            paired.summarize([screen.root])

    def test_predeclared_mirror_can_meet_only_directional_criteria(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        mirror = RunFixture(self.root, "mirror-v1")
        result = paired.summarize([mirror.root, screen.root])
        self.assertTrue(result["analysisComplete"])
        self.assertEqual(result["analysisStatus"], "valid")
        self.assertEqual(result["denominator"]["attempts"], 40)
        self.assertEqual(result["promotion"]["status"], "not_promotable")
        self.assertTrue(result["promotion"]["directionalCriteriaMet"])
        self.assertIn(
            "development_experiment_never_promotable",
            result["promotion"]["blockingReasons"],
        )
        self.assertEqual(
            result["claimClass"], "directional_five_task_development_result"
        )

    def test_missing_duplicate_and_symlink_trials_fail_closed(self) -> None:
        for mutation in ("missing", "duplicate", "symlink"):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                if mutation == "missing":
                    shutil.rmtree(trial)
                elif mutation == "duplicate":
                    duplicate = trial.parent / "duplicate"
                    shutil.copytree(trial, duplicate)
                    other = next(
                        path
                        for path in trial.parent.iterdir()
                        if path.is_dir() and path != trial and path != duplicate
                    )
                    shutil.rmtree(other)
                else:
                    other = next(
                        path
                        for path in trial.parent.iterdir()
                        if path.is_dir() and path != trial
                    )
                    shutil.rmtree(other)
                    other.symlink_to(trial, target_is_directory=True)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_rehashed_prepared_compose_cannot_restore_builds_or_change_image(
        self,
    ) -> None:
        for mutation in ("build", "image", "pull"):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                record_path = screen.root / "run-record.json"
                record = json.loads(record_path.read_text())
                entry = record["providers"][0]
                compose_path = screen.root / entry["compose"]
                compose = yaml.safe_load(compose_path.read_text())
                relay = compose["services"]["open-agent-lab-relay"]
                if mutation == "build":
                    relay["build"] = {"context": "."}
                elif mutation == "image":
                    relay["image"] = "sha256:" + "f" * 64
                else:
                    relay["pull_policy"] = "always"
                rendered = yaml.safe_dump(compose, sort_keys=False)
                compose_path.write_text(rendered)
                entry["composeSha256"] = paired._digest_bytes(rendered.encode())
                _write(record_path, record)
                with self.assertRaisesRegex(paired.IntegrityError, "pinned"):
                    paired.summarize([screen.root])

    def test_order_lock_binding_and_relay_copy_tampering_fail_closed(self) -> None:
        mutations = (
            "order",
            "job-lock",
            "job-config",
            "cancelled",
            "binding",
            "context",
            "relay-copy",
            "relay-field",
            "relay-build",
            "provider-schema",
            "provider-extra",
            "artifact-manifest",
            "artifact-extra",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = screen.trials[("deepseek", screen.tasks[0], "control-v1")]
                if mutation == "order":
                    result = json.loads((trial / "result.json").read_text())
                    result["started_at"] = "2026-08-22T00:00:30+00:00"
                    result["finished_at"] = "2026-08-22T00:00:40+00:00"
                    result["agent_execution"] = {
                        "started_at": "2026-08-22T00:00:31+00:00",
                        "finished_at": "2026-08-22T00:00:39+00:00",
                    }
                    _write(trial / "result.json", result)
                elif mutation == "job-lock":
                    job_lock = trial.parent / "lock.json"
                    value = json.loads(job_lock.read_text())
                    value["trials"][0]["task"]["digest"] = "sha256:" + "f" * 64
                    _write(job_lock, value)
                elif mutation == "job-config":
                    job_config = trial.parent / "config.json"
                    value = json.loads(job_config.read_text())
                    value["tasks"][0]["source"] = "unexpected/source"
                    _write(job_config, value)
                elif mutation == "cancelled":
                    job_result = trial.parent / "result.json"
                    value = json.loads(job_result.read_text())
                    value["stats"]["n_cancelled_trials"] = 1
                    _write(job_result, value)
                elif mutation == "binding":
                    result = json.loads((trial / "result.json").read_text())
                    result["agent_result"]["metadata"]["open_agent_lab_provider"][
                        "harbor_binding"
                    ]["run_binding"]["source_revision"] = "f" * 40
                    _write(trial / "result.json", result)
                elif mutation == "context":
                    result = json.loads((trial / "result.json").read_text())
                    binding = result["agent_result"]["metadata"][
                        "open_agent_lab_provider"
                    ]["harbor_binding"]
                    binding["harbor_context_id"] = str(UUID(int=9999))
                    body = {
                        key: value
                        for key, value in binding.items()
                        if key != "binding_sha256"
                    }
                    binding["binding_sha256"] = paired._digest(body)
                    _write(trial / "result.json", result)
                elif mutation == "relay-copy":
                    generic = trial / "artifacts" / "provider-metadata.ndjson"
                    generic.write_text(generic.read_text() + "\n")
                elif mutation == "relay-field":
                    canonical = (
                        trial / "artifacts/provider-evidence/provider-metadata.ndjson"
                    )
                    records = [
                        json.loads(line) for line in canonical.read_text().splitlines()
                    ]
                    records[0].pop("requestBytes")
                    rendered = (
                        "\n".join(paired._canonical(record) for record in records)
                        + "\n"
                    )
                    canonical.write_text(rendered)
                    (trial / "artifacts/provider-metadata.ndjson").write_text(rendered)
                elif mutation == "relay-build":
                    result_path = trial / "result.json"
                    result = json.loads(result_path.read_text())
                    wrong = _relay(
                        trial,
                        "deepseek",
                        paired._PROVIDERS["deepseek"]["model"],
                        "sha256:" + "f" * 64,
                        "wrong-build",
                        datetime.fromisoformat(result["started_at"]),
                        datetime.fromisoformat(result["finished_at"]),
                    )
                    provider_data = result["agent_result"]["metadata"][
                        "open_agent_lab_provider"
                    ]
                    for key in (
                        "event_count",
                        "chain_head",
                        "seal",
                        "records",
                        "publication_gate",
                    ):
                        provider_data[key] = wrong[key]
                    binding = provider_data["harbor_binding"]
                    binding["relay_instance_id"] = wrong["seal"]["relayInstanceId"]
                    binding["relay_build_id"] = wrong["seal"]["buildId"]
                    binding["relay_marker_sha256"] = wrong["seal"]["markerSha256"]
                    binding["binding_sha256"] = paired._digest(
                        {
                            key: value
                            for key, value in binding.items()
                            if key != "binding_sha256"
                        }
                    )
                    _write(result_path, result)
                elif mutation in {"provider-schema", "provider-extra"}:
                    result_path = trial / "result.json"
                    result = json.loads(result_path.read_text())
                    provider_data = result["agent_result"]["metadata"][
                        "open_agent_lab_provider"
                    ]
                    if mutation == "provider-schema":
                        provider_data["schema_version"] = 2
                    else:
                        provider_data["unredacted"] = "unexpected"
                    _write(result_path, result)
                elif mutation == "artifact-manifest":
                    manifest_path = trial / "artifacts/manifest.json"
                    value = json.loads(manifest_path.read_text())
                    value[0]["status"] = "failed"
                    _write(manifest_path, value)
                else:
                    manifest_path = trial / "artifacts/manifest.json"
                    value = json.loads(manifest_path.read_text())
                    value.append(
                        {
                            "source": "/tmp/unexpected",
                            "destination": "artifacts/unexpected",
                            "type": "file",
                            "status": "ok",
                            "service": "open-agent-lab-relay",
                        }
                    )
                    _write(manifest_path, value)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_job_result_requires_the_full_harbor_022_schema(self) -> None:
        for mutation in (
            "id",
            "updated_at",
            "n_running_trials",
            "n_retries",
            "coerced-total",
            "bool-total",
            "missing-metrics",
            "extra-eval-field",
            "time-order",
            "mixed-awareness",
            "updated-before-finished",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                result_path = trial.parent / "result.json"
                result = json.loads(result_path.read_text())
                if mutation in {"id", "updated_at"}:
                    result.pop(mutation)
                elif mutation.startswith("n_"):
                    result["stats"].pop(mutation)
                elif mutation in {"coerced-total", "bool-total"}:
                    result["n_total_trials"] = (
                        "10" if mutation == "coerced-total" else True
                    )
                elif mutation == "time-order":
                    result["started_at"] = "2026-08-23T00:00:00"
                    result["updated_at"] = "2026-08-21T00:00:00"
                    result["finished_at"] = "2026-08-21T00:00:00"
                elif mutation == "mixed-awareness":
                    result["started_at"] += "+00:00"
                elif mutation == "updated-before-finished":
                    result["updated_at"] = "2026-08-22T00:59:59"
                else:
                    entry = next(iter(result["stats"]["evals"].values()))
                    if mutation == "missing-metrics":
                        entry.pop("metrics")
                    else:
                        entry["unexpected"] = "field"
                _write(result_path, result)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_job_lock_requires_exact_harbor_shape_and_timing(self) -> None:
        for mutation in (
            "missing-created-at",
            "extra-root",
            "extra-retry",
            "extra-harbor",
            "coerced-concurrency",
            "bool-concurrency",
            "bool-wait",
            "bool-min-wait",
            "numeric-editable",
            "before-preflight",
            "after-first-trial",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                lock_path = trial.parent / "lock.json"
                lock = json.loads(lock_path.read_text())
                if mutation == "missing-created-at":
                    lock.pop("created_at")
                elif mutation == "extra-root":
                    lock["unexpected"] = True
                elif mutation == "extra-retry":
                    lock["retry"]["unexpected"] = True
                elif mutation == "extra-harbor":
                    lock["harbor"]["unexpected"] = True
                elif mutation == "coerced-concurrency":
                    lock["n_concurrent_trials"] = "1"
                elif mutation == "bool-concurrency":
                    lock["n_concurrent_trials"] = True
                elif mutation == "bool-wait":
                    lock["retry"]["wait_multiplier"] = True
                elif mutation == "bool-min-wait":
                    lock["retry"]["min_wait_sec"] = True
                elif mutation == "numeric-editable":
                    lock["harbor"]["is_editable"] = 0
                elif mutation == "before-preflight":
                    lock["created_at"] = "2026-08-21T23:59:59Z"
                else:
                    lock["created_at"] = "2026-08-22T00:00:01Z"
                _write(lock_path, lock)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_run_record_schema_versions_are_type_exact(self) -> None:
        for mutation in ("record", "preflight"):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                path = screen.root / "run-record.json"
                record = json.loads(path.read_text())
                if mutation == "record":
                    record["schemaVersion"] = True
                else:
                    record["preflight"]["schemaVersion"] = True
                    record["preflightSha256"] = paired._digest(record["preflight"])
                _write(path, record)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_authoritative_run_files_cannot_be_symlinks(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = next(iter(screen.trials.values()))
        targets = {
            "run-record": screen.root / "run-record.json",
            "job-config": trial.parent / "config.json",
            "job-lock": trial.parent / "lock.json",
            "job-result": trial.parent / "result.json",
            "trial-lock": trial / "lock.json",
            "trial-result": trial / "result.json",
            "cleanup-receipt": trial / "environment-cleanup.json",
            "artifact-manifest": trial / "artifacts/manifest.json",
            "trajectory": trial / "agent/trajectory.json",
            "relay-journal": (
                trial / "artifacts/provider-evidence/provider-metadata.ndjson"
            ),
            "relay-seal": (
                trial / "artifacts/provider-evidence/provider-metadata.ndjson.sealed"
            ),
            "relay-copy": trial / "artifacts/provider-metadata.ndjson",
        }
        for index, (name, target) in enumerate(targets.items()):
            with self.subTest(name=name):
                data = target.read_bytes()
                external = self.root / f"outside-{index}.artifact"
                external.write_bytes(data)
                target.unlink()
                target.symlink_to(external)
                try:
                    with self.assertRaises(paired.IntegrityError):
                        paired.summarize([screen.root])
                finally:
                    target.unlink()
                    target.write_bytes(data)

    def test_cleanup_receipt_is_exact_and_post_verifier(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = next(iter(screen.trials.values()))
        path = trial / "environment-cleanup.json"
        original = json.loads(path.read_text())
        result = json.loads((trial / "result.json").read_text())
        mutations = {
            "missing": None,
            "schema-type": {**original, "schemaVersion": True},
            "extra-field": {**original, "unexpected": True},
            "experiment": {**original, "experimentId": "other"},
            "binding": {
                **original,
                "runBindingSha256": "sha256:" + "f" * 64,
            },
            "compose": {**original, "fullComposeSha256": "not-a-digest"},
            "task": {**original, "taskId": "terminal-bench/other"},
            "before-verifier": {
                **original,
                "stoppedAt": (
                    datetime.fromisoformat(result["verifier"]["finished_at"])
                    - timedelta(microseconds=1)
                ).isoformat(),
            },
            "after-trial": {
                **original,
                "stoppedAt": (
                    datetime.fromisoformat(result["finished_at"])
                    + timedelta(microseconds=1)
                ).isoformat(),
            },
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                path.unlink(missing_ok=True)
                if mutation is not None:
                    _write(path, mutation)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])
        _write(path, original)

    def test_scored_failure_still_requires_a_valid_harbor_trajectory(self) -> None:
        for mutation in (
            "empty-steps",
            "scalar-tool-call",
            "extra-root",
            "extra-step",
            "bool-step-id",
            "bool-total-steps",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                result["exception_info"] = _failure_info(result)
                _write_trial_result(result_path, result)
                _refresh_job_result(trial.parent)
                path = trial / "agent" / "trajectory.json"
                trajectory = json.loads(path.read_text())
                if mutation == "empty-steps":
                    trajectory["steps"] = []
                elif mutation == "scalar-tool-call":
                    trajectory["steps"][0]["tool_calls"] = [1]
                elif mutation == "extra-root":
                    trajectory["unexpected"] = True
                elif mutation == "bool-step-id":
                    trajectory["steps"][0]["step_id"] = True
                elif mutation == "bool-total-steps":
                    trajectory["final_metrics"]["total_steps"] = True
                else:
                    trajectory["steps"][0]["unexpected"] = True
                _write(path, trajectory)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_trial_result_requires_exact_harbor_provenance_and_shape(self) -> None:
        for mutation in (
            "missing-field",
            "nested-default",
            "source",
            "task-id",
            "full-config",
            "bool-token",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                if mutation == "missing-field":
                    result.pop("trial_uri")
                elif mutation == "nested-default":
                    result["agent_result"].pop("cost_usd")
                elif mutation == "source":
                    result["source"] = "terminal-bench/other"
                elif mutation == "task-id":
                    result["task_id"]["ref"] = "sha256:" + "f" * 64
                elif mutation == "full-config":
                    result["config"]["timeout_multiplier"] = 2.0
                else:
                    result["agent_result"]["n_input_tokens"] = True
                _write(result_path, result)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_job_aggregates_crosscheck_children_but_metrics_are_non_authoritative(
        self,
    ) -> None:
        for mutation in ("tokens", "eval-reward"):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                result_path = trial.parent / "result.json"
                result = json.loads(result_path.read_text())
                if mutation == "tokens":
                    result["stats"]["n_input_tokens"] += 1
                else:
                    entry = next(iter(result["stats"]["evals"].values()))
                    reward_key = next(iter(entry["reward_stats"]["reward"]))
                    entry["reward_stats"]["reward"][reward_key].append("forged")
                _write(result_path, result)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

        metrics_root = self.root / "metrics"
        metrics_root.mkdir()
        screen = RunFixture(metrics_root, "screen-v1")
        trial = next(iter(screen.trials.values()))
        result_path = trial.parent / "result.json"
        result = json.loads(result_path.read_text())
        for entry in result["stats"]["evals"].values():
            entry["metrics"] = [{"external-presentation-metric": 0.5}]
        _write(result_path, result)
        self.assertEqual(
            paired.summarize([screen.root])["analysisStatus"], "valid_incomplete"
        )

    def test_missing_usage_reward_and_model_fail_closed(self) -> None:
        mutations = (
            "reward",
            "model",
            "zero-time",
            "task-checksum",
            "reward-range",
            "exception-schema",
            "exception-type",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                if mutation == "reward":
                    result["verifier_result"]["rewards"].pop("reward")
                    _write(result_path, result)
                elif mutation == "model":
                    result["agent_info"]["model_info"]["name"] = "alias"
                    _write(result_path, result)
                elif mutation == "zero-time":
                    result["finished_at"] = result["started_at"]
                    _write(result_path, result)
                elif mutation == "task-checksum":
                    result["task_checksum"] = "not-a-sha256"
                    _write(result_path, result)
                elif mutation == "exception-schema":
                    result["exception_info"] = {"exception_type": "AgentTimeoutError"}
                    _write(result_path, result)
                elif mutation == "exception-type":
                    result["exception_info"] = _failure_info(
                        result, "AgentRuntimeError"
                    )
                    _write_trial_result(result_path, result)
                else:
                    result["verifier_result"]["rewards"]["reward"] = 1.01
                    _write(result_path, result)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_unexposed_optional_token_subsets_are_null_not_zero(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        verified = _relay(
            trial,
            "zai",
            paired._PROVIDERS["zai"]["model"],
            screen.binding["relay_build_sha256"],
            "screen-v1-zai-no-optional-usage",
            datetime.fromisoformat(result["started_at"]),
            datetime.fromisoformat(result["finished_at"]),
            include_optional_usage=False,
        )
        _replace_relay(result, verified)
        _write(result_path, result)

        summary = paired.summarize([screen.root])

        attempt = next(
            item for item in summary["attempts"] if item["trialId"] == result["id"]
        )
        self.assertEqual(attempt["tokens"]["input_tokens"], 3)
        self.assertEqual(attempt["tokens"]["output_tokens"], 2)
        self.assertIsNone(attempt["tokens"]["cached_input_tokens"])
        self.assertIsNone(attempt["tokens"]["reasoning_output_tokens"])
        self.assertEqual(
            attempt["telemetryMissing"],
            ["cached_input_tokens", "reasoning_output_tokens"],
        )
        self.assertFalse(summary["analysisComplete"])
        self.assertIn(
            "attempt_telemetry_missing", summary["promotion"]["blockingReasons"]
        )

    def test_scored_exception_remains_in_the_official_denominator(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        result["exception_info"] = _failure_info(result, "ApiRateLimitError")
        expected_reward = result["verifier_result"]["rewards"]["reward"]
        _write_trial_result(result_path, result)
        _refresh_job_result(trial.parent)

        summary = paired.summarize([screen.root])

        self.assertEqual(summary["denominator"]["attempts"], 20)
        attempt = next(
            item for item in summary["attempts"] if item["trialId"] == result["id"]
        )
        self.assertEqual(attempt["reward"], expected_reward)
        self.assertEqual(attempt["topLevelException"], "ApiRateLimitError")
        self.assertEqual(attempt["failureClass"], "provider_quota")
        self.assertEqual(summary["denominator"]["erroredAttempts"], 1)
        self.assertEqual(summary["exceptionCounts"], {"provider_quota": 1})

    def test_successes_are_not_classified_as_failures(self) -> None:
        screen = RunFixture(self.root, "screen-v1")

        summary = paired.summarize([screen.root])

        self.assertEqual(summary["denominator"]["erroredAttempts"], 0)
        self.assertEqual(summary["exceptionCounts"], {})
        self.assertTrue(
            all(item["failureClass"] is None for item in summary["attempts"])
        )

    def test_zero_reward_without_an_exception_is_not_counted_as_errored(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "control-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        result["verifier_result"]["rewards"]["reward"] = 0.0
        _write_trial_result(result_path, result)
        _refresh_job_result(trial.parent)

        summary = paired.summarize([screen.root])

        attempt = next(
            item for item in summary["attempts"] if item["trialId"] == result["id"]
        )
        self.assertEqual(attempt["reward"], 0)
        self.assertIsNone(attempt["topLevelException"])
        self.assertIsNone(attempt["failureClass"])
        self.assertEqual(summary["denominator"]["erroredAttempts"], 0)
        self.assertEqual(summary["exceptionCounts"], {})

    def test_scored_failure_preserves_relay_tokens_when_derivations_are_missing(
        self,
    ) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        result["exception_info"] = _failure_info(result)
        for key in ("n_input_tokens", "n_output_tokens", "n_cache_tokens"):
            result["agent_result"][key] = None
        _write_trial_result(result_path, result)
        trajectory_path = trial / "agent" / "trajectory.json"
        trajectory = json.loads(trajectory_path.read_text())
        trajectory["final_metrics"] = None
        _write(trajectory_path, trajectory)
        _refresh_job_result(trial.parent)

        summary = paired.summarize([screen.root])

        attempt = next(
            item for item in summary["attempts"] if item["trialId"] == result["id"]
        )
        self.assertEqual(attempt["tokens"]["total_tokens"], 5)
        self.assertEqual(attempt["toolCalls"], 1)
        self.assertEqual(attempt["trajectorySteps"], 1)
        self.assertEqual(
            attempt["telemetryMissing"],
            ["atif_final_metrics", "agent_token_crosscheck"],
        )

        conflict_root = self.root / "explicit-agent-token-conflict"
        conflict_root.mkdir()
        conflict = RunFixture(conflict_root, "screen-v1")
        conflict_trial = conflict.trials[
            ("zai", conflict.tasks[0], "verify-instruction-v1")
        ]
        conflict_path = conflict_trial / "result.json"
        conflict_result = json.loads(conflict_path.read_text())
        conflict_result["exception_info"] = _failure_info(conflict_result)
        conflict_result["agent_result"]["n_input_tokens"] = 99
        _write_trial_result(conflict_path, conflict_result)
        _refresh_job_result(conflict_trial.parent)
        with self.assertRaisesRegex(paired.IntegrityError, "disagrees"):
            paired.summarize([conflict.root])

    def test_scored_failure_with_no_request_keeps_score_and_marks_missing(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("deepseek", screen.tasks[0], "control-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        started = datetime.fromisoformat(result["started_at"])
        finished = datetime.fromisoformat(result["finished_at"])
        verified = _relay(
            trial,
            "deepseek",
            paired._PROVIDERS["deepseek"]["model"],
            screen.binding["relay_build_sha256"],
            "screen-v1-deepseek-empty",
            started,
            finished,
            empty=True,
        )
        _replace_relay(result, verified, trajectory_missing=True)
        result["exception_info"] = _failure_info(result)
        _write_trial_result(result_path, result)
        (trial / "agent" / "trajectory.json").unlink()
        _refresh_job_result(trial.parent)

        summary = paired.summarize([screen.root])

        attempt = next(
            item for item in summary["attempts"] if item["trialId"] == result["id"]
        )
        self.assertEqual(summary["denominator"]["attempts"], 20)
        self.assertEqual(
            summary["telemetryCoverage"],
            {
                "completeAttempts": 19,
                "totalAttempts": 20,
            },
        )
        self.assertFalse(summary["analysisComplete"])
        self.assertEqual(summary["analysisStatus"], "valid_incomplete")
        self.assertIn(
            "attempt_telemetry_missing", summary["promotion"]["blockingReasons"]
        )
        self.assertFalse(summary["promotion"]["directionalCriteriaMet"])
        self.assertEqual(attempt["topLevelException"], "AgentTimeoutError")
        self.assertEqual(attempt["providerRequests"], 0)
        self.assertIsNone(attempt["chainHead"])
        self.assertIsNone(attempt["tokens"])
        self.assertIsNone(attempt["toolCalls"])
        self.assertIsNone(attempt["trajectorySteps"])
        self.assertEqual(attempt["agentWallSeconds"], 8.0)
        self.assertEqual(
            attempt["telemetryMissing"],
            ["provider_usage", "trajectory"],
        )
        deepseek = next(
            item
            for item in summary["providerSummary"]
            if item["provider"] == "deepseek"
        )
        self.assertIsNone(deepseek["medianPrimaryTokenIncrease"])
        self.assertEqual(deepseek["primaryTokenCoveragePairs"], 4)

    def test_scored_failure_accepts_real_header_only_and_unreliable_metadata(
        self,
    ) -> None:
        for mutation in (
            "header-only",
            "parse-error",
            "parse-error-completed",
            "response-id-conflict",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = screen.trials[("zai", screen.tasks[0], "control-v1")]
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                verified = _relay(
                    trial,
                    "zai",
                    paired._PROVIDERS["zai"]["model"],
                    screen.binding["relay_build_sha256"],
                    f"screen-v1-zai-{mutation}",
                    datetime.fromisoformat(result["started_at"]),
                    datetime.fromisoformat(result["finished_at"]),
                    transport_state=(
                        "completed"
                        if mutation == "parse-error-completed"
                        else "aborted"
                    ),
                    terminal_metadata=mutation == "parse-error-completed",
                    parse_errors=1 if mutation.startswith("parse-error") else 0,
                    metadata_conflicts=(
                        ("response_id",) if mutation == "response-id-conflict" else ()
                    ),
                )
                _replace_relay(result, verified)
                result["exception_info"] = _failure_info(
                    result, "NonZeroAgentExitCodeError"
                )
                _write_trial_result(result_path, result)
                _refresh_job_result(trial.parent)

                summary = paired.summarize([screen.root])

                attempt = next(
                    item
                    for item in summary["attempts"]
                    if item["trialId"] == result["id"]
                )
                self.assertIsNone(attempt["tokens"])
                self.assertEqual(summary["analysisStatus"], "valid_incomplete")

    def test_scored_failure_requires_complete_native_phase_timing(self) -> None:
        for mutation in (
            "missing-agent",
            "missing-verifier",
            "verifier-before-agent",
            "early-exception",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                result["exception_info"] = _failure_info(result)
                if mutation == "missing-agent":
                    result["agent_execution"] = None
                elif mutation == "missing-verifier":
                    result["verifier"] = None
                elif mutation == "verifier-before-agent":
                    result["verifier"] = result["agent_setup"]
                else:
                    result["exception_info"]["occurred_at"] = result["agent_execution"][
                        "started_at"
                    ]
                _write_trial_result(result_path, result)
                _refresh_job_result(trial.parent)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_scored_failure_cannot_waive_a_returned_model_mismatch(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        started = datetime.fromisoformat(result["started_at"])
        finished = datetime.fromisoformat(result["finished_at"])
        verified = _relay(
            trial,
            "zai",
            paired._PROVIDERS["zai"]["model"],
            screen.binding["relay_build_sha256"],
            "screen-v1-zai-wrong-model",
            started,
            finished,
            returned_model="unexpected-model",
        )
        _replace_relay(result, verified)
        result["exception_info"] = _failure_info(result, "NonZeroAgentExitCodeError")
        _write_trial_result(result_path, result)
        _refresh_job_result(trial.parent)
        with self.assertRaisesRegex(paired.IntegrityError, "publication gate"):
            paired.summarize([screen.root])

    def test_incomplete_transport_cannot_hide_invalid_model_identity(self) -> None:
        for mutation in ("mismatch", "conflict", "unknown-state"):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                if mutation == "unknown-state":
                    _rewrite_relay(trial, "unknown-model-state")
                    result["exception_info"] = _failure_info(
                        result, "NonZeroAgentExitCodeError"
                    )
                    _write_trial_result(result_path, result)
                    _refresh_job_result(trial.parent)
                    with self.assertRaises(paired.IntegrityError):
                        paired.summarize([screen.root])
                    continue
                verified = _relay(
                    trial,
                    "zai",
                    paired._PROVIDERS["zai"]["model"],
                    screen.binding["relay_build_sha256"],
                    f"screen-v1-zai-aborted-{mutation}",
                    datetime.fromisoformat(result["started_at"]),
                    datetime.fromisoformat(result["finished_at"]),
                    returned_model=(
                        "unexpected-model" if mutation == "mismatch" else None
                    ),
                    transport_state="aborted",
                    model_consistency={
                        "conflict": "conflict",
                        "unknown-state": "unknown",
                    }.get(mutation, "consistent"),
                )
                _replace_relay(result, verified)
                result["exception_info"] = _failure_info(
                    result, "NonZeroAgentExitCodeError"
                )
                _write_trial_result(result_path, result)
                _refresh_job_result(trial.parent)
                with self.assertRaisesRegex(paired.IntegrityError, "publication gate"):
                    paired.summarize([screen.root])

    def test_rehashed_raw_relay_contradictions_fail_closed(self) -> None:
        for mutation in (
            "wrong-header-model",
            "wrong-source-model",
            "numeric-provider-request-id",
            "utf16-oversized-provider-request-id",
            "numeric-response-id",
            "non-stream-request",
            "completed-error",
            "completed-bodyless-status",
            "completed-redirect-status",
            "redirect-error-with-success-status",
            "connect-timeout-after-headers",
            "body-missing-before-headers",
            "aborted-nonclient-error",
            "failed-client-error",
            "garbage-model-source",
            "nonterminal-model-source",
            "terminal-header-model-source",
            "header-only-model-source",
            "setup-time",
            "verifier-seal",
            "invalid-sealed-at",
            "missing-terminal-state",
            "second-terminal-source",
            "zero-response-contradiction",
            "first-byte-after-duration",
            "headers-after-duration",
            "duration-timestamp-mismatch",
            "fractional-duration",
            "empty-request",
            "undersized-request",
            "oversized-request",
            "oversized-response",
            "parse-errors-exceed-response",
            "event-index-exceeds-response",
            "empty-request-hash",
            "empty-response-hash",
            "completed-null-status",
            "null-status-header-metadata",
            "unsafe-usage",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = next(iter(screen.trials.values()))
                _rewrite_relay(trial, mutation)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_scored_failure_cannot_waive_impossible_terminal_observer_state(
        self,
    ) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "control-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        _rewrite_relay(trial, "missing-terminal-state")
        evidence = trial / "artifacts/provider-evidence"
        verified = relay_metadata(
            evidence / "provider-metadata.ndjson",
            evidence / "provider-metadata.ndjson.sealed",
        )
        _replace_relay(result, verified)
        result["exception_info"] = _failure_info(result)
        _write_trial_result(result_path, result)
        _refresh_job_result(trial.parent)
        with self.assertRaisesRegex(paired.IntegrityError, "publication gate"):
            paired.summarize([screen.root])

    def test_scored_failure_cannot_waive_invalid_raw_relay_state(self) -> None:
        for mutation in (
            "null-status-header-metadata",
            "aborted-nonclient-error",
            "failed-client-error",
            "informational-status",
            "noncanonical-time",
            "extra-event-field",
            "extra-seal-field",
            "impossible-rejections",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = screen.trials[("zai", screen.tasks[0], "control-v1")]
                result_path = trial / "result.json"
                result = json.loads(result_path.read_text())
                _rewrite_relay(trial, mutation)
                result["exception_info"] = _failure_info(result)
                _write_trial_result(result_path, result)
                _refresh_job_result(trial.parent)
                with self.assertRaisesRegex(paired.IntegrityError, "relay"):
                    paired.summarize([screen.root])

    def test_relay_artifacts_are_bounded_before_reading(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "control-v1")]
        journal = trial / "artifacts/provider-evidence/provider-metadata.ndjson"
        with journal.open("wb") as artifact:
            artifact.truncate(paired._RELAY_JOURNAL_CAP + 1)
        with self.assertRaisesRegex(paired.IntegrityError, "too large"):
            paired.summarize([screen.root])

    def test_json_artifacts_are_bounded_before_reading(self) -> None:
        for name, relative, cap in (
            ("run-record", "run-record.json", paired._JSON_ARTIFACT_CAP),
            (
                "trajectory",
                "jobs/deepseek/open-agent-lab-screen-v1-deepseek/"
                + "trial-00-control-v1/agent/trajectory.json",
                paired._TRAJECTORY_CAP,
            ),
        ):
            with self.subTest(name=name):
                case_root = self.root / name
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                path = screen.root / relative
                with path.open("wb") as artifact:
                    artifact.truncate(cap + 1)
                with self.assertRaisesRegex(paired.IntegrityError, "too large"):
                    paired.summarize([screen.root])

    def test_complete_metrics_reject_replayed_upstream_identities(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "control-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        verified = _relay(
            trial,
            "zai",
            paired._PROVIDERS["zai"]["model"],
            screen.binding["relay_build_sha256"],
            "screen-v1-zai-replayed-identities",
            datetime.fromisoformat(result["started_at"]),
            datetime.fromisoformat(result["finished_at"]),
            request_count=2,
        )
        _replace_relay(result, verified)
        _write_trial_result(result_path, result)
        _refresh_job_result(trial.parent)
        _rewrite_relay(trial, "duplicate-upstream-identities")
        with self.assertRaisesRegex(
            paired.IntegrityError, "relay evidence failed validation"
        ):
            paired.summarize([screen.root])

    def test_scored_failure_cannot_exceed_the_relay_request_cap(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        trial = screen.trials[("zai", screen.tasks[0], "verify-instruction-v1")]
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        verified = _relay(
            trial,
            "zai",
            paired._PROVIDERS["zai"]["model"],
            screen.binding["relay_build_sha256"],
            "screen-v1-zai-over-cap",
            datetime.fromisoformat(result["started_at"]),
            datetime.fromisoformat(result["finished_at"]),
            transport_state="aborted",
            request_count=paired._RELAY_REQUEST_CAP + 1,
        )
        _replace_relay(result, verified)
        result["exception_info"] = _failure_info(result, "NonZeroAgentExitCodeError")
        _write_trial_result(result_path, result)
        _refresh_job_result(trial.parent)
        with self.assertRaisesRegex(paired.IntegrityError, "request count"):
            paired.summarize([screen.root])

    def test_self_consistent_trial_lock_drift_still_fails_closed(self) -> None:
        for mutation in (
            "task-source",
            "verifier",
            "environment",
            "timeout",
            "timeout-bool",
            "compose-digest",
        ):
            with self.subTest(mutation=mutation):
                case_root = self.root / mutation
                case_root.mkdir()
                screen = RunFixture(case_root, "screen-v1")
                trial = screen.trials[("deepseek", screen.tasks[0], "control-v1")]
                child_path = trial / "lock.json"
                child = json.loads(child_path.read_text())
                original = paired._canonical(child)
                if mutation == "task-source":
                    child["task"]["source"] = "terminal-bench/other"
                elif mutation == "verifier":
                    child["verifier"]["disable"] = True
                elif mutation == "environment":
                    child["environment"]["extra_docker_compose"] = []
                elif mutation in {"timeout", "timeout-bool"}:
                    child["timeout_multiplier"] = 2.0 if mutation == "timeout" else True
                else:
                    child["extra_docker_compose"][0]["digest"] = "sha256:" + "f" * 64
                root_path = trial.parent / "lock.json"
                root_lock = json.loads(root_path.read_text())
                matching = [
                    index
                    for index, value in enumerate(root_lock["trials"])
                    if paired._canonical(value) == original
                ]
                self.assertEqual(len(matching), 1)
                root_lock["trials"][matching[0]] = child
                _write(child_path, child)
                _write(root_path, root_lock)
                with self.assertRaises(paired.IntegrityError):
                    paired.summarize([screen.root])

    def test_trial_and_relay_identities_are_globally_unique(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        mirror = RunFixture(self.root, "mirror-v1")
        screen_trial = screen.trials[("deepseek", screen.tasks[0], "control-v1")]
        mirror_trial = mirror.trials[("deepseek", mirror.tasks[0], "control-v1")]
        screen_result = json.loads((screen_trial / "result.json").read_text())
        mirror_path = mirror_trial / "result.json"
        mirror_result = json.loads(mirror_path.read_text())
        mirror_result["id"] = screen_result["id"]
        binding = mirror_result["agent_result"]["metadata"]["open_agent_lab_provider"][
            "harbor_binding"
        ]
        binding["harbor_context_id"] = screen_result["id"]
        binding["binding_sha256"] = paired._digest(
            {key: value for key, value in binding.items() if key != "binding_sha256"}
        )
        _write(mirror_path, mirror_result)
        with self.assertRaisesRegex(paired.IntegrityError, "trialId must be unique"):
            paired.summarize([screen.root, mirror.root])

        relay_root = self.root / "relay-identity"
        relay_root.mkdir()
        replay = RunFixture(relay_root, "screen-v1")
        second = replay.trials[("deepseek", replay.tasks[0], "verify-instruction-v1")]
        second_path = second / "result.json"
        second_result = json.loads(second_path.read_text())
        verified = _relay(
            second,
            "deepseek",
            paired._PROVIDERS["deepseek"]["model"],
            replay.binding["relay_build_sha256"],
            "screen-v1-deepseek-0",
            datetime.fromisoformat(second_result["started_at"]),
            datetime.fromisoformat(second_result["finished_at"]),
        )
        provider_data = second_result["agent_result"]["metadata"][
            "open_agent_lab_provider"
        ]
        for key in (
            "event_count",
            "chain_head",
            "seal",
            "records",
            "publication_gate",
        ):
            provider_data[key] = verified[key]
        relay_binding = provider_data["harbor_binding"]
        relay_binding["relay_instance_id"] = verified["seal"]["relayInstanceId"]
        relay_binding["relay_build_id"] = verified["seal"]["buildId"]
        relay_binding["relay_marker_sha256"] = verified["seal"]["markerSha256"]
        relay_binding["binding_sha256"] = paired._digest(
            {
                key: value
                for key, value in relay_binding.items()
                if key != "binding_sha256"
            }
        )
        _write(second_path, second_result)
        with self.assertRaisesRegex(paired.IntegrityError, "relayRunId must be unique"):
            paired.summarize([replay.root])

    def test_task_bytes_must_match_across_providers(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        task = screen.tasks[0]
        replacement = "sha256:" + "f" * 64
        for variant in paired._VARIANTS:
            trial = screen.trials[("zai", task, variant)]
            lock_path, result_path = trial / "lock.json", trial / "result.json"
            lock = json.loads(lock_path.read_text())
            result = json.loads(result_path.read_text())
            lock["task"]["digest"] = replacement
            result["task_checksum"] = "f" * 64
            _write(lock_path, lock)
            _write(result_path, result)
        job_lock_path = (
            next(
                screen.trials[("zai", task, variant)].parent
                for variant in paired._VARIANTS
            )
            / "lock.json"
        )
        job_lock = json.loads(job_lock_path.read_text())
        for lock in job_lock["trials"]:
            if lock["task"]["name"] == task.removeprefix("terminal-bench/"):
                lock["task"]["digest"] = replacement
        _write(job_lock_path, job_lock)
        with self.assertRaisesRegex(paired.IntegrityError, "task identity"):
            paired.summarize([screen.root])

    def test_one_provider_cannot_offset_the_other(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        mirror = RunFixture(self.root, "mirror-v1")
        for fixture in (screen, mirror):
            changed_jobs: set[Path] = set()
            for (provider, _task, variant), trial in fixture.trials.items():
                if provider == "zai" and variant == "verify-instruction-v1":
                    path = trial / "result.json"
                    result = json.loads(path.read_text())
                    result["verifier_result"]["rewards"]["reward"] = 0.1
                    _write_trial_result(path, result)
                    changed_jobs.add(trial.parent)
            for job_dir in changed_jobs:
                _refresh_job_result(job_dir)
        summary = paired.summarize([screen.root, mirror.root])
        self.assertTrue(summary["integrityOk"])
        self.assertIn(
            "zai_mean_reward_delta_not_positive",
            summary["promotion"]["blockingReasons"],
        )

    def test_mirrored_replication_must_share_the_source_revision(self) -> None:
        screen = RunFixture(self.root, "screen-v1")
        mirror = RunFixture(self.root, "mirror-v1", source="f" * 40)
        with self.assertRaisesRegex(paired.IntegrityError, "different source"):
            paired.summarize([screen.root, mirror.root])

    def test_all_replications_must_precede_the_first_live_probe(self) -> None:
        screen = RunFixture(
            self.root,
            "screen-v1",
            trial_start=datetime(2026, 8, 22, 0, 1, tzinfo=timezone.utc),
        )
        mirror = RunFixture(
            self.root,
            "mirror-v1",
            created_at="2026-08-22T00:02:00Z",
            trial_start=datetime(2026, 8, 22, 0, 3, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(
            paired.IntegrityError, "before the first live probe"
        ):
            paired.summarize([screen.root, mirror.root])

    def test_all_replications_must_precede_the_first_trial(self) -> None:
        screen = RunFixture(
            self.root,
            "screen-v1",
            trial_start=datetime(2026, 8, 22, 0, 1, tzinfo=timezone.utc),
        )
        mirror = RunFixture(
            self.root,
            "mirror-v1",
            created_at="2026-08-22T00:01:00.050Z",
            trial_start=datetime(2026, 8, 22, 0, 3, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(paired.IntegrityError, "before the first trial"):
            paired.summarize([screen.root, mirror.root])

    def test_at_least_one_run_is_required(self) -> None:
        with self.assertRaisesRegex(paired.IntegrityError, "at least one"):
            paired.summarize([])


if __name__ == "__main__":
    unittest.main()
