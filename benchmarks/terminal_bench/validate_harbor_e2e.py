"""Fail closed unless the provider-free Harbor trial proves the full path."""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, TrialLock
from harbor.models.job.result import JobResult
from harbor.models.trajectories.trajectory import Trajectory
from harbor.models.trial.result import TrialResult

from benchmarks.terminal_bench.relay_evidence import relay_metadata

_DATASET_DIGEST = (
    "sha256:d10e96e201d6816b22553504e06e7de0153a26381e808d11404cbca530b9d388"
)
_TASK_DIGEST = "sha256:38d7a077f07fbee8efc78db5dec9a72f82e727510ad1dcfeac0b55fa845256b7"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
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


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


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
    _require(
        _SHA256.fullmatch(seal["buildId"]), "Relay build identity is not immutable."
    )
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
    binding_hash = "sha256:" + hashlib.sha256(_canonical(binding_body)).hexdigest()
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
