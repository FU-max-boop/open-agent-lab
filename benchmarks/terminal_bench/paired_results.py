"""Prepare and audit the frozen paired Terminal-Bench development experiment."""

from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from dirhash import dirhash
from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, build_trial_lock
from harbor.models.job.result import JobResult
from harbor.models.task.id import PackageTaskId
from harbor.models.trajectories.trajectory import Trajectory
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult
from harbor.publisher.packager import Packager
from harbor.tasks.client import TaskClient, TaskDownloadResult

from .codex_runtime import (
    CODEX_ARCHIVE_ENV,
    CODEX_RUNTIME_INSTALL_ROOT,
    CODEX_RUNTIME_PREPARED_RELATIVE,
    CODEX_RUNTIME_SPEC_SHA256,
    codex_runtime_spec,
    prepare_tree,
    validate_codex_runtime_spec,
    verify_tree,
)
from .experiment_contract import (
    CODEX_VERSION,
    ENVIRONMENT_IMPORT,
    EXPERIMENT_ID,
    LIVE_ROUTE_PROBE_LIMITS,
    LIVE_ROUTE_PROBE_TASK,
    PILOT_RECEIPT_ENV,
    PREFLIGHT_KEYS,
    RELAY_ARTIFACT_LIMITS,
    RELAY_CLAIM_FIELDS,
    RELAY_JOURNAL_PATH,
    RELAY_SEAL_PATH,
    artifact_manifest,
    canonical_digest,
    canonical_json,
    digest_bytes,
    is_digest,
    is_revision,
    is_strict_int,
    live_route_probe_config,
    live_route_probe_networks,
    live_route_probe_relay_command,
    relay_claim_name,
    same_json,
)
from .failure_classification import classify_failure
from .relay_evidence import _EVENT_FIELDS as _RELAY_FIELDS
from .relay_evidence import (
    _SEAL_FIELDS,
    _provider_response_identity_error,
    relay_metadata,
)

_MANIFEST = "benchmarks/terminal_bench/verify-instruction-v1.experiment.json"
_POLICY_SHA256 = (
    "sha256:62d10b28f16d104bdb3fa7d9e76a58fa3c33338970c602c4c9753408e138c585"
)
_HARBOR_VERSION = "0.22.0"
_CODEX_VERSION = CODEX_VERSION
_SUMMARY_SCHEMA_VERSION = 4
_PAIRED_BOOTSTRAP_RESAMPLES = 10_000
_PAIRED_BOOTSTRAP_SEED = 20260822
_RELAY_REQUEST_CAP = 256
_RELAY_JOURNAL_CAP = RELAY_ARTIFACT_LIMITS[RELAY_JOURNAL_PATH]
_RELAY_SEAL_CAP = RELAY_ARTIFACT_LIMITS[RELAY_SEAL_PATH]
_JSON_ARTIFACT_CAP = 4 * 1024 * 1024
_TRAJECTORY_CAP = 64 * 1024 * 1024
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_JOB_RESULT_FIELDS = frozenset(
    {"id", "started_at", "updated_at", "finished_at", "n_total_trials", "stats"}
)
_JOB_STATS_FIELDS = frozenset(
    {
        "n_completed_trials",
        "n_errored_trials",
        "n_running_trials",
        "n_pending_trials",
        "n_cancelled_trials",
        "n_retries",
        "evals",
        "n_input_tokens",
        "n_cache_tokens",
        "n_output_tokens",
        "cost_usd",
    }
)
_PROVIDER_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "event_count",
        "chain_head",
        "seal",
        "publication_gate",
        "records",
        "harbor_binding",
        "agent_variant",
    }
)
_PROBE_RECEIPT_FIELDS = frozenset(
    {
        "schemaVersion",
        "proofClass",
        "provider",
        "model",
        "sourceRevision",
        "preflightSha256",
        "runBindingSha256",
        "configSha256",
        "composeSha256",
        "fullComposeSha256",
        "providerCredentialSha256",
        "probeClaimSha256",
        "relayMarkerSha256",
        "relayChainHead",
        "providerRequestIdsSha256",
        "responseIdsSha256",
        "requestCount",
        "usage",
        "credentialLeakScan",
        "spendCap",
        "pilotJob",
        "probeStartedAt",
        "probeFinishedAt",
        "authorizationExpiresAt",
        "liveProviderRouteObserved",
        "liveProviderConformance",
        "benchmarkTaskInstructionUsed",
        "benchmarkRewardUsed",
        "spendCapVerification",
        "benchmarkStartAuthorized",
        "verifiedAt",
    }
)
_DATASET = "terminal-bench/terminal-bench-2-1"
_DATASET_DIGEST = (
    "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VARIANTS = {
    "control-v1": {
        "name": "open-agent-lab-codex",
        "import_path": "benchmarks.terminal_bench.harbor_agent:OpenAgentLabCodex",
        "enabled": False,
        "instruction_sha256": None,
    },
    "verify-instruction-v1": {
        "name": "open-agent-lab-codex-verify-instruction-v1",
        "import_path": (
            "benchmarks.terminal_bench.harbor_agent:"
            "OpenAgentLabCodexVerifyInstructionV1"
        ),
        "enabled": True,
        "instruction_sha256": (
            "sha256:9f855e1e34702265ed0ff4c4fcfb2483cb9777c5f37d8c29daccd2c454f84e4a"
        ),
    },
}
_PROVIDERS = {
    "deepseek": {"model": "deepseek-v4-pro", "reasoning": "high"},
    "zai": {"model": "glm-5.3", "reasoning": "max"},
}
_TASKS = (
    "terminal-bench/model-extraction-relu-logits",
    "terminal-bench/video-processing",
    "terminal-bench/feal-differential-cryptanalysis",
    "terminal-bench/large-scale-text-editing",
    "terminal-bench/multi-source-data-merger",
)
_ALL_TASKS = (*_TASKS, LIVE_ROUTE_PROBE_TASK)
_TASK_DIGESTS = {
    "terminal-bench/model-extraction-relu-logits": (
        "sha256:1ae5045ad68b5d34c3398b612066a07c4a08b6dc330d28868ec4021e17c94b17"
    ),
    "terminal-bench/video-processing": (
        "sha256:d3f02e177b49e5768b6ce6709fc4ae3ef2ce0cdecb63b09fc9b07f9d3ddb7203"
    ),
    "terminal-bench/feal-differential-cryptanalysis": (
        "sha256:8ea56995fcc43fb94f0e4e15adb12dd28836bf3e9c766b2cc7ae78a7ce90341f"
    ),
    "terminal-bench/large-scale-text-editing": (
        "sha256:1f1cddc3df15e452fe2d3c6928f6b1e5b5330a7ae67cab373a0d089ea7d334a2"
    ),
    "terminal-bench/multi-source-data-merger": (
        "sha256:70367c38732e1beda7b229968a48d60242277c2fa4db91339c3f064c4c230d49"
    ),
}
_TASK_RUNTIME_BINDINGS = {
    "terminal-bench/model-extraction-relu-logits": {
        "taskDigest": _TASK_DIGESTS["terminal-bench/model-extraction-relu-logits"],
        "taskChecksum": (
            "8b3048320aa04676089f9240563c2d7d381be03d8810bcf5f5d336cab538f523"
        ),
        "declaredImage": "alexgshaw/model-extraction-relu-logits:20251031",
        "immutableImage": (
            "alexgshaw/model-extraction-relu-logits@sha256:"
            "52fe1f089f38650f0dc22d7531ee6e01ebd526de1b349aaa2ecb331dca9fabce"
        ),
        "imageConfigDigest": (
            "sha256:cc17ca081e08612a89a3d185fc4af6735ee2a08eaeb9478f8b2474392c4feac8"
        ),
        "platform": "linux/amd64",
    },
    "terminal-bench/video-processing": {
        "taskDigest": _TASK_DIGESTS["terminal-bench/video-processing"],
        "taskChecksum": (
            "1b34c811ec16da1fb7a40d07b8a8256ea79a83e33fbbe07ce361cd53383850cf"
        ),
        "declaredImage": "alexgshaw/video-processing:20251031",
        "immutableImage": (
            "alexgshaw/video-processing@sha256:"
            "a2c0f39e3ab04e67ac6e49cad9167bb0d987babe32511768c01aa0c6905a875f"
        ),
        "imageConfigDigest": (
            "sha256:48c2f3683967aff593e02b1ef5b138ae5e4db7980bccd403ea550694b6fa8e43"
        ),
        "platform": "linux/amd64",
    },
    "terminal-bench/feal-differential-cryptanalysis": {
        "taskDigest": _TASK_DIGESTS["terminal-bench/feal-differential-cryptanalysis"],
        "taskChecksum": (
            "edacb57935c0b4181a115ac309d3b0e7c26c7cf49ccadf69762822068445df78"
        ),
        "declaredImage": "alexgshaw/feal-differential-cryptanalysis:20251031",
        "immutableImage": (
            "alexgshaw/feal-differential-cryptanalysis@sha256:"
            "bea93bafe9eab601ca58729f5735a13f8339f52055322d1cf840e3742b54a287"
        ),
        "imageConfigDigest": (
            "sha256:ad58defcdd2544ac3494b06b77af0f6715fa1b94221e6d509578b156ef4817e2"
        ),
        "platform": "linux/amd64",
    },
    "terminal-bench/large-scale-text-editing": {
        "taskDigest": _TASK_DIGESTS["terminal-bench/large-scale-text-editing"],
        "taskChecksum": (
            "e2851ab29f9dc799ae4ba2ad8f7495ccd1625476a3954dde8cec09771e41208a"
        ),
        "declaredImage": "alexgshaw/large-scale-text-editing:20251031",
        "immutableImage": (
            "alexgshaw/large-scale-text-editing@sha256:"
            "719adca3f1388220546ce6a155eee56eff3c4fe318183100320606a210f6b59c"
        ),
        "imageConfigDigest": (
            "sha256:4ca0b429596fd62b9fab0a787e276ee7ee3faf2761b56473232327218c462c56"
        ),
        "platform": "linux/amd64",
    },
    "terminal-bench/multi-source-data-merger": {
        "taskDigest": _TASK_DIGESTS["terminal-bench/multi-source-data-merger"],
        "taskChecksum": (
            "33fa3b988ff60ec62b6ce40ee455208cb083ac1ac46ddc5247e954c88b9d5e8e"
        ),
        "declaredImage": "alexgshaw/multi-source-data-merger:20251031",
        "immutableImage": (
            "alexgshaw/multi-source-data-merger@sha256:"
            "8b32782078ff7383a1b4e5d3cecca8ce287e30f50bb4c0e5db009a18064e666e"
        ),
        "imageConfigDigest": (
            "sha256:5096d3409e5c692afd62a136f90a2e52dd87653aa6637a3e1ca31ad30fab1096"
        ),
        "platform": "linux/amd64",
    },
    LIVE_ROUTE_PROBE_TASK: {
        "taskDigest": (
            "sha256:79484e87208b106689f18701db89b85e10c59fc8ea923f55c727e630196f4e8f"
        ),
        "taskChecksum": (
            "fb65a775a56b52655c0877b546a583010023f97ef237d66e61db7423469aaf45"
        ),
        "declaredImage": "ubuntu:24.04",
        "immutableImage": (
            "ubuntu@sha256:1e0a86e57d247923571b75e0aaf48a1449cf8c543d51fb3e07a4a7d7bfa79316"
        ),
        "imageConfigDigest": (
            "sha256:a6f81fb630d51837271b89f8193810a5fc493fa4f30a55d7ebcdb3a66f3cc63a"
        ),
        "platform": "linux/amd64",
    },
}


def _task_relative(task: str) -> str:
    return (
        task.removeprefix("terminal-bench/")
        if task.startswith("terminal-bench/")
        else task.removeprefix("open-agent-lab/")
    )


def _declared_task_snapshots() -> dict[str, dict[str, str]]:
    return {
        task: {
            "relativePath": f"tasks/{_task_relative(task)}",
            "taskDigest": binding["taskDigest"],
            "taskChecksum": binding["taskChecksum"],
        }
        for task, binding in _TASK_RUNTIME_BINDINGS.items()
    }


_TEMPLATE_ORDERS = {
    "deepseek": ["control-v1", "verify-instruction-v1"],
    "zai": ["verify-instruction-v1", "control-v1"],
}
_TEMPLATES = {
    provider: f"benchmarks/terminal_bench/pilot-v2.{provider}.yaml"
    for provider in _PROVIDERS
}
_E2E_TEMPLATES = (
    "harbor-e2e.yaml",
    "harbor-verify-instruction-e2e.yaml",
)
_RELAY_BUILD_INPUTS = (
    ".dockerignore",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.base.json",
    "apps/cli/package.json",
    "apps/cli/src/relay-command.ts",
    "apps/cli/src/relay-entry.ts",
    "apps/cli/src/relay-evidence.ts",
    "apps/cli/src/responses-metadata.ts",
    "apps/cli/src/responses-relay.ts",
    "apps/cli/tsconfig.relay.json",
    "benchmarks/terminal_bench/relay.Dockerfile",
    "packages/contracts/package.json",
    "packages/contracts/tsconfig.json",
    "packages/evidence/package.json",
    "packages/evidence/tsconfig.json",
    "packages/kernel/package.json",
    "packages/model-driver/package.json",
    "packages/tool-broker/package.json",
)
_RELAY_IMAGES = {
    "production": "production",
    "providerFreeFixture": "fixture",
}
_RELAY_BUILD_DIRECTORIES = (
    "packages/contracts/src",
    "packages/evidence/src",
)
_RELAY_FIXTURE_INPUTS = (
    "apps/cli/src/relay-fixture-entry.ts",
    "apps/cli/src/responses-fixture.ts",
    "benchmarks/terminal_bench/verify-instruction-v1.txt",
)
_SCORABLE_INCOMPLETE_RELAY_REASONS = {
    "no_completed_response",
    "provider_request_id_missing",
    "provider_request_incomplete_or_failed",
    "provider_metadata_unreliable",
    "relay_rejected_requests",
    "response_id_missing",
    "returned_model_missing",
    "terminal_event_missing",
    "usage_missing_or_invalid",
}


class IntegrityError(ValueError):
    """An experiment artifact cannot be included in the analysis."""


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise IntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise IntegrityError(f"non-finite JSON number: {value}")


def _loads(text: str, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid JSON in {label}: {error}") from error


def _load(path: Path) -> Any:
    try:
        return _loads(path.read_text(), path.name)
    except (OSError, UnicodeError) as error:
        raise IntegrityError(
            f"cannot read required artifact {path.name}: {error}"
        ) from error


def _artifact_bytes(
    root: Path, relative: Path | str, *, max_bytes: int | None = None
) -> bytes:
    relative = _relative(Path(relative).as_posix(), "artifact")
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise IntegrityError("artifact root is unavailable") from error
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        directory = os.open(root, directory_flags)
        descriptors.append(directory)
        for part in relative.parts[:-1]:
            directory = os.open(part, directory_flags, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
    except OSError as error:
        for opened in reversed(descriptors):
            os.close(opened)
        raise IntegrityError(f"cannot open required artifact: {relative}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise IntegrityError(f"required artifact is not a regular file: {relative}")
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise IntegrityError(f"required artifact is too large: {relative}")
        with os.fdopen(descriptor, "rb") as artifact:
            descriptor = -1
            data = artifact.read(None if max_bytes is None else max_bytes + 1)
            if max_bytes is not None and len(data) > max_bytes:
                raise IntegrityError(f"required artifact is too large: {relative}")
            return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for opened in reversed(descriptors):
            os.close(opened)


def _artifact_json(
    root: Path,
    relative: Path | str,
    label: str,
    *,
    max_bytes: int = _JSON_ARTIFACT_CAP,
) -> Any:
    try:
        text = _artifact_bytes(root, relative, max_bytes=max_bytes).decode()
    except UnicodeError as error:
        raise IntegrityError(f"{label} must be UTF-8") from error
    return _loads(text, label)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader: _UniqueLoader, node: yaml.Node, deep: bool = False) -> Any:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise IntegrityError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping
)


def _yaml_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = yaml.load(data.decode(), Loader=_UniqueLoader)
    except (UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid YAML in {label}: {error}") from error
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must contain one mapping")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise IntegrityError(f"invalid YAML in {path.name}: {error}") from error
    return _yaml_bytes(data, path.name)


def _canonical(value: Any) -> str:
    return canonical_json(value).decode()


def _same_json(left: Any, right: Any) -> bool:
    try:
        return same_json(left, right)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"non-canonical JSON value: {error}") from error


def _digest_bytes(value: bytes) -> str:
    return digest_bytes(value)


def _digest(value: Any) -> str:
    return canonical_digest(value)


def _task_content_identity(path: Path) -> tuple[str, str]:
    content_hash, _ = Packager.compute_content_hash(path)
    return f"sha256:{content_hash}", dirhash(path, "sha256")


def _validate_task_snapshots(
    run_root: Path, snapshots: dict[str, Any]
) -> dict[str, dict[str, str]]:
    expected = _declared_task_snapshots()
    if not _same_json(snapshots, expected):
        raise IntegrityError("prepared task snapshot authority drifted")
    tasks_root = run_root / "tasks"
    if tasks_root.is_symlink() or not tasks_root.is_dir():
        raise IntegrityError("prepared task snapshot root is unavailable")
    if {item.name for item in tasks_root.iterdir()} != {
        _task_relative(task) for task in expected
    }:
        raise IntegrityError("prepared task snapshot root has unexpected entries")
    for task, binding in expected.items():
        relative = _relative(binding["relativePath"], "prepared task snapshot")
        path = run_root / relative
        if (
            path.is_symlink()
            or not path.is_dir()
            or path.resolve().parent != tasks_root.resolve()
            or any(item.is_symlink() for item in path.rglob("*"))
        ):
            raise IntegrityError(f"prepared task snapshot is unsafe: {task}")
        try:
            digest, checksum = _task_content_identity(path)
        except (OSError, ValueError) as error:
            raise IntegrityError(
                f"prepared task snapshot cannot be hashed: {task}"
            ) from error
        if digest != binding["taskDigest"] or checksum != binding["taskChecksum"]:
            raise IntegrityError(f"prepared task snapshot drifted: {task}")
    return expected


def _materialize_task_snapshots(source: Path, temp: Path) -> dict[str, dict[str, str]]:
    tasks_root = temp / "tasks"
    tasks_root.mkdir()
    task_ids = [
        PackageTaskId(
            org="terminal-bench",
            name=task.removeprefix("terminal-bench/"),
            ref=_TASK_DIGESTS[task],
        )
        for task in _TASKS
    ]
    try:
        batch = asyncio.run(
            TaskClient().download_tasks(
                task_ids,
                overwrite=True,
                output_dir=tasks_root,
                export=True,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise IntegrityError("frozen task materialization failed") from error
    expected_paths = [tasks_root / task.name for task in task_ids]
    if len(batch.results) != len(task_ids) or any(
        result.path.resolve() != expected.resolve()
        for result, expected in zip(batch.results, expected_paths, strict=True)
    ):
        raise IntegrityError("frozen task materialization returned unexpected paths")
    shutil.copytree(
        source / "benchmarks" / "terminal_bench" / "live-route-probe-task",
        tasks_root / _task_relative(LIVE_ROUTE_PROBE_TASK),
    )
    return _validate_task_snapshots(temp, _declared_task_snapshots())


def _frozen_file_digest(root: Path, name: str) -> str:
    data = (root / name).read_bytes()
    if name == "benchmarks/terminal_bench/paired_results.py":
        needle = _POLICY_SHA256.encode()
        if data.count(needle) != 1:
            raise IntegrityError("policy digest literal is not unique")
        data = data.replace(needle, b"sha256:" + b"0" * 64)
    return _digest_bytes(data)


def _relay_build_id(root: Path, *, fixture: bool = False) -> str:
    """Reproduce relay.Dockerfile's sorted sha256sum-of-sha256s identity."""
    names = list(_RELAY_BUILD_INPUTS)
    for directory in _RELAY_BUILD_DIRECTORIES:
        names.extend(
            path.relative_to(root).as_posix()
            for path in (root / directory).rglob("*")
            if path.is_file()
        )
    if fixture:
        names.extend(_RELAY_FIXTURE_INPUTS)
    payload = "".join(
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(names)
    )
    return _digest_bytes(payload.encode())


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IntegrityError(f"{label} must be a non-negative integer")
    return value


def _number(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise IntegrityError(f"{label} must be a finite number")
    return float(value)


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


def _relative(path: str, label: str) -> Path:
    if not isinstance(path, str) or not path:
        raise IntegrityError(f"{label} must be a safe relative path")
    value = Path(path)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise IntegrityError(f"{label} must be a safe relative path")
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if completed.returncode:
        raise IntegrityError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _clean_revision(root: Path) -> str:
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("prepare requires a clean Git worktree")
    revision = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if not is_revision(revision):
        raise IntegrityError("source revision is not a 40-character commit")
    return revision


def _materialize_revision(root: Path, revision: str, target: Path) -> Path:
    """Materialize one self-contained detached clone of the committed source."""
    commands = (
        (
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            "--",
            str(root),
            str(target),
        ),
        ("-C", str(target), "checkout", "--quiet", "--detach", revision),
    )
    for command in commands:
        completed = subprocess.run(
            ["git", *command], text=True, capture_output=True, check=False
        )
        if completed.returncode:
            raise IntegrityError(
                completed.stderr.strip() or "committed source snapshot failed"
            )
    if _clean_revision(target) != revision:
        raise IntegrityError("committed source snapshot identity drifted")
    return target


def _docker(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["docker", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise IntegrityError(detail or "Docker command failed")
    return completed.stdout.strip()


def _relay_image_tags(output: Path, revision: str) -> dict[str, str]:
    token = hashlib.sha256(f"{output.resolve()}\0{revision}".encode()).hexdigest()[:32]
    return {
        identity: f"open-agent-lab-prepared:{token}-{target}"
        for identity, target in _RELAY_IMAGES.items()
    }


def _prepare_lock_path(output: Path) -> Path:
    return output.parent / f".{output.name}.open-agent-lab-prepare.lock"


def _acquire_prepare_lock(output: Path) -> int:
    path = _prepare_lock_path(output)
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise IntegrityError("prepare lock is not a safe regular file") from error
    identity = os.fstat(descriptor)
    if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
        os.close(descriptor)
        raise IntegrityError("prepare lock is not a safe regular file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise IntegrityError(
            "another preparation for this output is already in progress"
        ) from error
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"pid={os.getpid()}\n".encode())
    os.fsync(descriptor)
    return descriptor


def _release_prepare_lock(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _assert_relay_tags_available(tags: dict[str, str]) -> None:
    for tag in tags.values():
        retained = _docker(
            "image",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"reference={tag}",
        ).splitlines()
        if retained:
            raise IntegrityError("a prepared relay image tag already exists")


def _read_built_image_id(iidfile: Path) -> str:
    try:
        image = iidfile.read_text().strip()
    except OSError as error:
        raise IntegrityError("Docker did not publish an immutable image ID") from error
    finally:
        iidfile.unlink(missing_ok=True)
    if not _SHA256.fullmatch(image):
        raise IntegrityError("Docker returned an invalid immutable image ID")
    return image


def _build_relay_image(
    snapshot: Path,
    identity: str,
    target: str,
    expected_build_id: str,
    tag: str,
    dockerignore_sha: str,
    workspace: Path,
    known_images: set[str],
    created_images: set[str],
) -> str:
    iidfile = workspace / f"{target}.iid"
    _docker(
        "build",
        "--file",
        str(snapshot / "benchmarks/terminal_bench/relay.Dockerfile"),
        "--target",
        target,
        "--tag",
        tag,
        "--build-arg",
        f"OAL_DOCKERIGNORE_SHA256={dockerignore_sha}",
        "--iidfile",
        str(iidfile),
        str(snapshot),
    )
    image = _read_built_image_id(iidfile)
    if image not in known_images:
        created_images.add(image)
    embedded = _docker(
        "run", "--rm", "--entrypoint", "cat", image, "/app/relay-build-id"
    )
    if embedded != expected_build_id:
        raise IntegrityError(f"{identity} relay image build identity drifted")
    if _docker("image", "inspect", "--format", "{{.Id}}", tag) != image:
        raise IntegrityError(f"{identity} relay image tag drifted")
    return image


def _build_relay_images(
    snapshot: Path,
    expected_build_ids: dict[str, str],
    workspace: Path,
    tags: dict[str, str],
    created_images: set[str],
) -> dict[str, str]:
    if set(tags) != set(_RELAY_IMAGES):
        raise IntegrityError("relay image tags are incomplete")
    _assert_relay_tags_available(tags)
    known_images = set(_docker("image", "ls", "--quiet", "--no-trunc").splitlines())
    dockerignore_sha = hashlib.sha256(
        (snapshot / ".dockerignore").read_bytes()
    ).hexdigest()
    images: dict[str, str] = {}
    for identity, target in _RELAY_IMAGES.items():
        images[identity] = _build_relay_image(
            snapshot,
            identity,
            target,
            expected_build_ids[identity],
            tags[identity],
            dockerignore_sha,
            workspace,
            known_images,
            created_images,
        )
    if len(set(images.values())) != len(images):
        raise IntegrityError("production and fixture relay images must differ")
    return images


def _discard_relay_images(tags: dict[str, str], created_images: set[str]) -> None:
    for reference in (*tags.values(), *created_images):
        try:
            _docker("image", "rm", reference)
        except IntegrityError:
            pass


def _merge_compose(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_compose(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _pinned_overlay(
    source: Path,
    provider: str,
    image: str,
    *,
    fixture: bool = False,
    live_route_probe: bool = False,
) -> dict[str, Any]:
    if fixture and live_route_probe:
        raise IntegrityError("fixture and live-route policies are mutually exclusive")
    base = _load_yaml(
        source / "benchmarks" / "terminal_bench" / f"relay.{provider}.compose.yaml"
    )
    if fixture:
        base = _merge_compose(
            base,
            _load_yaml(
                source / "benchmarks" / "terminal_bench" / "relay.fixture.compose.yaml"
            ),
        )
    services = _mapping(base.get("services"), "compose services")
    relay = _mapping(services.get("open-agent-lab-relay"), "relay service")
    relay.pop("build", None)
    relay["image"] = image
    relay["pull_policy"] = "never"
    if live_route_probe:
        relay["command"] = live_route_probe_relay_command(relay.get("command"))
        base = live_route_probe_networks(base)
    return base


def _render_pinned_overlays(
    source: Path, temp: Path, images: dict[str, str]
) -> dict[str, dict[str, str]]:
    overlays = temp / "overlays"
    overlays.mkdir()
    rendered: dict[str, dict[str, str]] = {}
    for provider in _PROVIDERS:
        name = f"relay.{provider}.compose.yaml"
        text = yaml.safe_dump(
            _pinned_overlay(source, provider, images["production"]), sort_keys=False
        )
        (overlays / name).write_text(text)
        rendered[provider] = {
            "path": f"overlays/{name}",
            "sha256": _digest_bytes(text.encode()),
            "image": images["production"],
        }
        probe_name = f"relay.{provider}.live-route-probe.compose.yaml"
        probe_text = yaml.safe_dump(
            _pinned_overlay(
                source,
                provider,
                images["production"],
                live_route_probe=True,
            ),
            sort_keys=False,
        )
        (overlays / probe_name).write_text(probe_text)
        rendered[f"{provider}-live-route-probe"] = {
            "path": f"overlays/{probe_name}",
            "sha256": _digest_bytes(probe_text.encode()),
            "image": images["production"],
        }
    fixture_name = "relay.fixture.compose.yaml"
    fixture_text = yaml.safe_dump(
        _pinned_overlay(
            source, "deepseek", images["providerFreeFixture"], fixture=True
        ),
        sort_keys=False,
    )
    (overlays / fixture_name).write_text(fixture_text)
    rendered["fixture"] = {
        "path": f"overlays/{fixture_name}",
        "sha256": _digest_bytes(fixture_text.encode()),
        "image": images["providerFreeFixture"],
    }
    return rendered


def _validated_codex_runtime(runtime: dict[str, Any]) -> dict[str, object]:
    try:
        validated = validate_codex_runtime_spec(runtime.get("codexRuntime"))
    except (TypeError, ValueError) as error:
        raise IntegrityError("frozen Codex runtime specification drifted") from error
    if not _same_json(validated, codex_runtime_spec()):
        raise IntegrityError("frozen Codex runtime specification drifted")
    return validated


def _manifest(root: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    path = root / _MANIFEST
    manifest = _mapping(_load(path), "experiment manifest")
    raw_hash = _digest_bytes(path.read_bytes())
    hashes = _mapping(manifest.get("fileSha256"), "fileSha256")
    runtime = _mapping(manifest.get("runtime"), "runtime")
    _validated_codex_runtime(runtime)
    configs = _sequence(manifest.get("pairedConfigs"), "pairedConfigs")
    arms = _sequence(manifest.get("arms"), "arms")
    if (
        _digest(manifest) != _POLICY_SHA256
        or manifest.get("schemaVersion") != 2
        or manifest.get("experimentId") != EXPERIMENT_ID
        or manifest.get("runClass") != "development"
        or runtime.get("harborVersion") != _HARBOR_VERSION
        or runtime.get("codexVersion") != _CODEX_VERSION
        or runtime.get("datasetName") != _DATASET
        or runtime.get("datasetDigest") != _DATASET_DIGEST
        or runtime.get("concurrency") != 1
        or runtime.get("harborRetries") != 0
        or runtime.get("hermeticCodexRuntimeReady") is not True
        or runtime.get("relayRequestCapPerTrial") != _RELAY_REQUEST_CAP
        or runtime.get("taskOrder") != list(_TASKS)
        or runtime.get("taskDigests") != _TASK_DIGESTS
        or manifest.get("taskRuntimeBindings") != _TASK_RUNTIME_BINDINGS
        or configs
        != [
            {
                "provider": provider,
                "model": spec["model"],
                "reasoningEffort": spec["reasoning"],
                "templateArmOrder": _TEMPLATE_ORDERS[provider],
                "path": _TEMPLATES[provider],
            }
            for provider, spec in _PROVIDERS.items()
        ]
        or [
            (
                item.get("variantId"),
                item.get("developerInstructionRequested"),
                item.get("requestedDeveloperInstructionsSha256"),
            )
            for item in arms
            if isinstance(item, dict)
        ]
        != [
            (variant, spec["enabled"], spec["instruction_sha256"])
            for variant, spec in _VARIANTS.items()
        ]
        or manifest.get("relayBuildIds")
        != {
            "production": _relay_build_id(root),
            "providerFreeFixture": _relay_build_id(root, fixture=True),
        }
        or manifest.get("failureScoring")
        != {
            "missingOfficialReward": "invalidates_analysis",
            "exceptionOrTimeout": ("scores_official_reward_and_remains_in_denominator"),
            "missingTelemetry": ("preserved_as_null_and_blocks_complete_analysis"),
            "rerunPolicy": "new_predeclared_experiment_only",
        }
    ):
        raise IntegrityError("experiment manifest policy drifted")
    for name, expected in hashes.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise IntegrityError("fileSha256 has an invalid entry")
        relative = _relative(name, "frozen file")
        try:
            actual = _frozen_file_digest(root, relative.as_posix())
        except OSError as error:
            raise IntegrityError(f"frozen file is unavailable: {name}") from error
        if actual != expected:
            raise IntegrityError(f"frozen file drifted: {name}")
    selection_spec = _mapping(manifest.get("selection"), "selection")
    selection_path = root / _relative(selection_spec.get("manifest"), "selection")
    if _digest_bytes(selection_path.read_bytes()) != selection_spec.get("sha256"):
        raise IntegrityError("selection manifest drifted")
    selection = _mapping(_load(selection_path), "selection manifest")
    tasks = _sequence(selection.get("selection", {}).get("tasks"), "selected tasks")
    if [item.get("id") for item in tasks if isinstance(item, dict)] != [
        "model-extraction-relu-logits",
        "multi-source-data-merger",
        "feal-differential-cryptanalysis",
        "video-processing",
        "large-scale-text-editing",
    ]:
        raise IntegrityError("selected task order drifted")
    if {task.removeprefix("terminal-bench/") for task in _TASKS} != {
        item["id"] for item in tasks
    }:
        raise IntegrityError("runtime task order drifted")
    return manifest, selection, raw_hash


def _replication(manifest: dict[str, Any], replication_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in _sequence(manifest.get("replications"), "replications")
        if isinstance(item, dict) and item.get("id") == replication_id
    ]
    if len(matches) != 1:
        raise IntegrityError(f"unknown replication: {replication_id}")
    orders = _mapping(matches[0].get("armOrderByProvider"), "armOrderByProvider")
    if set(orders) != set(_PROVIDERS) or any(
        order
        not in (
            ["control-v1", "verify-instruction-v1"],
            ["verify-instruction-v1", "control-v1"],
        )
        for order in orders.values()
    ):
        raise IntegrityError("replication arm order drifted")
    return matches[0]


def _variant_from_agent(agent: dict[str, Any]) -> str:
    kwargs = _mapping(agent.get("kwargs"), "agent kwargs")
    enabled = kwargs.get("enable_verify_instruction_v1")
    if type(enabled) is not bool:
        raise IntegrityError("experiment switch must be a boolean")
    variant = "verify-instruction-v1" if enabled else "control-v1"
    expected = _VARIANTS[variant]
    if (
        agent.get("name") != expected["name"]
        or agent.get("import_path") != expected["import_path"]
    ):
        raise IntegrityError(f"{variant} agent identity drifted")
    return variant


def _validate_task_selection(
    config: dict[str, Any], provider: str, tasks: list[str], task_root: Path | None
) -> None:
    if task_root is None:
        datasets = _sequence(config.get("datasets"), "datasets")
        if len(datasets) != 1:
            raise IntegrityError(f"{provider} task selection drifted")
        dataset = _mapping(datasets[0], "dataset")
        if (
            dataset.get("name") != _DATASET
            or dataset.get("ref") != _DATASET_DIGEST
            or dataset.get("task_names") != tasks
            or config.get("tasks", []) != []
        ):
            raise IntegrityError(f"{provider} task selection drifted")
    elif config.get("datasets", []) != [] or config.get("tasks") != [
        {
            "path": str(task_root / task.removeprefix("terminal-bench/")),
            "source": _DATASET,
        }
        for task in tasks
    ]:
        raise IntegrityError(f"{provider} prepared task selection drifted")


def _validate_template(
    config: dict[str, Any],
    provider: str,
    model: str,
    tasks: list[str],
    task_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    if (
        not is_strict_int(config.get("n_attempts"))
        or config["n_attempts"] != 1
        or not is_strict_int(config.get("n_concurrent_trials"))
        or config["n_concurrent_trials"] != 1
        or not _same_json(config.get("retry"), {"max_retries": 0})
    ):
        raise IntegrityError(f"{provider} execution policy drifted")
    _validate_task_selection(config, provider, tasks, task_root)
    agents = _sequence(config.get("agents"), "agents")
    if len(agents) != 2:
        raise IntegrityError(f"{provider} must contain exactly two arms")
    by_variant: dict[str, dict[str, Any]] = {}
    for raw in agents:
        agent = _mapping(raw, "agent")
        variant = _variant_from_agent(agent)
        if variant in by_variant or agent.get("model_name") != f"{provider}/{model}":
            raise IntegrityError(f"{provider} agent variants drifted")
        if agent["kwargs"].get("version") != _CODEX_VERSION:
            raise IntegrityError("Codex version drifted")
        if agent["kwargs"].get("reasoning_effort") != _PROVIDERS[provider]["reasoning"]:
            raise IntegrityError(f"{provider} reasoning effort drifted")
        by_variant[variant] = agent
    if set(by_variant) != set(_VARIANTS):
        raise IntegrityError(f"{provider} agent variants are incomplete")
    return by_variant


def _bound_config(
    config: dict[str, Any],
    by_variant: dict[str, dict[str, Any]],
    order: list[str],
    binding: dict[str, Any],
    task_root: Path,
    job_name: str,
    jobs_dir: Path,
    compose_path: Path,
    compose_sha256: str,
    runtime_root: Path,
    authorization_path: Path,
) -> dict[str, Any]:
    config["datasets"] = []
    config["tasks"] = [
        {
            "path": str(task_root / task.removeprefix("terminal-bench/")),
            "source": _DATASET,
        }
        for task in _TASKS
    ]
    config["agents"] = [by_variant[variant] for variant in order]
    for agent in config["agents"]:
        agent["kwargs"] = {**agent["kwargs"], "run_binding": binding}
        agent["env"] = {
            "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
            PILOT_RECEIPT_ENV: str(authorization_path),
        }
    config["job_name"] = job_name
    config["jobs_dir"] = str(jobs_dir)
    environment = _mapping(config.get("environment"), "environment")
    environment["mounts"] = [_codex_runtime_mount(runtime_root)]
    environment["extra_docker_compose"] = [str(compose_path)]
    environment["import_path"] = ENVIRONMENT_IMPORT
    environment["kwargs"] = {
        "relay_compose_sha256": compose_sha256,
        "run_binding": binding,
    }
    return config


def _codex_runtime_mount(runtime_root: Path) -> dict[str, object]:
    if not runtime_root.is_absolute():
        raise IntegrityError("prepared Codex runtime path must be absolute")
    return {
        "type": "bind",
        "source": str(runtime_root),
        "target": CODEX_RUNTIME_INSTALL_ROOT,
        "read_only": True,
    }


def _materialize_codex_runtime(
    temp: Path, spec: object
) -> tuple[Path, dict[str, object]]:
    archive = os.environ.get(CODEX_ARCHIVE_ENV)
    if not archive:
        raise IntegrityError(f"{CODEX_ARCHIVE_ENV} must name the pinned Codex archive")
    archive_path = Path(archive)
    if not archive_path.is_absolute():
        raise IntegrityError(f"{CODEX_ARCHIVE_ENV} must be an absolute path")
    runtime_root = temp / CODEX_RUNTIME_PREPARED_RELATIVE
    runtime_root.parent.mkdir(parents=True)
    try:
        receipt = prepare_tree(archive_path, runtime_root, spec)
    except (OSError, TypeError, ValueError) as error:
        raise IntegrityError("pinned Codex runtime preparation failed") from error
    return runtime_root, receipt


def _write_fixture_configs(
    root: Path,
    temp: Path,
    output: Path,
    preflight: dict[str, Any],
    binding: dict[str, Any],
    fixture_build_id: str,
    fixture_image_id: str,
    fixture_compose_path: Path,
    fixture_compose_sha256: str,
    runtime_root: Path,
) -> None:
    fixture_preflight = {
        **preflight,
        "relayBuildSha256": fixture_build_id,
        "relayImageSha256": fixture_image_id,
    }
    fixture_binding = {
        **binding,
        "relay_build_sha256": fixture_build_id,
        "relay_image_sha256": fixture_image_id,
        "preflight_sha256": _digest(fixture_preflight),
    }
    (temp / "fixtures" / "preflight.json").write_text(
        _canonical(fixture_preflight) + "\n"
    )
    for name in _E2E_TEMPLATES:
        fixture = _load_yaml(root / "benchmarks" / "terminal_bench" / name)
        agents = _sequence(fixture.get("agents"), f"{name} agents")
        if len(agents) != 1:
            raise IntegrityError(f"{name} must contain exactly one fixture agent")
        agent = _mapping(agents[0], f"{name} agent")
        agent["kwargs"] = {
            **_mapping(agent.get("kwargs"), f"{name} agent kwargs"),
            "run_binding": fixture_binding,
        }
        environment = _mapping(fixture.get("environment"), f"{name} environment")
        environment["mounts"] = [_codex_runtime_mount(runtime_root)]
        environment["extra_docker_compose"] = [str(fixture_compose_path)]
        environment["import_path"] = ENVIRONMENT_IMPORT
        environment["kwargs"] = {
            "relay_compose_sha256": fixture_compose_sha256,
            "run_binding": fixture_binding,
        }
        fixture["jobs_dir"] = str(output / "fixture-jobs" / Path(name).stem)
        (temp / "fixtures" / name).write_text(yaml.safe_dump(fixture, sort_keys=False))


def _write_live_route_probe_configs(
    temp: Path,
    output: Path,
    binding: dict[str, Any],
    overlays: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for provider, profile in _PROVIDERS.items():
        job_name = (
            f"open-agent-lab-{binding['replication_id']}-{provider}-live-route-probe"
        )
        overlay = overlays[f"{provider}-live-route-probe"]
        config = live_route_probe_config(
            output,
            binding,
            provider,
            profile["model"],
            profile["reasoning"],
            output / overlay["path"],
            overlay["sha256"],
        )
        try:
            JobConfig.model_validate(config)
        except (TypeError, ValueError) as error:
            raise IntegrityError("live-route probe config is invalid") from error
        rendered = yaml.safe_dump(config, sort_keys=False)
        relative = f"live-route-probes/{provider}.yaml"
        (temp / relative).write_text(rendered)
        records.append(
            {
                "provider": provider,
                "model": profile["model"],
                "reasoning": profile["reasoning"],
                "task": LIVE_ROUTE_PROBE_TASK,
                "config": relative,
                "configSha256": _digest_bytes(rendered.encode()),
                "jobDir": f"live-route-jobs/{provider}/{job_name}",
                "compose": overlay["path"],
                "composeSha256": overlay["sha256"],
                "relayImageSha256": overlay["image"],
                "limits": dict(LIVE_ROUTE_PROBE_LIMITS),
            }
        )
    return records


def _prepare_run(
    root: Path,
    output: Path,
    replication_id: str,
    revision: str,
    temp: Path,
    tags: dict[str, str],
    created_images: set[str],
) -> None:
    snapshot = _materialize_revision(root, revision, temp / "source")
    manifest, _, manifest_sha = _manifest(snapshot)
    runtime_spec = validate_codex_runtime_spec(manifest["runtime"]["codexRuntime"])
    _, runtime_receipt = _materialize_codex_runtime(temp, runtime_spec)
    published_runtime_root = output / CODEX_RUNTIME_PREPARED_RELATIVE
    replication = _replication(manifest, replication_id)
    task_snapshots = _materialize_task_snapshots(snapshot, temp)
    images = _build_relay_images(
        snapshot, manifest["relayBuildIds"], temp, tags, created_images
    )
    overlays = _render_pinned_overlays(snapshot, temp, images)
    preflight = {
        "schemaVersion": 1,
        "experimentId": EXPERIMENT_ID,
        "replicationId": replication_id,
        "sourceRevision": revision,
        "experimentManifestSha256": manifest_sha,
        "relayBuildSha256": manifest["relayBuildIds"]["production"],
        "relayImageSha256": images["production"],
        "taskSnapshotsSha256": _digest(task_snapshots),
        "cleanTree": True,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    preflight_sha = _digest(preflight)
    binding = _expected_binding(preflight, preflight_sha)
    configs = {
        item["provider"]: item
        for item in _sequence(manifest.get("pairedConfigs"), "pairedConfigs")
    }
    if set(configs) != set(_PROVIDERS):
        raise IntegrityError("paired config providers drifted")
    (temp / "configs").mkdir()
    (temp / "fixtures").mkdir()
    (temp / "live-route-probes").mkdir()
    (temp / "authorizations").mkdir(mode=0o700)
    providers: list[dict[str, Any]] = []
    for provider, spec in _PROVIDERS.items():
        configured = _mapping(configs[provider], f"{provider} paired config")
        if configured.get("model") != spec["model"]:
            raise IntegrityError(f"{provider} model drifted")
        config = _load_yaml(
            snapshot / _relative(configured.get("path"), "paired config")
        )
        by_variant = _validate_template(config, provider, spec["model"], list(_TASKS))
        order = replication["armOrderByProvider"][provider]
        job_name = f"open-agent-lab-{replication_id}-{provider}"
        config = _bound_config(
            config,
            by_variant,
            order,
            binding,
            output / "tasks",
            job_name,
            output / "jobs" / provider,
            output / overlays[provider]["path"],
            overlays[provider]["sha256"],
            published_runtime_root,
            output / "authorizations" / f"{provider}.json",
        )
        rendered = yaml.safe_dump(config, sort_keys=False)
        (temp / "configs" / f"{provider}.yaml").write_text(rendered)
        providers.append(
            {
                "provider": provider,
                "model": spec["model"],
                "armOrder": order,
                "config": f"configs/{provider}.yaml",
                "configSha256": _digest_bytes(rendered.encode()),
                "jobDir": f"jobs/{provider}/{job_name}",
                "compose": overlays[provider]["path"],
                "composeSha256": overlays[provider]["sha256"],
                "relayImageSha256": overlays[provider]["image"],
            }
        )
    _write_fixture_configs(
        snapshot,
        temp,
        output,
        preflight,
        binding,
        manifest["relayBuildIds"]["providerFreeFixture"],
        images["providerFreeFixture"],
        output / overlays["fixture"]["path"],
        overlays["fixture"]["sha256"],
        published_runtime_root,
    )
    live_route_probes = _write_live_route_probe_configs(
        temp,
        output,
        binding,
        overlays,
    )
    if _clean_revision(root) != revision:
        raise IntegrityError("source revision changed while preparing the run")
    record = {
        "schemaVersion": 1,
        "preflight": preflight,
        "preflightSha256": preflight_sha,
        "relayImages": images,
        "relayImageTags": tags,
        "taskSnapshots": task_snapshots,
        "codexRuntime": runtime_receipt,
        "providers": providers,
        "liveRouteProbes": live_route_probes,
    }
    pending_record = temp / ".run-record.json"
    pending_record.write_text(_canonical(record) + "\n")
    shutil.copytree(temp, output, ignore=shutil.ignore_patterns(pending_record.name))
    os.link(pending_record, output / "run-record.json")


def prepare(output: Path, replication_id: str = "screen-v1") -> Path:
    """Create a non-overwriting, source-bound live-run directory."""
    root = _repo_root()
    output = output.expanduser().resolve()
    if output == root or root in output.parents:
        raise IntegrityError("run output must be outside the repository")
    if output.exists() or not output.parent.is_dir():
        raise IntegrityError("run output must not exist and its parent must exist")
    lock_descriptor = _acquire_prepare_lock(output)
    tags: dict[str, str] = {}
    created_images: set[str] = set()
    published = False
    temp: Path | None = None
    try:
        if output.exists():
            raise IntegrityError("run output appeared while acquiring ownership")
        revision = _clean_revision(root)
        tags.update(_relay_image_tags(output, revision))
        temp = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        _prepare_run(root, output, replication_id, revision, temp, tags, created_images)
        published = True
    except FileExistsError as error:
        raise IntegrityError(
            "run output appeared during non-overwriting publication"
        ) from error
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        if not published:
            _discard_relay_images(tags, created_images)
        _release_prepare_lock(lock_descriptor)
    return output / "run-record.json"


def _validate_live_route_probe_records(
    run_dir: Path,
    probes: list[Any],
    preflight: dict[str, Any],
    preflight_sha: str,
    production_image: str,
) -> list[dict[str, Any]]:
    if len(probes) != len(_PROVIDERS):
        raise IntegrityError("live-route probe records are incomplete")
    observed_providers: set[str] = set()
    validated: list[dict[str, Any]] = []
    binding = _expected_binding(preflight, preflight_sha)
    for raw in probes:
        probe = _mapping(raw, "live-route probe")
        provider = probe.get("provider")
        job_name = (
            f"open-agent-lab-{preflight['replicationId']}-{provider}-live-route-probe"
        )
        if (
            set(probe)
            != {
                "provider",
                "model",
                "reasoning",
                "task",
                "config",
                "configSha256",
                "jobDir",
                "compose",
                "composeSha256",
                "relayImageSha256",
                "limits",
            }
            or provider not in _PROVIDERS
            or provider in observed_providers
            or probe.get("config") != f"live-route-probes/{provider}.yaml"
            or probe.get("jobDir") != f"live-route-jobs/{provider}/{job_name}"
            or probe.get("compose")
            != f"overlays/relay.{provider}.live-route-probe.compose.yaml"
            or probe.get("model") != _PROVIDERS[provider]["model"]
            or probe.get("reasoning") != _PROVIDERS[provider]["reasoning"]
            or probe.get("task") != LIVE_ROUTE_PROBE_TASK
            or probe.get("relayImageSha256") != production_image
            or probe.get("limits") != dict(LIVE_ROUTE_PROBE_LIMITS)
        ):
            raise IntegrityError("live-route probe record drifted")
        observed_providers.add(provider)
        artifacts: dict[str, bytes] = {}
        for field in ("config", "compose"):
            relative = _relative(probe.get(field), f"live-route {field}")
            try:
                data = _artifact_bytes(run_dir, relative.as_posix())
            except OSError as error:
                raise IntegrityError(f"live-route {field} is unavailable") from error
            if _digest_bytes(data) != probe.get(f"{field}Sha256"):
                raise IntegrityError(f"live-route {field} drifted")
            artifacts[field] = data
        compose_path = run_dir / str(probe["compose"])
        expected_config = live_route_probe_config(
            run_dir,
            binding,
            provider,
            probe["model"],
            probe["reasoning"],
            compose_path,
            probe["composeSha256"],
        )
        if not _same_json(
            _yaml_bytes(artifacts["config"], "live-route probe config"),
            expected_config,
        ) or not _same_json(
            _yaml_bytes(artifacts["compose"], "live-route probe compose"),
            _pinned_overlay(
                _repo_root(),
                provider,
                production_image,
                live_route_probe=True,
            ),
        ):
            raise IntegrityError("live-route probe policy drifted")
        validated.append(probe)
    if observed_providers != set(_PROVIDERS):
        raise IntegrityError("live-route probe providers drifted")
    return validated


def _validate_record(
    run_dir: Path,
    manifest_sha: str,
    production_build_id: str,
    runtime_spec: object,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    record = _mapping(
        _artifact_json(run_dir, "run-record.json", "run record"), "run record"
    )
    if (
        set(record)
        != {
            "schemaVersion",
            "preflight",
            "preflightSha256",
            "relayImages",
            "relayImageTags",
            "taskSnapshots",
            "codexRuntime",
            "providers",
            "liveRouteProbes",
        }
        or not is_strict_int(record.get("schemaVersion"))
        or record["schemaVersion"] != 1
    ):
        raise IntegrityError("run record schema drifted")
    preflight = _mapping(record["preflight"], "preflight")
    if set(preflight) != PREFLIGHT_KEYS:
        raise IntegrityError("preflight schema drifted")
    relay_images = _mapping(record.get("relayImages"), "relayImages")
    relay_tags = _mapping(record.get("relayImageTags"), "relayImageTags")
    task_snapshots = _mapping(record.get("taskSnapshots"), "taskSnapshots")
    runtime_receipt = _mapping(record.get("codexRuntime"), "codexRuntime")
    if (
        not is_strict_int(preflight.get("schemaVersion"))
        or preflight["schemaVersion"] != 1
        or preflight.get("experimentId") != EXPERIMENT_ID
        or preflight.get("cleanTree") is not True
        or preflight.get("experimentManifestSha256") != manifest_sha
        or preflight.get("relayBuildSha256") != production_build_id
        or set(relay_images) != set(_RELAY_IMAGES)
        or any(not _SHA256.fullmatch(str(value)) for value in relay_images.values())
        or len(set(relay_images.values())) != len(relay_images)
        or preflight.get("relayImageSha256") != relay_images.get("production")
        or preflight.get("taskSnapshotsSha256") != _digest(task_snapshots)
        or not is_revision(preflight.get("sourceRevision"))
        or relay_tags
        != _relay_image_tags(run_dir.resolve(), str(preflight.get("sourceRevision")))
        or _digest(preflight) != record.get("preflightSha256")
    ):
        raise IntegrityError("preflight binding is invalid")
    _validate_task_snapshots(run_dir, task_snapshots)
    try:
        observed_runtime = verify_tree(
            run_dir / CODEX_RUNTIME_PREPARED_RELATIVE, runtime_spec
        )
    except (OSError, TypeError, ValueError) as error:
        raise IntegrityError("prepared Codex runtime drifted") from error
    if not _same_json(runtime_receipt, observed_runtime):
        raise IntegrityError("prepared Codex runtime receipt drifted")
    _iso(preflight.get("createdAt"), "preflight.createdAt")
    probes = _validate_live_route_probe_records(
        run_dir,
        _sequence(record.get("liveRouteProbes"), "liveRouteProbes"),
        preflight,
        str(record["preflightSha256"]),
        relay_images["production"],
    )
    return preflight, _sequence(record.get("providers"), "providers"), probes


def _expected_binding(preflight: dict[str, Any], preflight_sha: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "replication_id": preflight["replicationId"],
        "source_revision": preflight["sourceRevision"],
        "experiment_manifest_sha256": preflight["experimentManifestSha256"],
        "relay_build_sha256": preflight["relayBuildSha256"],
        "relay_image_sha256": preflight["relayImageSha256"],
        "task_snapshots_sha256": preflight["taskSnapshotsSha256"],
        "preflight_sha256": preflight_sha,
    }


def _relay_usage(records: list[dict[str, Any]], model: str) -> dict[str, int | None]:
    closed = [
        item for item in records if item.get("event") == "transport.responses.closed"
    ]
    if not 1 <= len(closed) <= _RELAY_REQUEST_CAP:
        raise IntegrityError("relay request count is outside the frozen limit")
    required = ("input_tokens", "output_tokens", "total_tokens")
    optional = ("cached_input_tokens", "reasoning_output_tokens")
    totals: dict[str, int | None] = {key: 0 for key in (*required, *optional)}
    for record in closed:
        usage = _mapping(record.get("usage"), "relay usage")
        if not set(required) <= set(usage) <= set(totals):
            raise IntegrityError("relay usage has missing required or unknown fields")
        for key in required:
            total = totals[key]
            assert isinstance(total, int)
            totals[key] = total + _integer(usage[key], f"usage.{key}")
        for key in optional:
            if key not in usage:
                totals[key] = None
            elif totals[key] is not None:
                totals[key] += _integer(usage[key], f"usage.{key}")
        if (
            usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
            or (
                "cached_input_tokens" in usage
                and usage["cached_input_tokens"] > usage["input_tokens"]
            )
            or (
                "reasoning_output_tokens" in usage
                and usage["reasoning_output_tokens"] > usage["output_tokens"]
            )
            or record.get("returnedModel") != model
            or not record.get("providerRequestId")
            or record.get("terminalEvent") != "response.completed"
        ):
            raise IntegrityError("provider response identity is incomplete")
    if (
        totals["total_tokens"] != totals["input_tokens"] + totals["output_tokens"]
        or any(
            value is not None and value > _MAX_SAFE_INTEGER for value in totals.values()
        )
        or (
            totals["cached_input_tokens"] is not None
            and totals["cached_input_tokens"] > totals["input_tokens"]
        )
        or (
            totals["reasoning_output_tokens"] is not None
            and totals["reasoning_output_tokens"] > totals["output_tokens"]
        )
    ):
        raise IntegrityError("relay usage arithmetic is invalid")
    return totals


def _validate_artifact_manifest(trial_dir: Path) -> None:
    entries = [
        _mapping(item, "artifact manifest entry")
        for item in _sequence(
            _artifact_json(trial_dir, "artifacts/manifest.json", "artifact manifest"),
            "artifact manifest",
        )
    ]
    expected = artifact_manifest()
    if Counter(_canonical(entry) for entry in entries) != Counter(
        _canonical(entry) for entry in expected
    ):
        raise IntegrityError("relay artifact collection did not complete cleanly")


def _read_relay_evidence(
    trial_dir: Path,
) -> tuple[Path, Path, list[dict[str, Any]], dict[str, Any]]:
    _validate_artifact_manifest(trial_dir)
    evidence = trial_dir / "artifacts" / "provider-evidence"
    journal = evidence / Path(RELAY_JOURNAL_PATH).name
    seal = evidence / Path(RELAY_SEAL_PATH).name
    try:
        journal_bytes = _artifact_bytes(
            trial_dir,
            f"artifacts/provider-evidence/{journal.name}",
            max_bytes=_RELAY_JOURNAL_CAP,
        )
        seal_bytes = _artifact_bytes(
            trial_dir,
            f"artifacts/provider-evidence/{seal.name}",
            max_bytes=_RELAY_SEAL_CAP,
        )
        if (
            _artifact_bytes(
                trial_dir,
                f"artifacts/{journal.name}",
                max_bytes=_RELAY_JOURNAL_CAP,
            )
            != journal_bytes
            or _artifact_bytes(
                trial_dir, f"artifacts/{seal.name}", max_bytes=_RELAY_SEAL_CAP
            )
            != seal_bytes
        ):
            raise IntegrityError("retained relay evidence copies differ")
        journal_text, seal_text = journal_bytes.decode(), seal_bytes.decode()
    except UnicodeDecodeError as error:
        raise IntegrityError("relay evidence must be UTF-8") from error
    except OSError as error:
        raise IntegrityError(
            "both retained relay evidence copies are required"
        ) from error
    records = []
    for index, line in enumerate(journal_text.splitlines(), 1):
        record = _mapping(_loads(line, f"relay line {index}"), "relay record")
        if set(record) != _RELAY_FIELDS.get(record.get("event")):
            raise IntegrityError("relay evidence contains a non-redacted field")
        records.append(record)
    marker = _mapping(_loads(seal_text, "relay seal"), "relay seal")
    if set(marker) != _SEAL_FIELDS:
        raise IntegrityError("relay seal contains an unknown field")
    return journal, seal, records, marker


def _validate_relay_timing(
    records: list[dict[str, Any]],
    marker: dict[str, Any],
    started: datetime,
    finished: datetime,
) -> None:
    record_times = [_iso(record.get("at"), "relay event time") for record in records]
    sealed_at = _iso(marker.get("sealedAt"), "relay sealedAt")
    request_ids = [
        record.get("relayRequestId")
        for record in records
        if record.get("event") == "transport.responses.request"
    ]
    identities = (marker.get("runId"), marker.get("relayInstanceId"))
    invalid = (
        record_times != sorted(record_times)
        or any(value < started or value > finished for value in record_times)
        or not started <= sealed_at <= finished
        or (record_times and sealed_at < record_times[-1])
        or any(not isinstance(value, str) or not value for value in identities)
        or any(not isinstance(value, str) or not value for value in request_ids)
        or len(set(request_ids)) != len(request_ids)
    )
    if invalid:
        raise IntegrityError("relay evidence identity or timing is invalid")


def _relay_gate_is_complete(gate: Any, allow_incomplete: bool) -> bool:
    if gate == {"ok": True, "reasons": []}:
        return True
    reasons = set(_mapping(gate, "relay publication gate").get("reasons", []))
    if (
        not allow_incomplete
        or not reasons
        or not reasons <= _SCORABLE_INCOMPLETE_RELAY_REASONS
    ):
        raise IntegrityError("relay publication gate failed")
    return False


def _validate_relay(
    trial_dir: Path,
    provider: str,
    model: str,
    expected_build_id: str,
    started: datetime,
    finished: datetime,
    allow_incomplete: bool,
) -> tuple[dict[str, Any], dict[str, int | None] | None]:
    journal, seal, records, marker = _read_relay_evidence(trial_dir)
    try:
        verified = relay_metadata(journal, seal, allow_empty=allow_incomplete)
    except (OSError, TypeError, ValueError) as error:
        raise IntegrityError(f"relay evidence failed validation: {error}") from error
    if len(verified["records"]) // 3 > _RELAY_REQUEST_CAP:
        raise IntegrityError("relay request count is outside the frozen limit")
    complete = _relay_gate_is_complete(
        verified.get("publication_gate"), allow_incomplete
    )
    if (
        verified["records"] != records
        or verified["seal"] != marker
        or marker.get("providerId") != provider
        or marker.get("expectedModel") != model
        or marker.get("buildId") != expected_build_id
    ):
        raise IntegrityError("relay identity or derived evidence drifted")
    _validate_relay_timing(records, marker, started, finished)
    totals = _relay_usage(records, model) if complete else None
    return verified, totals


def _nullable_zero(value: Any, expected: int, label: str) -> None:
    if expected == 0 and value is None:
        return
    if _integer(value, label) != expected:
        raise IntegrityError(f"{label} disagrees with relay usage")


def _trajectory_counts(steps: list[Any]) -> tuple[int, int]:
    tool_calls = 0
    for index, step in enumerate(steps, 1):
        item = _mapping(step, "trajectory step")
        if _integer(item.get("step_id"), "trajectory step_id") != index:
            raise IntegrityError("ATIF step IDs are not sequential")
        calls = item.get("tool_calls")
        if calls is not None:
            tool_calls += len(_sequence(calls, "tool_calls"))
    return tool_calls, len(steps)


def _validate_trajectory_metrics(
    metrics: dict[str, Any], totals: dict[str, int | None], steps: int
) -> None:
    if _integer(metrics.get("total_steps"), "ATIF total_steps") != steps:
        raise IntegrityError("ATIF total_steps drifted")
    input_tokens = totals["input_tokens"]
    output_tokens = totals["output_tokens"]
    total_tokens = totals["total_tokens"]
    assert all(
        isinstance(value, int) for value in (input_tokens, output_tokens, total_tokens)
    )
    _nullable_zero(metrics.get("total_prompt_tokens"), input_tokens, "ATIF input")
    _nullable_zero(metrics.get("total_completion_tokens"), output_tokens, "ATIF output")
    extra = _mapping(metrics.get("extra"), "trajectory.final_metrics.extra")
    _nullable_zero(extra.get("total_tokens"), total_tokens, "ATIF total")
    cached = totals["cached_input_tokens"]
    reasoning = totals["reasoning_output_tokens"]
    if cached is not None:
        _nullable_zero(metrics.get("total_cached_tokens"), cached, "ATIF cache")
    if reasoning is not None:
        _nullable_zero(
            extra.get("reasoning_output_tokens"), reasoning, "ATIF reasoning"
        )


def _validate_trajectory(
    trial_dir: Path,
    model: str,
    binding: dict[str, Any],
    totals: dict[str, int | None] | None,
    allow_incomplete: bool,
) -> tuple[int | None, int | None, bool]:
    path = trial_dir / "agent" / "trajectory.json"
    if not path.is_file():
        if (
            allow_incomplete
            and not path.exists()
            and not path.is_symlink()
            and binding.get("trajectory_session_id") is None
        ):
            return None, None, False
        raise IntegrityError("ATIF trajectory is unavailable")
    trajectory = _mapping(
        _artifact_json(
            trial_dir,
            "agent/trajectory.json",
            "trajectory",
            max_bytes=_TRAJECTORY_CAP,
        ),
        "trajectory",
    )
    try:
        Trajectory.model_validate_json(_canonical(trajectory))
    except ValueError as error:
        raise IntegrityError(f"Harbor ATIF trajectory is invalid: {error}") from error
    agent = _mapping(trajectory.get("agent"), "trajectory.agent")
    steps = _sequence(trajectory.get("steps"), "trajectory.steps")
    if (
        trajectory.get("schema_version") != "ATIF-v1.7"
        or not isinstance(trajectory.get("session_id"), str)
        or not trajectory["session_id"]
        or trajectory["session_id"] != binding.get("trajectory_session_id")
        or agent.get("name") != "codex"
        or agent.get("version") != _CODEX_VERSION
        or agent.get("model_name") != model
    ):
        raise IntegrityError("ATIF identity is invalid")
    tool_calls, step_count = _trajectory_counts(steps)
    raw_metrics = trajectory.get("final_metrics")
    if raw_metrics is None and allow_incomplete:
        return tool_calls, step_count, False
    metrics = _mapping(raw_metrics, "trajectory.final_metrics")
    if totals is None and (
        _integer(metrics.get("total_steps"), "ATIF total_steps") != step_count
    ):
        raise IntegrityError("ATIF total_steps drifted")
    if totals is not None:
        _validate_trajectory_metrics(metrics, totals, step_count)
    return tool_calls, step_count, True


def _validate_variant(
    agent: dict[str, Any], provider: str, model: str, binding: dict[str, Any]
) -> str:
    variant = _variant_from_agent(agent)
    kwargs = _mapping(agent.get("kwargs"), "agent kwargs")
    if (
        agent.get("model_name") != f"{provider}/{model}"
        or kwargs.get("version") != _CODEX_VERSION
        or kwargs.get("reasoning_effort") != _PROVIDERS[provider]["reasoning"]
        or kwargs.get("run_binding") != binding
    ):
        raise IntegrityError("locked agent configuration drifted")
    return variant


def _expected_trial_lock(
    provider: str,
    model: str,
    task: str,
    variant: str,
    binding: dict[str, Any],
    task_path: Path,
    compose_path: Path,
    compose_sha256: str,
    *,
    install_only: bool = False,
) -> dict[str, Any]:
    compose = str(compose_path)
    spec = _VARIANTS[variant]
    runtime_root = task_path.parent.parent / CODEX_RUNTIME_PREPARED_RELATIVE
    return {
        "schema_version": 2,
        "task": {
            "name": task.removeprefix("terminal-bench/"),
            "type": "local",
            "digest": _TASK_DIGESTS[task],
            "source": _DATASET,
            "path": str(task_path),
        },
        "install_only": install_only,
        "timeout_multiplier": 1.0,
        "agent": {
            "name": spec["name"],
            "import_path": spec["import_path"],
            "model_name": f"{provider}/{model}",
            "skills": [],
            "resume_trajectory": False,
            "extra_allowed_hosts": [],
            "kwargs": {
                "version": _CODEX_VERSION,
                "reasoning_effort": _PROVIDERS[provider]["reasoning"],
                "enable_verify_instruction_v1": spec["enabled"],
                "run_binding": binding,
            },
            "env": {
                "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                PILOT_RECEIPT_ENV: str(
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
            "mounts": [_codex_runtime_mount(runtime_root)],
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
        "verifier": {"disable": install_only, "environment_mode": "shared"},
    }


def _harbor_trial_result(value: dict[str, Any]) -> TrialResult:
    try:
        result = TrialResult.model_validate_json(_canonical(value))
    except ValueError as error:
        raise IntegrityError(f"Harbor TrialResult is invalid: {error}") from error
    normalized = json.loads(result.model_dump_json())
    if not _same_json(value, normalized):
        raise IntegrityError(
            "Harbor TrialResult serialization is incomplete or drifted"
        )
    return result


def _expected_result_config(
    trial_dir: Path,
    task: str,
    variant: str,
    job_config: JobConfig,
    job_id: UUID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    agents = [
        agent for agent in job_config.agents if agent.name == _VARIANTS[variant]["name"]
    ]
    if len(agents) != 1:
        raise IntegrityError("job does not contain exactly one expected variant")
    task_configs = [
        configured
        for configured in job_config.tasks
        if configured.path is not None
        and configured.path.name == task.removeprefix("terminal-bench/")
        and configured.source == _DATASET
    ]
    if len(task_configs) != 1:
        raise IntegrityError("job does not contain exactly one prepared task")
    task_config = task_configs[0]
    config = TrialConfig(
        task=task_config,
        trial_name=trial_dir.name,
        trials_dir=job_config.jobs_dir / job_config.job_name,
        install_only=job_config.install_only,
        timeout_multiplier=job_config.timeout_multiplier,
        agent_timeout_multiplier=job_config.agent_timeout_multiplier,
        verifier_timeout_multiplier=job_config.verifier_timeout_multiplier,
        agent_setup_timeout_multiplier=job_config.agent_setup_timeout_multiplier,
        environment_build_timeout_multiplier=(
            job_config.environment_build_timeout_multiplier
        ),
        agent=agents[0],
        user_agent=job_config.user_agent,
        environment=job_config.environment,
        verifier=job_config.verifier,
        artifacts=job_config.artifacts,
        extra_instruction_paths=job_config.extra_instruction_paths,
        extra_instructions=job_config.extra_instructions,
        job_id=job_id,
    )
    return (
        json.loads(config.model_dump_json()),
        json.loads(task_config.get_task_id().model_dump_json()),
    )


def _validate_trial_provenance(
    trial_dir: Path,
    result: dict[str, Any],
    task: str,
    variant: str,
    job_config: JobConfig,
    job_id: UUID,
) -> None:
    expected_config, expected_task_id = _expected_result_config(
        trial_dir, task, variant, job_config, job_id
    )
    if (
        result.get("trial_uri") != trial_dir.resolve().as_uri()
        or not _same_json(result.get("task_id"), expected_task_id)
        or result.get("source") != _DATASET
        or result.get("verifier_environment_mode") != "shared"
        or not _same_json(result.get("config"), expected_config)
    ):
        raise IntegrityError("trial provenance or full result config drifted")


def _trial_identity(
    trial_dir: Path,
    result: dict[str, Any],
    lock: dict[str, Any],
    provider: str,
    model: str,
    binding: dict[str, Any],
    selected_tasks: set[str],
    job_config: JobConfig,
    job_id: UUID,
    compose_path: Path,
    compose_sha256: str,
) -> tuple[str, str, str, dict[str, Any], str]:
    trial_id = str(result.get("id"))
    trial_name = result.get("trial_name")
    try:
        UUID(trial_id)
    except ValueError as error:
        raise IntegrityError("trial result ID is invalid") from error
    if not isinstance(trial_name, str) or trial_name != trial_dir.name:
        raise IntegrityError("trial directory and result name disagree")
    config = _mapping(result.get("config"), "trial result config")
    variant = _validate_variant(
        _mapping(lock.get("agent"), "trial lock agent"), provider, model, binding
    )
    if (
        _validate_variant(
            _mapping(config.get("agent"), "trial result agent"),
            provider,
            model,
            binding,
        )
        != variant
    ):
        raise IntegrityError("result and lock variants disagree")
    task_name = result.get("task_name")
    matching_tasks = [task for task in selected_tasks if task == task_name]
    task = matching_tasks[0] if len(matching_tasks) == 1 else None
    task_lock = _mapping(lock.get("task"), "trial task lock")
    if (
        task is None
        or result.get("task_checksum") != _TASK_RUNTIME_BINDINGS[task]["taskChecksum"]
    ):
        raise IntegrityError("trial task identity drifted")
    task_config = [
        configured
        for configured in job_config.tasks
        if configured.path is not None
        and configured.path.name == task.removeprefix("terminal-bench/")
    ]
    if len(task_config) != 1 or task_config[0].path is None:
        raise IntegrityError("prepared task configuration drifted")
    if not _same_json(
        lock,
        _expected_trial_lock(
            provider,
            model,
            task,
            variant,
            binding,
            task_config[0].path,
            compose_path,
            compose_sha256,
        ),
    ):
        raise IntegrityError("executed trial lock differs from the frozen policy")
    _validate_trial_provenance(trial_dir, result, task, variant, job_config, job_id)
    agent_info = _mapping(result.get("agent_info"), "agent_info")
    expected = _VARIANTS[variant]
    if (
        agent_info.get("name") != expected["name"]
        or agent_info.get("version") != _CODEX_VERSION
        or not _same_json(
            _mapping(agent_info.get("model_info"), "agent_info.model_info"),
            {"name": model, "provider": provider},
        )
    ):
        raise IntegrityError("runtime agent identity drifted")
    return trial_id, trial_name, task, task_lock, variant


def _exception_info(value: Any, label: str) -> tuple[str, datetime] | None:
    if value is None:
        return None
    info = _mapping(value, label)
    exception_type = info.get("exception_type")
    if (
        set(info)
        != {
            "exception_type",
            "exception_message",
            "exception_traceback",
            "occurred_at",
        }
        or not isinstance(exception_type, str)
        or not exception_type
        or not isinstance(info.get("exception_message"), str)
        or not isinstance(info.get("exception_traceback"), str)
    ):
        raise IntegrityError(f"{label} is not a complete Harbor ExceptionInfo")
    return exception_type, _iso(info.get("occurred_at"), f"{label}.occurred_at")


def _trial_outcome(
    result: dict[str, Any],
) -> tuple[float, tuple[str, datetime] | None, str | None]:
    raw_steps = result.get("step_results")
    steps = [] if raw_steps is None else _sequence(raw_steps, "step_results")
    if steps:
        raise IntegrityError("the frozen SingleStepTrial policy forbids step_results")
    top_level = _exception_info(result.get("exception_info"), "exception_info")
    try:
        failure_class = (
            None if top_level is None else classify_failure(top_level[0]).value
        )
    except ValueError as error:
        raise IntegrityError(
            "officially scored exception type is not Harbor-native"
        ) from error
    verifier = _mapping(result.get("verifier_result"), "verifier_result")
    reward = _number(
        _mapping(verifier.get("rewards"), "rewards").get("reward"), "official reward"
    )
    if not 0 <= reward <= 1:
        raise IntegrityError("official reward must be between zero and one")
    return reward, top_level, failure_class


def _phase_timing(result: dict[str, Any], name: str) -> tuple[datetime, datetime]:
    phase = _mapping(result.get(name), name)
    if set(phase) != {"started_at", "finished_at"}:
        raise IntegrityError(f"{name} timing schema drifted")
    return (
        _iso(phase.get("started_at"), f"{name}.started_at"),
        _iso(phase.get("finished_at"), f"{name}.finished_at"),
    )


def _trial_timing(
    result: dict[str, Any],
) -> tuple[datetime, datetime, float, datetime, datetime, datetime, datetime]:
    started = _iso(result.get("started_at"), "started_at")
    finished = _iso(result.get("finished_at"), "finished_at")
    wall = (finished - started).total_seconds()
    if wall <= 0:
        raise IntegrityError("trial wall time must be positive")
    phases = [
        _phase_timing(result, name)
        for name in ("environment_setup", "agent_setup", "agent_execution", "verifier")
    ]
    timeline = [started, *(point for phase in phases for point in phase), finished]
    if any(left > right for left, right in pairwise(timeline)):
        raise IntegrityError("Harbor trial phase timing is invalid")
    agent_started, agent_finished = phases[2]
    if agent_finished <= agent_started:
        raise IntegrityError("agent execution time must be positive")
    verifier_started, verifier_finished = phases[3]
    return (
        started,
        finished,
        wall,
        agent_started,
        agent_finished,
        verifier_started,
        verifier_finished,
    )


def _validate_cleanup_receipt(
    trial_dir: Path,
    binding: dict[str, Any],
    task: str,
    provider_credential_sha256: str,
    verifier_finished: datetime,
    trial_finished: datetime,
) -> None:
    receipt = _mapping(
        _artifact_json(
            trial_dir, "environment-cleanup.json", "environment cleanup receipt"
        ),
        "environment cleanup receipt",
    )
    stopped_at = _iso(receipt.get("stoppedAt"), "cleanup.stoppedAt")
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
        or receipt["schemaVersion"] != 1
        or receipt.get("experimentId") != EXPERIMENT_ID
        or receipt.get("replicationId") != binding["replication_id"]
        or receipt.get("sourceRevision") != binding["source_revision"]
        or receipt.get("experimentManifestSha256")
        != binding["experiment_manifest_sha256"]
        or receipt.get("preflightSha256") != binding["preflight_sha256"]
        or receipt.get("runBindingSha256") != _digest(binding)
        or receipt.get("relayImageSha256") != binding["relay_image_sha256"]
        or receipt.get("providerCredentialSha256") != provider_credential_sha256
        or not _SHA256.fullmatch(str(receipt.get("fullComposeSha256", "")))
        or receipt.get("taskId") != task
        or receipt.get("taskDigest") != _TASK_RUNTIME_BINDINGS[task]["taskDigest"]
        or receipt.get("taskChecksum") != _TASK_RUNTIME_BINDINGS[task]["taskChecksum"]
        or receipt.get("sessionId") != f"{trial_dir.name}__env"
        or receipt.get("projectName")
        != _sanitize_docker_compose_project_name(f"{trial_dir.name}__env")
        or not verifier_finished <= stopped_at <= trial_finished
    ):
        raise IntegrityError("environment cleanup receipt drifted")


def _provider_binding(
    provider_data: dict[str, Any],
    verified: dict[str, Any],
    expected_variant: dict[str, Any],
    expected_fields: dict[str, Any],
    allow_incomplete: bool,
) -> dict[str, Any]:
    if (
        set(provider_data) != _PROVIDER_METADATA_FIELDS
        or not is_strict_int(provider_data.get("schema_version"))
        or provider_data["schema_version"] != 1
    ):
        raise IntegrityError("embedded provider metadata schema drifted")
    for key in ("event_count", "chain_head", "seal", "records"):
        if not _same_json(provider_data.get(key), verified.get(key)):
            raise IntegrityError(f"embedded relay {key} drifted")
    if not _same_json(provider_data.get("agent_variant"), expected_variant):
        raise IntegrityError("embedded agent variant drifted")
    harbor_binding = _mapping(provider_data.get("harbor_binding"), "harbor_binding")
    if set(harbor_binding) != set(expected_fields) | {
        "trajectory_session_id",
        "binding_sha256",
    }:
        raise IntegrityError("Harbor binding is invalid")
    unhashed = {
        key: value for key, value in harbor_binding.items() if key != "binding_sha256"
    }
    trajectory_session = unhashed.get("trajectory_session_id")
    trajectory_identity_ok = (
        isinstance(trajectory_session, str) and bool(trajectory_session)
    ) or (allow_incomplete and trajectory_session is None)
    reasons = list(verified["publication_gate"]["reasons"])
    if trajectory_session is None:
        reasons.append("trajectory_session_missing")
    expected_gate = {"ok": not reasons, "reasons": sorted(set(reasons))}
    if (
        not _same_json(provider_data.get("publication_gate"), expected_gate)
        or (not expected_gate["ok"] and not allow_incomplete)
        or harbor_binding.get("binding_sha256") != _digest(unhashed)
        or any(
            not _same_json(unhashed.get(key), value)
            for key, value in expected_fields.items()
        )
        or not trajectory_identity_ok
    ):
        raise IntegrityError("Harbor binding is invalid")
    return harbor_binding


def _validate_agent_totals(
    agent_result: dict[str, Any],
    totals: dict[str, int | None] | None,
    allow_incomplete: bool,
) -> bool:
    if totals is None:
        return True
    complete = True
    for key, expected in (
        ("n_input_tokens", totals["input_tokens"]),
        ("n_output_tokens", totals["output_tokens"]),
    ):
        assert isinstance(expected, int)
        value = agent_result.get(key)
        if value is None and allow_incomplete:
            complete = False
        elif _integer(value, f"agent_result.{key}") != expected:
            raise IntegrityError(f"agent_result.{key} disagrees with relay usage")
    cached = totals["cached_input_tokens"]
    if cached is not None:
        value = agent_result.get("n_cache_tokens")
        if value is None and allow_incomplete:
            complete = False
        elif _integer(value, "agent_result.n_cache_tokens") != cached:
            raise IntegrityError(
                "agent_result.n_cache_tokens disagrees with relay usage"
            )
    return complete


def _telemetry_gaps(
    totals: dict[str, int | None] | None,
    cost_usd: float | None,
    tool_calls: int | None,
    agent_wall: float | None,
    agent_tokens_complete: bool,
    trajectory_metrics_complete: bool,
) -> list[str]:
    gaps = []
    if totals is None:
        gaps.append("provider_usage")
    else:
        gaps.extend(
            key
            for key in ("cached_input_tokens", "reasoning_output_tokens")
            if totals[key] is None
        )
    if cost_usd is None:
        gaps.append("cost_usd")
    if tool_calls is None:
        gaps.append("trajectory")
    elif not trajectory_metrics_complete:
        gaps.append("atif_final_metrics")
    if agent_wall is None:
        gaps.append("agent_timing")
    if not agent_tokens_complete:
        gaps.append("agent_token_crosscheck")
    return gaps


def _job_agent_totals(agent_result: dict[str, Any]) -> dict[str, int | float | None]:
    totals: dict[str, int | float | None] = {}
    for field in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
        value = agent_result.get(field)
        totals[field] = (
            None if value is None else _integer(value, f"agent_result.{field}")
        )
    cost = agent_result.get("cost_usd")
    if cost is None:
        totals["cost_usd"] = None
    else:
        normalized_cost = _number(cost, "agent_result.cost_usd")
        if normalized_cost < 0:
            raise IntegrityError("agent_result.cost_usd must be non-negative")
        totals["cost_usd"] = normalized_cost
    return totals


def _probe_receipt_payloads(
    receipt: dict[str, Any], pilot: dict[str, Any]
) -> tuple[datetime, datetime]:
    usage = _mapping(receipt.get("usage"), "probe receipt usage")
    scan = _mapping(receipt.get("credentialLeakScan"), "credential leak scan")
    spend = _mapping(receipt.get("spendCap"), "probe spend cap")
    pilot_job = _mapping(receipt.get("pilotJob"), "probe pilot job")
    input_tokens = _integer(usage.get("input_tokens"), "probe input_tokens")
    output_tokens = _integer(usage.get("output_tokens"), "probe output_tokens")
    total_tokens = _integer(usage.get("total_tokens"), "probe total_tokens")
    cached = usage.get("cached_input_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    cached = None if cached is None else _integer(cached, "probe cached_input_tokens")
    reasoning = (
        None
        if reasoning is None
        else _integer(reasoning, "probe reasoning_output_tokens")
    )
    scan_counts = [
        _integer(scan.get(field), f"credentialLeakScan.{field}")
        for field in ("files", "bytes", "directories")
    ]
    limit = _number(spend.get("limitUsd"), "probe spend cap limit")
    expected_pilot = {
        key: pilot[key]
        for key in (
            "armOrder",
            "config",
            "configSha256",
            "jobDir",
            "compose",
            "composeSha256",
        )
    }
    if (
        set(usage)
        != {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        }
        or total_tokens != input_tokens + output_tokens
        or (cached is not None and cached > input_tokens)
        or (reasoning is not None and reasoning > output_tokens)
        or set(scan) != {"ok", "files", "bytes", "directories"}
        or scan.get("ok") is not True
        or any(value > _MAX_SAFE_INTEGER for value in scan_counts)
        or set(spend)
        != {"limitUsd", "observedAt", "expiresAt", "evidenceSha256", "assertedBy"}
        or not 0 < limit <= 2
        or not is_digest(spend.get("evidenceSha256"))
        or not isinstance(spend.get("assertedBy"), str)
        or not spend["assertedBy"].strip()
        or not _same_json(pilot_job, expected_pilot)
    ):
        raise IntegrityError("pilot authorization receipt payload drifted")
    observed = _iso(spend.get("observedAt"), "probe spend cap observedAt")
    expires = _iso(spend.get("expiresAt"), "probe spend cap expiresAt")
    if not timedelta(0) < expires - observed <= timedelta(hours=24):
        raise IntegrityError("pilot authorization receipt window drifted")
    return observed, expires


def _validated_pilot_receipt(
    receipt: dict[str, Any],
    provider: str,
    model: str,
    binding: dict[str, Any],
    probe: dict[str, Any],
    pilot: dict[str, Any],
    not_before: datetime,
    claimed_at: datetime,
    pilot_started: datetime,
    pilot_finished: datetime,
) -> tuple[str, datetime]:
    observed, expires = _probe_receipt_payloads(receipt, pilot)
    probe_started = _iso(receipt.get("probeStartedAt"), "probe startedAt")
    probe_finished = _iso(receipt.get("probeFinishedAt"), "probe finishedAt")
    verified = _iso(receipt.get("verifiedAt"), "probe verifiedAt")
    credential_sha256 = receipt.get("providerCredentialSha256")
    digest_fields = (
        "configSha256",
        "composeSha256",
        "fullComposeSha256",
        "providerCredentialSha256",
        "probeClaimSha256",
        "relayMarkerSha256",
        "relayChainHead",
        "providerRequestIdsSha256",
        "responseIdsSha256",
    )
    if (
        set(receipt) != _PROBE_RECEIPT_FIELDS
        or not is_strict_int(receipt.get("schemaVersion"))
        or receipt.get("schemaVersion") != 1
        or receipt.get("proofClass") != "live-route-probe-v1"
        or receipt.get("provider") != provider
        or receipt.get("model") != model
        or receipt.get("sourceRevision") != binding["source_revision"]
        or receipt.get("preflightSha256") != binding["preflight_sha256"]
        or receipt.get("runBindingSha256") != _digest(binding)
        or receipt.get("configSha256") != probe["configSha256"]
        or receipt.get("composeSha256") != probe["composeSha256"]
        or any(not is_digest(receipt.get(field)) for field in digest_fields)
        or not is_strict_int(receipt.get("requestCount"))
        or receipt.get("requestCount") != 2
        or receipt.get("authorizationExpiresAt") != receipt["spendCap"]["expiresAt"]
        or receipt.get("liveProviderRouteObserved") is not True
        or receipt.get("liveProviderConformance") is not False
        or receipt.get("benchmarkTaskInstructionUsed") is not False
        or receipt.get("benchmarkRewardUsed") is not False
        or receipt.get("spendCapVerification") != "operator_attested"
        or receipt.get("benchmarkStartAuthorized") is not True
        or not (
            not_before
            <= observed
            <= probe_started
            < probe_finished
            <= verified
            <= claimed_at
            <= pilot_started
            < pilot_finished
            <= expires
        )
    ):
        raise IntegrityError("pilot authorization receipt drifted")
    return str(credential_sha256), probe_started


def _validate_pilot_claim(
    run_dir: Path,
    provider: str,
    job_dir: Path,
    job_id: UUID,
    trial_lock: dict[str, Any],
    model: str,
    binding: dict[str, Any],
    probe: dict[str, Any],
    pilot: dict[str, Any],
    not_before: datetime,
    before: datetime,
    trial_finished: datetime,
) -> tuple[str, datetime]:
    lock_sha256 = _digest(trial_lock)
    authorization = run_dir / "authorizations" / f"{provider}.json"
    claim = (
        run_dir / "authorizations" / relay_claim_name(provider, "pilot", lock_sha256)
    )
    try:
        authorization_info = authorization.lstat()
        claim_info = claim.lstat()
    except OSError as error:
        raise IntegrityError("pilot authorization claim is unavailable") from error
    if any(
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        for info in (authorization_info, claim_info)
    ):
        raise IntegrityError("pilot authorization evidence must be private files")
    authorization_raw = _artifact_bytes(
        run_dir,
        authorization.relative_to(run_dir),
        max_bytes=_JSON_ARTIFACT_CAP,
    )
    claim_raw = _artifact_bytes(
        run_dir, claim.relative_to(run_dir), max_bytes=_JSON_ARTIFACT_CAP
    )
    try:
        authorization_value = _mapping(
            _loads(authorization_raw.decode(), "pilot authorization"),
            "pilot authorization",
        )
        claim_value = _mapping(
            _loads(claim_raw.decode(), "pilot authorization claim"),
            "pilot authorization claim",
        )
    except UnicodeError as error:
        raise IntegrityError("pilot authorization claim must be UTF-8") from error
    claimed_at = _iso(claim_value.get("claimedAt"), "pilot authorization claimedAt")
    expected = {
        "schemaVersion": 1,
        "proofClass": "pilot-relay-slot-claim-v1",
        "provider": provider,
        "policySha256": _digest_bytes(authorization_raw),
        "jobId": str(job_id),
        "jobDir": str(job_dir),
        "trialLockSha256": lock_sha256,
        "claimedAt": claim_value.get("claimedAt"),
    }
    if (
        authorization_raw != canonical_json(authorization_value)
        or set(claim_value) != RELAY_CLAIM_FIELDS
        or claim_raw != canonical_json(claim_value)
        or not is_strict_int(claim_value.get("schemaVersion"))
        or not _same_json(claim_value, expected)
        or not not_before <= claimed_at <= before
    ):
        raise IntegrityError("pilot authorization claim differs from the trial")
    return _validated_pilot_receipt(
        authorization_value,
        provider,
        model,
        binding,
        probe,
        pilot,
        not_before,
        claimed_at,
        before,
        trial_finished,
    )


def _attempt(
    run_dir: Path,
    job_dir: Path,
    trial_dir: Path,
    provider: str,
    model: str,
    probe: dict[str, Any],
    pilot: dict[str, Any],
    preflight: dict[str, Any],
    preflight_sha: str,
    selected_tasks: set[str],
    job_config: JobConfig,
    job_id: UUID,
    compose_path: Path,
    compose_sha256: str,
) -> dict[str, Any]:
    if (
        trial_dir.is_symlink()
        or trial_dir.resolve().parent != trial_dir.parent.resolve()
    ):
        raise IntegrityError("trial directories must be direct, non-symlink children")
    result = _mapping(
        _artifact_json(trial_dir, "result.json", "trial result"), "trial result"
    )
    _harbor_trial_result(result)
    lock = _mapping(_artifact_json(trial_dir, "lock.json", "trial lock"), "trial lock")
    binding_expected = _expected_binding(preflight, preflight_sha)
    trial_id, trial_name, task, task_lock, variant = _trial_identity(
        trial_dir,
        result,
        lock,
        provider,
        model,
        binding_expected,
        selected_tasks,
        job_config,
        job_id,
        compose_path,
        compose_sha256,
    )
    expected = _VARIANTS[variant]
    reward, exception_info, failure_class = _trial_outcome(result)
    top_level_exception = exception_info[0] if exception_info is not None else None
    scored_failure = exception_info is not None
    (
        started,
        finished,
        wall,
        agent_started,
        agent_finished,
        verifier_started,
        verifier_finished,
    ) = _trial_timing(result)
    credential_sha256, probe_started = _validate_pilot_claim(
        run_dir,
        provider,
        job_dir,
        job_id,
        lock,
        model,
        binding_expected,
        probe,
        pilot,
        _iso(preflight["createdAt"], "preflight.createdAt"),
        agent_started,
        finished,
    )
    _validate_cleanup_receipt(
        trial_dir,
        binding_expected,
        task,
        credential_sha256,
        verifier_finished,
        finished,
    )
    agent_wall = (agent_finished - agent_started).total_seconds()
    if exception_info is not None and not (
        agent_finished <= exception_info[1] <= verifier_started
    ):
        raise IntegrityError("exception timestamp is outside Harbor's failure interval")
    agent_result = _mapping(result.get("agent_result"), "agent_result")
    metadata = _mapping(agent_result.get("metadata"), "agent_result.metadata")
    provider_data = _mapping(
        metadata.get("open_agent_lab_provider"), "provider metadata"
    )
    verified, totals = _validate_relay(
        trial_dir,
        provider,
        model,
        binding_expected["relay_build_sha256"],
        agent_started,
        agent_finished,
        scored_failure,
    )
    expected_variant = {
        "schema_version": 1,
        "variant_id": variant,
        "developer_instruction_requested": expected["enabled"],
        "requested_developer_instructions_sha256": expected["instruction_sha256"],
    }
    harbor_binding = _provider_binding(
        provider_data,
        verified,
        expected_variant,
        {
            "schema_version": 1,
            "harbor_context_id": trial_id,
            "harbor_session_id": f"{trial_name}__agent",
            "relay_instance_id": verified["seal"].get("relayInstanceId"),
            "relay_build_id": verified["seal"].get("buildId"),
            "relay_marker_sha256": verified["seal"].get("markerSha256"),
            "codex_runtime_spec_sha256": CODEX_RUNTIME_SPEC_SHA256,
            "provider_id": provider,
            "requested_model": model,
            "variant_id": variant,
            "requested_developer_instructions_sha256": expected["instruction_sha256"],
            "run_binding": binding_expected,
        },
        scored_failure,
    )
    agent_tokens_complete = _validate_agent_totals(agent_result, totals, scored_failure)
    agent_totals = _job_agent_totals(agent_result)
    tool_calls, steps, trajectory_metrics_complete = _validate_trajectory(
        trial_dir, model, harbor_binding, totals, scored_failure
    )
    telemetry_missing = _telemetry_gaps(
        totals,
        agent_totals["cost_usd"],
        tool_calls,
        agent_wall,
        agent_tokens_complete,
        trajectory_metrics_complete,
    )
    return {
        "provider": provider,
        "model": model,
        "replication": preflight["replicationId"],
        "sourceRevision": preflight["sourceRevision"],
        "probeStartedAt": probe_started,
        "task": task,
        "taskDigest": task_lock["digest"],
        "taskChecksum": result["task_checksum"],
        "variant": variant,
        "reward": reward,
        "topLevelException": top_level_exception,
        "failureClass": failure_class,
        "stepExceptions": [],
        "telemetryComplete": not telemetry_missing,
        "telemetryMissing": telemetry_missing,
        "costUsd": agent_totals["cost_usd"],
        "harborAgentTotals": agent_totals,
        "relayPublicationGate": verified["publication_gate"],
        "tokens": totals,
        "providerRequests": len(verified["records"]) // 3,
        "toolCalls": tool_calls,
        "trajectorySteps": steps,
        "wallSeconds": wall,
        "agentWallSeconds": agent_wall,
        "startedAt": started,
        "finishedAt": finished,
        "trialId": trial_id,
        "trialName": trial_name,
        "lock": lock,
        "relayRunId": verified["seal"]["runId"],
        "relayInstanceId": verified["seal"]["relayInstanceId"],
        "relayRequestIds": [
            record["relayRequestId"]
            for record in verified["records"]
            if record["event"] == "transport.responses.request"
        ],
        "providerResponseIdentities": [
            (record["providerRequestId"], record["responseId"])
            for record in verified["records"]
            if record["event"] == "transport.responses.closed"
        ],
        "chainHead": verified["chain_head"],
    }


def _validated_provider_compose(
    run_dir: Path, entry: dict[str, Any], provider: str, image: str
) -> tuple[Path, str]:
    compose_relative = _relative(entry.get("compose"), "prepared compose")
    compose_path = run_dir / compose_relative
    compose_bytes = _artifact_bytes(run_dir, compose_relative)
    compose_sha256 = _digest_bytes(compose_bytes)
    if compose_path.resolve().parent != (
        run_dir / "overlays"
    ).resolve() or compose_sha256 != entry.get("composeSha256"):
        raise IntegrityError("prepared compose drifted")
    compose = _yaml_bytes(compose_bytes, compose_path.name)
    relay_service = _mapping(
        _mapping(compose.get("services"), "compose services").get(
            "open-agent-lab-relay"
        ),
        "relay service",
    )
    if (
        not _same_json(compose, _pinned_overlay(_repo_root(), provider, image))
        or "build" in relay_service
        or relay_service.get("image") != image
        or relay_service.get("pull_policy") != "never"
    ):
        raise IntegrityError("prepared compose is not pinned to the relay image")
    return compose_path, compose_sha256


def _validated_job_dir(
    run_dir: Path,
    entry: dict[str, Any],
    preflight: dict[str, Any],
    preflight_sha: str,
    tasks: list[str],
    order: list[str],
) -> tuple[str, Path, dict[str, Any], Path, str]:
    provider = entry.get("provider")
    expected_entry_keys = {
        "provider",
        "model",
        "armOrder",
        "config",
        "configSha256",
        "jobDir",
        "compose",
        "composeSha256",
        "relayImageSha256",
    }
    if (
        set(entry) != expected_entry_keys
        or not isinstance(provider, str)
        or provider not in _PROVIDERS
        or entry.get("model") != _PROVIDERS[provider]["model"]
        or entry.get("armOrder") != order
        or entry.get("config") != f"configs/{provider}.yaml"
        or entry.get("jobDir")
        != f"jobs/{provider}/open-agent-lab-{preflight['replicationId']}-{provider}"
        or entry.get("compose") != f"overlays/relay.{provider}.compose.yaml"
        or entry.get("relayImageSha256") != preflight["relayImageSha256"]
    ):
        raise IntegrityError("run-record provider identity drifted")
    compose_path, compose_sha256 = _validated_provider_compose(
        run_dir, entry, provider, preflight["relayImageSha256"]
    )
    config_path = run_dir / _relative(entry.get("config"), "generated config")
    configs_dir = run_dir / "configs"
    try:
        config_bytes = _artifact_bytes(
            run_dir, _relative(entry.get("config"), "config")
        )
    except (OSError, IntegrityError) as error:
        raise IntegrityError("generated config is unavailable") from error
    if (
        configs_dir.is_symlink()
        or config_path.resolve().parent != configs_dir.resolve()
        or not config_path.resolve().is_relative_to(run_dir)
        or _digest_bytes(config_bytes) != entry.get("configSha256")
    ):
        raise IntegrityError("generated config drifted")
    config = _yaml_bytes(config_bytes, config_path.name)
    task_root = run_dir / "tasks"
    by_variant = _validate_template(config, provider, entry["model"], tasks, task_root)
    if [_variant_from_agent(agent) for agent in config["agents"]] != order:
        raise IntegrityError("generated config arm order drifted")
    binding = _expected_binding(preflight, preflight_sha)
    if any(
        agent["kwargs"].get("run_binding") != binding for agent in by_variant.values()
    ):
        raise IntegrityError("generated config lacks the preflight binding")
    job_relative = _relative(entry.get("jobDir"), "job directory")
    job_dir = run_dir / job_relative
    if job_dir.is_symlink() or not job_dir.resolve().is_relative_to(run_dir):
        raise IntegrityError("job directory escapes the prepared run")
    expected_jobs_dir = job_dir.parent
    configured_jobs_dir = config.get("jobs_dir")
    if (
        not isinstance(configured_jobs_dir, str)
        or Path(configured_jobs_dir).resolve() != expected_jobs_dir.resolve()
        or config.get("job_name") != job_dir.name
    ):
        raise IntegrityError("generated config job target drifted")
    expected_config = _load_yaml(_repo_root() / _TEMPLATES[provider])
    expected_agents = _validate_template(
        expected_config, provider, entry["model"], tasks
    )
    expected_config = _bound_config(
        expected_config,
        expected_agents,
        order,
        binding,
        task_root,
        job_dir.name,
        expected_jobs_dir,
        compose_path,
        compose_sha256,
        run_dir / CODEX_RUNTIME_PREPARED_RELATIVE,
        run_dir / "authorizations" / f"{provider}.json",
    )
    if not _same_json(config, expected_config):
        raise IntegrityError("generated config differs from its frozen template")
    return provider, job_dir, config, compose_path, compose_sha256


def _ordered_job_lock(value: dict[str, Any]) -> dict[str, Any]:
    ordered = json.loads(_canonical(value))
    retry = _mapping(ordered.get("retry"), "job lock retry")
    for key in ("include_exceptions", "exclude_exceptions"):
        if isinstance(retry.get(key), list):
            retry[key] = sorted(retry[key])
    return ordered


def _harbor_job_lock(value: dict[str, Any]) -> JobLock:
    try:
        parsed = JobLock.model_validate_json(_canonical(value))
    except ValueError as error:
        raise IntegrityError(f"Harbor JobLock is invalid: {error}") from error
    normalized = json.loads(parsed.model_dump_json(exclude_none=True))
    if not _same_json(_ordered_job_lock(value), _ordered_job_lock(normalized)):
        raise IntegrityError("Harbor JobLock serialization is incomplete or drifted")
    if (
        parsed.schema_version != 3
        or parsed.n_concurrent_trials != 1
        or parsed.retry.max_retries != 0
        or parsed.harbor.model_dump(exclude_none=True)
        != {"version": _HARBOR_VERSION, "is_editable": False}
        or parsed.created_at.tzinfo is None
    ):
        raise IntegrityError("job lock policy drifted")
    return parsed


def _validate_job_completion(
    job_dir: Path, config: dict[str, Any], expected_trials: int
) -> tuple[dict[str, Any], dict[str, Any], JobConfig, JobResult, JobLock]:
    job_lock = _mapping(_artifact_json(job_dir, "lock.json", "job lock"), "job lock")
    raw_job_result = _mapping(
        _artifact_json(job_dir, "result.json", "job result"), "job result"
    )
    job_config = _mapping(
        _artifact_json(job_dir, "config.json", "job config"), "job config"
    )
    try:
        parsed_job_config = JobConfig.model_validate(config)
        normalized_job_config = parsed_job_config.model_dump(
            mode="json", exclude_defaults=True
        )
    except ValueError as error:
        raise IntegrityError(f"generated JobConfig is invalid: {error}") from error
    if not _same_json(job_config, normalized_job_config):
        raise IntegrityError("Harbor job config differs from the generated config")
    parsed_job_lock = _harbor_job_lock(job_lock)
    stats = _mapping(raw_job_result.get("stats"), "job result stats")
    if set(raw_job_result) != _JOB_RESULT_FIELDS or set(stats) != _JOB_STATS_FIELDS:
        raise IntegrityError("Harbor job result schema drifted")
    try:
        job_result = JobResult.model_validate_json(_canonical(raw_job_result))
    except ValueError as error:
        raise IntegrityError(f"Harbor JobResult is invalid: {error}") from error
    normalized_job_result = json.loads(
        job_result.model_dump_json(exclude={"trial_results"})
    )
    if not _same_json(raw_job_result, normalized_job_result):
        raise IntegrityError("Harbor JobResult serialization is incomplete or drifted")
    counts = {
        field: _integer(stats[field], field)
        for field in (
            "n_completed_trials",
            "n_errored_trials",
            "n_running_trials",
            "n_pending_trials",
            "n_cancelled_trials",
            "n_retries",
        )
    }
    if (
        job_result.n_total_trials != expected_trials
        or job_result.updated_at is None
        or job_result.finished_at is None
        or counts["n_completed_trials"] != expected_trials
        or counts["n_running_trials"] != 0
        or counts["n_pending_trials"] != 0
        or counts["n_retries"] != 0
        or counts["n_errored_trials"] > expected_trials
        or counts["n_cancelled_trials"] > counts["n_errored_trials"]
    ):
        raise IntegrityError("job completion or lock policy drifted")
    awareness = {
        value.tzinfo is None
        for value in (
            job_result.started_at,
            job_result.updated_at,
            job_result.finished_at,
        )
    }
    if len(awareness) != 1 or not (
        job_result.started_at <= job_result.updated_at == job_result.finished_at
    ):
        raise IntegrityError("job result timing is invalid")
    return job_lock, stats, parsed_job_config, job_result, parsed_job_lock


@dataclass(frozen=True, slots=True)
class PreparedJob:
    """One validated Harbor job target from a prepared experiment run."""

    run_dir: Path
    provider: str
    entry: dict[str, Any]
    job_dir: Path
    binding: dict[str, Any]
    config: dict[str, Any]
    compose_path: Path
    compose_sha256: str
    role: str

    def _trial_config(
        self, job: JobConfig, task: Any, agent: Any, trial_name: str, job_id: UUID
    ) -> TrialConfig:
        return TrialConfig(
            task=task,
            trial_name=trial_name,
            trials_dir=job.jobs_dir / job.job_name,
            install_only=job.install_only,
            timeout_multiplier=job.timeout_multiplier,
            agent_timeout_multiplier=job.agent_timeout_multiplier,
            verifier_timeout_multiplier=job.verifier_timeout_multiplier,
            agent_setup_timeout_multiplier=job.agent_setup_timeout_multiplier,
            environment_build_timeout_multiplier=(
                job.environment_build_timeout_multiplier
            ),
            agent=agent,
            user_agent=job.user_agent,
            environment=job.environment,
            verifier=job.verifier,
            artifacts=job.artifacts,
            extra_instruction_paths=job.extra_instruction_paths,
            extra_instructions=job.extra_instructions,
            job_id=job_id,
        )

    def expected_trial_locks(self) -> list[dict[str, Any]]:
        """Build the complete frozen lock set for this prepared job."""
        if self.role == "pilot":
            order = self.entry.get("armOrder")
            if not isinstance(order, list) or set(order) != set(_VARIANTS):
                raise IntegrityError("prepared pilot arm order is invalid")
            locks = [
                _expected_trial_lock(
                    self.provider,
                    str(self.entry["model"]),
                    task,
                    variant,
                    self.binding,
                    self.run_dir / "tasks" / task.removeprefix("terminal-bench/"),
                    self.compose_path,
                    self.compose_sha256,
                )
                for task in _TASKS
                for variant in order
            ]
            if len(locks) != len(_TASKS) * len(_VARIANTS):
                raise IntegrityError("prepared pilot arm order is invalid")
            return locks
        if self.role != "probe":
            raise IntegrityError("unknown prepared job role")
        try:
            job = JobConfig.model_validate(self.config)
        except ValueError as error:
            raise IntegrityError("prepared Harbor job config is invalid") from error
        if len(job.tasks) != 1 or len(job.agents) != 1:
            raise IntegrityError("live-route probe config cardinality drifted")
        lock = build_trial_lock(
            trial_config=self._trial_config(
                job, job.tasks[0], job.agents[0], "live-route-probe", UUID(int=0)
            ),
            task_download_result=TaskDownloadResult(
                path=self.run_dir / "tasks" / "live-route-probe",
                download_time_sec=0,
                cached=True,
            ),
        )
        value = json.loads(lock.model_dump_json(exclude_none=True))
        if _sequence(value.get("extra_docker_compose"), "probe compose lock") != [
            {"path": str(self.compose_path), "digest": self.compose_sha256}
        ]:
            raise IntegrityError("live-route probe compose lock drifted")
        return [value]

    def _active_trial_dir(self, active_trial_dir: Path) -> Path:
        raw_trial = active_trial_dir.expanduser()
        if not raw_trial.is_absolute():
            raise IntegrityError("active trial directory must be absolute")
        trial_dir = Path(os.path.abspath(raw_trial))
        absolute_job = Path(os.path.abspath(self.job_dir))
        try:
            resolved_job = self.job_dir.resolve(strict=True)
            resolved_trial = trial_dir.resolve(strict=True)
        except OSError as error:
            raise IntegrityError("active Harbor job is unavailable") from error
        path_is_bound = all(
            (
                not self.job_dir.is_symlink(),
                not trial_dir.is_symlink(),
                resolved_job == absolute_job,
                resolved_trial == trial_dir,
                resolved_trial.parent == resolved_job,
            )
        )
        if not path_is_bound:
            raise IntegrityError("authorization belongs to another Harbor job")
        return trial_dir

    def claim_active_trial(self, active_trial_dir: Path) -> tuple[UUID, str]:
        """Bind authorization to the exact prepared Harbor trial now executing."""
        trial_dir = self._active_trial_dir(active_trial_dir)
        try:
            parsed_config = JobConfig.model_validate(self.config)
        except ValueError as error:
            raise IntegrityError("prepared Harbor job config is invalid") from error
        actual_config = _mapping(
            _artifact_json(self.job_dir, "config.json", "active job config"),
            "active job config",
        )
        if not _same_json(
            actual_config,
            parsed_config.model_dump(mode="json", exclude_defaults=True),
        ):
            raise IntegrityError(
                "active Harbor job config differs from the prepared run"
            )
        job_lock = _harbor_job_lock(
            _mapping(
                _artifact_json(self.job_dir, "lock.json", "active job lock"),
                "active job lock",
            )
        )
        expected = sorted(canonical_json(item) for item in self.expected_trial_locks())
        actual = sorted(
            canonical_json(json.loads(item.model_dump_json(exclude_none=True)))
            for item in job_lock.trials
        )
        if actual != expected:
            raise IntegrityError("active Harbor job lock differs from the prepared run")
        active_lock = _mapping(
            _artifact_json(trial_dir, "lock.json", "active trial lock"),
            "active trial lock",
        )
        if canonical_json(active_lock) not in expected:
            raise IntegrityError(
                "active Harbor trial lock differs from the prepared run"
            )
        locked_agent = _mapping(active_lock.get("agent"), "active trial agent")
        agents = [
            agent
            for agent in parsed_config.agents
            if _same_json(
                json.loads(agent.model_dump_json(exclude_none=True)), locked_agent
            )
        ]
        locked_task = _mapping(active_lock.get("task"), "active trial task")
        tasks = [
            task
            for task in parsed_config.tasks
            if task.path is not None
            and Path(task.path).resolve()
            == Path(str(locked_task.get("path"))).resolve()
            and task.source == locked_task.get("source")
        ]
        job_result = _mapping(
            _artifact_json(self.job_dir, "result.json", "active job result"),
            "active job result",
        )
        try:
            job_id = UUID(str(job_result.get("id")))
        except ValueError as error:
            raise IntegrityError("active Harbor job ID is invalid") from error
        if len(agents) != 1 or len(tasks) != 1:
            raise IntegrityError(
                "active Harbor trial is absent from the prepared config"
            )
        expected_trial = self._trial_config(
            parsed_config, tasks[0], agents[0], trial_dir.name, job_id
        )
        actual_trial = _mapping(
            _artifact_json(trial_dir, "config.json", "active trial config"),
            "active trial config",
        )
        if not _same_json(
            actual_trial,
            expected_trial.model_dump(mode="json", exclude_defaults=True),
        ):
            raise IntegrityError(
                "active Harbor trial config differs from the prepared run"
            )
        return job_id, _digest(active_lock)

    def validate_completion(self) -> CompletedJob:
        """Validate a finished single-trial probe job and its Harbor envelope."""
        if self.role != "probe" or not self.job_dir.is_dir():
            raise IntegrityError("live-route job directory is unavailable")
        _, stats, config, result, lock = _validate_job_completion(
            self.job_dir, self.config, 1
        )
        if stats["n_errored_trials"] != 0 or stats["n_cancelled_trials"] != 0:
            raise IntegrityError("live-route probe did not complete successfully")
        return CompletedJob(self, stats, config, result, lock)

    @staticmethod
    def environment_identity(trial_dir: Path) -> tuple[str, str]:
        session = f"{trial_dir.name}__env"
        return session, _sanitize_docker_compose_project_name(session)


@dataclass(frozen=True, slots=True)
class CompletedJob:
    """A completed Harbor envelope validated against one prepared job."""

    prepared: PreparedJob
    stats: dict[str, Any]
    config: JobConfig
    result: JobResult
    lock: JobLock

    def single_trial(self) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        with os.scandir(self.prepared.job_dir) as entries:
            children = [
                entry for entry in entries if entry.is_dir(follow_symlinks=False)
            ]
        if len(children) != 1:
            raise IntegrityError("live-route job must contain exactly one trial")
        trial_dir = Path(children[0].path)
        result = _mapping(
            _artifact_json(trial_dir, "result.json", "trial result"), "trial result"
        )
        _harbor_trial_result(result)
        lock = _mapping(
            _artifact_json(trial_dir, "lock.json", "trial lock"), "trial lock"
        )
        root_locks = [
            json.loads(item.model_dump_json(exclude_none=True))
            for item in self.lock.trials
        ]
        if len(root_locks) != 1 or not _same_json(root_locks[0], lock):
            raise IntegrityError("job and trial locks disagree")
        return trial_dir, result, lock

    @staticmethod
    def artifact_json(
        root: Path, relative: str, label: str, *, max_bytes: int = _JSON_ARTIFACT_CAP
    ) -> Any:
        return _artifact_json(root, relative, label, max_bytes=max_bytes)


@dataclass(frozen=True, slots=True)
class RelayEvidence:
    """Complete relay evidence and the usage derived from its sealed journal."""

    verified: dict[str, Any]
    usage: dict[str, int | None]

    @classmethod
    def complete(
        cls,
        trial_dir: Path,
        provider: str,
        model: str,
        binding: dict[str, Any],
        started: datetime,
        finished: datetime,
    ) -> RelayEvidence:
        verified, usage = _validate_relay(
            trial_dir,
            provider,
            model,
            str(binding["relay_build_sha256"]),
            started,
            finished,
            False,
        )
        if usage is None:
            raise IntegrityError("complete relay evidence has no usage")
        return cls(verified, usage)

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.verified["records"]

    @property
    def seal(self) -> dict[str, Any]:
        return self.verified["seal"]

    def validate_embedded(
        self,
        agent_result: dict[str, Any],
        trajectory: dict[str, Any],
        expected_variant: dict[str, Any],
        expected_harbor: dict[str, Any],
    ) -> None:
        """Validate Harbor accounting and the embedded copy of sealed evidence."""
        _validate_agent_totals(agent_result, self.usage, False)
        metrics = _mapping(trajectory.get("final_metrics"), "trajectory metrics")
        _validate_trajectory_metrics(metrics, self.usage, len(trajectory["steps"]))
        metadata = _mapping(agent_result.get("metadata"), "agent metadata")
        provider_data = _mapping(
            metadata.get("open_agent_lab_provider"), "provider metadata"
        )
        if set(provider_data) != _PROVIDER_METADATA_FIELDS:
            raise IntegrityError("provider metadata schema drifted")
        for key in (
            "schema_version",
            "event_count",
            "chain_head",
            "seal",
            "records",
            "publication_gate",
        ):
            if not _same_json(provider_data.get(key), self.verified.get(key)):
                raise IntegrityError(f"embedded provider {key} drifted")
        if not _same_json(provider_data.get("agent_variant"), expected_variant):
            raise IntegrityError("live-route agent variant drifted")
        harbor = _mapping(provider_data.get("harbor_binding"), "harbor binding")
        unhashed = {
            key: value for key, value in harbor.items() if key != "binding_sha256"
        }
        if not _same_json(unhashed, expected_harbor) or harbor.get(
            "binding_sha256"
        ) != _digest(expected_harbor):
            raise IntegrityError("live-route Harbor binding drifted")


@dataclass(frozen=True, slots=True)
class LiveRouteRun:
    """Validated live-route and pilot views of one prepared experiment run."""

    run_dir: Path
    provider: str
    preflight: dict[str, Any]
    binding: dict[str, Any]
    probe: dict[str, Any]
    pilot: dict[str, Any]
    probe_job: PreparedJob

    @classmethod
    def providers(cls) -> tuple[str, ...]:
        return tuple(_PROVIDERS)

    @classmethod
    def open(cls, run_dir: Path, provider: str) -> LiveRouteRun:
        if provider not in _PROVIDERS:
            raise IntegrityError("unknown live-route provider")
        manifest, _, manifest_sha = _manifest(_repo_root())
        runtime = _mapping(manifest.get("runtime"), "runtime")["codexRuntime"]
        preflight, providers, probes = _validate_record(
            run_dir,
            manifest_sha,
            manifest["relayBuildIds"]["production"],
            runtime,
        )
        matches = [
            _mapping(item, "live-route probe")
            for item in probes
            if isinstance(item, dict) and item.get("provider") == provider
        ]
        if len(matches) != 1:
            raise IntegrityError("live-route probe record is missing or duplicated")
        provider_entries = _provider_entries(providers)
        probe, pilot = matches[0], provider_entries[provider]
        profile = _PROVIDERS[provider]
        expected_job = (
            f"live-route-jobs/{provider}/open-agent-lab-"
            f"{preflight['replicationId']}-{provider}-live-route-probe"
        )
        if (
            probe.get("model") != profile["model"]
            or probe.get("reasoning") != profile["reasoning"]
            or probe.get("task") != LIVE_ROUTE_PROBE_TASK
            or probe.get("config") != f"live-route-probes/{provider}.yaml"
            or probe.get("compose")
            != f"overlays/relay.{provider}.live-route-probe.compose.yaml"
            or probe.get("jobDir") != expected_job
            or probe.get("limits") != dict(LIVE_ROUTE_PROBE_LIMITS)
            or probe.get("relayImageSha256") != preflight["relayImageSha256"]
        ):
            raise IntegrityError("live-route probe record drifted")
        replication = _replication(manifest, preflight["replicationId"])
        preflight_sha = _digest(preflight)
        for entry_provider, entry in provider_entries.items():
            _validated_job_dir(
                run_dir,
                entry,
                preflight,
                preflight_sha,
                list(_TASKS),
                replication["armOrderByProvider"][entry_provider],
            )
        binding = _expected_binding(preflight, preflight_sha)
        job_dir = run_dir / _relative(probe.get("jobDir"), "live-route job")
        config_relative = _relative(probe.get("config"), "live-route config")
        compose_relative = _relative(probe.get("compose"), "live-route compose")
        config_bytes = _artifact_bytes(run_dir, config_relative)
        compose_bytes = _artifact_bytes(run_dir, compose_relative)
        if _digest_bytes(config_bytes) != probe.get("configSha256") or _digest_bytes(
            compose_bytes
        ) != probe.get("composeSha256"):
            raise IntegrityError("live-route config or compose drifted")
        config = _yaml_bytes(config_bytes, "live-route config")
        compose = _yaml_bytes(compose_bytes, "live-route compose")
        compose_path = run_dir / compose_relative
        if not _same_json(
            compose,
            _pinned_overlay(
                _repo_root(),
                provider,
                probe["relayImageSha256"],
                live_route_probe=True,
            ),
        ) or not _same_json(
            config,
            live_route_probe_config(
                run_dir,
                binding,
                provider,
                profile["model"],
                profile["reasoning"],
                compose_path,
                probe["composeSha256"],
            ),
        ):
            raise IntegrityError("live-route probe policy drifted")
        if job_dir.is_symlink() or not job_dir.resolve().is_relative_to(
            run_dir.resolve()
        ):
            raise IntegrityError("live-route job directory escapes the prepared run")
        prepared = PreparedJob(
            run_dir,
            provider,
            probe,
            job_dir,
            binding,
            config,
            compose_path,
            str(probe["composeSha256"]),
            "probe",
        )
        return cls(run_dir, provider, preflight, binding, probe, pilot, prepared)

    @property
    def model(self) -> str:
        return str(_PROVIDERS[self.provider]["model"])

    @property
    def reasoning(self) -> str:
        return str(_PROVIDERS[self.provider]["reasoning"])

    @property
    def codex_version(self) -> str:
        return _CODEX_VERSION

    @property
    def probe_task_binding(self) -> dict[str, Any]:
        return _TASK_RUNTIME_BINDINGS[LIVE_ROUTE_PROBE_TASK]

    def pilot_job(self) -> PreparedJob:
        provider, job_dir, config, compose_path, compose_sha256 = _validated_job_dir(
            self.run_dir,
            self.pilot,
            self.preflight,
            _digest(self.preflight),
            list(_TASKS),
            self.pilot["armOrder"],
        )
        return PreparedJob(
            self.run_dir,
            provider,
            self.pilot,
            job_dir,
            self.binding,
            config,
            compose_path,
            compose_sha256,
            "pilot",
        )


def _optional_total(attempts: list[dict[str, Any]], field: str) -> int | float | None:
    total = None
    for item in sorted(attempts, key=lambda attempt: attempt["startedAt"]):
        value = item["harborAgentTotals"][field]
        if value is not None:
            total = (total or 0) + value
    return total


def _expected_eval_cores(
    attempts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for item in sorted(attempts, key=lambda attempt: attempt["startedAt"]):
        key = f"{_VARIANTS[item['variant']]['name']}__{item['model']}__{_DATASET}"
        core = expected.setdefault(
            key,
            {
                "n_trials": 0,
                "n_errors": 0,
                "reward_stats": {"reward": {}},
                "exception_stats": {},
            },
        )
        core["n_trials"] += 1
        rewards = core["reward_stats"]["reward"]
        rewards.setdefault(item["reward"], []).append(item["trialName"])
        exception_type = item["topLevelException"]
        if exception_type is not None:
            core["n_errors"] += 1
            core["exception_stats"].setdefault(exception_type, []).append(
                item["trialName"]
            )
    return expected


def _validate_job_aggregates(
    job_result: JobResult, attempts: list[dict[str, Any]]
) -> None:
    stats = job_result.stats
    for field in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd"):
        if getattr(stats, field) != _optional_total(attempts, field):
            raise IntegrityError(f"job aggregate {field} disagrees with child trials")
    expected_evals = _expected_eval_cores(attempts)
    if set(stats.evals) != set(expected_evals):
        raise IntegrityError("job aggregate eval identities disagree with child trials")
    for key, expected in expected_evals.items():
        actual = stats.evals[key]
        if (
            actual.n_trials != expected["n_trials"]
            or actual.n_errors != expected["n_errors"]
            or actual.reward_stats != expected["reward_stats"]
            or actual.exception_stats != expected["exception_stats"]
            or actual.pass_at_k
        ):
            raise IntegrityError("job aggregate evals disagree with child trials")
        # Harbor metric plugins are presentation aggregates. The analyzer uses
        # only sealed per-trial rewards, so typed metric payloads are non-authoritative.


def _job_attempts(
    run_dir: Path,
    entry: dict[str, Any],
    probe: dict[str, Any],
    preflight: dict[str, Any],
    preflight_sha: str,
    tasks: list[str],
    order: list[str],
) -> list[dict[str, Any]]:
    provider, job_dir, config, compose_path, compose_sha256 = _validated_job_dir(
        run_dir, entry, preflight, preflight_sha, tasks, order
    )
    job_lock, stats, parsed_job_config, job_result, parsed_job_lock = (
        _validate_job_completion(job_dir, config, len(tasks) * 2)
    )
    child_dirs = sorted(
        path for path in job_dir.iterdir() if path.is_dir() or path.is_symlink()
    )
    if len(child_dirs) != len(tasks) * 2:
        raise IntegrityError("job has a missing or extra trial directory")
    attempts = [
        _attempt(
            run_dir,
            job_dir,
            path,
            provider,
            entry["model"],
            probe,
            entry,
            preflight,
            preflight_sha,
            set(tasks),
            parsed_job_config,
            job_result.id,
            compose_path,
            compose_sha256,
        )
        for path in child_dirs
    ]
    if len({item["trialId"] for item in attempts}) != len(attempts) or len(
        {item["trialName"] for item in attempts}
    ) != len(attempts):
        raise IntegrityError("trial IDs and names must be unique")
    root_locks = Counter(
        _canonical(item)
        for item in _sequence(job_lock.get("trials"), "job lock trials")
    )
    child_locks = Counter(_canonical(item["lock"]) for item in attempts)
    if root_locks != child_locks:
        raise IntegrityError("job and child trial locks disagree")
    if stats.get("n_errored_trials", 0) != sum(
        item["topLevelException"] is not None for item in attempts
    ) or stats.get("n_cancelled_trials", 0) != sum(
        item["topLevelException"] == "CancelledError" for item in attempts
    ):
        raise IntegrityError("job error counts disagree with completed trials")
    _validate_job_aggregates(job_result, attempts)
    expected_sequence = [(task, variant) for task in tasks for variant in order]
    actual = sorted(attempts, key=lambda item: item["startedAt"])
    if not (
        _iso(preflight["createdAt"], "preflight.createdAt")
        <= parsed_job_lock.created_at
        <= actual[0]["startedAt"]
    ):
        raise IntegrityError("job lock creation time is outside the prepared run")
    if [(item["task"], item["variant"]) for item in actual] != expected_sequence:
        raise IntegrityError("actual serial arm order drifted")
    if any(left["finishedAt"] > right["startedAt"] for left, right in pairwise(actual)):
        raise IntegrityError("serial trials overlap")
    for item in attempts:
        item.pop("lock")
        item.pop("finishedAt")
        item.pop("trialName")
        item.pop("harborAgentTotals")
    return attempts


def _increase(treatment: float | None, control: float | None) -> float | str | None:
    if treatment is None or control is None:
        return None
    if control == 0:
        return 0.0 if treatment == 0 else "infinite"
    return treatment / control - 1


def _median(values: list[float | str]) -> float | str | None:
    if not values:
        return None
    median = statistics.median(
        math.inf if value == "infinite" else value for value in values
    )
    return "infinite" if math.isinf(median) else median


def _clean_attempt(item: dict[str, Any]) -> dict[str, Any]:
    hidden = {"startedAt", "probeStartedAt", "providerResponseIdentities"}
    return {key: value for key, value in item.items() if key not in hidden}


def _exception_counts(attempts: list[dict[str, Any]]) -> dict[str, int]:
    if any(
        (item["topLevelException"] is None) != (item["failureClass"] is None)
        for item in attempts
    ):
        raise IntegrityError("scored exception and failure class disagree")
    errored = [item for item in attempts if item["topLevelException"] is not None]
    counts = Counter(item["failureClass"] for item in errored)
    if sum(counts.values()) != len(errored):
        raise IntegrityError("failure classes disagree with the errored denominator")
    return dict(sorted(counts.items()))


def _validate_global_uniqueness(attempts: list[dict[str, Any]]) -> None:
    for key in ("trialId", "relayRunId", "relayInstanceId"):
        values = [item[key] for item in attempts]
        if len(set(values)) != len(values):
            raise IntegrityError(f"{key} must be unique across every supplied trial")
    chain_heads = [item["chainHead"] for item in attempts if item["chainHead"]]
    if len(set(chain_heads)) != len(chain_heads):
        raise IntegrityError("nonempty chainHead must be unique across supplied trials")
    request_ids = [
        request_id for item in attempts for request_id in item["relayRequestIds"]
    ]
    if len(set(request_ids)) != len(request_ids):
        raise IntegrityError("relay request IDs must be globally unique")
    lifecycles = [
        (item["provider"], *identity)
        for item in attempts
        for identity in item["providerResponseIdentities"]
    ]
    conflict = _provider_response_identity_error(lifecycles)
    if conflict is not None:
        raise IntegrityError(conflict)


def _build_pairs(
    attempts: list[dict[str, Any]], tasks: list[str]
) -> list[dict[str, Any]]:
    task_index = {task: index for index, task in enumerate(tasks)}
    attempts.sort(
        key=lambda item: (
            item["provider"],
            item["replication"],
            task_index[item["task"]],
            item["variant"],
        )
    )
    grouped: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
    for item in attempts:
        key = (
            item["provider"],
            item["model"],
            item["task"],
            item["taskDigest"],
            item["taskChecksum"],
            item["replication"],
            item["sourceRevision"],
        )
        variants = grouped.setdefault(key, {})
        if item["variant"] in variants:
            raise IntegrityError("duplicate arm in a pair")
        variants[item["variant"]] = item
    task_identities: dict[str, set[tuple[str, str]]] = {}
    for item in attempts:
        task_identities.setdefault(item["task"], set()).add(
            (item["taskDigest"], item["taskChecksum"])
        )
    if any(len(identities) != 1 for identities in task_identities.values()):
        raise IntegrityError("task identity differs across providers or replications")
    expected_pairs = (
        len(_PROVIDERS) * len(tasks) * len({item["replication"] for item in attempts})
    )
    if len(grouped) != expected_pairs or any(
        set(pair) != set(_VARIANTS) for pair in grouped.values()
    ):
        raise IntegrityError("paired denominator is incomplete")
    pairs: list[dict[str, Any]] = []
    for key, arms in sorted(
        grouped.items(),
        key=lambda value: (value[0][0], value[0][5], task_index[value[0][2]]),
    ):
        control, treatment = arms["control-v1"], arms["verify-instruction-v1"]
        reward_delta = treatment["reward"] - control["reward"]
        control_tokens = control["tokens"]
        treatment_tokens = treatment["tokens"]
        pairs.append(
            {
                "provider": key[0],
                "model": key[1],
                "task": key[2],
                "taskDigest": key[3],
                "taskChecksum": key[4],
                "replication": key[5],
                "sourceRevision": key[6],
                "reward": {
                    "control": control["reward"],
                    "treatment": treatment["reward"],
                    "delta": reward_delta,
                },
                "primaryTokenIncrease": _increase(
                    None
                    if treatment_tokens is None
                    else treatment_tokens["input_tokens"]
                    + treatment_tokens["output_tokens"],
                    None
                    if control_tokens is None
                    else control_tokens["input_tokens"]
                    + control_tokens["output_tokens"],
                ),
                "wallTimeIncrease": _increase(
                    treatment["wallSeconds"], control["wallSeconds"]
                ),
            }
        )
    return pairs


def _task_reward_deltas(pairs: list[dict[str, Any]], tasks: list[str]) -> list[float]:
    deltas = []
    for task in tasks:
        task_pairs = [pair for pair in pairs if pair["task"] == task]
        control = statistics.fmean(pair["reward"]["control"] for pair in task_pairs)
        treatment = statistics.fmean(pair["reward"]["treatment"] for pair in task_pairs)
        deltas.append(treatment - control)
    return deltas


def _paired_reward_bootstrap(task_deltas: list[float]) -> dict[str, Any]:
    if not task_deltas:
        raise ValueError("paired bootstrap requires at least one task")
    rng = random.Random(_PAIRED_BOOTSTRAP_SEED)
    means = sorted(
        statistics.fmean(
            task_deltas[rng.randrange(len(task_deltas))] for _ in task_deltas
        )
        for _ in range(_PAIRED_BOOTSTRAP_RESAMPLES)
    )
    lower = means[math.ceil(0.025 * len(means)) - 1]
    upper = means[math.ceil(0.975 * len(means)) - 1]
    return {
        "resamplingUnit": "task",
        "taskCount": len(task_deltas),
        "method": "percentile_nearest_rank",
        "confidenceLevel": 0.95,
        "sidedness": "two-sided",
        "resamples": _PAIRED_BOOTSTRAP_RESAMPLES,
        "seed": _PAIRED_BOOTSTRAP_SEED,
        "meanDeltaPercentagePoints": 100 * statistics.fmean(task_deltas),
        "confidenceIntervalPercentagePoints": [100 * lower, 100 * upper],
    }


def _summary(
    attempts: list[dict[str, Any]], manifest: dict[str, Any], tasks: list[str]
) -> dict[str, Any]:
    _validate_global_uniqueness(attempts)
    pairs = _build_pairs(attempts, tasks)
    provider_summary = []
    blockers = []
    required_replications = {item["id"] for item in manifest["replications"]}
    actual_replications = {item["replication"] for item in attempts}
    replications_complete = actual_replications == required_replications
    telemetry_complete = all(item["telemetryComplete"] for item in attempts)
    analysis_complete = replications_complete and telemetry_complete
    exception_counts = _exception_counts(attempts)
    errored_attempts = sum(exception_counts.values())
    if not replications_complete:
        blockers.append("mirrored_within_provider_replication_missing")
    if not telemetry_complete:
        blockers.append("attempt_telemetry_missing")
    for provider in _PROVIDERS:
        own = [pair for pair in pairs if pair["provider"] == provider]
        deltas = [pair["reward"]["delta"] for pair in own]
        task_deltas = _task_reward_deltas(own, tasks)
        token_values = [
            pair["primaryTokenIncrease"]
            for pair in own
            if pair["primaryTokenIncrease"] is not None
        ]
        wall_values = [
            pair["wallTimeIncrease"]
            for pair in own
            if pair["wallTimeIncrease"] is not None
        ]
        token_overhead = (
            _median(token_values) if len(token_values) == len(own) else None
        )
        wall_overhead = _median(wall_values) if len(wall_values) == len(own) else None
        mean_delta = statistics.fmean(task_deltas)
        wins = sum(value > 0 for value in deltas)
        ties = sum(value == 0 for value in deltas)
        losses = sum(value < 0 for value in deltas)
        provider_summary.append(
            {
                "provider": provider,
                "pairs": len(own),
                "meanPairedRewardDelta": mean_delta,
                "pairedRewardBootstrap": _paired_reward_bootstrap(task_deltas),
                "winTieLoss": [wins, ties, losses],
                "medianPrimaryTokenIncrease": token_overhead,
                "primaryTokenCoveragePairs": len(token_values),
                "medianWallTimeIncrease": wall_overhead,
                "wallTimeCoveragePairs": len(wall_values),
            }
        )
        if mean_delta <= manifest["promotionRule"]["meanPairedRewardDeltaMustExceed"]:
            blockers.append(f"{provider}_mean_reward_delta_not_positive")
        limit = manifest["promotionRule"]["maximumMedianTokenOrWallTimeIncrease"]
        over_limit = any(
            value == "infinite" or value > limit
            for value in (token_overhead, wall_overhead)
            if value is not None
        )
        if (
            over_limit
            and mean_delta
            < manifest["promotionRule"]["minimumRewardGainIfOverheadLimitExceeded"]
        ):
            blockers.append(f"{provider}_overhead_not_justified")
    directional_criteria_met = not blockers
    blockers.append("development_experiment_never_promotable")
    return {
        "schemaVersion": _SUMMARY_SCHEMA_VERSION,
        "experimentId": EXPERIMENT_ID,
        "claimClass": "directional_five_task_development_result",
        "integrityOk": True,
        "analysisComplete": analysis_complete,
        "analysisStatus": "valid" if analysis_complete else "valid_incomplete",
        "denominator": {
            "attempts": len(attempts),
            "erroredAttempts": errored_attempts,
            "pairs": len(pairs),
            "tasksPerProvider": len(tasks),
        },
        "exceptionCounts": exception_counts,
        "telemetryCoverage": {
            "completeAttempts": sum(item["telemetryComplete"] for item in attempts),
            "costUsdAttempts": sum(item["costUsd"] is not None for item in attempts),
            "totalAttempts": len(attempts),
        },
        "attempts": [_clean_attempt(item) for item in attempts],
        "pairs": pairs,
        "providerSummary": provider_summary,
        "promotion": {
            "ok": False,
            "status": "not_promotable",
            "directionalCriteriaMet": directional_criteria_met,
            "blockingReasons": sorted(set(blockers)),
        },
    }


def _provider_entries(providers: list[Any]) -> dict[str, dict[str, Any]]:
    by_provider: dict[str, dict[str, Any]] = {}
    for entry in providers:
        if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str):
            raise IntegrityError("run record provider set drifted")
        provider = entry["provider"]
        if provider in by_provider:
            raise IntegrityError("run record provider set drifted")
        by_provider[provider] = entry
    if set(by_provider) != set(_PROVIDERS):
        raise IntegrityError("run record provider set drifted")
    return by_provider


def summarize(run_dirs: list[Path]) -> dict[str, Any]:
    """Validate exact live artifacts and return a deterministic redacted summary."""
    if not run_dirs:
        raise IntegrityError("at least one prepared run is required")
    root = _repo_root()
    manifest, _, manifest_sha = _manifest(root)
    tasks = list(_TASKS)
    attempts: list[dict[str, Any]] = []
    prepared_at: list[datetime] = []
    seen_replications: set[str] = set()
    source_revision: str | None = None
    for raw in run_dirs:
        run_dir = raw.expanduser().resolve()
        if not run_dir.is_dir():
            raise IntegrityError("each input must be an exact prepared run directory")
        preflight, providers, probes = _validate_record(
            run_dir,
            manifest_sha,
            manifest["relayBuildIds"]["production"],
            manifest["runtime"]["codexRuntime"],
        )
        prepared_at.append(_iso(preflight["createdAt"], "preflight.createdAt"))
        replication_id = preflight["replicationId"]
        replication = _replication(manifest, replication_id)
        if replication_id in seen_replications:
            raise IntegrityError("replication IDs must be unique")
        seen_replications.add(replication_id)
        if source_revision is None:
            source_revision = preflight["sourceRevision"]
        elif source_revision != preflight["sourceRevision"]:
            raise IntegrityError("replications use different source revisions")
        by_provider = _provider_entries(providers)
        by_probe = _provider_entries(probes)
        for provider in _PROVIDERS:
            attempts.extend(
                _job_attempts(
                    run_dir,
                    by_provider[provider],
                    by_probe[provider],
                    preflight,
                    _digest(preflight),
                    tasks,
                    replication["armOrderByProvider"][provider],
                )
            )
    if any(item["probeStartedAt"] < max(prepared_at) for item in attempts):
        raise IntegrityError(
            "all replications must be prepared before the first live probe"
        )
    if any(item["startedAt"] < max(prepared_at) for item in attempts):
        raise IntegrityError("all replications must be prepared before the first trial")
    return _summary(attempts, manifest, tasks)


def cleanup_images(run_dir: Path) -> dict[str, str]:
    """Remove only the run-owned image tags after its trials finish."""
    run_dir = run_dir.expanduser().resolve(strict=True)
    manifest, _, manifest_sha = _manifest(_repo_root())
    _validate_record(
        run_dir,
        manifest_sha,
        manifest["relayBuildIds"]["production"],
        manifest["runtime"]["codexRuntime"],
    )
    record = _mapping(
        _artifact_json(run_dir, "run-record.json", "run record"), "run record"
    )
    images = _mapping(record["relayImages"], "relayImages")
    tags = _mapping(record["relayImageTags"], "relayImageTags")
    for identity in _RELAY_IMAGES:
        observed = _docker("image", "inspect", "--format", "{{.Id}}", tags[identity])
        if observed != images[identity]:
            raise IntegrityError(f"{identity} retained image tag drifted")
    for tag in tags.values():
        _docker("image", "rm", tag)
    return tags


def _write_once(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(_canonical(value) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("output", type=Path)
    prepare_parser.add_argument(
        "--replication", choices=("screen-v1", "mirror-v1"), default="screen-v1"
    )
    summarize_parser = commands.add_parser("summarize")
    summarize_parser.add_argument("runs", type=Path, nargs="+")
    summarize_parser.add_argument("--output", type=Path)
    cleanup_parser = commands.add_parser("cleanup-images")
    cleanup_parser.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(prepare(args.output, args.replication))
            return 0
        if args.command == "cleanup-images":
            print(_canonical(cleanup_images(args.run)))
            return 0
        result = summarize(args.runs)
        if args.output is None:
            print(_canonical(result))
        else:
            _write_once(args.output, result)
        return 0
    except IntegrityError as error:
        if args.command == "summarize":
            invalid = {
                "schemaVersion": _SUMMARY_SCHEMA_VERSION,
                "experimentId": EXPERIMENT_ID,
                "integrityOk": False,
                "analysisComplete": False,
                "analysisStatus": "invalid",
                "promotion": {
                    "ok": False,
                    "status": "not_promotable",
                    "blockingReasons": [str(error)],
                },
            }
            print(_canonical(invalid))
            return 1
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
