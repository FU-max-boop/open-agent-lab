"""Validate the production-bound Harbor install-only compatibility proof."""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

from harbor.environments.docker.docker import _sanitize_docker_compose_project_name

from benchmarks.terminal_bench import paired_results as policy
from benchmarks.terminal_bench.codex_runtime import CODEX_RUNTIME_PREPARED_RELATIVE
from benchmarks.terminal_bench.experiment_contract import (
    ENVIRONMENT_IMPORT,
    canonical_json,
    digest_bytes,
    is_digest,
)

_PROOF_CLASS = "harbor_install_only_compatibility"
_JOB_FILES = {"config.json", "job.log", "lock.json", "result.json"}
_OUTPUT_TREES = {
    "agent": {"setup"},
    "artifacts": {"logs", "logs/artifacts"},
    "verifier": set(),
}
_TRIAL_FILES = {
    "config.json",
    "environment-cleanup.json",
    "lock.json",
    "result.json",
    "trial.log",
}
_FORBIDDEN_OUTPUTS = {
    "exception.txt",
    "manifest.json",
    "provider-metadata.ndjson",
    "provider-metadata.ndjson.sealed",
    "reward.json",
    "reward.txt",
    "trajectory.json",
}


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _contains(path: Path, needle: bytes) -> bool:
    overlap = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            window = overlap + chunk
            if needle in window:
                return True
            overlap = window[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def _assert_outputs_are_non_scorable(job_dir: Path, secret: bytes) -> None:
    _require(secret, "The provider-free proof key is empty.")
    for path in job_dir.rglob("*"):
        _require(not path.is_symlink(), f"Unexpected proof symlink: {path}.")
        if path.is_file():
            _require(
                path.name not in _FORBIDDEN_OUTPUTS, f"Scored output exists: {path}."
            )
            _require(not _contains(path, secret), f"Proof key leaked into {path}.")


def _trial_directories(job_dir: Path, expected: int) -> list[Path]:
    entries = list(job_dir.iterdir())
    files = {path.name: path for path in entries if path.is_file()}
    children = sorted(path for path in entries if path.is_dir() or path.is_symlink())
    _require(
        set(files) == _JOB_FILES
        and all(not path.is_symlink() for path in files.values())
        and len(entries) == len(_JOB_FILES) + expected
        and len(children) == expected
        and all(path.is_dir() and not path.is_symlink() for path in children),
        "Install-only proof has missing or unsafe trial directories.",
    )
    return children


def _assert_install_only_tree(trial_dir: Path) -> None:
    entries = {path.name: path for path in trial_dir.iterdir()}
    _require(
        set(entries) == _TRIAL_FILES | set(_OUTPUT_TREES),
        "Install-only trial produced an unexpected output tree.",
    )
    for name in _TRIAL_FILES:
        path = entries[name]
        _require(
            path.is_file() and not path.is_symlink(),
            f"Install-only trial file is unsafe: {path}.",
        )
    for name, expected in _OUTPUT_TREES.items():
        path = entries[name]
        descendants = {
            item.relative_to(path).as_posix(): item for item in path.rglob("*")
        }
        _require(
            path.is_dir()
            and not path.is_symlink()
            and set(descendants) == expected
            and all(
                item.is_dir() and not item.is_symlink() for item in descendants.values()
            ),
            f"Install-only {name} output tree drifted.",
        )


def _proof_projection(
    prepared: Path, provider: str, config: dict[str, Any], replication: str
) -> tuple[dict[str, Any], Path, str]:
    _require(replication in {"screen-v1", "mirror-v1"}, "Replication drifted.")
    jobs_dir = prepared / "install-only-jobs" / provider
    job_name = f"open-agent-lab-{replication}-{provider}-install-only"
    projected = copy.deepcopy(config)
    projected["install_only"] = True
    projected["job_name"] = job_name
    projected["jobs_dir"] = str(jobs_dir)
    projected["verifier"] = {
        **policy._mapping(projected.get("verifier", {}), "verifier"),
        "disable": True,
    }
    return projected, jobs_dir / job_name, replication


def _prepared_authority(
    prepared: Path, provider: str
) -> tuple[dict[str, Any], dict[str, Any], Path, str, dict[str, Any], str]:
    source = prepared / "source"
    manifest, _, manifest_sha = policy._manifest(source)
    _require(
        manifest["runtime"]["hermeticCodexRuntimeReady"] is True,
        "The frozen runtime gate is not ready.",
    )
    preflight, providers = policy._validate_record(
        prepared,
        manifest_sha,
        manifest["relayBuildIds"]["production"],
        manifest["runtime"]["codexRuntime"],
    )
    preflight_sha = policy._digest(preflight)
    _require(
        policy._clean_revision(source) == preflight.get("sourceRevision"),
        "Prepared source authority drifted.",
    )
    binding = policy._expected_binding(preflight, preflight_sha)
    entry = policy._provider_entries(providers)[provider]
    replication = policy._replication(manifest, preflight["replicationId"])
    _, _, config, compose_path, compose_sha = policy._validated_job_dir(
        prepared,
        entry,
        preflight,
        preflight_sha,
        list(policy._TASKS),
        replication["armOrderByProvider"][provider],
    )
    return binding, entry, compose_path, compose_sha, config, preflight["replicationId"]


def _assert_cleanup(
    trial_dir: Path,
    result: dict[str, Any],
    task: str,
    binding: dict[str, Any],
) -> None:
    receipt = policy._mapping(
        policy._artifact_json(trial_dir, "environment-cleanup.json", "cleanup receipt"),
        "cleanup receipt",
    )
    session_id = f"{trial_dir.name}__env"
    expected = {
        "schemaVersion": 1,
        "experimentId": binding["experiment_id"],
        "replicationId": binding["replication_id"],
        "sourceRevision": binding["source_revision"],
        "experimentManifestSha256": binding["experiment_manifest_sha256"],
        "preflightSha256": binding["preflight_sha256"],
        "runBindingSha256": digest_bytes(canonical_json(binding)),
        "relayImageSha256": binding["relay_image_sha256"],
        "fullComposeSha256": receipt.get("fullComposeSha256"),
        "taskId": task,
        "taskDigest": policy._TASK_RUNTIME_BINDINGS[task]["taskDigest"],
        "taskChecksum": policy._TASK_RUNTIME_BINDINGS[task]["taskChecksum"],
        "sessionId": session_id,
        "projectName": _sanitize_docker_compose_project_name(session_id),
        "stoppedAt": receipt.get("stoppedAt"),
    }
    _require(receipt == expected, "Cleanup receipt identity drifted.")
    stopped = policy._iso(receipt.get("stoppedAt"), "stoppedAt")
    agent_finished = policy._phase_timing(result, "agent_setup")[1]
    finished = policy._iso(result.get("finished_at"), "finished_at")
    _require(
        is_digest(receipt["fullComposeSha256"])
        and agent_finished <= stopped <= finished,
        "Cleanup receipt timing drifted.",
    )


def _assert_trial(
    trial_dir: Path,
    provider: str,
    model: str,
    binding: dict[str, Any],
    job_config: Any,
    job_id: Any,
    compose_path: Path,
    compose_sha: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _assert_install_only_tree(trial_dir)
    raw_result = policy._mapping(
        policy._artifact_json(trial_dir, "result.json", "trial result"),
        "trial result",
    )
    raw_lock = policy._mapping(
        policy._artifact_json(trial_dir, "lock.json", "trial lock"), "trial lock"
    )
    parsed = policy._harbor_trial_result(raw_result)
    _require(
        raw_result.get("trial_name") == trial_dir.name
        and str(UUID(str(raw_result.get("id")))) == str(parsed.id),
        "Trial directory or UUID identity drifted.",
    )
    variant = policy._validate_variant(
        policy._mapping(raw_lock.get("agent"), "trial agent"),
        provider,
        model,
        binding,
    )
    task_matches = [
        task for task in policy._TASKS if task == raw_result.get("task_name")
    ]
    _require(len(task_matches) == 1, "Trial task identity drifted.")
    task = task_matches[0]
    _require(
        raw_result.get("task_checksum")
        == policy._TASK_RUNTIME_BINDINGS[task]["taskChecksum"],
        "Trial task checksum drifted.",
    )
    task_path = next(
        (
            item.path
            for item in job_config.tasks
            if item.path is not None
            and item.path.name == task.removeprefix("terminal-bench/")
        ),
        None,
    )
    _require(task_path is not None, "Prepared trial task is unavailable.")
    _require(
        policy._same_json(
            raw_lock,
            policy._expected_trial_lock(
                provider,
                model,
                task,
                variant,
                binding,
                task_path,
                compose_path,
                compose_sha,
                install_only=True,
            ),
        ),
        "Install-only TrialLock drifted.",
    )
    policy._validate_trial_provenance(
        trial_dir, raw_result, task, variant, job_config, job_id
    )
    info = policy._mapping(raw_result.get("agent_info"), "agent_info")
    _require(
        info.get("name") == policy._VARIANTS[variant]["name"]
        and info.get("version") == policy._CODEX_VERSION
        and policy._same_json(
            policy._mapping(info.get("model_info"), "model_info"),
            {"name": model, "provider": provider},
        ),
        "Install-only agent identity drifted.",
    )
    _require(
        raw_result.get("exception_info") is None
        and raw_result.get("agent_result") is None
        and raw_result.get("agent_execution") is None
        and raw_result.get("verifier_result") is None
        and raw_result.get("verifier") is None
        and raw_result.get("step_results") is None,
        "Install-only trial produced execution or scoring output.",
    )
    started = policy._iso(raw_result.get("started_at"), "started_at")
    environment = policy._phase_timing(raw_result, "environment_setup")
    agent = policy._phase_timing(raw_result, "agent_setup")
    finished = policy._iso(raw_result.get("finished_at"), "finished_at")
    _require(
        started <= environment[0] <= environment[1] <= agent[0] <= agent[1] <= finished,
        "Install-only lifecycle timing drifted.",
    )
    _assert_cleanup(trial_dir, raw_result, task, binding)
    return raw_lock, {"task": task, "variant": variant, "id": str(parsed.id)}


def _assert_job_aggregates(job_result: Any, provider: str) -> None:
    stats = job_result.stats
    expected_keys = {
        f"{spec['name']}__{policy._PROVIDERS[provider]['model']}__{policy._DATASET}"
        for spec in policy._VARIANTS.values()
    }
    _require(
        stats.n_input_tokens is None
        and stats.n_cache_tokens is None
        and stats.n_output_tokens is None
        and stats.cost_usd is None
        and set(stats.evals) == expected_keys,
        "Install-only aggregate unexpectedly contains usage or identity data.",
    )
    for aggregate in stats.evals.values():
        _require(
            aggregate.n_trials == 0
            and aggregate.n_errors == 0
            and aggregate.reward_stats == {}
            and aggregate.exception_stats == {}
            and aggregate.pass_at_k == {}
            and aggregate.metrics == [{"mean": 0.0}],
            "Install-only aggregate unexpectedly contains scoring data.",
        )


def validate(
    prepared: Path, secret: bytes, provider: str = "deepseek"
) -> dict[str, Any]:
    requested = prepared.expanduser()
    _require(not requested.is_symlink(), "Prepared proof root must not be a symlink.")
    prepared = requested.resolve(strict=True)
    _require(provider in policy._PROVIDERS, "Unknown provider proof.")
    binding, entry, compose_path, compose_sha, config, replication = (
        _prepared_authority(prepared, provider)
    )
    expected_config, job_dir, replication = _proof_projection(
        prepared, provider, config, replication
    )
    _require(
        job_dir.parent == prepared / "install-only-jobs" / provider,
        "Install-only proof entered the scoring namespace.",
    )
    _require(
        job_dir.is_dir() and not job_dir.is_symlink() and job_dir.resolve() == job_dir,
        "Install-only proof job directory is unsafe.",
    )
    expected_trials = len(policy._TASKS) * len(policy._VARIANTS)
    raw_job_lock, stats, job_config, job_result, _ = policy._validate_job_completion(
        job_dir, expected_config, expected_trials
    )
    _require(
        (
            stats.get("n_completed_trials"),
            stats.get("n_errored_trials"),
            stats.get("n_running_trials"),
            stats.get("n_pending_trials"),
            stats.get("n_cancelled_trials"),
            stats.get("n_retries"),
        )
        == (expected_trials, 0, 0, 0, 0, 0),
        "Install-only Harbor job did not complete cleanly.",
    )
    _require(
        all(agent.kwargs.get("run_binding") == binding for agent in job_config.agents)
        and job_config.environment.import_path == ENVIRONMENT_IMPORT
        and job_config.environment.kwargs
        == {"relay_compose_sha256": compose_sha, "run_binding": binding}
        and job_config.environment.mounts
        == [policy._codex_runtime_mount(prepared / CODEX_RUNTIME_PREPARED_RELATIVE)]
        and [path.resolve() for path in job_config.environment.extra_docker_compose]
        == [compose_path.resolve()]
        and entry.get("model") == policy._PROVIDERS[provider]["model"],
        "Install-only production binding drifted.",
    )
    children = _trial_directories(job_dir, expected_trials)
    observed = [
        _assert_trial(
            path,
            provider,
            policy._PROVIDERS[provider]["model"],
            binding,
            job_config,
            job_result.id,
            compose_path,
            compose_sha,
        )
        for path in children
    ]
    root_locks = Counter(
        policy._canonical(item)
        for item in policy._sequence(raw_job_lock.get("trials"), "job locks")
    )
    child_locks = Counter(policy._canonical(item[0]) for item in observed)
    identities = Counter((item[1]["task"], item[1]["variant"]) for item in observed)
    _require(root_locks == child_locks, "Job and trial locks disagree.")
    _require(
        identities
        == Counter(
            (task, variant) for task in policy._TASKS for variant in policy._VARIANTS
        ),
        "Install-only task-arm coverage drifted.",
    )
    _require(
        len({item[1]["id"] for item in observed}) == expected_trials,
        "Install-only trial UUIDs are not unique.",
    )
    _assert_job_aggregates(job_result, provider)
    _assert_outputs_are_non_scorable(job_dir, secret)
    return {
        "schemaVersion": 1,
        "proofClass": _PROOF_CLASS,
        "scorable": False,
        "provider": provider,
        "replicationId": replication,
        "sourceRevision": binding["source_revision"],
        "taskCount": len(policy._TASKS),
        "armCount": len(policy._VARIANTS),
        "trialCount": expected_trials,
        "productionRelayImageSha256": binding["relay_image_sha256"],
    }


if __name__ == "__main__":
    if len(sys.argv) not in {2, 4} or (
        len(sys.argv) == 4 and sys.argv[2] != "--provider"
    ):
        raise SystemExit(
            "usage: PROOF_KEY | python -m "
            "benchmarks.terminal_bench.validate_harbor_install_only "
            "PREPARED_RUN [--provider PROVIDER]"
        )
    print(
        json.dumps(
            validate(
                Path(sys.argv[1]),
                sys.stdin.buffer.read().strip(),
                sys.argv[3] if len(sys.argv) == 4 else "deepseek",
            ),
            separators=(",", ":"),
            sort_keys=True,
        )
    )
