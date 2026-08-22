import asyncio
import base64
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml
from harbor.environments.docker.docker import DockerEnvironment

from benchmarks.terminal_bench.codex_runtime import (
    CODEX_RUNTIME_INSTALL_ROOT,
    CODEX_RUNTIME_PREPARED_RELATIVE,
    codex_runtime_spec,
)
from benchmarks.terminal_bench.experiment_contract import (
    EXPERIMENT_ID,
    canonical_json,
    digest_bytes,
)
from benchmarks.terminal_bench.harbor_environment import (
    PinnedRelayDockerEnvironment,
    _allowed_main_binds,
    _assert_resolved_graph,
    _assert_runtime_gate,
    _live_task_authority,
    _memfd_path,
    _minimal_compose_env,
    _relay_image_tags,
    _sanitized_compose_env,
    _sealed_memfd,
    _strip_local_rmi,
    _task_hashes,
    _validate_compose_authority,
    _validate_prepared_source,
    _validated_compose_bytes,
    _validated_mounts,
    _validated_run_binding,
    _write_cleanup_receipt,
)


def _binding(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "replication_id": "screen-v1",
        "source_revision": "a" * 40,
        "experiment_manifest_sha256": "sha256:" + "b" * 64,
        "relay_build_sha256": "sha256:" + "c" * 64,
        "relay_image_sha256": "sha256:" + "d" * 64,
        "preflight_sha256": "sha256:" + "e" * 64,
        "task_snapshots_sha256": "sha256:" + "8" * 64,
    }
    value.update(changes)
    return value


class PinnedRelayEnvironmentTest(unittest.TestCase):
    def _compose(
        self, root: Path, *, attack: dict[str, object] | None = None
    ) -> tuple[Path, str]:
        relay: dict[str, object] = {
            "image": _binding()["relay_image_sha256"],
            "pull_policy": "never",
            "secrets": ["provider-api-key"],
        }
        relay.update(attack or {})
        text = yaml.safe_dump(
            {
                "services": {
                    "main": {"depends_on": {"open-agent-lab-relay": {}}},
                    "open-agent-lab-relay": relay,
                },
                "secrets": {"provider-api-key": {"file": "/tmp/key"}},
            },
            sort_keys=False,
        )
        path = root / "relay.yaml"
        path.write_text(text)
        return path, "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    def _cleanup_environment(
        self, root: Path, *, descriptor: int = 41
    ) -> PinnedRelayDockerEnvironment:
        environment = object.__new__(PinnedRelayDockerEnvironment)
        environment._closed = False
        environment._keep_containers = False
        environment._full_fd = descriptor
        environment._full_compose_sha256 = "sha256:" + "f" * 64
        environment._source_fds = []
        environment._seed_fd = -1
        environment._run_binding = _binding()
        environment._task_runtime = None
        environment.session_id = "pilot__abc123__env"
        environment.trial_paths = SimpleNamespace(trial_dir=root)
        environment.prepare_logs_for_host = AsyncMock()
        environment._run_docker_compose_command = AsyncMock()
        environment._assert_project_empty = AsyncMock()
        cleanup = Mock()
        environment._cleanup_mounts_compose_file = cleanup
        environment._cleanup_resources_compose_file = cleanup
        environment._cleanup_env_compose_file = cleanup
        environment._cleanup_egress_control_services_compose_file = cleanup
        return environment

    def test_binding_and_compose_are_exact(self) -> None:
        binding = _binding()
        self.assertEqual(_validated_run_binding(binding), binding)
        binding["relay_image_sha256"] = "sha256:bad"
        with self.assertRaisesRegex(ValueError, "invalid values"):
            _validated_run_binding(binding)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            path, digest = self._compose(root)
            binding = _binding()
            self.assertEqual(
                _validated_compose_bytes(path, digest, binding), path.read_bytes()
            )
            with self.assertRaisesRegex(ValueError, "digest drifted"):
                _validated_compose_bytes(path, "sha256:" + "1" * 64, binding)
            for attack in (
                {"build": {"context": "."}},
                {"image": "sha256:" + "2" * 64},
                {"env_file": "mutable.env"},
                {"extends": {"file": "mutable.yaml", "service": "relay"}},
                {"label_file": "mutable.labels"},
            ):
                path, digest = self._compose(root, attack=attack)
                with self.assertRaises(ValueError):
                    _validated_compose_bytes(path, digest, binding)
            target = root / "target.yaml"
            path.replace(target)
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                _validated_compose_bytes(path, digest, binding)

    def test_only_run_local_logs_and_frozen_runtime_mounts_are_authorized(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared = Path(raw).resolve()
            runtime = prepared / CODEX_RUNTIME_PREPARED_RELATIVE
            runtime.mkdir(parents=True)
            trial = prepared / "jobs" / "trial"
            trial_paths = SimpleNamespace(
                verifier_dir=trial / "verifier",
                agent_dir=trial / "agent",
                artifacts_dir=trial / "artifacts",
            )
            logs = [
                trial_paths.verifier_dir,
                trial_paths.agent_dir,
                trial_paths.artifacts_dir / "logs" / "artifacts",
            ]
            for path in logs:
                path.mkdir(parents=True)
            mounts = [
                {"type": "bind", "source": str(source), "target": target}
                for source, target in zip(
                    logs,
                    ("/logs/verifier", "/logs/agent", "/logs/artifacts"),
                    strict=True,
                )
            ] + [
                {
                    "type": "bind",
                    "source": str(runtime),
                    "target": CODEX_RUNTIME_INSTALL_ROOT,
                    "read_only": True,
                }
            ]
            self.assertEqual(_validated_mounts(mounts, prepared, trial_paths), mounts)

            attacks = []
            writable = [dict(item) for item in mounts]
            writable[-1].pop("read_only")
            attacks.append(writable)
            wrong_target = [dict(item) for item in mounts]
            wrong_target[-1]["target"] = "/usr/local/bin"
            attacks.append(wrong_target)
            extra = [dict(item) for item in mounts]
            extra.append(
                {"type": "bind", "source": str(prepared), "target": "/workspace"}
            )
            attacks.append(extra)
            outside = [dict(item) for item in mounts]
            outside[0]["source"] = str(prepared.parent)
            attacks.append(outside)
            writable_runtime_alias = [dict(item) for item in mounts]
            writable_runtime_alias[0]["source"] = str(runtime)
            attacks.append(writable_runtime_alias)
            for attack in attacks:
                with self.subTest(attack=attack), self.assertRaises(ValueError):
                    _validated_mounts(attack, prepared, trial_paths)

            trial_paths.agent_dir.rmdir()
            trial_paths.agent_dir.symlink_to(runtime, target_is_directory=True)
            symlink_alias = [dict(item) for item in mounts]
            symlink_alias[1]["source"] = str(runtime)
            with self.assertRaisesRegex(ValueError, "authority"):
                _validated_mounts(symlink_alias, prepared, trial_paths)

    def test_live_task_snapshot_and_image_are_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw).resolve()
            task_dir = output / "tasks" / "example"
            environment = task_dir / "environment"
            environment.mkdir(parents=True)
            (task_dir / "task.toml").write_text(
                '[task]\nname="terminal-bench/example"\n'
            )
            (task_dir / "instruction.md").write_text("do the task\n")
            (environment / "Dockerfile").write_text("FROM scratch\n")
            task_digest, task_checksum = _task_hashes(task_dir)
            authority = {
                "taskDigest": task_digest,
                "taskChecksum": task_checksum,
                "declaredImage": "alexgshaw/example:20251031",
                "immutableImage": "alexgshaw/example@sha256:" + "3" * 64,
                "imageConfigDigest": "sha256:" + "4" * 64,
                "platform": "linux/amd64",
            }
            snapshot = {
                "relativePath": "tasks/example",
                "taskDigest": task_digest,
                "taskChecksum": task_checksum,
            }
            relay_images = {
                "production": "sha256:" + "5" * 64,
                "providerFreeFixture": "sha256:" + "6" * 64,
            }
            selected = _live_task_authority(
                output,
                environment.parent,
                relay_images["production"],
                relay_images,
                {"terminal-bench/example": authority},
                {"terminal-bench/example": snapshot},
            )
            self.assertIsNotNone(selected)
            pinned = object.__new__(PinnedRelayDockerEnvironment)
            pinned._task_runtime = selected
            pinned._capture = AsyncMock(
                side_effect=[
                    b"pulled\n",
                    f"{authority['imageConfigDigest']}|linux|amd64\n".encode(),
                ]
            )
            graph = {
                "services": {
                    "main": {"image": authority["declaredImage"]},
                    "open-agent-lab-relay": {},
                }
            }
            asyncio.run(pinned._pin_task_runtime(graph))
            self.assertEqual(
                graph["services"]["main"],
                {
                    "image": authority["immutableImage"],
                    "platform": "linux/amd64",
                    "pull_policy": "never",
                },
            )
            pinned._capture.assert_any_await(
                [
                    "docker",
                    "pull",
                    "--quiet",
                    "--platform",
                    "linux/amd64",
                    authority["immutableImage"],
                ],
                timeout=900,
            )

            graph["services"]["main"]["image"] = "alexgshaw/example:latest"
            pinned._capture.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "declared image"):
                asyncio.run(pinned._pin_task_runtime(graph))
            pinned._capture.assert_not_awaited()

    def test_task_snapshot_is_rechecked_around_task_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "tasks" / "example"
            tests = task / "tests"
            tests.mkdir(parents=True)
            digest = "sha256:" + "1" * 64
            checksum = "2" * 64
            environment = object.__new__(PinnedRelayDockerEnvironment)
            environment._task_runtime = (
                "terminal-bench/example",
                task,
                {},
                {"taskDigest": digest, "taskChecksum": checksum},
            )
            with (
                patch(
                    "benchmarks.terminal_bench.harbor_environment._task_hashes",
                    return_value=(digest, checksum),
                ) as hashes,
                patch.object(
                    DockerEnvironment, "upload_dir", new_callable=AsyncMock
                ) as upload,
            ):
                asyncio.run(environment.upload_dir(tests, "/tests"))
            self.assertEqual(hashes.call_count, 2)
            upload.assert_awaited_once_with(tests, "/tests")

    def test_resolved_graph_blocks_relay_capability_leaks(self) -> None:
        binding = _binding()
        relay = {
            "image": binding["relay_image_sha256"],
            "pull_policy": "never",
            "secrets": [{"source": "provider-api-key", "target": "provider-api-key"}],
        }
        secret = {"file": "/private/keys/provider", "name": "run_provider-api-key"}
        expected = {
            "services": {"open-agent-lab-relay": relay},
            "secrets": {"provider-api-key": secret},
        }

        def graph(service: dict[str, object] | None = None) -> dict[str, object]:
            return {
                "services": {
                    "main": service or {"image": "task"},
                    "open-agent-lab-relay": relay,
                },
                "secrets": {"provider-api-key": secret},
            }

        _assert_resolved_graph(graph(), expected, binding)
        allowed_mount = {
            "type": "bind",
            "source": "/workspace/logs",
            "target": "/logs",
            "read_only": True,
        }
        allowed_binds = _allowed_main_binds(
            canonical_json({"services": {"main": {"volumes": [allowed_mount]}}})
        )
        _assert_resolved_graph(
            graph({"image": "task", "volumes": [allowed_mount]}),
            expected,
            binding,
            allowed_binds=allowed_binds,
        )
        _assert_resolved_graph(
            graph({"build": {"context": "/workspace"}}),
            expected,
            binding,
            build_root=Path("/workspace"),
        )
        attacks = (
            {"secrets": [{"source": "provider-api-key"}]},
            {"secrets": [{"source": "other-secret"}]},
            {"configs": [{"source": "task-config"}]},
            {"network_mode": "service:open-agent-lab-relay"},
            {"pid": "service:open-agent-lab-relay"},
            {"ipc": "service:open-agent-lab-relay"},
            {"volumes_from": ["open-agent-lab-relay:ro"]},
            {"volumes_from": ["external-container:ro"]},
            {"privileged": True},
            {"pid": "host"},
            {"pid": "container:relay"},
            {"ipc": "host"},
            {"ipc": "shareable"},
            {"cap_add": ["SYS_ADMIN"]},
            {"cap_add": ["CAP_SYS_ADMIN"]},
            {"cap_add": ["SYS_PTRACE"]},
            {"use_api_socket": True},
            {"devices": ["/dev/kvm:/dev/kvm"]},
            {"network_mode": "host"},
            {"provider": {"type": "model"}},
            {"post_start": [{"command": "steal"}]},
            {
                "volumes": [
                    {"type": "bind", "source": "/private/keys", "target": "/keys"}
                ]
            },
            {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    }
                ]
            },
            {
                "volumes": [
                    {"type": "bind", "source": "/var/run", "target": "/host-run"}
                ]
            },
            {"volumes": [{"type": "bind", "source": "/proc", "target": "/hostproc"}]},
        )
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                _assert_resolved_graph(graph(attack), expected, binding)
        config_leak = graph({"configs": [{"source": "leak"}]})
        config_leak["configs"] = {"leak": {"file": "/private/keys"}}
        with self.assertRaises((TypeError, ValueError)):
            _assert_resolved_graph(config_leak, expected, binding)
        secret_alias = graph({"secrets": [{"source": "leak"}]})
        secret_alias["secrets"]["leak"] = {"file": "/private/keys"}
        with self.assertRaises((TypeError, ValueError)):
            _assert_resolved_graph(secret_alias, expected, binding)
        for build in (
            {"context": "/private/keys"},
            {
                "context": "/workspace",
                "additional_contexts": {"key": "/private"},
            },
            {"context": "/workspace", "dockerfile": "/private/Dockerfile"},
            {"context": "/workspace", "dockerfile_inline": "FROM scratch"},
            {"context": "/workspace", "privileged": True},
            {"context": "/workspace", "entitlements": ["security.insecure"]},
            {
                "context": "/workspace",
                "secrets": [{"source": "provider-api-key"}],
            },
            {"context": "/workspace", "secrets": [{"source": "other-secret"}]},
            {"context": "/workspace", "ssh": ["default=/private/keys"]},
            {
                "context": "/workspace",
                "cache_from": ["type=local,src=/private/keys"],
            },
        ):
            with self.subTest(build=build), self.assertRaises(ValueError):
                _assert_resolved_graph(graph({"build": build}), expected, binding)
        drifted = graph()
        drifted["services"]["open-agent-lab-relay"] = {**relay, "privileged": True}
        with self.assertRaisesRegex(ValueError, "drifted"):
            _assert_resolved_graph(drifted, expected, binding)

    def test_compose_control_environment_and_cleanup_are_fixed(self) -> None:
        observed = _sanitized_compose_env(
            {
                "PATH": "/bin",
                "COMPOSE_FILE": "attack.yaml",
                "COMPOSE_PROFILES": "attack",
                "COMPOSE_ENV_FILES": "attack.env",
                "COMPOSE_DISABLE_ENV_FILE": "0",
            }
        )
        self.assertEqual(observed, {"PATH": "/bin", "COMPOSE_DISABLE_ENV_FILE": "1"})
        self.assertEqual(
            _strip_local_rmi(
                ["down", "--rmi", "local", "--volumes", "--remove-orphans"]
            ),
            ["down", "--volumes", "--remove-orphans"],
        )
        self.assertEqual(_memfd_path(7), Path(f"/proc/{os.getpid()}/fd/7"))
        minimal = _minimal_compose_env(
            {"MAIN_IMAGE_NAME": "main", "COMPOSE_FILE": "attack.yaml"},
            b"file: ${OAL_ZAI_API_KEY_FILE:?required}",
            {
                "PATH": "/bin",
                "DOCKER_HOST": "unix:///run/docker.sock",
                "OAL_ZAI_API_KEY_FILE": "/keys/zai",
                "ZAI_API_KEY": "direct-secret",
                "GITHUB_TOKEN": "ci-secret",
            },
        )
        self.assertEqual(
            minimal,
            {
                "PATH": "/bin",
                "DOCKER_HOST": "unix:///run/docker.sock",
                "OAL_ZAI_API_KEY_FILE": "/keys/zai",
                "MAIN_IMAGE_NAME": "main",
                "COMPOSE_DISABLE_ENV_FILE": "1",
            },
        )

    def test_attach_is_explicitly_unsupported(self) -> None:
        environment = object.__new__(PinnedRelayDockerEnvironment)
        with self.assertRaisesRegex(RuntimeError, "attach is unavailable"):
            asyncio.run(environment.attach())

    def test_relay_tmpfs_artifacts_use_a_bounded_exact_path_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifacts = Path(raw)
            environment = object.__new__(PinnedRelayDockerEnvironment)
            environment.trial_paths = SimpleNamespace(artifacts_dir=artifacts)
            environment.service_exec = AsyncMock()
            for name, data, limit in (
                ("provider-metadata.ndjson", b'{"event":"request"}\n', 4 * 1024 * 1024),
                (
                    "provider-metadata.ndjson.sealed",
                    b'{"state":"sealed"}\n',
                    64 * 1024,
                ),
            ):
                environment.service_exec.return_value = SimpleNamespace(
                    return_code=0,
                    stdout=base64.b64encode(data).decode(),
                )
                target = artifacts / name
                asyncio.run(
                    environment.service_download_file(
                        f"/var/lib/open-agent-lab/{name}",
                        target,
                        service="open-agent-lab-relay",
                    )
                )
                self.assertEqual(target.read_bytes(), data)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                args, kwargs = environment.service_exec.await_args
                self.assertIn(f"test ! -L /var/lib/open-agent-lab/{name}", args[0])
                self.assertIn(f'[ "$bytes" -le {limit} ]', args[0])
                self.assertEqual(
                    kwargs,
                    {
                        "service": "open-agent-lab-relay",
                        "timeout_sec": 10,
                        "user": "1000",
                    },
                )
                environment.service_exec.reset_mock()

    def test_relay_tmpfs_artifact_export_fails_closed(self) -> None:
        source = "/var/lib/open-agent-lab/provider-metadata.ndjson.sealed"
        with tempfile.TemporaryDirectory() as raw:
            artifacts = Path(raw)
            environment = object.__new__(PinnedRelayDockerEnvironment)
            environment.trial_paths = SimpleNamespace(artifacts_dir=artifacts)
            environment.service_exec = AsyncMock(
                return_value=SimpleNamespace(return_code=1, stdout="")
            )
            with self.assertRaisesRegex(RuntimeError, "export failed"):
                asyncio.run(
                    environment.service_download_file(
                        source,
                        artifacts / Path(source).name,
                        service="open-agent-lab-relay",
                    )
                )
            environment.service_exec.return_value = SimpleNamespace(
                return_code=0, stdout="not-base64"
            )
            with self.assertRaisesRegex(RuntimeError, "valid base64"):
                asyncio.run(
                    environment.service_download_file(
                        source,
                        artifacts / Path(source).name,
                        service="open-agent-lab-relay",
                    )
                )
            environment.service_exec.return_value = SimpleNamespace(
                return_code=0,
                stdout=base64.b64encode(b"x" * (64 * 1024 + 1)).decode(),
            )
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                asyncio.run(
                    environment.service_download_file(
                        source,
                        artifacts / Path(source).name,
                        service="open-agent-lab-relay",
                    )
                )
            self.assertFalse((artifacts / Path(source).name).exists())
            with self.assertRaisesRegex(RuntimeError, "destination"):
                asyncio.run(
                    environment.service_download_file(
                        source,
                        artifacts / "wrong-name",
                        service="open-agent-lab-relay",
                    )
                )

    def test_other_artifact_downloads_delegate_to_harbor(self) -> None:
        environment = object.__new__(PinnedRelayDockerEnvironment)
        parent = AsyncMock()
        with patch.object(DockerEnvironment, "service_download_file", parent):
            asyncio.run(
                environment.service_download_file(
                    "/tmp/other", "/tmp/target", service="open-agent-lab-relay"
                )
            )
            asyncio.run(
                environment.service_download_file(
                    "/var/lib/open-agent-lab/provider-metadata.ndjson",
                    "/tmp/target",
                    service="main",
                )
            )
        self.assertEqual(parent.await_count, 2)

    def test_retained_containers_are_rejected_before_harbor_initialization(
        self,
    ) -> None:
        with (
            patch.object(DockerEnvironment, "__init__") as initialize,
            self.assertRaisesRegex(ValueError, "cannot retain containers"),
        ):
            PinnedRelayDockerEnvironment(
                relay_compose_sha256="sha256:" + "1" * 64,
                run_binding={},
                keep_containers=True,
            )
        initialize.assert_not_called()

    def test_failed_cleanup_keeps_the_sealed_graph_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            environment = self._cleanup_environment(Path(raw))
            environment._run_docker_compose_command = AsyncMock(
                side_effect=RuntimeError("down failed")
            )
            with self.assertRaisesRegex(RuntimeError, "down failed"):
                asyncio.run(environment.stop(delete=False))
            self.assertFalse(environment._closed)
            self.assertEqual(environment._full_fd, 41)
            self.assertEqual(environment._run_docker_compose_command.await_count, 3)
            environment._run_docker_compose_command.assert_awaited_with(
                ["down"], timeout_sec=120
            )
            environment._cleanup_mounts_compose_file.assert_not_called()

    def test_cleanup_retries_then_writes_canonical_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            descriptor = os.open(os.devnull, os.O_RDONLY)
            environment = self._cleanup_environment(root, descriptor=descriptor)
            environment._run_docker_compose_command = AsyncMock(
                side_effect=[RuntimeError("transient"), None]
            )
            asyncio.run(environment.stop(delete=True))

            receipt_path = root / "environment-cleanup.json"
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            self.assertEqual(receipt_bytes, canonical_json(receipt))
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
            self.assertEqual(
                receipt,
                {
                    "schemaVersion": 1,
                    "experimentId": EXPERIMENT_ID,
                    "replicationId": "screen-v1",
                    "sourceRevision": "a" * 40,
                    "experimentManifestSha256": "sha256:" + "b" * 64,
                    "preflightSha256": "sha256:" + "e" * 64,
                    "runBindingSha256": digest_bytes(canonical_json(_binding())),
                    "relayImageSha256": "sha256:" + "d" * 64,
                    "fullComposeSha256": "sha256:" + "f" * 64,
                    "taskId": None,
                    "taskDigest": None,
                    "taskChecksum": None,
                    "sessionId": "pilot__abc123__env",
                    "projectName": "pilot__abc123__env",
                    "stoppedAt": receipt["stoppedAt"],
                },
            )
            self.assertRegex(
                receipt["stoppedAt"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
            )
            self.assertTrue(environment._closed)
            self.assertEqual(environment._run_docker_compose_command.await_count, 2)
            environment._assert_project_empty.assert_awaited_once()
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_cleanup_timeouts_are_bounded_and_fail_closed(self) -> None:
        async def never(*args: object, **kwargs: object) -> None:
            del args, kwargs
            await asyncio.Future()

        with tempfile.TemporaryDirectory() as raw:
            environment = self._cleanup_environment(Path(raw))
            environment._run_docker_compose_command = AsyncMock(side_effect=never)
            with (
                patch(
                    "benchmarks.terminal_bench.harbor_environment."
                    "_CLEANUP_TIMEOUT_SECONDS",
                    0.001,
                ),
                self.assertRaisesRegex(RuntimeError, "cleanup failed"),
            ):
                asyncio.run(environment.stop(delete=False))
            self.assertEqual(environment._run_docker_compose_command.await_count, 3)
            self.assertFalse((Path(raw) / "environment-cleanup.json").exists())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            descriptor = os.open(os.devnull, os.O_RDONLY)
            environment = self._cleanup_environment(root, descriptor=descriptor)
            environment.prepare_logs_for_host = never
            with (
                patch(
                    "benchmarks.terminal_bench.harbor_environment."
                    "_LOG_EXPORT_TIMEOUT_SECONDS",
                    0.001,
                ),
                self.assertRaisesRegex(RuntimeError, "Logs could not be prepared"),
            ):
                asyncio.run(environment.stop(delete=False))
            self.assertTrue((root / "environment-cleanup.json").is_file())
            self.assertTrue(environment._closed)

    def test_log_export_failure_cannot_skip_project_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            descriptor = os.open(os.devnull, os.O_RDONLY)
            environment = self._cleanup_environment(root, descriptor=descriptor)
            environment.prepare_logs_for_host = AsyncMock(
                side_effect=RuntimeError("logs unavailable")
            )
            with self.assertRaisesRegex(RuntimeError, "Logs could not be prepared"):
                asyncio.run(environment.stop(delete=False))
            environment._run_docker_compose_command.assert_awaited_once()
            environment._assert_project_empty.assert_awaited_once()
            self.assertTrue((root / "environment-cleanup.json").is_file())
            self.assertTrue(environment._closed)
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_cleanup_rejects_residual_project_containers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = self._cleanup_environment(root)
            environment._assert_project_empty = AsyncMock(
                side_effect=RuntimeError("containers behind")
            )
            with self.assertRaisesRegex(RuntimeError, "containers behind"):
                asyncio.run(environment.stop(delete=False))
            self.assertEqual(environment._run_docker_compose_command.await_count, 3)
            self.assertEqual(environment._assert_project_empty.await_count, 3)
            self.assertFalse((root / "environment-cleanup.json").exists())
            self.assertEqual(environment._full_fd, 41)

    def test_project_check_is_exactly_label_scoped(self) -> None:
        environment = object.__new__(PinnedRelayDockerEnvironment)
        environment.session_id = "pilot__abc123__env"
        environment._capture = AsyncMock(return_value=b"")
        asyncio.run(environment._assert_project_empty())
        environment._capture.assert_awaited_once_with(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=pilot__abc123__env",
            ]
        )
        environment._capture = AsyncMock(return_value=b"container-id\n")
        with self.assertRaisesRegex(RuntimeError, "containers behind"):
            asyncio.run(environment._assert_project_empty())

    def test_receipt_failure_keeps_sealed_graph_and_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = self._cleanup_environment(root)
            with (
                patch(
                    "benchmarks.terminal_bench.harbor_environment."
                    "_write_cleanup_receipt",
                    side_effect=OSError("disk failed"),
                ),
                self.assertRaisesRegex(OSError, "disk failed"),
            ):
                asyncio.run(environment.stop(delete=False))
            self.assertFalse((root / "environment-cleanup.json").exists())
            self.assertFalse(environment._closed)
            self.assertEqual(environment._full_fd, 41)

    def test_cleanup_receipt_never_overwrites_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            receipt = root / "environment-cleanup.json"
            _write_cleanup_receipt(receipt, {"first": True})
            with self.assertRaises(FileExistsError):
                _write_cleanup_receipt(receipt, {"first": False})
            self.assertEqual(receipt.read_bytes(), canonical_json({"first": True}))

            receipt.unlink()
            target = root / "target.json"
            target.write_bytes(b"untouched")
            receipt.symlink_to(target)
            with self.assertRaises(FileExistsError):
                _write_cleanup_receipt(receipt, {"attack": True})
            self.assertEqual(target.read_bytes(), b"untouched")

    def test_compose_digest_is_authorized_by_the_prepared_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw).resolve() / "run"
            root = output / "source"
            directory = root / "benchmarks" / "terminal_bench"
            directory.mkdir(parents=True)
            binding = _binding()
            source = {
                "services": {
                    "main": {"depends_on": {"open-agent-lab-relay": {}}},
                    "open-agent-lab-relay": {
                        "build": {"context": "."},
                        "command": ["--provider", "deepseek"],
                        "secrets": ["provider-api-key"],
                    },
                },
                "secrets": {"provider-api-key": {"file": "${KEY_FILE}"}},
            }
            source_path = directory / "relay.deepseek.compose.yaml"
            source_path.write_text(yaml.safe_dump(source, sort_keys=False))
            overlay = yaml.safe_load(yaml.safe_dump(source))
            relay = overlay["services"]["open-agent-lab-relay"]
            relay.pop("build")
            relay["image"] = binding["relay_image_sha256"]
            relay["pull_policy"] = "never"
            overlay_path = output / "overlays" / "relay.deepseek.compose.yaml"
            overlay_path.parent.mkdir(parents=True)
            data = yaml.safe_dump(overlay, sort_keys=False).encode()
            overlay_path.write_bytes(data)
            digest = digest_bytes(data)
            record = {
                "relayImages": {
                    "production": binding["relay_image_sha256"],
                    "providerFreeFixture": "sha256:" + "9" * 64,
                },
                "providers": [
                    {
                        "provider": "deepseek",
                        "compose": "overlays/relay.deepseek.compose.yaml",
                        "composeSha256": digest,
                        "relayImageSha256": binding["relay_image_sha256"],
                    }
                ],
            }
            hashes = {
                "benchmarks/terminal_bench/relay.deepseek.compose.yaml": digest_bytes(
                    source_path.read_bytes()
                )
            }
            _validate_compose_authority(
                data, overlay_path, digest, binding, record, root, hashes
            )
            overlay["services"]["open-agent-lab-relay"]["command"] = ["exfiltrate"]
            with self.assertRaisesRegex(RuntimeError, "not authorized"):
                _validate_compose_authority(
                    yaml.safe_dump(overlay).encode(),
                    overlay_path,
                    digest,
                    binding,
                    record,
                    root,
                    hashes,
                )

    def test_prepared_source_validates_the_complete_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw).resolve() / "run"
            root = output / "source"
            module = root / "benchmarks" / "terminal_bench" / "harbor_environment.py"
            module.parent.mkdir(parents=True)
            module.write_text("# frozen module\n")
            contract = module.with_name("experiment_contract.py")
            contract.write_text("# frozen contract\n")
            runtime_module = module.with_name("codex_runtime.py")
            runtime_module.write_text("# frozen runtime\n")
            build = "sha256:" + "c" * 64
            runtime = {
                "terminal-bench/example": {
                    "taskDigest": "sha256:" + "1" * 64,
                    "taskChecksum": "2" * 64,
                    "declaredImage": "alexgshaw/example:20251031",
                    "immutableImage": "alexgshaw/example@sha256:" + "3" * 64,
                    "imageConfigDigest": "sha256:" + "4" * 64,
                    "platform": "linux/amd64",
                }
            }
            snapshots = {
                "terminal-bench/example": {
                    "relativePath": "tasks/example",
                    "taskDigest": "sha256:" + "1" * 64,
                    "taskChecksum": "2" * 64,
                }
            }
            manifest = {
                "schemaVersion": 2,
                "experimentId": EXPERIMENT_ID,
                "relayBuildIds": {
                    "production": build,
                    "providerFreeFixture": "sha256:" + "f" * 64,
                },
                "fileSha256": {
                    "benchmarks/terminal_bench/harbor_environment.py": digest_bytes(
                        module.read_bytes()
                    ),
                    "benchmarks/terminal_bench/experiment_contract.py": digest_bytes(
                        contract.read_bytes()
                    ),
                    "benchmarks/terminal_bench/codex_runtime.py": digest_bytes(
                        runtime_module.read_bytes()
                    ),
                },
                "runtime": {
                    "hermeticCodexRuntimeReady": True,
                    "codexRuntime": codex_runtime_spec(),
                },
                "taskRuntimeBindings": runtime,
            }
            manifest_path = module.with_name("verify-instruction-v1.experiment.json")
            manifest_path.write_bytes(canonical_json(manifest))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.com",
                    "add",
                    ".",
                ],
                cwd=root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=root,
                check=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            binding = _binding(
                source_revision=revision,
                experiment_manifest_sha256=digest_bytes(manifest_path.read_bytes()),
                relay_build_sha256=build,
                task_snapshots_sha256=digest_bytes(canonical_json(snapshots)),
            )
            preflight = {
                "schemaVersion": 1,
                "experimentId": binding["experiment_id"],
                "replicationId": binding["replication_id"],
                "sourceRevision": revision,
                "experimentManifestSha256": binding["experiment_manifest_sha256"],
                "relayBuildSha256": build,
                "relayImageSha256": binding["relay_image_sha256"],
                "taskSnapshotsSha256": binding["task_snapshots_sha256"],
                "cleanTree": True,
                "createdAt": "2026-08-22T00:00:00Z",
            }
            binding["preflight_sha256"] = digest_bytes(canonical_json(preflight))
            record = {
                "schemaVersion": 1,
                "preflight": preflight,
                "preflightSha256": binding["preflight_sha256"],
                "relayImages": {
                    "production": binding["relay_image_sha256"],
                    "providerFreeFixture": "sha256:" + "9" * 64,
                },
                "relayImageTags": _relay_image_tags(output, revision),
                "taskSnapshots": snapshots,
                "codexRuntime": {"verified": True},
                "providers": [],
            }
            (output / "run-record.json").write_bytes(canonical_json(record) + b"\n")
            with (
                patch.dict(os.environ, {"OPEN_AGENT_LAB_REPO_ROOT": str(root)}),
                patch(
                    "benchmarks.terminal_bench.harbor_environment.verify_tree",
                    return_value={"verified": True},
                ),
            ):
                _validate_prepared_source(binding, root)
                module.write_text("# drift\n")
                with self.assertRaisesRegex(RuntimeError, "identity|drifted"):
                    _validate_prepared_source(binding, root)
                module.write_text("# frozen module\n")
                contract.write_text("# drift\n")
                with self.assertRaisesRegex(RuntimeError, "identity|drifted"):
                    _validate_prepared_source(binding, root)

    def test_production_runtime_gate_is_enforced_by_the_environment(self) -> None:
        binding = _binding()
        record = {
            "relayImages": {
                "production": binding["relay_image_sha256"],
                "providerFreeFixture": "sha256:" + "f" * 64,
            }
        }
        manifest = {"runtime": {"hermeticCodexRuntimeReady": False}}
        with self.assertRaisesRegex(RuntimeError, "Live work is blocked"):
            _assert_runtime_gate(manifest, record, binding)
        fixture_binding = {
            **binding,
            "relay_image_sha256": record["relayImages"]["providerFreeFixture"],
        }
        _assert_runtime_gate(manifest, record, fixture_binding)

    @unittest.skipUnless(sys.platform == "linux", "Linux memfd integrity boundary")
    def test_compose_memfd_is_write_sealed(self) -> None:
        descriptor = _sealed_memfd(b"trusted-compose")
        try:
            with self.assertRaises(OSError) as captured:
                os.write(descriptor, b"tamper")
            self.assertEqual(captured.exception.errno, errno.EPERM)
            os.lseek(descriptor, 0, os.SEEK_SET)
            self.assertEqual(os.read(descriptor, 64), b"trusted-compose")
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
