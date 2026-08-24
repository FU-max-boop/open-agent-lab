"""Harbor Docker environment with a source-bound, sealed Compose graph."""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, override

import yaml
from dirhash import dirhash
from harbor.environments.base import ExecResult, OutputCallback
from harbor.environments.docker.docker import (
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)
from harbor.publisher.packager import Packager

from .codex_runtime import (
    CODEX_RUNTIME_INSTALL_ROOT,
    CODEX_RUNTIME_PREPARED_RELATIVE,
    validate_codex_runtime_spec,
    verify_tree,
)
from .experiment_contract import (
    EXPERIMENT_ID,
    LIVE_ROUTE_PROBE_EGRESS_NETWORK,
    LIVE_ROUTE_PROBE_INTERNAL_NETWORK,
    LIVE_ROUTE_PROBE_LIMITS,
    LIVE_ROUTE_PROBE_TASK,
    PREFLIGHT_KEYS,
    RELAY_ARTIFACT_LIMITS,
    RELAY_BUILD_ID_PATH,
    RELAY_SERVICE,
    RUN_BINDING_KEYS,
    canonical_json,
    digest_bytes,
    is_digest,
    is_revision,
    is_strict_int,
    live_route_probe_config,
    live_route_probe_networks,
    live_route_probe_relay_command,
)

_SECRET = "provider-api-key"
_EXTERNAL_KEYS = {"include", "env_file", "extends", "label_file"}
_MAX_COMPOSE_BYTES = 4 * 1024 * 1024
_MAX_CREDENTIAL_BYTES = 64 * 1024
_RELAY_RUNTIME_GID = 1000
_CLEANUP_ATTEMPTS = 3
_CLEANUP_TIMEOUT_SECONDS = 120
_LOG_EXPORT_TIMEOUT_SECONDS = 60
_CLEANUP_RECEIPT = "environment-cleanup.json"
_TASK_RUNTIME_KEYS = {
    "taskDigest",
    "taskChecksum",
    "declaredImage",
    "immutableImage",
    "imageConfigDigest",
    "platform",
}
_TASK_SNAPSHOT_KEYS = {"relativePath", "taskDigest", "taskChecksum"}
_PROCESS_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION",
}
_PROVIDER_FILES = {"OAL_DEEPSEEK_API_KEY_FILE", "OAL_ZAI_API_KEY_FILE"}
_DOCKER_AUTH_ENV = {
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
}
_SECRET_ENV = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSW|PRIVATE[_-]?KEY|CREDENTIAL)",
    re.IGNORECASE,
)
_STANDARD_MOUNT_TARGETS = frozenset(
    {"/logs/verifier", "/logs/agent", "/logs/artifacts"}
)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader: _UniqueLoader, node: yaml.Node, deep: bool = False) -> Any:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate Compose key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _publish_new_bytes(path: Path, data: bytes) -> None:
    """Durably publish bytes without following links or replacing a path."""
    directory = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    temporary = f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    descriptor = -1
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("file write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        published = True
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        if published:
            try:
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
            except OSError:
                pass
        raise
    finally:
        os.close(directory)


def _write_cleanup_receipt(path: Path, value: dict[str, Any]) -> None:
    """Publish a new receipt only after its complete contents are durable."""
    _publish_new_bytes(path, canonical_json(value))


def _unique_json(data: bytes, label: str) -> dict[str, Any]:
    def mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key in {label}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=mapping)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid JSON.") from error
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping.")
    return value


def _yaml(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = yaml.load(data.decode(), Loader=_UniqueLoader)
    except (UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid YAML.") from error
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping.")
    return value


def _regular_bytes(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    directory = os.open(
        absolute.anchor,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    descriptor = -1
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_COMPOSE_BYTES:
            raise ValueError("Compose inputs must be bounded regular files.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(_MAX_COMPOSE_BYTES + 1)
            if len(data) > _MAX_COMPOSE_BYTES:
                raise ValueError("Compose inputs must be bounded regular files.")
            return data
    except OSError as error:
        raise ValueError("Compose inputs must not contain symlinks.") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _sealed_memfd(data: bytes) -> int:
    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        raise RuntimeError("Pinned Terminal-Bench provider runs require Linux memfd.")
    descriptor = os.memfd_create(
        "open-agent-lab-compose", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _memfd_path(descriptor: int) -> Path:
    # The Docker CLI may delegate to a plugin that does not inherit our fd.
    return Path(f"/proc/{os.getpid()}/fd/{descriptor}")


def _reject_external_references(value: Any) -> None:
    if isinstance(value, dict):
        if _EXTERNAL_KEYS.intersection(value):
            raise ValueError("External Compose includes are forbidden.")
        for child in value.values():
            _reject_external_references(child)
    elif isinstance(value, list):
        for child in value:
            _reject_external_references(child)


def _validated_run_binding(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RUN_BINDING_KEYS:
        raise ValueError("run_binding has an invalid schema.")
    digest_keys = RUN_BINDING_KEYS - {
        "schema_version",
        "experiment_id",
        "replication_id",
        "source_revision",
    }
    if (
        not is_strict_int(value["schema_version"])
        or value["schema_version"] != 1
        or value["experiment_id"] != EXPERIMENT_ID
        or value["replication_id"] not in {"screen-v1", "mirror-v1"}
        or not isinstance(value["source_revision"], str)
        or not is_revision(value["source_revision"])
        or any(not is_digest(value[key]) for key in digest_keys)
    ):
        raise ValueError("run_binding has invalid values.")
    return dict(value)


def _binding_from_preflight(value: object, digest: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(digest, str):
        raise TypeError("preflight binding is unavailable")
    return _validated_run_binding(
        {
            "schema_version": 1,
            "experiment_id": value["experimentId"],
            "replication_id": value["replicationId"],
            "source_revision": value["sourceRevision"],
            "experiment_manifest_sha256": value["experimentManifestSha256"],
            "relay_build_sha256": value["relayBuildSha256"],
            "relay_image_sha256": value["relayImageSha256"],
            "preflight_sha256": digest,
            "task_snapshots_sha256": value["taskSnapshotsSha256"],
        }
    )


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if completed.returncode:
        raise RuntimeError("Prepared source identity could not be verified.")
    return completed.stdout.strip()


def _relay_image_tags(output: Path, revision: str) -> dict[str, str]:
    token = hashlib.sha256(f"{output.resolve()}\0{revision}".encode()).hexdigest()[:32]
    return {
        "production": f"open-agent-lab-prepared:{token}-production",
        "providerFreeFixture": f"open-agent-lab-prepared:{token}-fixture",
    }


def _task_authorities(
    manifest: dict[str, Any], record: dict[str, Any], binding: dict[str, Any]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    runtime = manifest.get("taskRuntimeBindings")
    snapshots = record.get("taskSnapshots")
    if not isinstance(runtime, dict) or not isinstance(snapshots, dict):
        raise TypeError("task snapshot authority is unavailable")
    if (
        set(runtime) != set(snapshots)
        or digest_bytes(canonical_json(snapshots)) != binding["task_snapshots_sha256"]
    ):
        raise ValueError("task snapshot authority drifted")
    for task, authority in runtime.items():
        snapshot = snapshots.get(task)
        short = (
            task.removeprefix("terminal-bench/")
            if task.startswith("terminal-bench/")
            else task.removeprefix("open-agent-lab/")
        )
        if (
            not isinstance(task, str)
            or not (task.startswith("terminal-bench/") or task == LIVE_ROUTE_PROBE_TASK)
            or not short
            or not isinstance(authority, dict)
            or set(authority) != _TASK_RUNTIME_KEYS
            or not isinstance(snapshot, dict)
            or set(snapshot) != _TASK_SNAPSHOT_KEYS
            or snapshot.get("relativePath") != f"tasks/{short}"
            or snapshot.get("taskDigest") != authority.get("taskDigest")
            or snapshot.get("taskChecksum") != authority.get("taskChecksum")
            or not is_digest(authority.get("taskDigest"))
            or not re.fullmatch(r"[0-9a-f]{64}", str(authority.get("taskChecksum", "")))
            or not is_digest(authority.get("imageConfigDigest"))
            or authority.get("platform") != "linux/amd64"
            or not isinstance(authority.get("declaredImage"), str)
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*",
                authority["declaredImage"],
            )
            or authority.get("immutableImage")
            != authority["declaredImage"].rsplit(":", 1)[0]
            + "@"
            + str(authority["immutableImage"]).rsplit("@", 1)[-1]
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}",
                str(authority.get("immutableImage", "")),
            )
        ):
            raise ValueError("task snapshot authority has an invalid entry")
    return (
        {key: dict(value) for key, value in runtime.items()},
        {key: dict(value) for key, value in snapshots.items()},
    )


def _assert_runtime_gate(
    manifest: dict[str, Any], record: dict[str, Any], binding: dict[str, Any]
) -> None:
    runtime = manifest.get("runtime")
    images = record.get("relayImages")
    if (
        not isinstance(runtime, dict)
        or type(runtime.get("hermeticCodexRuntimeReady")) is not bool
        or not isinstance(images, dict)
    ):
        raise ValueError("hermetic Codex runtime gate is invalid")
    if (
        binding["relay_image_sha256"] == images.get("production")
        and not runtime["hermeticCodexRuntimeReady"]
    ):
        raise RuntimeError("Live work is blocked until Codex runtime bytes are frozen.")


def _plain_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    resolved = absolute.resolve(strict=True)
    if resolved != absolute or not resolved.is_dir():
        raise ValueError("Harbor log mount paths must be plain directories.")
    return resolved


def _validated_mounts(
    mounts: object, prepared_root: Path, trial_paths: Any
) -> list[dict[str, Any]]:
    """Authorize only Harbor logs plus the frozen read-only Codex tree."""
    if not isinstance(mounts, list) or len(mounts) != 4:
        raise ValueError("Prepared runtime mounts are invalid.")
    try:
        expected_logs = {
            "/logs/verifier": _plain_directory(trial_paths.verifier_dir),
            "/logs/agent": _plain_directory(trial_paths.agent_dir),
            "/logs/artifacts": _plain_directory(
                trial_paths.artifacts_dir / "logs" / "artifacts"
            ),
        }
    except (AttributeError, OSError, ValueError) as error:
        raise ValueError("Harbor log mount authority is unavailable.") from error
    expected_runtime = (prepared_root / CODEX_RUNTIME_PREPARED_RELATIVE).resolve(
        strict=True
    )
    if (
        len(set(expected_logs.values())) != 3
        or expected_runtime in expected_logs.values()
    ):
        raise ValueError("Harbor log mount authority overlaps protected paths.")
    verified: list[dict[str, Any]] = []
    targets: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) not in (
            {"type", "source", "target"},
            {"type", "source", "target", "read_only"},
        ):
            raise ValueError("Prepared runtime mounts are invalid.")
        source = mount.get("source")
        target = mount.get("target")
        if (
            mount.get("type") != "bind"
            or not isinstance(source, str)
            or not isinstance(target, str)
            or target in targets
        ):
            raise ValueError("Prepared runtime mounts are invalid.")
        try:
            absolute = Path(os.path.abspath(source))
            resolved = absolute.resolve(strict=True)
            resolved.relative_to(prepared_root)
        except (OSError, ValueError) as error:
            raise ValueError("Prepared runtime mount escapes its run.") from error
        runtime_mount = (
            target == CODEX_RUNTIME_INSTALL_ROOT
            and resolved == expected_runtime
            and resolved == absolute
            and mount.get("read_only") is True
        )
        log_mount = (
            target in _STANDARD_MOUNT_TARGETS
            and resolved == expected_logs[target]
            and resolved == absolute
            and "read_only" not in mount
            and resolved.is_dir()
        )
        if not runtime_mount and not log_mount:
            raise ValueError("Prepared runtime mount target is unauthorized.")
        targets.add(target)
        verified.append(dict(mount))
    if targets != {*_STANDARD_MOUNT_TARGETS, CODEX_RUNTIME_INSTALL_ROOT}:
        raise ValueError("Prepared runtime mount set is incomplete.")
    return verified


def _live_task_authority(
    output: Path,
    task_dir: Path,
    relay_image: str,
    relay_images: dict[str, Any],
    runtime: dict[str, dict[str, str]],
    snapshots: dict[str, dict[str, str]],
    relay_role: str,
) -> tuple[str, Path, dict[str, str], dict[str, str]] | None:
    if relay_role == "fixture":
        if relay_image != relay_images.get("providerFreeFixture"):
            raise ValueError("fixture relay image drifted")
        return None
    if relay_role not in {"pilot", "live-route-probe"}:
        raise ValueError("relay role is invalid")
    if relay_image != relay_images.get("production"):
        raise ValueError("relay image has no prepared-run role")
    actual = Path(os.path.abspath(task_dir))
    matches: list[tuple[str, Path, dict[str, str], dict[str, str]]] = []
    for task, snapshot in snapshots.items():
        authorized = (
            task == LIVE_ROUTE_PROBE_TASK
            if relay_role == "live-route-probe"
            else task.startswith("terminal-bench/")
        )
        if not authorized:
            continue
        expected = Path(os.path.abspath(output / snapshot["relativePath"]))
        if actual == expected:
            if (
                expected.is_symlink()
                or expected.parent.is_symlink()
                or expected.resolve(strict=True) != expected
            ):
                raise ValueError("prepared task path is not a plain directory")
            matches.append((task, expected, runtime[task], snapshot))
    if len(matches) != 1:
        raise ValueError("live run did not use one prepared task snapshot")
    return matches[0]


def _task_hashes(task_dir: Path) -> tuple[str, str]:
    if (
        not task_dir.is_dir()
        or task_dir.is_symlink()
        or any(path.is_symlink() for path in task_dir.rglob("*"))
    ):
        raise ValueError("prepared task snapshot contains an unsafe path")
    content_hash, _ = Packager.compute_content_hash(task_dir)
    return "sha256:" + content_hash, dirhash(task_dir, "sha256")


def _validate_live_route_probe_authorities(
    run_dir: Path,
    binding: dict[str, Any],
    manifest: dict[str, Any],
    probes: list[Any],
    relay_images: dict[str, Any],
) -> None:
    probe_profiles = {
        item.get("provider"): (item.get("model"), item.get("reasoningEffort"))
        for item in manifest.get("pairedConfigs", [])
        if isinstance(item, dict)
    }
    if set(probe_profiles) != {"deepseek", "zai"} or len(probes) != 2:
        raise ValueError("live-route probe authority is incomplete")
    seen: set[str] = set()
    for entry in probes:
        if not isinstance(entry, dict):
            raise TypeError("live-route probe authority is invalid")
        provider = entry.get("provider")
        config_path = _safe_output_path(run_dir, entry.get("config"))
        compose_path = _safe_output_path(run_dir, entry.get("compose"))
        job_name = (
            f"open-agent-lab-{binding['replication_id']}-{provider}-live-route-probe"
        )
        if (
            set(entry)
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
            or provider not in probe_profiles
            or provider in seen
            or entry.get("config") != f"live-route-probes/{provider}.yaml"
            or entry.get("jobDir") != f"live-route-jobs/{provider}/{job_name}"
            or entry.get("compose")
            != f"overlays/relay.{provider}.live-route-probe.compose.yaml"
            or (entry.get("model"), entry.get("reasoning")) != probe_profiles[provider]
            or entry.get("task") != LIVE_ROUTE_PROBE_TASK
            or entry.get("relayImageSha256") != relay_images.get("production")
            or entry.get("limits") != dict(LIVE_ROUTE_PROBE_LIMITS)
            or config_path is None
            or compose_path is None
            or digest_bytes(_regular_bytes(config_path)) != entry.get("configSha256")
            or digest_bytes(_regular_bytes(compose_path)) != entry.get("composeSha256")
            or _yaml(_regular_bytes(config_path), "live-route probe config")
            != live_route_probe_config(
                run_dir,
                binding,
                provider,
                entry["model"],
                entry["reasoning"],
                compose_path,
                entry["composeSha256"],
            )
            or _safe_output_path(run_dir, entry.get("jobDir")) is None
        ):
            raise ValueError("live-route probe authority drifted")
        seen.add(provider)


def _validate_bound_preflight(
    run_dir: Path, binding: dict[str, Any], record: dict[str, Any]
) -> None:
    matches: list[dict[str, Any]] = []
    production = record.get("preflight")
    if isinstance(production, dict) and record.get("preflightSha256") == digest_bytes(
        canonical_json(production)
    ):
        matches.append(production)
    fixture_path = run_dir / "fixtures" / "preflight.json"
    if fixture_path.is_file() and not fixture_path.is_symlink():
        matches.append(_unique_json(_regular_bytes(fixture_path), "preflight"))
    matches = [
        candidate
        for candidate in matches
        if digest_bytes(canonical_json(candidate)) == binding["preflight_sha256"]
    ]
    if len(matches) != 1:
        raise ValueError("bound preflight is unavailable")
    preflight = matches[0]
    if (
        set(preflight) != PREFLIGHT_KEYS
        or preflight
        != {
            "schemaVersion": 1,
            "experimentId": binding["experiment_id"],
            "replicationId": binding["replication_id"],
            "sourceRevision": binding["source_revision"],
            "experimentManifestSha256": binding["experiment_manifest_sha256"],
            "relayBuildSha256": binding["relay_build_sha256"],
            "relayImageSha256": binding["relay_image_sha256"],
            "taskSnapshotsSha256": binding["task_snapshots_sha256"],
            "cleanTree": True,
            "createdAt": preflight.get("createdAt"),
        }
        or not isinstance(preflight["createdAt"], str)
        or not is_strict_int(preflight["schemaVersion"])
    ):
        raise ValueError("bound preflight drifted")


def _validate_prepared_source(
    binding: dict[str, Any], root: Path | None = None
) -> tuple[
    dict[str, Any],
    dict[str, str],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    root = (root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    try:
        configured = Path(os.environ["OPEN_AGENT_LAB_REPO_ROOT"]).resolve(strict=True)
        manifest_path = (
            root
            / "benchmarks"
            / "terminal_bench"
            / "verify-instruction-v1.experiment.json"
        )
        manifest_bytes = _regular_bytes(manifest_path)
        manifest = _unique_json(manifest_bytes, "experiment manifest")
        file_hashes = manifest["fileSha256"]
        build_ids = manifest["relayBuildIds"]
        runtime = manifest["runtime"]
        if not isinstance(runtime, dict):
            raise TypeError("runtime has an invalid schema")
        runtime_spec = validate_codex_runtime_spec(runtime.get("codexRuntime"))
        module = root / "benchmarks" / "terminal_bench" / "harbor_environment.py"
        contract = root / "benchmarks" / "terminal_bench" / "experiment_contract.py"
        runtime_module = root / "benchmarks" / "terminal_bench" / "codex_runtime.py"
        if (
            configured != root
            or not is_strict_int(manifest.get("schemaVersion"))
            or manifest.get("schemaVersion") != 2
            or manifest.get("experimentId") != binding["experiment_id"]
            or digest_bytes(manifest_bytes) != binding["experiment_manifest_sha256"]
            or not isinstance(file_hashes, dict)
            or file_hashes.get("benchmarks/terminal_bench/harbor_environment.py")
            != digest_bytes(_regular_bytes(module))
            or file_hashes.get("benchmarks/terminal_bench/experiment_contract.py")
            != digest_bytes(_regular_bytes(contract))
            or file_hashes.get("benchmarks/terminal_bench/codex_runtime.py")
            != digest_bytes(_regular_bytes(runtime_module))
            or not isinstance(build_ids, dict)
            or set(build_ids) != {"production", "providerFreeFixture"}
            or any(not is_digest(value) for value in build_ids.values())
            or len(set(build_ids.values())) != 2
            or binding["relay_build_sha256"] not in set(build_ids.values())
        ):
            raise ValueError("prepared source metadata drifted")
        runtime_receipt = verify_tree(
            root.parent / CODEX_RUNTIME_PREPARED_RELATIVE,
            runtime_spec,
        )
        record = _unique_json(
            _regular_bytes(root.parent / "run-record.json"), "run record"
        )
        task_runtime, task_snapshots = _task_authorities(manifest, record, binding)
        relay_images = record.get("relayImages")
        providers = record.get("providers")
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
            or record.get("schemaVersion") != 1
            or not isinstance(relay_images, dict)
            or set(relay_images) != {"production", "providerFreeFixture"}
            or any(not is_digest(value) for value in relay_images.values())
            or len(set(relay_images.values())) != 2
            or record.get("relayImageTags")
            != _relay_image_tags(root.parent, binding["source_revision"])
            or record.get("codexRuntime") != runtime_receipt
            or not isinstance(providers, list)
            or not isinstance(record.get("liveRouteProbes"), list)
        ):
            raise ValueError("run record drifted")
        production_binding = _binding_from_preflight(
            record.get("preflight"), record.get("preflightSha256")
        )
        if (
            production_binding["relay_build_sha256"] != build_ids["production"]
            or production_binding["relay_image_sha256"] != relay_images["production"]
            or any(
                production_binding[key] != binding[key]
                for key in RUN_BINDING_KEYS
                - {
                    "relay_build_sha256",
                    "relay_image_sha256",
                    "preflight_sha256",
                }
            )
        ):
            raise ValueError("production run binding drifted")
        _validate_bound_preflight(root.parent, production_binding, record)
        _validate_live_route_probe_authorities(
            root.parent,
            production_binding,
            manifest,
            record["liveRouteProbes"],
            relay_images,
        )
        _assert_runtime_gate(manifest, record, binding)
        _validate_bound_preflight(root.parent, binding, record)
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise RuntimeError("Prepared source identity could not be verified.") from error
    if (
        _git(root, "rev-parse", "--show-toplevel") != str(root)
        or _git(root, "rev-parse", "--verify", "HEAD^{commit}")
        != binding["source_revision"]
        or _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        raise RuntimeError("Prepared source drifted after the clean preflight.")
    return record, dict(file_hashes), task_runtime, task_snapshots


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(left))
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value))
    return result


def _expected_overlay(
    root: Path,
    provider: str,
    image: str,
    file_hashes: dict[str, str],
    *,
    fixture: bool = False,
    live_route_probe: bool = False,
) -> dict[str, Any]:
    if fixture and live_route_probe:
        raise RuntimeError("fixture and live-route policies are mutually exclusive")
    relative = f"benchmarks/terminal_bench/relay.{provider}.compose.yaml"
    data = _regular_bytes(root / relative)
    if digest_bytes(data) != file_hashes.get(relative):
        raise RuntimeError(f"Frozen {provider} relay Compose drifted.")
    document = _yaml(
        data,
        f"frozen {provider} relay Compose",
    )
    if fixture:
        fixture_relative = "benchmarks/terminal_bench/relay.fixture.compose.yaml"
        fixture_data = _regular_bytes(root / fixture_relative)
        if digest_bytes(fixture_data) != file_hashes.get(fixture_relative):
            raise RuntimeError("Frozen fixture relay Compose drifted.")
        document = _merge(
            document,
            _yaml(
                fixture_data,
                "frozen fixture relay Compose",
            ),
        )
    relay = document["services"][RELAY_SERVICE]
    relay.pop("build", None)
    relay["image"] = image
    relay["pull_policy"] = "never"
    if live_route_probe:
        relay["command"] = live_route_probe_relay_command(relay.get("command"))
        document = live_route_probe_networks(document)
    return document


def _safe_output_path(output: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str):
        return None
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    return Path(os.path.abspath(output / path))


def _validate_compose_authority(
    data: bytes,
    path: Path,
    digest: str,
    binding: dict[str, Any],
    record: dict[str, Any],
    root: Path,
    file_hashes: dict[str, str],
) -> str:
    output = root.parent
    absolute = Path(os.path.abspath(path))
    image = binding["relay_image_sha256"]
    candidates: list[tuple[str, dict[str, Any]]] = []
    for entry in record["providers"]:
        if not isinstance(entry, dict) or entry.get("provider") not in {
            "deepseek",
            "zai",
        }:
            continue
        if (
            _safe_output_path(output, entry.get("compose")) == absolute
            and entry.get("composeSha256") == digest
            and entry.get("relayImageSha256") == image
        ):
            candidates.append(
                (
                    "pilot",
                    _expected_overlay(root, entry["provider"], image, file_hashes),
                )
            )
    for entry in record["liveRouteProbes"]:
        if not isinstance(entry, dict) or entry.get("provider") not in {
            "deepseek",
            "zai",
        }:
            continue
        if (
            set(entry)
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
            or entry.get("task") != LIVE_ROUTE_PROBE_TASK
            or entry.get("limits") != dict(LIVE_ROUTE_PROBE_LIMITS)
        ):
            raise RuntimeError("live-route probe authority drifted")
        if (
            _safe_output_path(output, entry.get("compose")) == absolute
            and entry.get("composeSha256") == digest
            and entry.get("relayImageSha256") == image
        ):
            candidates.append(
                (
                    "live-route-probe",
                    _expected_overlay(
                        root,
                        entry["provider"],
                        image,
                        file_hashes,
                        live_route_probe=True,
                    ),
                )
            )
    fixture = output / "overlays" / "relay.fixture.compose.yaml"
    if (
        absolute == fixture
        and record["relayImages"].get("providerFreeFixture") == image
        and digest_bytes(_regular_bytes(fixture)) == digest
    ):
        candidates.append(
            (
                "fixture",
                _expected_overlay(root, "deepseek", image, file_hashes, fixture=True),
            )
        )
    if len(candidates) != 1 or _yaml(data, "pinned relay Compose") != candidates[0][1]:
        raise RuntimeError(
            "Pinned relay Compose is not authorized by the prepared run."
        )
    return candidates[0][0]


def _validated_compose_bytes(
    path: Path, digest: str, run_binding: dict[str, Any]
) -> bytes:
    if not is_digest(digest):
        raise ValueError("Pinned relay environment binding is invalid.")
    data = _regular_bytes(path)
    if digest_bytes(data) != digest:
        raise ValueError("Pinned relay Compose digest drifted.")
    document = _yaml(data, "pinned relay Compose")
    _reject_external_references(document)
    services = document.get("services")
    secrets = document.get("secrets")
    networks = document.get("networks")
    main = services.get("main") if isinstance(services, dict) else None
    relay = services.get(RELAY_SERVICE) if isinstance(services, dict) else None
    probe_networks = {
        LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {"internal": True},
        LIVE_ROUTE_PROBE_EGRESS_NETWORK: {"internal": False},
    }
    probe_topology = (
        networks == probe_networks
        and isinstance(main, dict)
        and main.get("networks") == {LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {}}
        and isinstance(relay, dict)
        and relay.get("networks")
        == {
            LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {"aliases": [RELAY_SERVICE]},
            LIVE_ROUTE_PROBE_EGRESS_NETWORK: {},
        }
    )
    plain_topology = (
        networks is None
        and isinstance(main, dict)
        and "networks" not in main
        and isinstance(relay, dict)
        and "networks" not in relay
    )
    if (
        set(document)
        not in ({"services", "secrets"}, {"services", "secrets", "networks"})
        or not isinstance(services, dict)
        or set(services) != {"main", RELAY_SERVICE}
        or not isinstance(main, dict)
        or not isinstance(relay, dict)
        or "network_mode" in main
        or "network_mode" in relay
        or not (plain_topology or probe_topology)
        or "build" in relay
        or relay.get("image") != run_binding["relay_image_sha256"]
        or relay.get("pull_policy") != "never"
        or not isinstance(secrets, dict)
        or set(secrets) != {_SECRET}
    ):
        raise ValueError("Pinned relay Compose can only use the bound image.")
    return data


def _relay_only(data: bytes) -> bytes:
    document = _yaml(data, "pinned relay Compose")
    result = {
        "services": {RELAY_SERVICE: document["services"][RELAY_SERVICE]},
        "secrets": {_SECRET: document["secrets"][_SECRET]},
    }
    if "networks" in document:
        result["networks"] = document["networks"]
    return canonical_json(result)


def _secret_source(secret: dict[str, Any], *, durable: bool = False) -> Path:
    source = secret.get("file")
    if not isinstance(source, str) or not source:
        raise ValueError("Resolved provider secret must be file-backed.")
    raw = Path(source).expanduser()
    if not durable:
        return raw.resolve(strict=False)
    absolute = Path(os.path.abspath(raw))
    try:
        resolved = absolute.resolve(strict=True)
        metadata = absolute.lstat()
        parent_metadata = absolute.parent.lstat()
        ancestor_metadata = [parent.lstat() for parent in absolute.parent.parents]
    except OSError as error:
        raise ValueError("Live provider secret is unavailable.") from error
    harbor_gid = os.getegid()
    if (
        sys.platform != "linux"
        or not raw.is_absolute()
        or resolved != absolute
        or harbor_gid == _RELAY_RUNTIME_GID
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != harbor_gid
        or stat.S_IMODE(metadata.st_mode) != 0o440
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= _MAX_CREDENTIAL_BYTES
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != harbor_gid
        or stat.S_IMODE(parent_metadata.st_mode) != 0o750
        or any(
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            for info in ancestor_metadata
        )
    ):
        raise ValueError(
            "Live provider secret must be root:<Harbor gid> 0440 beneath a "
            "root:<Harbor gid> 0750 directory; Harbor gid 1000 is forbidden."
        )
    return resolved


def _credential_identity(path: Path) -> tuple[int, int, int, int, str]:
    path = _secret_source({"file": str(path)}, durable=True)
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        info = os.fstat(descriptor)
        data = os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) != 0o440
            or info.st_nlink != 1
            or not 0 < len(data) <= _MAX_CREDENTIAL_BYTES
            or len(data) != info.st_size
        ):
            raise ValueError("Live provider secret changed or exceeds its bound.")
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            digest_bytes(data),
        )
    finally:
        os.close(descriptor)


def _path_exposes(secret: Path, source: Any) -> bool:
    if not isinstance(source, str) or not source:
        return False
    try:
        secret.relative_to(Path(source).expanduser().resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _has_docker_socket(value: Any) -> bool:
    if isinstance(value, str):
        return bool(re.search(r"(?:^|/)docker\.sock(?:$|[/:])", value))
    if isinstance(value, dict):
        return any(_has_docker_socket(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_docker_socket(item) for item in value)
    return False


def _daemon_sockets(environment: dict[str, str]) -> tuple[Path, ...]:
    paths = [Path("/var/run/docker.sock"), Path("/run/docker.sock")]
    host = environment.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        paths.append(Path(host.removeprefix("unix://")))
    if runtime := environment.get("XDG_RUNTIME_DIR"):
        paths.append(Path(runtime) / "docker.sock")
    if home := environment.get("HOME"):
        paths.append(Path(home) / ".docker" / "run" / "docker.sock")
        if "DOCKER_CONFIG" not in environment:
            paths.append(Path(home) / ".docker")
    for key in ("DOCKER_CONFIG", "DOCKER_CERT_PATH"):
        if value := environment.get(key):
            paths.append(Path(value))
    return tuple({path.expanduser().resolve(strict=False) for path in paths})


def _resolved_relay(
    actual: dict[str, Any],
    expected: dict[str, Any],
    binding: dict[str, Any],
    *,
    durable_secret: bool = False,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    services = actual.get("services")
    expected_services = expected.get("services")
    secrets = actual.get("secrets")
    expected_secrets = expected.get("secrets")
    relay = services.get(RELAY_SERVICE) if isinstance(services, dict) else None
    expected_relay = (
        expected_services.get(RELAY_SERVICE)
        if isinstance(expected_services, dict)
        else None
    )
    secret = secrets.get(_SECRET) if isinstance(secrets, dict) else None
    expected_secret = (
        expected_secrets.get(_SECRET) if isinstance(expected_secrets, dict) else None
    )
    networks = actual.get("networks", {})
    expected_networks = expected.get("networks", {})
    if (
        not isinstance(services, dict)
        or not isinstance(expected_services, dict)
        or not isinstance(relay, dict)
        or not isinstance(expected_relay, dict)
        or relay != expected_relay
        or not isinstance(secrets, dict)
        or not isinstance(expected_secrets, dict)
        or not isinstance(secret, dict)
        or not isinstance(expected_secret, dict)
        or secret != expected_secret
        or set(secrets) != {_SECRET}
        or not isinstance(networks, dict)
        or not isinstance(expected_networks, dict)
        or networks != expected_networks
        or relay.get("image") != binding["relay_image_sha256"]
        or _has_docker_socket(actual)
    ):
        raise ValueError("Resolved relay Compose graph drifted.")
    volumes = actual.get("volumes", {})
    configs = actual.get("configs", {})
    if not isinstance(volumes, dict) or not isinstance(configs, dict) or configs:
        raise TypeError("Resolved top-level mounts are invalid.")
    secret_path = _secret_source(secret, durable=durable_secret)
    return services, secret_path, volumes


def _build_contexts(build: dict[str, Any]) -> list[str]:
    contexts = [build.get("context")]
    additional = build.get("additional_contexts", {})
    if isinstance(additional, dict):
        contexts.extend(additional.values())
    elif isinstance(additional, list):
        contexts.extend(
            item.split("=", 1)[1]
            for item in additional
            if isinstance(item, str) and "=" in item
        )
    return [item for item in contexts if isinstance(item, str)]


def _build_host_paths(build: dict[str, Any]) -> list[str]:
    paths = _build_contexts(build)
    ssh = build.get("ssh", [])
    if isinstance(ssh, dict):
        paths.extend(value for value in ssh.values() if isinstance(value, str))
    elif isinstance(ssh, list):
        paths.extend(
            item.split("=", 1)[1]
            for item in ssh
            if isinstance(item, str) and "=" in item
        )
    for key in ("cache_from", "cache_to"):
        caches = build.get(key, [])
        if not isinstance(caches, list):
            continue
        for cache in caches:
            if isinstance(cache, dict) and cache.get("type") == "local":
                paths.extend(
                    value
                    for name, value in cache.items()
                    if name in {"src", "dest"} and isinstance(value, str)
                )
            elif isinstance(cache, str) and cache.startswith("type=local,"):
                paths.extend(
                    field.split("=", 1)[1]
                    for field in cache.split(",")
                    if field.startswith(("src=", "dest="))
                )
    return paths


def _assert_safe_build(service: dict[str, Any], secret_path: Path) -> None:
    build = service.get("build")
    if build is None:
        return
    if isinstance(build, str):
        build = {"context": build}
    if (
        not isinstance(build, dict)
        or "dockerfile_inline" in build
        or build.get("privileged") is True
        or bool(build.get("entitlements"))
    ):
        raise ValueError("A service has an unsafe build definition.")
    contexts = _build_host_paths(build)
    context = build.get("context")
    if not isinstance(context, str) or any(
        _path_exposes(secret_path, item)
        for item in contexts
        if "://" not in item and not item.startswith("service:")
    ):
        raise ValueError("A build context exposes the provider secret.")
    root = Path(context).expanduser().resolve(strict=False)
    dockerfile = build.get("dockerfile")
    if isinstance(dockerfile, str):
        candidate = Path(dockerfile).expanduser()
        candidate = (
            (root / candidate).resolve(strict=False)
            if not candidate.is_absolute()
            else candidate.resolve(strict=False)
        )
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("A Dockerfile escapes its build context.") from error
    build_secrets = build.get("secrets", [])
    if not isinstance(build_secrets, list) or build_secrets:
        raise ValueError("Build secrets are forbidden.")


def _within(root: Path, candidate: Any) -> bool:
    if not isinstance(candidate, str) or not candidate or "://" in candidate:
        return False
    try:
        Path(candidate).expanduser().resolve(strict=False).relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _assert_build_is_task_local(service: dict[str, Any], root: Path) -> None:
    build = service.get("build")
    if build is None:
        return
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        raise TypeError("A service has an unsafe build definition.")
    if any(not _within(root, path) for path in _build_contexts(build)) or any(
        build.get(key) for key in ("ssh", "cache_from", "cache_to", "configs")
    ):
        raise ValueError("Build inputs must stay inside the task environment.")


def _contains_forbidden(value: Any, forbidden: frozenset[str]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in forbidden)
    if isinstance(value, dict):
        return any(_contains_forbidden(item, forbidden) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden(item, forbidden) for item in value)
    return False


def _assert_safe_namespace(service: dict[str, Any]) -> None:
    pid = service.get("pid")
    ipc = service.get("ipc")
    caps = service.get("cap_add", [])
    if (
        service.get("privileged") is True
        or (
            isinstance(pid, str)
            and (pid == "host" or pid.startswith(("container:", "service:")))
        )
        or (
            isinstance(ipc, str)
            and (
                ipc in {"host", "shareable"}
                or ipc.startswith(("container:", "service:"))
            )
        )
        or not isinstance(caps, list)
        or {str(cap).upper().removeprefix("CAP_") for cap in caps}
        & {"ALL", "SYS_ADMIN", "SYS_PTRACE"}
        or service.get("use_api_socket") is True
        or bool(service.get("devices"))
        or bool(service.get("device_cgroup_rules"))
        or service.get("network_mode") == "host"
        or service.get("uts") == "host"
        or service.get("userns_mode") == "host"
        or service.get("cgroup") == "host"
        or any(
            service.get(key)
            for key in (
                "cgroup_parent",
                "runtime",
                "security_opt",
                "provider",
                "pre_start",
                "post_start",
                "pre_stop",
            )
        )
    ):
        raise ValueError("A service requests a dangerous container namespace.")


def _bind_identity(volume: Any) -> tuple[Path, str, bool] | None:
    if not isinstance(volume, dict) or volume.get("type") != "bind":
        return None
    source = volume.get("source")
    target = volume.get("target")
    if not isinstance(source, str) or not isinstance(target, str):
        raise TypeError("Resolved bind mount is invalid.")
    return (
        Path(source).expanduser().resolve(strict=False),
        target,
        bool(volume.get("read_only")),
    )


def _allowed_main_binds(data: bytes) -> frozenset[tuple[Path, str, bool]]:
    document = _unique_json(data, "Harbor mounts Compose")
    try:
        main = document["services"]["main"]
        volumes = main["volumes"]
    except (KeyError, TypeError) as error:
        raise ValueError("Harbor mounts Compose is invalid.") from error
    if (
        set(document) != {"services"}
        or set(document["services"]) != {"main"}
        or set(main) != {"volumes"}
        or not isinstance(volumes, list)
    ):
        raise ValueError("Harbor mounts Compose is invalid.")
    identities = [_bind_identity(volume) for volume in volumes]
    if any(identity is None for identity in identities):
        raise ValueError("Only Harbor bind mounts are allowed.")
    return frozenset(identity for identity in identities if identity is not None)


def _assert_safe_mounts(
    name: str,
    service: dict[str, Any],
    secret_path: Path,
    volumes: dict[str, Any],
    protected_paths: tuple[Path, ...],
    allowed_binds: frozenset[tuple[Path, str, bool]],
) -> None:
    attached = service.get("secrets", [])
    configs = service.get("configs", [])
    if not isinstance(attached, list) or not isinstance(configs, list):
        raise TypeError("Resolved service secrets or configs are invalid.")
    if attached or configs:
        raise ValueError("Service secrets and configs are forbidden outside the relay.")
    service_volumes = service.get("volumes", [])
    if not isinstance(service_volumes, list):
        raise TypeError("Resolved service volumes are invalid.")
    for volume in service_volumes:
        if not isinstance(volume, dict):
            raise TypeError("Resolved service volume is invalid.")
        source = volume.get("source")
        definition = volumes.get(source, {})
        options = (
            definition.get("driver_opts", {})
            if volume.get("type") == "volume" and isinstance(definition, dict)
            else {}
        )
        targets = (secret_path, *protected_paths)
        identity = _bind_identity(volume)
        if identity is not None and (name != "main" or identity not in allowed_binds):
            raise ValueError("A service requests an unauthorized host bind mount.")
        if volume.get("type") not in {"bind", "volume", "tmpfs"}:
            raise ValueError("A service requests an unsafe volume type.")
        if isinstance(options, dict) and options:
            raise ValueError("A named volume requests a host-backed driver.")
        if (
            volume.get("type") == "bind"
            and any(_path_exposes(path, source) for path in targets)
        ) or (
            isinstance(options, dict)
            and any(_path_exposes(path, options.get("device")) for path in targets)
        ):
            raise ValueError("A volume exposes a protected host path.")


def _assert_service_isolated(
    name: str,
    service: dict[str, Any],
    secret_path: Path,
    volumes: dict[str, Any],
    forbidden: frozenset[str],
    protected_paths: tuple[Path, ...],
    allowed_binds: frozenset[tuple[Path, str, bool]],
    build_root: Path | None,
) -> None:
    environment = service.get("environment", {})
    if not isinstance(environment, dict) or any(
        _SECRET_ENV.search(str(key)) for key in environment
    ):
        raise ValueError("A service requests a sensitive environment variable.")
    if _contains_forbidden(service, forbidden):
        raise ValueError("An ambient secret reached a service definition.")
    _assert_safe_namespace(service)
    _assert_safe_build(service, secret_path)
    if build_root is None and service.get("build") is not None:
        raise ValueError("Build inputs are not authorized.")
    if build_root is not None:
        _assert_build_is_task_local(service, build_root)
    _assert_safe_mounts(
        name, service, secret_path, volumes, protected_paths, allowed_binds
    )
    volumes_from = service.get("volumes_from", [])
    if not isinstance(volumes_from, list):
        raise TypeError("Resolved service volumes_from is invalid.")
    if volumes_from:
        raise ValueError("Service volume inheritance is forbidden.")
    if any(
        service.get(key) == f"service:{RELAY_SERVICE}"
        for key in ("network_mode", "pid", "ipc")
    ):
        raise ValueError("A service joins the relay namespace.")


def _assert_resolved_graph(
    actual: dict[str, Any],
    expected: dict[str, Any],
    binding: dict[str, Any],
    forbidden: frozenset[str] = frozenset(),
    protected_paths: tuple[Path, ...] = (),
    allowed_binds: frozenset[tuple[Path, str, bool]] = frozenset(),
    build_root: Path | None = None,
    durable_secret: bool = False,
) -> Path:
    protected_paths = protected_paths or _daemon_sockets({})
    services, secret_path, volumes = _resolved_relay(
        actual, expected, binding, durable_secret=durable_secret
    )
    network_names = set(actual.get("networks", {}))
    probe_networks = {
        LIVE_ROUTE_PROBE_INTERNAL_NETWORK,
        LIVE_ROUTE_PROBE_EGRESS_NETWORK,
    }
    if network_names & probe_networks:
        main = services.get("main")
        relay = services.get(RELAY_SERVICE)
        main_networks = main.get("networks") if isinstance(main, dict) else None
        relay_networks = relay.get("networks") if isinstance(relay, dict) else None
        if (
            network_names != probe_networks
            or not isinstance(main_networks, dict)
            or set(main_networks) != {LIVE_ROUTE_PROBE_INTERNAL_NETWORK}
            or not isinstance(relay_networks, dict)
            or set(relay_networks) != probe_networks
        ):
            raise ValueError("Live-route probe network isolation drifted.")
    for name, service in services.items():
        if name == RELAY_SERVICE or not isinstance(service, dict):
            continue
        _assert_service_isolated(
            name,
            service,
            secret_path,
            volumes,
            forbidden,
            protected_paths,
            allowed_binds,
            build_root,
        )
    return secret_path


def _sanitized_compose_env(environment: dict[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in environment.items()
        if not key.startswith("COMPOSE_")
    }
    result["COMPOSE_DISABLE_ENV_FILE"] = "1"
    return result


def _minimal_compose_env(
    infrastructure: dict[str, str], overlay: bytes, ambient: dict[str, str]
) -> dict[str, str]:
    result = {key: ambient[key] for key in _PROCESS_ENV if key in ambient}
    result.update(infrastructure)
    text = overlay.decode()
    provider_files = [key for key in _PROVIDER_FILES if f"${{{key}" in text]
    if len(provider_files) != 1 or provider_files[0] not in ambient:
        raise RuntimeError("The relay credential file environment is invalid.")
    result[provider_files[0]] = ambient[provider_files[0]]
    return _sanitized_compose_env(result)


def _forbidden_values(environment: dict[str, str]) -> frozenset[str]:
    return frozenset(
        value
        for key, value in environment.items()
        if len(value) >= 8 and (_SECRET_ENV.search(key) or key in _DOCKER_AUTH_ENV)
    )


def _strip_local_rmi(command: list[str]) -> list[str]:
    if not command or command[0] != "down":
        return list(command)
    result = [command[0]]
    index = 1
    while index < len(command):
        if command[index : index + 2] == ["--rmi", "local"]:
            index += 2
        else:
            result.append(command[index])
            index += 1
    return result


class PinnedRelayDockerEnvironment(DockerEnvironment):
    """Run Harbor against one immutable, source-bound Compose graph."""

    def __init__(
        self,
        *args: Any,
        relay_compose_sha256: str,
        run_binding: dict[str, Any],
        mounts: list[dict[str, Any]] | None = None,
        extra_docker_compose: Sequence[Path | str] | None = None,
        keep_containers: bool = False,
        **kwargs: Any,
    ) -> None:
        if keep_containers:
            raise ValueError("Sealed provider runs cannot retain containers.")
        binding = _validated_run_binding(run_binding)
        root = Path(__file__).resolve().parents[2]
        record, file_hashes, task_runtime, task_snapshots = _validate_prepared_source(
            binding, root
        )
        authorized_mounts = _validated_mounts(
            mounts, root.parent, kwargs.get("trial_paths")
        )
        paths = list(extra_docker_compose or [])
        if len(paths) != 1:
            raise ValueError("Pinned relay environment binding is invalid.")
        data = _validated_compose_bytes(Path(paths[0]), relay_compose_sha256, binding)
        relay_role = _validate_compose_authority(
            data,
            Path(paths[0]),
            relay_compose_sha256,
            binding,
            record,
            root,
            file_hashes,
        )
        seed = _sealed_memfd(data)  # Linux gate, before Harbor can invoke Docker.
        self._run_binding = binding
        self._relay_role = relay_role
        self._relay_compose_bytes = data
        self._seed_fd = seed
        self._source_fds: list[int] = []
        self._full_fd = -1
        self._full_compose_sha256: str | None = None
        self._provider_secret_path: Path | None = None
        self._provider_credential_identity: tuple[int, int, int, int, str] | None = None
        self._freeze_lock = asyncio.Lock()
        self._compose_environment: dict[str, str] | None = None
        self._task_runtime: tuple[str, Path, dict[str, str], dict[str, str]] | None = (
            None
        )
        self._closed = False
        try:
            super().__init__(
                *args,
                mounts=authorized_mounts,
                extra_docker_compose=[],
                keep_containers=False,
                **kwargs,
            )
            self._task_runtime = _live_task_authority(
                root.parent,
                self.environment_dir.parent,
                binding["relay_image_sha256"],
                record["relayImages"],
                task_runtime,
                task_snapshots,
                relay_role,
            )
        except BaseException:
            os.close(seed)
            self._seed_fd = -1
            raise

    @property
    @override
    def _uses_compose(self) -> bool:
        return True

    def _source_paths(self) -> list[Path]:
        paths = super()._docker_compose_paths
        dynamic = {
            path
            for path in (
                self._env_compose_path,
                self._mounts_compose_path,
                self._DOCKER_COMPOSE_EGRESS_CONTROL_PATH
                if self._enable_egress_control
                else None,
                self._egress_control_services_compose_path,
            )
            if path is not None
        }
        index = next(
            (position for position, path in enumerate(paths) if path in dynamic),
            len(paths),
        )
        paths.insert(index, _memfd_path(self._seed_fd))
        return paths

    @property
    @override
    def _docker_compose_paths(self) -> list[Path]:
        return (
            [_memfd_path(self._full_fd)] if self._full_fd >= 0 else self._source_paths()
        )

    def _base_command(self, paths: Sequence[Path]) -> list[str]:
        command = [
            "docker",
            "compose",
            "--project-name",
            _sanitize_docker_compose_project_name(self.session_id),
            "--project-directory",
            str(self.environment_dir.resolve().absolute()),
        ]
        for path in paths:
            command.extend(["-f", str(path)])
        return command

    async def _capture(self, command: list[str], timeout: int = 60) -> bytes:
        assert self._compose_environment is not None
        process = await asyncio.create_subprocess_exec(
            *command,
            env=self._compose_environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            await self._terminate_process(process)
            raise
        if process.returncode:
            detail = stderr.decode(errors="replace")[-1000:]
            raise RuntimeError(f"Docker command failed: {detail}")
        return stdout

    async def _render(self, descriptors: Sequence[int]) -> dict[str, Any]:
        paths = [_memfd_path(descriptor) for descriptor in descriptors]
        output = await self._capture(
            [*self._base_command(paths), "config", "--format", "json"]
        )
        return _unique_json(output, "resolved Compose graph")

    async def _validate_local_relay_image(self) -> None:
        image = self._run_binding["relay_image_sha256"]
        observed = (
            (
                await self._capture(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", image]
                )
            )
            .decode(errors="replace")
            .strip()
        )
        if observed != image:
            raise RuntimeError("The bound relay image is not present locally.")
        build = (
            (
                await self._capture(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "cat",
                        image,
                        RELAY_BUILD_ID_PATH,
                    ]
                )
            )
            .decode(errors="replace")
            .strip()
        )
        if build != self._run_binding["relay_build_sha256"]:
            raise RuntimeError("The bound relay image has the wrong build identity.")

    async def _pin_task_runtime(self, graph: dict[str, Any]) -> None:
        if self._task_runtime is None:
            return
        _, _, authority, _ = self._task_runtime
        await self._recheck_task_snapshot()
        services = graph.get("services")
        main = services.get("main") if isinstance(services, dict) else None
        if (
            not isinstance(main, dict)
            or set(services) != {"main", RELAY_SERVICE}
            or "build" in main
            or main.get("image") != authority["declaredImage"]
        ):
            raise RuntimeError(
                "The selected task did not resolve to its declared image."
            )
        immutable = authority["immutableImage"]
        platform = authority["platform"]
        expected_image_ids = {
            authority["imageConfigDigest"],
            immutable.rsplit("@", 1)[-1],
        }
        cached = set(
            (
                await self._capture(
                    [
                        "docker",
                        "image",
                        "ls",
                        "--quiet",
                        "--no-trunc",
                        "--filter",
                        f"reference={immutable}",
                    ]
                )
            )
            .decode(errors="replace")
            .split()
        )
        if cached.isdisjoint(expected_image_ids):
            await self._capture(
                ["docker", "pull", "--quiet", "--platform", platform, immutable],
                timeout=900,
            )
        identity = (
            (
                await self._capture(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}|{{.Os}}|{{.Architecture}}",
                        immutable,
                    ]
                )
            )
            .decode(errors="replace")
            .strip()
        )
        os_name, architecture = platform.split("/", 1)
        if identity not in {
            f"{image_id}|{os_name}|{architecture}" for image_id in expected_image_ids
        }:
            raise RuntimeError("The immutable task image has the wrong identity.")
        main["image"] = immutable
        main["platform"] = platform
        main["pull_policy"] = "never"

    async def _recheck_task_snapshot(self) -> tuple[str | None, str | None, str | None]:
        if self._task_runtime is None:
            return None, None, None
        task, task_dir, _, snapshot = self._task_runtime
        observed_digest, observed_checksum = await asyncio.to_thread(
            _task_hashes, task_dir
        )
        if (
            observed_digest != snapshot["taskDigest"]
            or observed_checksum != snapshot["taskChecksum"]
        ):
            raise RuntimeError(f"Prepared task snapshot drifted: {task}")
        return task, observed_digest, observed_checksum

    @override
    async def service_download_file(
        self,
        source_path: str,
        target_path: Path | str,
        *,
        service: str | None = None,
    ) -> None:
        # Docker's archive API cannot see runtime files stored on tmpfs.
        limit = (
            RELAY_ARTIFACT_LIMITS.get(source_path) if service == RELAY_SERVICE else None
        )
        if limit is None:
            await super().service_download_file(
                source_path, target_path, service=service
            )
            return
        target = Path(os.path.abspath(target_path))
        expected = Path(
            os.path.abspath(self.trial_paths.artifacts_dir / Path(source_path).name)
        )
        if target != expected:
            raise RuntimeError("Relay evidence destination is not authorized.")
        result = await self.service_exec(
            f"test -f {source_path} && test ! -L {source_path} && "
            f'bytes=$(wc -c < {source_path}) && [ "$bytes" -le {limit} ] '
            f"&& base64 -w0 {source_path}",
            service=RELAY_SERVICE,
            timeout_sec=10,
            user="1000",
        )
        if result.return_code != 0:
            raise RuntimeError("Relay evidence export failed.")
        try:
            data = base64.b64decode((result.stdout or "").strip(), validate=True)
        except ValueError as error:
            raise RuntimeError("Relay evidence export was not valid base64.") from error
        if len(data) > limit:
            raise RuntimeError("Relay evidence export exceeded its size limit.")
        _publish_new_bytes(target, data)

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        guarded = False
        if self._task_runtime is not None:
            task_dir = Path(os.path.abspath(self._task_runtime[1]))
            source = Path(os.path.abspath(source_dir))
            guarded = source == task_dir or source.is_relative_to(task_dir)
        if guarded:
            await self._recheck_task_snapshot()
        try:
            await super().upload_dir(source_dir, target_dir)
        finally:
            if guarded:
                await self._recheck_task_snapshot()

    @override
    async def _validate_image_os(self, image_name: str) -> None:
        # For live tasks, freezing performs a stronger digest/config/platform
        # check before Harbor reaches its best-effort tag-based OS check.
        await self._freeze_compose()
        if self._task_runtime is None:
            await super()._validate_image_os(image_name)

    def _seal_sources(self) -> frozenset[tuple[Path, str, bool]]:
        allowed_binds: frozenset[tuple[Path, str, bool]] = frozenset()
        for path in self._source_paths():
            data = (
                self._relay_compose_bytes
                if path == _memfd_path(self._seed_fd)
                else _regular_bytes(path)
            )
            document = _yaml(data, f"Compose input {path.name}")
            _reject_external_references(document)
            if path == self._mounts_compose_path:
                allowed_binds = _allowed_main_binds(data)
            self._source_fds.append(_sealed_memfd(data))
        return allowed_binds

    async def _freeze_compose(self) -> None:
        if self._full_fd >= 0:
            return
        async with self._freeze_lock:
            if self._full_fd >= 0:
                return
            if self._env_compose_path is None or self._mounts_compose_path is None:
                raise RuntimeError("Harbor dynamic Compose inputs are not ready.")
            ambient = dict(os.environ)
            forbidden = _forbidden_values(ambient)
            self._compose_environment = _minimal_compose_env(
                self._compose_infra_env_vars(),
                self._relay_compose_bytes,
                ambient,
            )
            try:
                allowed_binds = self._seal_sources()
                expected_fd = _sealed_memfd(_relay_only(self._relay_compose_bytes))
                try:
                    actual = await self._render(self._source_fds)
                    expected = await self._render([expected_fd])
                finally:
                    os.close(expected_fd)
                secret_path = _assert_resolved_graph(
                    actual,
                    expected,
                    self._run_binding,
                    forbidden,
                    _daemon_sockets(ambient),
                    allowed_binds,
                    self.environment_dir.resolve(strict=True),
                    durable_secret=self._relay_role != "fixture",
                )
                if self._relay_role != "fixture":
                    self._provider_secret_path = secret_path
                    self._provider_credential_identity = _credential_identity(
                        secret_path
                    )
                await self._validate_local_relay_image()
                await self._pin_task_runtime(actual)
                full_compose = canonical_json(actual)
                self._full_fd = _sealed_memfd(full_compose)
                self._full_compose_sha256 = digest_bytes(full_compose)
                replayed = await self._render([self._full_fd])
                if replayed != actual:
                    raise RuntimeError("Resolved Compose graph is not replay-stable.")
                for descriptor in self._source_fds:
                    os.close(descriptor)
                self._source_fds.clear()
                os.close(self._seed_fd)
                self._seed_fd = -1
            except BaseException:
                if self._full_fd >= 0:
                    os.close(self._full_fd)
                    self._full_fd = -1
                self._full_compose_sha256 = None
                for descriptor in self._source_fds:
                    os.close(descriptor)
                self._source_fds.clear()
                raise

    @override
    async def _run_docker_compose_command(
        self,
        command: list[str],
        check: bool = True,
        timeout_sec: int | None = None,
        stdin_data: bytes | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        await self._freeze_compose()
        assert self._compose_environment is not None
        command = _strip_local_rmi(command)
        if (
            self._relay_role != "fixture"
            and command
            and command[0] in {"create", "run", "start", "up"}
            and (
                self._provider_secret_path is None
                or self._provider_credential_identity is None
                or _credential_identity(self._provider_secret_path)
                != self._provider_credential_identity
            )
        ):
            raise RuntimeError("Live provider secret changed before container start.")
        full_command = [
            *self._base_command([_memfd_path(self._full_fd)]),
            *command,
        ]
        process = await asyncio.create_subprocess_exec(
            *full_command,
            env=self._compose_environment,
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_data is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        collector = (
            self._collect_streamed_output
            if on_output is not None
            else self._collect_buffered_output
        )
        options: dict[str, Any] = {
            "timeout_sec": timeout_sec,
            "stdin_data": stdin_data,
        }
        if on_output is not None:
            options["on_output"] = on_output
        result = await collector(process, **options)
        if check and result.return_code != 0:
            raise RuntimeError(
                f"Docker compose failed for {self.environment_name}: "
                f"return code {result.return_code}; output {result.stdout}"
            )
        return result

    @override
    async def attach(self) -> None:
        raise RuntimeError("Interactive attach is unavailable for sealed Compose runs.")

    async def _assert_project_empty(self) -> None:
        project = _sanitize_docker_compose_project_name(self.session_id)
        containers = await self._capture(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
        )
        if containers.strip():
            raise RuntimeError("Compose cleanup left project containers behind.")

    async def _remove_project(self, command: list[str]) -> None:
        failure: Exception | None = None
        for _ in range(_CLEANUP_ATTEMPTS):
            try:
                await asyncio.wait_for(
                    self._run_docker_compose_command(
                        command, timeout_sec=_CLEANUP_TIMEOUT_SECONDS
                    ),
                    timeout=_CLEANUP_TIMEOUT_SECONDS,
                )
                await self._assert_project_empty()
                return
            except (OSError, RuntimeError, TimeoutError) as error:
                failure = error
        raise RuntimeError(
            f"Sealed Compose cleanup failed after {_CLEANUP_ATTEMPTS} attempts: "
            f"{failure}"
        ) from failure

    def _cleanup_receipt(
        self, task_identity: tuple[str | None, str | None, str | None]
    ) -> dict[str, Any]:
        full_compose = self._full_compose_sha256
        if not is_digest(full_compose):
            raise RuntimeError("The sealed Compose identity is unavailable.")
        binding = self._run_binding
        credential = self._provider_credential_identity
        task, task_digest, task_checksum = task_identity
        return {
            "schemaVersion": 1,
            "experimentId": binding["experiment_id"],
            "replicationId": binding["replication_id"],
            "sourceRevision": binding["source_revision"],
            "experimentManifestSha256": binding["experiment_manifest_sha256"],
            "preflightSha256": binding["preflight_sha256"],
            "runBindingSha256": digest_bytes(canonical_json(binding)),
            "relayImageSha256": binding["relay_image_sha256"],
            "providerCredentialSha256": credential[4] if credential else None,
            "fullComposeSha256": full_compose,
            "taskId": task,
            "taskDigest": task_digest,
            "taskChecksum": task_checksum,
            "sessionId": self.session_id,
            "projectName": _sanitize_docker_compose_project_name(self.session_id),
            "stoppedAt": _utc_now(),
        }

    @override
    async def stop(self, delete: bool) -> None:
        if self._closed:
            return
        log_failure: BaseException | None = None
        try:
            await asyncio.wait_for(
                self.prepare_logs_for_host(), timeout=_LOG_EXPORT_TIMEOUT_SECONDS
            )
        except BaseException as error:  # noqa: BLE001 - cleanup must still run
            # Harbor suppresses stop failures. Log export must therefore never
            # prevent the security-critical project removal below.
            log_failure = error
        if self._keep_containers and delete:
            self.logger.debug(
                "Both keep_containers and delete are set; keeping containers."
            )
        command = (
            ["stop"]
            if self._keep_containers
            else ["down", "--rmi", "local", "--volumes", "--remove-orphans"]
            if delete
            else ["down"]
        )
        # Harbor may suppress stop errors. Complete and attest cleanup here while
        # the exact sealed graph remains replayable.
        await self._remove_project(command)
        task_identity = await self._recheck_task_snapshot()
        self._cleanup_mounts_compose_file()
        self._cleanup_resources_compose_file()
        self._cleanup_env_compose_file()
        self._cleanup_egress_control_services_compose_file()
        _write_cleanup_receipt(
            self.trial_paths.trial_dir / _CLEANUP_RECEIPT,
            self._cleanup_receipt(task_identity),
        )
        for descriptor in [self._full_fd, *self._source_fds, self._seed_fd]:
            if descriptor >= 0:
                os.close(descriptor)
        self._full_fd = -1
        self._full_compose_sha256 = None
        self._source_fds.clear()
        self._seed_fd = -1
        self._closed = True
        if log_failure is not None:
            raise RuntimeError(
                "Logs could not be prepared before cleanup."
            ) from log_failure
