"""Fail closed unless the provider-free Harbor trial proves the full path."""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, TrialLock
from harbor.models.job.result import JobResult
from harbor.models.trajectories.trajectory import Trajectory
from harbor.models.trial.result import TrialResult

from benchmarks.terminal_bench.codex_runtime import (
    CODEX_RUNTIME_INSTALL_ROOT,
    CODEX_RUNTIME_PREPARED_RELATIVE,
    CODEX_RUNTIME_SPEC_SHA256,
    validate_codex_runtime_spec,
    verify_tree,
)
from benchmarks.terminal_bench.experiment_contract import (
    ENVIRONMENT_IMPORT,
    RUN_BINDING_KEYS,
    artifact_manifest,
    canonical_json,
    is_digest,
    is_revision,
)
from benchmarks.terminal_bench.relay_evidence import relay_metadata

_DATASET_DIGEST = (
    "sha256:d10e96e201d6816b22553504e06e7de0153a26381e808d11404cbca530b9d388"
)
_TASK_DIGEST = "sha256:38d7a077f07fbee8efc78db5dec9a72f82e727510ad1dcfeac0b55fa845256b7"
_MANIFEST = Path(__file__).with_name("verify-instruction-v1.experiment.json")
_MANIFEST_BYTES = _MANIFEST.read_bytes()
_MANIFEST_DATA = json.loads(_MANIFEST_BYTES)
_MANIFEST_SHA256 = "sha256:" + hashlib.sha256(_MANIFEST_BYTES).hexdigest()
_RUNTIME_SPEC = validate_codex_runtime_spec(_MANIFEST_DATA["runtime"]["codexRuntime"])
_FIXTURE_BUILD_ID = _MANIFEST_DATA["relayBuildIds"]["providerFreeFixture"]
_EFFECTIVE_MODEL_CONTEXT_WINDOW = 996_147
_VERIFY_INSTRUCTION_SHA256 = (
    "sha256:9f855e1e34702265ed0ff4c4fcfb2483cb9777c5f37d8c29daccd2c454f84e4a"
)
_VARIANTS = {
    "control-v1": {
        "developer_instruction_requested": False,
        "requested_developer_instructions_sha256": None,
        "agent_name": "open-agent-lab-codex",
        "response_ids": ["resp_fixture_1", "resp_fixture_2"],
    },
    "verify-instruction-v1": {
        "developer_instruction_requested": True,
        "requested_developer_instructions_sha256": _VERIFY_INSTRUCTION_SHA256,
        "agent_name": "open-agent-lab-codex-verify-instruction-v1",
        "response_ids": [
            "resp_fixture_verify_instruction_1",
            "resp_fixture_verify_instruction_2",
        ],
    },
}
_ISOLATION_COMMAND = r"""expected=__SECRET_SHA256__
for file in /proc/[0-9]*/environ /proc/[0-9]*/cmdline; do
  [ -r "$file" ] || continue
  while IFS= read -r candidate; do
    case "$candidate" in *=*) candidate=${candidate#*=} ;; esac
    [ -z "$candidate" ] || [ "$(printf %s "$candidate" | sha256sum | cut -d' ' -f1)" != "$expected" ] || exit 42
  done < <(tr '\0' '\n' < "$file" 2>/dev/null)
done
if [ -d /run/secrets ]; then
  for file in /run/secrets/*; do
    [ -r "$file" ] || continue
    [ "$(printf %s "$(cat "$file")" | sha256sum | cut -d' ' -f1)" != "$expected" ] || exit 42
  done
fi
printf 'Hello, world!\n' > /app/hello.txt"""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _assert_codex_model_metadata(trial_dir: Path) -> None:
    rollouts = sorted((trial_dir / "agent" / "sessions").rglob("rollout-*.jsonl"))
    _require(len(rollouts) == 1, "Expected one native Codex rollout.")
    contexts = []
    try:
        for line in rollouts[0].read_text().splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise TypeError("rollout event must be an object")
            payload = event.get("payload")
            if (
                event.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "task_started"
            ):
                contexts.append(payload.get("model_context_window"))
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise RuntimeError("Native Codex rollout metadata is invalid.") from error
    _require(
        contexts == [_EFFECTIVE_MODEL_CONTEXT_WINDOW],
        "Codex did not use the frozen DeepSeek context window.",
    )
    output = (trial_dir / "agent" / "codex.txt").read_text()
    _require(
        "Defaulting to fallback metadata" not in output,
        "Codex used fallback model metadata.",
    )


def _isolation_command(secret: bytes) -> str:
    digest = hashlib.sha256(secret).hexdigest()
    return _ISOLATION_COMMAND.replace("__SECRET_SHA256__", digest)


def _assert_isolation_call(arguments: object, secret: bytes) -> None:
    _require(
        arguments == {"cmd": _isolation_command(secret)},
        "Fixture isolation command drifted.",
    )


def _trial_dir(job_dir: Path) -> Path:
    trials = sorted(
        child
        for child in job_dir.iterdir()
        if child.is_dir() and (child / "result.json").is_file()
    )
    _require(len(trials) == 1, f"Expected one completed trial, found {len(trials)}.")
    return trials[0]


def _contains(path: Path, needle: bytes) -> bool:
    overlap = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            window = overlap + chunk
            if needle in window:
                return True
            overlap = window[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def _assert_secret_absent(job_dir: Path, secret: bytes) -> None:
    _require(secret, "Provider fixture key is empty.")
    for path in job_dir.rglob("*"):
        _require(
            not path.is_symlink(), f"Unexpected symlink in Harbor evidence: {path}."
        )
        if path.is_file():
            _require(not _contains(path, secret), f"Provider key leaked into {path}.")


def _assert_collected_relay_evidence(trial_dir: Path, evidence_dir: Path) -> None:
    artifacts = trial_dir / "artifacts"
    manifest = json.loads((artifacts / "manifest.json").read_text())
    expected_manifest = artifact_manifest()
    _require(
        manifest == expected_manifest,
        "Harbor artifact manifest does not match the frozen three entries.",
    )
    for name in ("provider-metadata.ndjson", "provider-metadata.ndjson.sealed"):
        _require(
            (artifacts / name).read_bytes() == (evidence_dir / name).read_bytes(),
            f"Harbor and adapter copies of {name} differ.",
        )


def _assert_fixture_preflight(
    job_dir: Path, result: TrialResult, run_binding: dict[str, Any]
) -> None:
    preflight = json.loads(
        (job_dir.parents[2] / "fixtures" / "preflight.json").read_text()
    )
    _require(
        preflight
        == {
            "schemaVersion": 1,
            "experimentId": run_binding["experiment_id"],
            "replicationId": run_binding["replication_id"],
            "sourceRevision": run_binding["source_revision"],
            "experimentManifestSha256": run_binding["experiment_manifest_sha256"],
            "relayBuildSha256": run_binding["relay_build_sha256"],
            "relayImageSha256": run_binding["relay_image_sha256"],
            "taskSnapshotsSha256": run_binding["task_snapshots_sha256"],
            "cleanTree": True,
            "createdAt": preflight.get("createdAt"),
        },
        "Fixture preflight fields drifted.",
    )
    _require(
        run_binding["experiment_manifest_sha256"] == _MANIFEST_SHA256,
        "Fixture manifest binding drifted.",
    )
    preflight_sha = "sha256:" + hashlib.sha256(canonical_json(preflight)).hexdigest()
    _require(
        run_binding["preflight_sha256"] == preflight_sha,
        "Fixture preflight hash drifted.",
    )
    try:
        created_at = datetime.fromisoformat(
            str(preflight["createdAt"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise RuntimeError("Fixture preflight time is invalid.") from error
    _require(
        created_at.tzinfo is not None
        and result.started_at is not None
        and created_at <= result.started_at,
        "Fixture was not prepared before the trial started.",
    )


def _assert_cleanup_receipt(
    trial_dir: Path, result: TrialResult, run_binding: dict[str, Any]
) -> None:
    receipt = json.loads((trial_dir / "environment-cleanup.json").read_text())
    session_id = f"{result.trial_name}__env"
    try:
        stopped_at = datetime.fromisoformat(
            str(receipt.get("stoppedAt", "")).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise RuntimeError("Cleanup receipt time is invalid.") from error
    expected = {
        "schemaVersion": 1,
        "experimentId": run_binding["experiment_id"],
        "replicationId": run_binding["replication_id"],
        "sourceRevision": run_binding["source_revision"],
        "experimentManifestSha256": run_binding["experiment_manifest_sha256"],
        "preflightSha256": run_binding["preflight_sha256"],
        "runBindingSha256": "sha256:"
        + hashlib.sha256(canonical_json(run_binding)).hexdigest(),
        "relayImageSha256": run_binding["relay_image_sha256"],
        "providerCredentialSha256": None,
        "fullComposeSha256": receipt.get("fullComposeSha256"),
        "taskId": None,
        "taskDigest": None,
        "taskChecksum": None,
        "sessionId": session_id,
        "projectName": _sanitize_docker_compose_project_name(session_id),
        "stoppedAt": receipt.get("stoppedAt"),
    }
    _require(receipt == expected, "Cleanup receipt identity drifted.")
    _require(
        is_digest(receipt["fullComposeSha256"]),
        "Cleanup Compose digest is invalid.",
    )
    _require(
        result.verifier is not None
        and result.verifier.finished_at is not None
        and result.finished_at is not None
        and result.verifier.finished_at <= stopped_at <= result.finished_at,
        "Cleanup receipt time is outside the completed verifier lifecycle.",
    )


def _assert_pinned_environment(
    job_dir: Path,
    job: JobConfig,
    lock: TrialLock,
    run_binding: dict[str, Any],
) -> None:
    path = (job_dir.parents[2] / "overlays" / "relay.fixture.compose.yaml").resolve()
    data = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    expected_kwargs = {
        "relay_compose_sha256": digest,
        "run_binding": run_binding,
    }
    runtime_root = job_dir.parents[2] / CODEX_RUNTIME_PREPARED_RELATIVE
    expected_mounts = [
        {
            "type": "bind",
            "source": str(runtime_root),
            "target": CODEX_RUNTIME_INSTALL_ROOT,
            "read_only": True,
        }
    ]
    for environment in (job.environment, lock.environment):
        _require(
            environment.import_path == ENVIRONMENT_IMPORT
            and environment.kwargs == expected_kwargs
            and environment.mounts == expected_mounts
            and [item.resolve() for item in environment.extra_docker_compose] == [path],
            "Pinned Harbor environment binding drifted.",
        )
    verify_tree(runtime_root, _RUNTIME_SPEC)
    compose = yaml.safe_load(data)
    relay = compose.get("services", {}).get("open-agent-lab-relay", {})
    _require(
        isinstance(relay, dict)
        and "build" not in relay
        and relay.get("image") == run_binding["relay_image_sha256"]
        and relay.get("pull_policy") == "never",
        "Fixture relay Compose is not pinned to the prepared image.",
    )
    compose_locks = lock.extra_docker_compose or []
    _require(
        len(compose_locks) == 1
        and compose_locks[0].path.resolve() == path
        and compose_locks[0].digest == digest,
        "Harbor did not lock the exact prepared Compose bytes.",
    )


def validate(
    job_dir: Path, secret: bytes, variant_id: str = "control-v1"
) -> dict[str, Any]:
    expected_variant = _VARIANTS.get(variant_id)
    _require(expected_variant is not None, f"Unknown E2E variant: {variant_id}.")
    trial_dir = _trial_dir(job_dir)
    job = JobConfig.model_validate_json((job_dir / "config.json").read_text())
    job_result = JobResult.model_validate_json((job_dir / "result.json").read_text())
    job_lock = JobLock.model_validate_json((job_dir / "lock.json").read_text())
    result = TrialResult.model_validate_json((trial_dir / "result.json").read_text())
    lock = TrialLock.model_validate_json((trial_dir / "lock.json").read_text())
    trajectory = Trajectory.model_validate_json(
        (trial_dir / "agent" / "trajectory.json").read_text()
    )
    _assert_codex_model_metadata(trial_dir)

    _require(result.exception_info is None, "Harbor recorded a trial exception.")
    _require(job_result.finished_at is not None, "Harbor job did not finish.")
    _require(job_result.n_total_trials == 1, "Harbor job trial count drifted.")
    _require(
        (
            job_result.stats.n_completed_trials,
            job_result.stats.n_errored_trials,
            job_result.stats.n_running_trials,
            job_result.stats.n_pending_trials,
            job_result.stats.n_cancelled_trials,
            job_result.stats.n_retries,
        )
        == (1, 0, 0, 0, 0, 0),
        "Harbor job did not complete exactly one clean trial.",
    )
    _require(
        len(job_lock.trials) == 1 and job_lock.trials[0] == lock,
        "Harbor job lock is not bound to the trial lock.",
    )
    _require(result.task_name == "hello-world/hello-world", "Unexpected task name.")
    _require(len(job.datasets) == 1, "Expected one Harbor dataset.")
    _require(job.datasets[0].name == "harbor/hello-world", "Dataset identity drifted.")
    _require(job.datasets[0].ref == _DATASET_DIGEST, "Dataset digest drifted.")
    _require(lock.task.digest == _TASK_DIGEST, "Task content digest drifted.")
    _require(
        getattr(result.task_id, "ref", None) == _TASK_DIGEST,
        "Resolved task digest drifted.",
    )
    rewards = result.verifier_result.rewards if result.verifier_result else None
    _require(rewards == {"reward": 1.0}, "Verifier reward is not exactly 1.")
    _require(len(job.agents) == 1, "Expected one Harbor agent config.")
    _require(
        job.agents[0].kwargs.get("enable_verify_instruction_v1")
        is expected_variant["developer_instruction_requested"],
        "Job config variant drifted.",
    )
    _require(
        lock.agent.kwargs.get("enable_verify_instruction_v1")
        is expected_variant["developer_instruction_requested"],
        "Trial lock variant drifted.",
    )
    run_binding = job.agents[0].kwargs.get("run_binding")
    _require(
        isinstance(run_binding, dict)
        and set(run_binding) == RUN_BINDING_KEYS
        and run_binding == lock.agent.kwargs.get("run_binding")
        and run_binding.get("schema_version") == 1
        and run_binding.get("experiment_id")
        == "terminal-bench-2.1-verify-instruction-v1"
        and run_binding.get("replication_id") == "screen-v1"
        and is_revision(run_binding.get("source_revision"))
        and run_binding.get("relay_build_sha256") == _FIXTURE_BUILD_ID,
        "Provider-free E2E source binding drifted.",
    )
    _assert_fixture_preflight(job_dir, result, run_binding)
    _assert_cleanup_receipt(trial_dir, result, run_binding)
    _assert_pinned_environment(job_dir, job, lock, run_binding)
    _require(
        result.agent_info.name == expected_variant["agent_name"],
        "Harbor agent identity drifted.",
    )

    _require(trajectory.schema_version == "ATIF-v1.7", "Unexpected ATIF version.")
    _require(trajectory.session_id, "ATIF session identity is missing.")
    _require(trajectory.agent.name == "codex", "ATIF agent is not Codex.")
    _require(trajectory.agent.version == "0.149.0", "Codex version drifted.")
    _require(trajectory.agent.model_name == "deepseek-v4-pro", "ATIF model drifted.")
    tool_steps = [step for step in trajectory.steps if step.tool_calls]
    calls = [call for step in tool_steps for call in (step.tool_calls or [])]
    _require(len(calls) == 1, f"Expected one tool call, found {len(calls)}.")
    _require(
        calls[0].tool_call_id == "call_open_agent_lab_harbor_e2e",
        "Fixture tool identity drifted.",
    )
    _require(calls[0].function_name == "exec_command", "Unexpected Codex tool.")
    _assert_isolation_call(calls[0].arguments, secret)
    _require(
        any(
            result.source_call_id == calls[0].tool_call_id
            for step in tool_steps
            for result in (step.observation.results if step.observation else [])
        ),
        "ATIF tool observation is missing.",
    )
    metrics = trajectory.final_metrics
    _require(
        metrics and (metrics.total_prompt_tokens or 0) > 0, "Input usage is missing."
    )
    _require(
        metrics and (metrics.total_completion_tokens or 0) > 0,
        "Output usage is missing.",
    )

    evidence_dir = trial_dir / "artifacts" / "provider-evidence"
    _assert_collected_relay_evidence(trial_dir, evidence_dir)
    metadata = relay_metadata(
        evidence_dir / "provider-metadata.ndjson",
        evidence_dir / "provider-metadata.ndjson.sealed",
    )
    _require(
        metadata["event_count"] == 6, "Relay did not retain two complete requests."
    )
    _require(
        metadata["publication_gate"]
        == {"ok": False, "reasons": ["synthetic_provider"]},
        f"Synthetic relay evidence was not quarantined: "
        f"{metadata['publication_gate']!r}.",
    )
    seal = metadata["seal"]
    _require(seal["providerId"] == "synthetic-fixture", "Relay provenance drifted.")
    _require(seal["expectedModel"] == "deepseek-v4-pro", "Relay model drifted.")
    _require(seal["buildId"] == _FIXTURE_BUILD_ID, "Relay build identity drifted.")
    closed = [
        record
        for record in metadata["records"]
        if record["event"] == "transport.responses.closed"
    ]
    _require(
        [record.get("providerRequestId") for record in closed]
        == ["provider-fixture-1", "provider-fixture-2"],
        "Fixture request binding drifted.",
    )
    _require(
        [record.get("responseId") for record in closed]
        == expected_variant["response_ids"],
        "Fixture response binding drifted.",
    )

    context = result.agent_result
    provider = (
        (context.metadata or {}).get("open_agent_lab_provider") if context else None
    )
    _require(isinstance(provider, dict), "Harbor provider metadata is missing.")
    _require(
        provider.get("agent_variant")
        == {
            "schema_version": 1,
            "variant_id": variant_id,
            "developer_instruction_requested": expected_variant[
                "developer_instruction_requested"
            ],
            "requested_developer_instructions_sha256": expected_variant[
                "requested_developer_instructions_sha256"
            ],
        },
        "Agent variant metadata drifted.",
    )
    _require(
        provider.get("publication_gate")
        == {
            "ok": False,
            "reasons": ["provider_mismatch", "synthetic_provider"],
        },
        "Harbor synthetic publication gate drifted.",
    )
    binding = provider.get("harbor_binding")
    _require(isinstance(binding, dict), "Harbor binding is missing.")
    binding_body = {
        key: value for key, value in binding.items() if key != "binding_sha256"
    }
    binding_hash = "sha256:" + hashlib.sha256(canonical_json(binding_body)).hexdigest()
    _require(
        binding.get("binding_sha256") == binding_hash, "Harbor binding hash failed."
    )
    _require(
        binding.get("harbor_context_id") == str(result.id),
        "Harbor context binding drifted.",
    )
    _require(
        binding.get("harbor_session_id") == f"{result.trial_name}__agent",
        "Harbor session binding drifted.",
    )
    _require(
        binding.get("trajectory_session_id") == trajectory.session_id,
        "ATIF session binding drifted.",
    )
    _require(
        binding.get("relay_instance_id") == seal["relayInstanceId"],
        "Relay binding drifted.",
    )
    _require(binding.get("relay_build_id") == seal["buildId"], "Build binding drifted.")
    _require(
        binding.get("run_binding") == run_binding,
        "Preflight binding was not retained.",
    )
    _require(
        binding.get("relay_marker_sha256") == seal["markerSha256"],
        "Seal binding drifted.",
    )
    _require(binding.get("provider_id") == "deepseek", "Bound provider drifted.")
    _require(
        binding.get("requested_model") == "deepseek-v4-pro", "Bound model drifted."
    )
    _require(binding.get("variant_id") == variant_id, "Bound variant drifted.")
    _require(
        binding.get("requested_developer_instructions_sha256")
        == expected_variant["requested_developer_instructions_sha256"],
        "Bound developer instruction drifted.",
    )
    _require(
        binding.get("codex_runtime_spec_sha256") == CODEX_RUNTIME_SPEC_SHA256,
        "Bound Codex runtime identity drifted.",
    )
    _assert_secret_absent(job_dir, secret)

    return {
        "ok": True,
        "reward": rewards,
        "task_digest": lock.task.digest,
        "requests": metadata["event_count"] // 3,
        "synthetic": True,
        "variant_id": variant_id,
        "trajectory_steps": len(trajectory.steps),
        "seal": seal["markerSha256"],
    }


if __name__ == "__main__":
    if len(sys.argv) not in {2, 3}:
        raise SystemExit(
            "usage: PROVIDER_KEY | python -m "
            "benchmarks.terminal_bench.validate_harbor_e2e JOB_DIR [VARIANT_ID]"
        )
    print(
        json.dumps(
            validate(
                Path(sys.argv[1]),
                sys.stdin.buffer.read().strip(),
                sys.argv[2] if len(sys.argv) == 3 else "control-v1",
            ),
            separators=(",", ":"),
            sort_keys=True,
        )
    )
