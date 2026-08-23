import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.models.job.config import JobConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult

from benchmarks.terminal_bench import paired_results as policy
from benchmarks.terminal_bench.experiment_contract import canonical_json, digest_bytes
from benchmarks.terminal_bench.validate_harbor_install_only import (
    _assert_cleanup,
    _assert_install_only_tree,
    _assert_job_aggregates,
    _assert_outputs_are_non_scorable,
    _assert_trial,
    _proof_projection,
    _trial_directories,
)


class InstallOnlyValidatorTest(unittest.TestCase):
    def test_trial_set_must_be_complete_and_plain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("config.json", "job.log", "lock.json", "result.json"):
                (root / name).write_text("{}")
            for index in range(2):
                (root / f"trial-{index}").mkdir()
            self.assertEqual(len(_trial_directories(root, 2)), 2)
            with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                _trial_directories(root, 3)
            (root / "score.csv").write_text("1")
            with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                _trial_directories(root, 2)

    def test_trial_validator_rejects_execution_and_lock_drift(self) -> None:
        provider = "deepseek"
        model = policy._PROVIDERS[provider]["model"]
        provider_credential_sha256 = digest_bytes(b"dummy-secret\n")
        variant = "control-v1"
        task = policy._TASKS[0]
        binding = {
            "schema_version": 1,
            "experiment_id": policy.EXPERIMENT_ID,
            "replication_id": "screen-v1",
            "source_revision": "a" * 40,
            "experiment_manifest_sha256": "sha256:" + "1" * 64,
            "relay_build_sha256": "sha256:" + "2" * 64,
            "relay_image_sha256": "sha256:" + "3" * 64,
            "task_snapshots_sha256": "sha256:" + "4" * 64,
            "preflight_sha256": "sha256:" + "5" * 64,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task_path = root / "tasks" / task.removeprefix("terminal-bench/")
            task_path.mkdir(parents=True)
            compose = root / "overlays/relay.deepseek.compose.yaml"
            compose.parent.mkdir()
            compose.write_text("services: {}\n")
            compose_sha = "sha256:" + "6" * 64
            job_id = UUID(int=1)
            job = JobConfig.model_validate(
                {
                    "job_name": "proof",
                    "jobs_dir": str(root / "jobs"),
                    "n_attempts": 1,
                    "n_concurrent_trials": 1,
                    "retry": {"max_retries": 0},
                    "install_only": True,
                    "tasks": [{"path": str(task_path), "source": policy._DATASET}],
                    "agents": [
                        {
                            "name": policy._VARIANTS[variant]["name"],
                            "import_path": policy._VARIANTS[variant]["import_path"],
                            "model_name": f"{provider}/{model}",
                            "kwargs": {
                                "version": policy._CODEX_VERSION,
                                "reasoning_effort": "high",
                                "enable_verify_instruction_v1": False,
                                "run_binding": binding,
                            },
                            "env": {
                                "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"
                            },
                        }
                    ],
                    "environment": {
                        "type": "docker",
                        "import_path": policy.ENVIRONMENT_IMPORT,
                        "delete": True,
                        "mounts": [
                            policy._codex_runtime_mount(
                                root / policy.CODEX_RUNTIME_PREPARED_RELATIVE
                            )
                        ],
                        "extra_docker_compose": [str(compose)],
                        "kwargs": {
                            "relay_compose_sha256": compose_sha,
                            "run_binding": binding,
                        },
                    },
                    "verifier": {"disable": True},
                }
            )
            trial = root / "jobs/proof/trial"
            trial.mkdir(parents=True)
            (trial / "agent/setup").mkdir(parents=True)
            (trial / "artifacts/logs/artifacts").mkdir(parents=True)
            (trial / "verifier").mkdir()
            config, task_id = policy._expected_result_config(
                trial, task, variant, job, job_id
            )
            (trial / "config.json").write_text(policy._canonical(config))
            (trial / "trial.log").write_text("setup complete\n")
            started = datetime(2026, 8, 23, tzinfo=timezone.utc)
            result = TrialResult(
                id=UUID(int=2),
                task_name=task,
                trial_name=trial.name,
                trial_uri=trial.resolve().as_uri(),
                task_id=task_id,
                source=policy._DATASET,
                task_checksum=policy._TASK_RUNTIME_BINDINGS[task]["taskChecksum"],
                config=TrialConfig.model_validate(config),
                agent_info={
                    "name": policy._VARIANTS[variant]["name"],
                    "version": policy._CODEX_VERSION,
                    "model_info": {"name": model, "provider": provider},
                },
                verifier_environment_mode="shared",
                started_at=started,
                environment_setup={
                    "started_at": started,
                    "finished_at": started + timedelta(seconds=1),
                },
                agent_setup={
                    "started_at": started + timedelta(seconds=1),
                    "finished_at": started + timedelta(seconds=2),
                },
                finished_at=started + timedelta(seconds=4),
            )
            lock = policy._expected_trial_lock(
                provider,
                model,
                task,
                variant,
                binding,
                task_path,
                compose,
                compose_sha,
                install_only=True,
            )
            (trial / "lock.json").write_text(policy._canonical(lock))
            raw_result = json.loads(result.model_dump_json())
            (trial / "result.json").write_text(policy._canonical(raw_result))
            session = f"{trial.name}__env"
            receipt = {
                "schemaVersion": 1,
                "experimentId": binding["experiment_id"],
                "replicationId": binding["replication_id"],
                "sourceRevision": binding["source_revision"],
                "experimentManifestSha256": binding["experiment_manifest_sha256"],
                "preflightSha256": binding["preflight_sha256"],
                "runBindingSha256": digest_bytes(canonical_json(binding)),
                "relayImageSha256": binding["relay_image_sha256"],
                "providerCredentialSha256": provider_credential_sha256,
                "fullComposeSha256": "sha256:" + "7" * 64,
                "taskId": task,
                "taskDigest": policy._TASK_RUNTIME_BINDINGS[task]["taskDigest"],
                "taskChecksum": policy._TASK_RUNTIME_BINDINGS[task]["taskChecksum"],
                "sessionId": session,
                "projectName": _sanitize_docker_compose_project_name(session),
                "stoppedAt": (started + timedelta(seconds=3)).isoformat(),
            }
            (trial / "environment-cleanup.json").write_text(policy._canonical(receipt))

            _assert_trial(
                trial,
                provider,
                model,
                binding,
                job,
                job_id,
                compose,
                compose_sha,
                provider_credential_sha256,
            )
            lock["install_only"] = False
            (trial / "lock.json").write_text(policy._canonical(lock))
            with self.assertRaisesRegex(RuntimeError, "TrialLock drifted"):
                _assert_trial(
                    trial,
                    provider,
                    model,
                    binding,
                    job,
                    job_id,
                    compose,
                    compose_sha,
                    provider_credential_sha256,
                )
            lock["install_only"] = True
            (trial / "lock.json").write_text(policy._canonical(lock))
            raw_result["agent_execution"] = {
                "started_at": started.isoformat(),
                "finished_at": (started + timedelta(seconds=1)).isoformat(),
            }
            raw_result = json.loads(
                TrialResult.model_validate(raw_result).model_dump_json()
            )
            (trial / "result.json").write_text(policy._canonical(raw_result))
            with self.assertRaisesRegex(RuntimeError, "execution or scoring"):
                _assert_trial(
                    trial,
                    provider,
                    model,
                    binding,
                    job,
                    job_id,
                    compose,
                    compose_sha,
                    provider_credential_sha256,
                )

    def test_projection_changes_only_the_proof_controls(self) -> None:
        config = {
            "job_name": "scored",
            "jobs_dir": "/tmp/scored",
            "verifier": {"import_path": "example:Verifier"},
            "agents": [{"name": "unchanged"}],
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            projected, job_dir, replication = _proof_projection(
                root, "deepseek", config, "screen-v1"
            )
        self.assertEqual(config["job_name"], "scored")
        self.assertTrue(projected["install_only"])
        self.assertTrue(projected["verifier"]["disable"])
        self.assertEqual(projected["verifier"]["import_path"], "example:Verifier")
        self.assertEqual(projected["agents"], config["agents"])
        self.assertEqual(replication, "screen-v1")
        self.assertEqual(
            job_dir,
            root
            / "install-only-jobs/deepseek"
            / "open-agent-lab-screen-v1-deepseek-install-only",
        )

    def test_non_scorable_outputs_reject_secrets_and_benchmark_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "trial.log").write_text("setup complete")
            _assert_outputs_are_non_scorable(root, b"dummy-secret")
            (root / "trial.log").write_bytes(b"dummy-secret")
            with self.assertRaisesRegex(RuntimeError, "leaked"):
                _assert_outputs_are_non_scorable(root, b"dummy-secret")
            (root / "trial.log").write_text("setup complete")
            (root / "trajectory.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "Scored output"):
                _assert_outputs_are_non_scorable(root, b"dummy-secret")

    def test_install_only_tree_rejects_any_execution_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            (trial / "agent/setup").mkdir(parents=True)
            (trial / "artifacts/logs/artifacts").mkdir(parents=True)
            (trial / "verifier").mkdir()
            for name in (
                "config.json",
                "environment-cleanup.json",
                "lock.json",
                "result.json",
                "trial.log",
            ):
                (trial / name).write_text("{}")
            _assert_install_only_tree(trial)
            (trial / "verifier/test-stdout.txt").write_text("score")
            with self.assertRaisesRegex(RuntimeError, "verifier output tree drifted"):
                _assert_install_only_tree(trial)
            (trial / "verifier/test-stdout.txt").unlink()
            (trial / "agent/bridge-trajectory.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "agent output tree drifted"):
                _assert_install_only_tree(trial)

    def test_cleanup_binds_lifecycle_and_raw_credential_bytes(self) -> None:
        task = policy._TASKS[0]
        binding = {
            "schema_version": 1,
            "experiment_id": policy.EXPERIMENT_ID,
            "replication_id": "screen-v1",
            "source_revision": "a" * 40,
            "experiment_manifest_sha256": "sha256:" + "1" * 64,
            "relay_build_sha256": "sha256:" + "2" * 64,
            "relay_image_sha256": "sha256:" + "3" * 64,
            "task_snapshots_sha256": "sha256:" + "4" * 64,
            "preflight_sha256": "sha256:" + "5" * 64,
        }
        result = {
            "agent_setup": {
                "started_at": "2026-08-23T00:00:01Z",
                "finished_at": "2026-08-23T00:00:02Z",
            },
            "finished_at": "2026-08-23T00:00:04Z",
        }
        raw_credential = b"dummy-secret\n"
        provider_credential_sha256 = digest_bytes(raw_credential)
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw) / "trial"
            trial.mkdir()
            session = "trial__env"
            receipt = {
                "schemaVersion": 1,
                "experimentId": binding["experiment_id"],
                "replicationId": binding["replication_id"],
                "sourceRevision": binding["source_revision"],
                "experimentManifestSha256": binding["experiment_manifest_sha256"],
                "preflightSha256": binding["preflight_sha256"],
                "runBindingSha256": digest_bytes(canonical_json(binding)),
                "relayImageSha256": binding["relay_image_sha256"],
                "providerCredentialSha256": provider_credential_sha256,
                "fullComposeSha256": "sha256:" + "6" * 64,
                "taskId": task,
                "taskDigest": policy._TASK_RUNTIME_BINDINGS[task]["taskDigest"],
                "taskChecksum": policy._TASK_RUNTIME_BINDINGS[task]["taskChecksum"],
                "sessionId": session,
                "projectName": _sanitize_docker_compose_project_name(session),
                "stoppedAt": "2026-08-23T00:00:03Z",
            }
            (trial / "environment-cleanup.json").write_text(json.dumps(receipt))
            _assert_cleanup(trial, result, task, binding, provider_credential_sha256)
            receipt["providerCredentialSha256"] = digest_bytes(raw_credential.strip())
            (trial / "environment-cleanup.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(RuntimeError, "identity drifted"):
                _assert_cleanup(
                    trial, result, task, binding, provider_credential_sha256
                )
            receipt["providerCredentialSha256"] = provider_credential_sha256
            receipt["taskDigest"] = "sha256:" + "0" * 64
            (trial / "environment-cleanup.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(RuntimeError, "identity drifted"):
                _assert_cleanup(
                    trial, result, task, binding, provider_credential_sha256
                )

    def test_aggregates_must_be_usage_and_reward_free(self) -> None:
        evals = {
            f"{spec['name']}__{policy._PROVIDERS['deepseek']['model']}__{policy._DATASET}": SimpleNamespace(
                n_trials=0,
                n_errors=0,
                reward_stats={},
                exception_stats={},
                pass_at_k={},
                metrics=[{"mean": 0.0}],
            )
            for spec in policy._VARIANTS.values()
        }
        stats = SimpleNamespace(
            n_input_tokens=None,
            n_cache_tokens=None,
            n_output_tokens=None,
            cost_usd=None,
            evals=evals,
        )
        _assert_job_aggregates(SimpleNamespace(stats=stats), "deepseek")
        next(iter(evals.values())).reward_stats = {"reward": {1: ["trial"]}}
        with self.assertRaisesRegex(RuntimeError, "scoring data"):
            _assert_job_aggregates(SimpleNamespace(stats=stats), "deepseek")


if __name__ == "__main__":
    unittest.main()
