import json
import os
import shutil
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import yaml
from harbor.models.job.lock import JobLock
from harbor.models.job.result import JobResult, JobStats
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult

from benchmarks.terminal_bench import live_route_probe as probe
from benchmarks.terminal_bench import paired_results as paired
from benchmarks.terminal_bench.experiment_contract import (
    CODEX_PROVIDER_RETRY_POLICY,
    ENVIRONMENT_IMPORT,
    EXPERIMENT_ID,
    LIVE_ROUTE_PROBE_AGENT,
    LIVE_ROUTE_PROBE_AGENT_IMPORT,
    LIVE_ROUTE_PROBE_CAP_ENV,
    LIVE_ROUTE_PROBE_COMMAND,
    LIVE_ROUTE_PROBE_LIMITS,
    LIVE_ROUTE_PROBE_TASK,
    canonical_json,
    live_route_probe_variant,
    relay_claim_name,
)
from benchmarks.terminal_bench.test_paired_results import RunFixture, _relay


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


class ProbeFixture:
    image = "sha256:" + "1" * 64
    build = "sha256:" + "2" * 64
    source = "a" * 40

    def __init__(
        self,
        parent: Path,
        provider: str = "deepseek",
        *,
        relay_lifecycles: tuple[dict[str, object], ...] | None = None,
        relay_budget_state: str | None = None,
        relay_rejected_requests: dict[str, int] | None = None,
    ) -> None:
        self.provider = provider
        self.model = paired._PROVIDERS[provider]["model"]
        self.relay_lifecycles = relay_lifecycles
        self.relay_budget_state = relay_budget_state
        self.relay_rejected_requests = relay_rejected_requests
        self.root = (parent / "run").resolve()
        self.root.mkdir()
        self.authorizations = self.root / "authorizations"
        self.authorizations.mkdir(mode=0o700)
        task = self.root / "tasks" / "live-route-probe"
        shutil.copytree(
            paired._repo_root()
            / "benchmarks"
            / "terminal_bench"
            / "live-route-probe-task",
            task,
        )
        self.preflight = {
            "schemaVersion": 1,
            "experimentId": EXPERIMENT_ID,
            "replicationId": "screen-v1",
            "sourceRevision": self.source,
            "experimentManifestSha256": "sha256:" + "3" * 64,
            "relayBuildSha256": self.build,
            "relayImageSha256": self.image,
            "taskSnapshotsSha256": "sha256:" + "4" * 64,
            "cleanTree": True,
            "createdAt": "2026-08-23T00:00:00Z",
        }
        self.binding = paired._expected_binding(
            self.preflight, paired._digest(self.preflight)
        )
        overlay = (
            self.root / "overlays" / f"relay.{provider}.live-route-probe.compose.yaml"
        )
        overlay.parent.mkdir()
        overlay.write_text(
            yaml.safe_dump(
                paired._pinned_overlay(
                    paired._repo_root(),
                    self.provider,
                    self.image,
                    live_route_probe=True,
                ),
                sort_keys=False,
            )
        )
        compose_sha = paired._digest_bytes(overlay.read_bytes())
        self.job_name = f"open-agent-lab-screen-v1-{provider}-live-route-probe"
        self.job_dir = self.root / "live-route-jobs" / self.provider / self.job_name
        self.job_dir.mkdir(parents=True)
        config = {
            "job_name": self.job_name,
            "jobs_dir": str(self.job_dir.parent),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "quiet": False,
            "timeout_multiplier": 1.0,
            "retry": {"max_retries": 0},
            "verifier": {"disable": True},
            "artifacts": [
                {
                    "source": "/var/lib/open-agent-lab/provider-metadata.ndjson",
                    "destination": "provider-metadata.ndjson",
                    "service": "open-agent-lab-relay",
                },
                {
                    "source": "/var/lib/open-agent-lab/provider-metadata.ndjson.sealed",
                    "destination": "provider-metadata.ndjson.sealed",
                    "service": "open-agent-lab-relay",
                },
            ],
            "environment": {
                "type": "docker",
                "delete": True,
                "mounts": [
                    paired._codex_runtime_mount(
                        self.root / paired.CODEX_RUNTIME_PREPARED_RELATIVE
                    )
                ],
                "extra_docker_compose": [str(overlay)],
                "import_path": ENVIRONMENT_IMPORT,
                "kwargs": {
                    "relay_compose_sha256": compose_sha,
                    "run_binding": self.binding,
                },
            },
            "agents": [
                {
                    "name": LIVE_ROUTE_PROBE_AGENT,
                    "import_path": LIVE_ROUTE_PROBE_AGENT_IMPORT,
                    "model_name": f"{self.provider}/{self.model}",
                    "kwargs": {
                        "version": paired._CODEX_VERSION,
                        "reasoning_effort": paired._PROVIDERS[self.provider][
                            "reasoning"
                        ],
                        "enable_verify_instruction_v1": False,
                        "run_binding": self.binding,
                    },
                    "env": {
                        "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                        LIVE_ROUTE_PROBE_CAP_ENV: str(
                            self.authorizations / f"{provider}.cap.json"
                        ),
                    },
                }
            ],
            "tasks": [{"path": str(task), "source": LIVE_ROUTE_PROBE_TASK}],
        }
        config_path = self.root / "live-route-probes" / f"{provider}.yaml"
        config_path.parent.mkdir()
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        self.entry = {
            "provider": self.provider,
            "model": self.model,
            "reasoning": paired._PROVIDERS[self.provider]["reasoning"],
            "task": LIVE_ROUTE_PROBE_TASK,
            "config": f"live-route-probes/{provider}.yaml",
            "configSha256": paired._digest_bytes(config_path.read_bytes()),
            "jobDir": f"live-route-jobs/{self.provider}/{self.job_name}",
            "compose": f"overlays/relay.{provider}.live-route-probe.compose.yaml",
            "composeSha256": compose_sha,
            "relayImageSha256": self.image,
            "limits": dict(LIVE_ROUTE_PROBE_LIMITS),
        }
        self.pilot_entry = {
            "provider": self.provider,
            "model": self.model,
            "armOrder": (
                ["control-v1", "verify-instruction-v1"]
                if provider == "deepseek"
                else ["verify-instruction-v1", "control-v1"]
            ),
            "config": f"configs/{provider}.yaml",
            "configSha256": "sha256:" + "7" * 64,
            "jobDir": f"jobs/{provider}/open-agent-lab-screen-v1-{provider}",
            "compose": f"overlays/relay.{provider}.compose.yaml",
            "composeSha256": "sha256:" + "8" * 64,
            "relayImageSha256": self.image,
        }
        other = "zai" if provider == "deepseek" else "deepseek"
        self.pilot_entries = [self.pilot_entry, {"provider": other}]
        _write(self.root / "run-record.json", {"liveRouteProbes": [self.entry]})
        self.credential = parent / "credential"
        self.credential.write_bytes(b"fixture-provider-secret-00000000\n")
        self._job(config, task, overlay, compose_sha)
        self.cap = self.authorizations / f"{provider}.cap.json"
        self.authorization = self.authorizations / f"{provider}.json"
        self.write_cap()
        self.manifest = {
            "runtime": {"codexRuntime": {}},
            "relayBuildIds": {"production": self.build},
            "replications": [
                {
                    "id": "screen-v1",
                    "armOrderByProvider": {
                        "deepseek": ["control-v1", "verify-instruction-v1"],
                        "zai": ["verify-instruction-v1", "control-v1"],
                    },
                }
            ],
        }

    def _valid_relay_lifecycles(self) -> tuple[dict[str, object], ...]:
        if self.provider == "deepseek":
            return ({"output_tokens": 2}, {"output_tokens": 2})
        return (
            {"requested_max": None, "effective_max": 8_192, "output_tokens": 4},
            {
                "requested_max": None,
                "effective_max": 256,
                "terminal_event": "response.incomplete",
                "terminal_status": "incomplete",
                "incomplete_reason": "max_output_tokens",
                "output_tokens": 200,
            },
        )

    @staticmethod
    def _relay_totals(verified: dict[str, object]) -> dict[str, int]:
        records = verified["records"]
        assert isinstance(records, list)
        usages = [
            item["usage"]
            for item in records
            if isinstance(item, dict)
            and item.get("event") == "transport.responses.closed"
            and isinstance(item.get("usage"), dict)
        ]
        totals = {
            field: sum(int(usage.get(field, 0)) for usage in usages)
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
        }
        return totals

    def _job(
        self, config: dict[str, object], task: Path, overlay: Path, compose_sha: str
    ) -> None:
        parsed = paired.JobConfig.model_validate(config)
        trial = self.job_dir / "trial-00-live-route-probe"
        trial.mkdir()
        trial_id = UUID(int=101)
        job_id = UUID(int=102)
        started = datetime.now(timezone.utc) - timedelta(minutes=5)
        finished = started + timedelta(seconds=10)
        agent_started = started + timedelta(seconds=1)
        agent_finished = started + timedelta(seconds=8)
        verified = _relay(
            trial,
            self.provider,
            self.model,
            self.build,
            f"{self.provider}-live-route-probe",
            agent_started,
            agent_finished,
            budget_class=(
                "zai_route_probe" if self.provider == "zai" else "unmetered_route_probe"
            ),
            budget_state=self.relay_budget_state,
            rejected_requests=self.relay_rejected_requests,
            lifecycles=self.relay_lifecycles or self._valid_relay_lifecycles(),
        )
        totals = self._relay_totals(verified)
        session = "probe-session"
        harbor_binding = {
            "schema_version": 1,
            "harbor_context_id": str(trial_id),
            "harbor_session_id": f"{trial.name}__agent",
            "trajectory_session_id": session,
            "relay_instance_id": verified["seal"]["relayInstanceId"],
            "relay_build_id": verified["seal"]["buildId"],
            "relay_marker_sha256": verified["seal"]["markerSha256"],
            "codex_runtime_spec_sha256": paired.CODEX_RUNTIME_SPEC_SHA256,
            "provider_id": self.provider,
            "requested_model": self.model,
            "variant_id": "live-route-probe-v1",
            "requested_developer_instructions_sha256": None,
            "run_binding": self.binding,
        }
        harbor_binding["binding_sha256"] = paired._digest(harbor_binding)
        variant = live_route_probe_variant(self.provider, effect_verified=True)
        provider_data = {
            **verified,
            "harbor_binding": harbor_binding,
            "agent_variant": variant,
        }
        trial_config = TrialConfig(
            task=parsed.tasks[0],
            trial_name=trial.name,
            trials_dir=self.job_dir,
            install_only=parsed.install_only,
            timeout_multiplier=parsed.timeout_multiplier,
            agent_timeout_multiplier=parsed.agent_timeout_multiplier,
            verifier_timeout_multiplier=parsed.verifier_timeout_multiplier,
            agent_setup_timeout_multiplier=parsed.agent_setup_timeout_multiplier,
            environment_build_timeout_multiplier=parsed.environment_build_timeout_multiplier,
            agent=parsed.agents[0],
            user_agent=parsed.user_agent,
            environment=parsed.environment,
            verifier=parsed.verifier,
            artifacts=parsed.artifacts,
            extra_instruction_paths=parsed.extra_instruction_paths,
            extra_instructions=parsed.extra_instructions,
            job_id=job_id,
        )
        result = TrialResult.model_validate(
            {
                "id": trial_id,
                "task_name": LIVE_ROUTE_PROBE_TASK,
                "trial_name": trial.name,
                "trial_uri": trial.resolve().as_uri(),
                "task_id": json.loads(parsed.tasks[0].get_task_id().model_dump_json()),
                "source": LIVE_ROUTE_PROBE_TASK,
                "task_checksum": paired._TASK_RUNTIME_BINDINGS[LIVE_ROUTE_PROBE_TASK][
                    "taskChecksum"
                ],
                "config": json.loads(trial_config.model_dump_json()),
                "agent_info": {
                    "name": LIVE_ROUTE_PROBE_AGENT,
                    "version": paired._CODEX_VERSION,
                    "model_info": {"name": self.model, "provider": self.provider},
                },
                "agent_result": {
                    "n_input_tokens": totals["input_tokens"],
                    "n_cache_tokens": totals["cached_input_tokens"],
                    "n_output_tokens": totals["output_tokens"],
                    "metadata": {"open_agent_lab_provider": provider_data},
                },
                "verifier_result": None,
                "verifier_environment_mode": "shared",
                "exception_info": None,
                "started_at": started,
                "finished_at": finished,
                "environment_setup": {
                    "started_at": started,
                    "finished_at": started + timedelta(milliseconds=200),
                },
                "agent_setup": {
                    "started_at": started + timedelta(milliseconds=300),
                    "finished_at": started + timedelta(milliseconds=800),
                },
                "agent_execution": {
                    "started_at": agent_started,
                    "finished_at": agent_finished,
                },
                "verifier": None,
                "step_results": None,
            }
        )
        _write(
            trial / "config.json",
            trial_config.model_dump(mode="json", exclude_defaults=True),
        )
        self.result_path = trial / "result.json"
        _write(self.result_path, json.loads(result.model_dump_json()))
        trial_lock = paired.PreparedJob(
            self.root,
            self.provider,
            self.entry,
            self.job_dir,
            self.binding,
            config,
            overlay,
            compose_sha,
            "probe",
        ).expected_trial_locks()[0]
        _write(trial / "lock.json", trial_lock)
        trajectory = {
            "schema_version": "ATIF-v1.7",
            "session_id": session,
            "agent": {
                "name": "codex",
                "version": paired._CODEX_VERSION,
                "model_name": self.model,
            },
            "steps": [
                {
                    "step_id": 1,
                    "source": "agent",
                    "message": "",
                    "tool_calls": [
                        {
                            "tool_call_id": "call-1",
                            "function_name": "exec_command",
                            "arguments": {"cmd": LIVE_ROUTE_PROBE_COMMAND},
                        }
                    ],
                    "observation": {
                        "results": [
                            {
                                "source_call_id": "call-1",
                                "content": "Process exited with code 0",
                            }
                        ]
                    },
                    "extra": {
                        "status": "completed",
                        "tool_metadata": {"exit_code": 0},
                    },
                },
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "LIVE_ROUTE_PROBE_OK",
                },
            ],
            "final_metrics": {
                "total_prompt_tokens": totals["input_tokens"],
                "total_completion_tokens": totals["output_tokens"],
                "total_cached_tokens": totals["cached_input_tokens"],
                "total_steps": 2,
                "extra": {
                    "reasoning_output_tokens": totals["reasoning_output_tokens"],
                    "total_tokens": totals["total_tokens"],
                },
            },
        }
        self.trajectory_path = trial / "agent" / "trajectory.json"
        _write(self.trajectory_path, trajectory)
        cleanup = {
            "schemaVersion": 1,
            "experimentId": EXPERIMENT_ID,
            "replicationId": self.binding["replication_id"],
            "sourceRevision": self.source,
            "experimentManifestSha256": self.binding["experiment_manifest_sha256"],
            "preflightSha256": self.binding["preflight_sha256"],
            "runBindingSha256": paired._digest(self.binding),
            "relayImageSha256": self.image,
            "providerCredentialSha256": paired._digest_bytes(
                self.credential.read_bytes()
            ),
            "fullComposeSha256": "sha256:" + "5" * 64,
            "taskId": LIVE_ROUTE_PROBE_TASK,
            "taskDigest": paired._TASK_RUNTIME_BINDINGS[LIVE_ROUTE_PROBE_TASK][
                "taskDigest"
            ],
            "taskChecksum": paired._TASK_RUNTIME_BINDINGS[LIVE_ROUTE_PROBE_TASK][
                "taskChecksum"
            ],
            "sessionId": f"{trial.name}__env",
            "projectName": f"{trial.name}__env",
            "stoppedAt": (finished - timedelta(milliseconds=100)).isoformat(),
        }
        _write(trial / "environment-cleanup.json", cleanup)
        _write(
            self.job_dir / "config.json",
            parsed.model_dump(mode="json", exclude_defaults=True),
        )
        job_lock = JobLock.model_validate(
            {
                "schema_version": 3,
                "created_at": started,
                "harbor": {"version": "0.22.0", "is_editable": False},
                "n_concurrent_trials": 1,
                "retry": parsed.retry,
                "trials": [trial_lock],
            }
        )
        _write(
            self.job_dir / "lock.json",
            json.loads(job_lock.model_dump_json(exclude_none=True)),
        )
        job_result = JobResult(
            id=job_id,
            started_at=started.replace(tzinfo=None),
            updated_at=finished.replace(tzinfo=None),
            finished_at=finished.replace(tzinfo=None),
            n_total_trials=1,
            stats=JobStats.from_trial_results([result], n_total_trials=1),
        )
        _write(
            self.job_dir / "result.json",
            json.loads(job_result.model_dump_json(exclude={"trial_results"})),
        )
        self.trial = trial
        self.job_id = job_id
        self.agent_started = agent_started
        self.probe_lock = trial_lock

    def write_cap(self, *, expired: bool = False) -> None:
        now = datetime.now(timezone.utc)
        observed = now - (timedelta(hours=2) if expired else timedelta(minutes=10))
        expires = (
            now - timedelta(hours=1)
            if expired
            else now + timedelta(hours=4, minutes=30)
            if self.provider == "zai"
            else now + timedelta(hours=5)
        )
        stamp = lambda value: value.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        control: dict[str, object] = {
            "controlClass": "provider_hard_spend_cap_usd",
            "scope": "campaign",
            "limitUsd": 2,
            "observedAt": stamp(observed),
            "expiresAt": stamp(expires),
            "evidenceSha256": "sha256:" + "6" * 64,
            "sourceUrls": {"providerControl": "https://platform.deepseek.com/"},
            "assertedBy": "fixture operator",
        }
        if self.provider == "zai":
            control = {
                "controlClass": ("coding_plan_subscription_quota_no_balance_deduction"),
                "scope": "campaign",
                "baseUrl": "https://api.z.ai/api/v1",
                "protocol": "openai_responses",
                "plan": "zai_coding_plan",
                "noBalanceDeduction": True,
                "quotaSnapshot": {
                    "fiveHour": {
                        "remainingPercent": 80,
                        "resetsAt": stamp(observed + timedelta(hours=5)),
                    },
                    "weekly": {
                        "remainingPercent": 70,
                        "resetsAt": stamp(observed + timedelta(days=7)),
                    },
                },
                "observedAt": stamp(observed),
                "expiresAt": stamp(expires),
                "evidenceSha256": "sha256:" + "6" * 64,
                "sourceUrls": {
                    "endpointProtocol": "https://docs.z.ai/devpack/tool/others",
                    "providerControl": "https://docs.z.ai/devpack/faq",
                },
                "assertedBy": "fixture operator",
            }
        self.cap.write_bytes(
            canonical_json(
                {
                    "schemaVersion": 2,
                    "proofClass": "live-route-provider-authorization-v2",
                    "experimentId": EXPERIMENT_ID,
                    "provider": self.provider,
                    "model": self.model,
                    "preflightSha256": self.binding["preflight_sha256"],
                    "providerCredentialSha256": paired._digest_bytes(
                        self.credential.read_bytes()
                    ),
                    "providerControl": control,
                    "verification": "operator_attested",
                }
            )
        )

    def bind_credential(self, value: bytes) -> None:
        self.credential.write_bytes(value)
        self.write_cap()
        cleanup_path = self.trial / "environment-cleanup.json"
        cleanup = json.loads(cleanup_path.read_text())
        cleanup["providerCredentialSha256"] = paired._digest_bytes(value)
        _write(cleanup_path, cleanup)

    def write_probe_claim(self) -> Path:
        lock_sha256 = paired._digest(self.probe_lock)
        path = self.authorizations / relay_claim_name(
            self.provider, "probe", lock_sha256
        )
        _write(
            path,
            {
                "schemaVersion": 1,
                "proofClass": "probe-relay-slot-claim-v1",
                "provider": self.provider,
                "policySha256": paired._digest_bytes(self.cap.read_bytes()),
                "jobId": str(self.job_id),
                "jobDir": str(self.job_dir),
                "trialLockSha256": lock_sha256,
                "claimedAt": (self.agent_started - timedelta(milliseconds=1))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            },
        )
        path.chmod(0o600)
        return path

    def prepared_probe(
        self, config: dict[str, object] | None = None
    ) -> paired.PreparedJob:
        overlay = self.root / self.entry["compose"]
        return paired.PreparedJob(
            self.root,
            self.provider,
            self.entry,
            self.job_dir,
            self.binding,
            config or yaml.safe_load((self.root / self.entry["config"]).read_text()),
            overlay,
            self.entry["composeSha256"],
            "probe",
        )

    def verify(
        self, output: Path | None = None, *, with_claim: bool = True
    ) -> dict[str, object]:
        if with_claim:
            claim = self.authorizations / relay_claim_name(
                self.provider, "probe", paired._digest(self.probe_lock)
            )
            if not claim.exists():
                self.write_probe_claim()
        probes = json.loads((self.root / "run-record.json").read_text())[
            "liveRouteProbes"
        ]
        with (
            patch.object(
                paired,
                "_manifest",
                return_value=(
                    self.manifest,
                    {},
                    self.preflight["experimentManifestSha256"],
                ),
            ),
            patch.object(
                paired,
                "_validate_record",
                return_value=(self.preflight, self.pilot_entries, probes),
            ),
            patch.object(paired, "_validated_job_dir"),
        ):
            return probe.verify_probe(
                self.root,
                self.provider,
                self.credential,
                self.cap,
                output,
            )

    def validate_authorization(self, *, scheduled: bool = True) -> dict[str, object]:
        pilot_job = self.root / self.pilot_entry["jobDir"]
        pilot_compose = self.root / self.pilot_entry["compose"]
        trial_lock_sha256 = "sha256:" + "a" * 64
        admission_path = probe.pilot_scheduler_admission_path(
            self.root, self.provider, trial_lock_sha256
        )
        if scheduled:
            _write(
                admission_path,
                {
                    "schemaVersion": 1,
                    "proofClass": "sequential-pilot-trial-admission-v1",
                    "experimentId": EXPERIMENT_ID,
                    "planSha256": "sha256:" + "b" * 64,
                    "ordinal": 1,
                    "replicationId": self.binding["replication_id"],
                    "provider": self.provider,
                    "model": self.model,
                    "preflightSha256": self.binding["preflight_sha256"],
                    "jobId": str(UUID(int=102)),
                    "jobDir": str(pilot_job),
                    "trialLockSha256": trial_lock_sha256,
                    "previousCheckpointSha256": None,
                    "admittedAt": datetime.now(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                },
            )
            os.chmod(admission_path, 0o600)
            admission = probe.active_pilot_scheduler_admission(
                paired._digest_bytes(admission_path.read_bytes())
            )
        else:
            admission = nullcontext()
        probes = json.loads((self.root / "run-record.json").read_text())[
            "liveRouteProbes"
        ]
        with (
            admission,
            patch.object(
                paired,
                "_manifest",
                return_value=(
                    self.manifest,
                    {},
                    self.preflight["experimentManifestSha256"],
                ),
            ),
            patch.object(
                paired,
                "_validate_record",
                return_value=(self.preflight, self.pilot_entries, probes),
            ),
            patch.object(
                paired,
                "_validated_job_dir",
                return_value=(
                    self.provider,
                    pilot_job,
                    {},
                    pilot_compose,
                    self.pilot_entry["composeSha256"],
                ),
            ),
            patch.object(
                paired.PreparedJob,
                "claim_active_trial",
                return_value=(UUID(int=102), trial_lock_sha256),
            ),
        ):
            return probe.validate_pilot_authorization(
                self.authorization,
                self.provider,
                self.model,
                self.binding,
                self.credential,
                pilot_job / "trial-00",
            )

    def validate_cap(self, trial: Path | None = None) -> dict[str, object]:
        probes = json.loads((self.root / "run-record.json").read_text())[
            "liveRouteProbes"
        ]
        with (
            patch.object(
                paired,
                "_manifest",
                return_value=(
                    self.manifest,
                    {},
                    self.preflight["experimentManifestSha256"],
                ),
            ),
            patch.object(
                paired,
                "_validate_record",
                return_value=(self.preflight, self.pilot_entries, probes),
            ),
            patch.object(paired, "_validated_job_dir"),
        ):
            return probe.validate_probe_cap(
                self.cap,
                self.provider,
                self.model,
                self.binding,
                self.credential,
                trial or self.trial,
            )


class LiveRouteProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = self._new_fixture()
        self.base = self.fixture.root.parent

    def _new_fixture(
        self,
        provider: str = "deepseek",
        *,
        relay_lifecycles: tuple[dict[str, object], ...] | None = None,
        relay_budget_state: str | None = None,
        relay_rejected_requests: dict[str, int] | None = None,
    ) -> ProbeFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return ProbeFixture(
            Path(temporary.name),
            provider,
            relay_lifecycles=relay_lifecycles,
            relay_budget_state=relay_budget_state,
            relay_rejected_requests=relay_rejected_requests,
        )

    def test_valid_probe_publishes_minimal_fail_closed_receipt(self) -> None:
        output = self.fixture.authorization
        receipt = self.fixture.verify(output)
        self.assertEqual(receipt["schemaVersion"], 3)
        self.assertEqual(receipt["proofClass"], "live-route-probe-v3")
        self.assertTrue(receipt["benchmarkStartAuthorized"])
        self.assertTrue(receipt["liveProviderRouteObserved"])
        self.assertFalse(receipt["liveProviderConformance"])
        self.assertFalse(receipt["benchmarkTaskInstructionUsed"])
        self.assertFalse(receipt["benchmarkRewardUsed"])
        self.assertEqual(receipt["requestCount"], 2)
        self.assertEqual(receipt["pilotJob"]["config"], "configs/deepseek.yaml")
        self.assertEqual(receipt["providerControl"]["limitUsd"], 2)
        self.assertIsNone(receipt["outputBudgetProbe"])
        self.assertEqual(receipt["harborTrialRetries"], 0)
        self.assertEqual(
            receipt["codexProviderRetryPolicy"], dict(CODEX_PROVIDER_RETRY_POLICY)
        )
        self.assertGreaterEqual(receipt["credentialLeakScan"]["directories"], 1)
        paired._probe_receipt_payloads(receipt, self.fixture.pilot_entry)
        self.assertEqual(output.read_bytes(), canonical_json(receipt))
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        self.assertNotIn(
            self.fixture.credential.read_bytes().strip(), output.read_bytes()
        )
        self.assertEqual(self.fixture.validate_authorization(), receipt)

        malformed = json.loads(json.dumps(receipt))
        malformed["credentialLeakScan"].pop("directories")
        with self.assertRaisesRegex(paired.IntegrityError, "directories"):
            paired._probe_receipt_payloads(malformed, self.fixture.pilot_entry)

    def test_zai_two_round_truncation_publishes_exact_receipt(self) -> None:
        fixture = self._new_fixture("zai")
        receipt = fixture.verify(fixture.authorization)
        self.assertTrue(receipt["benchmarkStartAuthorized"])
        self.assertTrue(receipt["empiricalResponseTruncationObserved"])
        self.assertEqual(receipt["requestCount"], 2)
        provider_data = json.loads(fixture.result_path.read_text())["agent_result"][
            "metadata"
        ]["open_agent_lab_provider"]
        closed = [
            item
            for item in provider_data["records"]
            if item["event"] == "transport.responses.closed"
        ]
        self.assertEqual(
            receipt["outputBudgetProbe"],
            {
                "schemaVersion": 1,
                "proofClass": "empirical-responses-truncation-v1",
                "evidenceScope": "exact_observed_date_model_endpoint",
                "observedAt": closed[-1]["at"],
                "endpoint": "https://api.z.ai/api/v1/responses",
                "model": fixture.model,
                "protocol": "openai_responses",
                "accountingMode": "fixed_round_allocations",
                "burnedAccounting": "reserved_budget_retired_not_usage",
                "slotOutputTokenLimit": 8_448,
                "minimumRequestedRound2OutputTokens": 1_024,
                "rounds": [
                    {
                        "ordinal": 1,
                        "effectiveMaxOutputTokens": 8_192,
                        "reportedOutputTokens": 4,
                        "burnedOutputBudgetTokens": 8_192,
                        "terminalEvent": "response.completed",
                        "terminalStatus": "completed",
                        "incompleteReason": None,
                    },
                    {
                        "ordinal": 2,
                        "effectiveMaxOutputTokens": 256,
                        "reportedOutputTokens": 200,
                        "burnedOutputBudgetTokens": 256,
                        "terminalEvent": "response.incomplete",
                        "terminalStatus": "incomplete",
                        "incompleteReason": "max_output_tokens",
                    },
                ],
                "totalReportedOutputTokens": 204,
                "totalBurnedOutputBudgetTokens": 8_448,
                "noThirdRequest": True,
            },
        )
        trajectory = json.loads(fixture.trajectory_path.read_text())
        calls = [
            call for step in trajectory["steps"] for call in step.get("tool_calls", [])
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function_name"], "exec_command")
        self.assertEqual(calls[0]["arguments"], {"cmd": LIVE_ROUTE_PROBE_COMMAND})
        self.assertTrue(provider_data["agent_variant"]["effect_verified"])

    def test_zai_incomplete_output_allows_missing_final_artifacts(self) -> None:
        fixture = self._new_fixture("zai")
        trajectory = json.loads(fixture.trajectory_path.read_text())
        trajectory["steps"] = trajectory["steps"][:1]
        trajectory.pop("final_metrics")
        _write(fixture.trajectory_path, trajectory)
        receipt = fixture.verify(fixture.authorization)
        self.assertTrue(receipt["benchmarkStartAuthorized"])
        self.assertEqual(
            receipt["outputBudgetProbe"]["rounds"][1]["incompleteReason"],
            "max_output_tokens",
        )

    def test_zai_round_and_accounting_mutations_fail_closed(self) -> None:
        completed = {
            "requested_max": None,
            "effective_max": 8_192,
            "output_tokens": 4,
        }
        incomplete = {
            "requested_max": None,
            "effective_max": 256,
            "terminal_event": "response.incomplete",
            "terminal_status": "incomplete",
            "incomplete_reason": "max_output_tokens",
            "output_tokens": 200,
        }
        cases = (
            (
                "round-2-completed",
                (completed, {"effective_max": 256, "output_tokens": 200}),
                "poisoned",
                None,
            ),
            (
                "wrong-incomplete-reason",
                (
                    completed,
                    {**incomplete, "incomplete_reason": "content_filter"},
                ),
                "poisoned",
                None,
            ),
            (
                "missing-usage",
                (completed, {**incomplete, "output_tokens": None}),
                "poisoned",
                None,
            ),
            (
                "round-2-over-limit",
                (completed, {**incomplete, "output_tokens": 257}),
                "poisoned",
                None,
            ),
            (
                "relay-rejection",
                (completed, incomplete),
                None,
                {"invalid_json": 1},
            ),
        )
        for label, lifecycles, state, rejected in cases:
            fixture = self._new_fixture(
                "zai",
                relay_lifecycles=lifecycles,
                relay_budget_state=state,
                relay_rejected_requests=rejected,
            )
            with (
                self.subTest(label=label),
                self.assertRaises(paired.IntegrityError),
            ):
                fixture.verify()

        invalid_evidence = (
            (
                "round-1-incomplete",
                {
                    **completed,
                    "terminal_event": "response.incomplete",
                    "terminal_status": "incomplete",
                    "incomplete_reason": "max_output_tokens",
                },
                incomplete,
            ),
            (
                "third-request",
                completed,
                incomplete,
                {"effective_max": 1, "output_tokens": 1},
            ),
        )
        for label, *lifecycles in invalid_evidence:
            with self.subTest(label=label), self.assertRaises(ValueError):
                self._new_fixture(
                    "zai",
                    relay_lifecycles=lifecycles,
                    relay_budget_state="poisoned",
                )

        fixture = self._new_fixture("zai")
        trajectory = json.loads(fixture.trajectory_path.read_text())
        trajectory["steps"][0]["tool_calls"] *= 2
        _write(fixture.trajectory_path, trajectory)
        with self.assertRaises(paired.IntegrityError):
            fixture.verify()

    def test_probe_receipt_requires_a_pre_execution_claim(self) -> None:
        with self.assertRaisesRegex(paired.IntegrityError, "claim is unavailable"):
            self.fixture.verify(with_claim=False)

    def test_probe_gate_binds_credential_window_and_single_slot(self) -> None:
        self.assertEqual(self.fixture.validate_cap()["providerControl"]["limitUsd"], 2)
        claims = list(self.fixture.authorizations.glob("deepseek.probe.*.claim.json"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(paired.IntegrityError, "already claimed"):
            self.fixture.validate_cap()

        fixture = self._new_fixture()
        fixture.credential.write_text("another-provider-account-secret-0001\n")
        with self.assertRaisesRegex(paired.IntegrityError, "another provider"):
            fixture.validate_cap()

        fixture = self._new_fixture()
        cap = json.loads(fixture.cap.read_text())
        cap["preflightSha256"] = "sha256:" + "f" * 64
        fixture.cap.write_bytes(canonical_json(cap))
        with self.assertRaisesRegex(paired.IntegrityError, "policy drifted"):
            fixture.validate_cap()

        fixture = self._new_fixture()
        cap = json.loads(fixture.cap.read_text())
        cap["providerControl"]["expiresAt"] = (
            (datetime.now(timezone.utc) + timedelta(minutes=5))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        fixture.cap.write_bytes(canonical_json(cap))
        with self.assertRaisesRegex(paired.IntegrityError, "relay lifetime"):
            fixture.validate_cap()

    def test_credential_boundary_matches_relay_ascii_trim(self) -> None:
        fixture = self._new_fixture()
        fixture.bind_credential(b"\t fixture-provider-secret-00000000 \r\n")
        self.assertTrue(fixture.verify()["liveProviderRouteObserved"])

        for label, credentials in (
            ("short", b"x" * 31 + b"\n"),
            ("internal-newline", b"x" * 16 + b"\n" + b"x" * 16),
            ("nul", b"x" * 32 + b"\0"),
            ("delete", b"x" * 32 + b"\x7f"),
            ("bom", b"\xef\xbb\xbf" + b"x" * 32),
            ("nbsp", "\u00a0".encode() + b"x" * 32),
        ):
            fixture = self._new_fixture()
            fixture.bind_credential(credentials)
            with (
                self.subTest(label=label, phase="probe authorization"),
                self.assertRaisesRegex(paired.IntegrityError, "ASCII bytes"),
            ):
                fixture.validate_cap()
            self.assertFalse(list(fixture.authorizations.glob("*.claim.json")))

            output = fixture.authorization
            with (
                self.subTest(label=label, phase="receipt"),
                self.assertRaisesRegex(paired.IntegrityError, "ASCII bytes"),
            ):
                fixture.verify(output)
            self.assertFalse(output.exists())

    def test_probe_gate_rejects_an_invalid_other_provider_before_claiming(self) -> None:
        probes = json.loads((self.fixture.root / "run-record.json").read_text())[
            "liveRouteProbes"
        ]

        def validate_all_providers(
            _run_dir: Path, entry: dict[str, object], *_args: object
        ) -> None:
            if entry.get("provider") == "zai":
                raise paired.IntegrityError("other provider is not fully prepared")

        with (
            patch.object(
                paired,
                "_manifest",
                return_value=(
                    self.fixture.manifest,
                    {},
                    self.fixture.preflight["experimentManifestSha256"],
                ),
            ),
            patch.object(
                paired,
                "_validate_record",
                return_value=(
                    self.fixture.preflight,
                    self.fixture.pilot_entries,
                    probes,
                ),
            ),
            patch.object(
                paired, "_validated_job_dir", side_effect=validate_all_providers
            ),
            self.assertRaisesRegex(
                paired.IntegrityError, "other provider is not fully prepared"
            ),
        ):
            probe.validate_probe_cap(
                self.fixture.cap,
                self.fixture.provider,
                self.fixture.model,
                self.fixture.binding,
                self.fixture.credential,
                self.fixture.trial,
            )
        self.assertFalse(list(self.fixture.authorizations.glob("*.claim.json")))

    def test_probe_claim_hashes_the_exact_validated_cap_bytes(self) -> None:
        validated = self.fixture.cap.read_bytes()

        def replace_after_validation(*_args: object) -> tuple[UUID, str]:
            replacement = json.loads(validated)
            replacement["providerControl"]["assertedBy"] = "concurrent replacement"
            self.fixture.cap.write_bytes(canonical_json(replacement))
            return self.fixture.job_id, paired._digest(self.fixture.probe_lock)

        with patch.object(
            paired.PreparedJob,
            "claim_active_trial",
            side_effect=replace_after_validation,
        ):
            self.fixture.validate_cap()
        claim_path = next(
            self.fixture.authorizations.glob("deepseek.probe.*.claim.json")
        )
        claim = json.loads(claim_path.read_text())
        self.assertEqual(claim["policySha256"], paired._digest_bytes(validated))
        self.assertNotEqual(
            claim["policySha256"], paired._digest_bytes(self.fixture.cap.read_bytes())
        )
        with self.assertRaisesRegex(paired.IntegrityError, "claim differs"):
            self.fixture.verify()

        probe_trial = probe._probe_trial

        def restore_before_trial(*args: object) -> object:
            self.fixture.cap.write_bytes(validated)
            return probe_trial(*args)

        with (
            patch.object(probe, "_probe_trial", side_effect=restore_before_trial),
            self.assertRaisesRegex(paired.IntegrityError, "claim differs"),
        ):
            self.fixture.verify(self.fixture.authorization)
        self.assertFalse(self.fixture.authorization.exists())

    def test_pilot_gate_requires_full_window_and_single_trial_slot(self) -> None:
        self.fixture.verify(self.fixture.authorization)
        self.fixture.validate_authorization()
        with self.assertRaisesRegex(paired.IntegrityError, "already claimed"):
            self.fixture.validate_authorization()

        fixture = self._new_fixture()
        cap = json.loads(fixture.cap.read_text())
        cap["providerControl"]["expiresAt"] = (
            (datetime.now(timezone.utc) + timedelta(hours=3))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        fixture.cap.write_bytes(canonical_json(cap))
        fixture.verify(fixture.authorization)
        with self.assertRaisesRegex(paired.IntegrityError, "relay lifetime"):
            fixture.validate_authorization()

    def test_direct_harbor_pilot_bypass_lacks_scheduler_admission(self) -> None:
        self.fixture.verify(self.fixture.authorization)
        with self.assertRaisesRegex(
            paired.IntegrityError, "project-owned sequential launcher"
        ):
            self.fixture.validate_authorization(scheduled=False)
        self.assertEqual(
            list(self.fixture.authorizations.glob("deepseek.pilot.*.claim.json")), []
        )

    def test_unpublished_or_misplaced_receipt_never_authorizes_a_pilot(self) -> None:
        self.assertFalse(self.fixture.verify()["benchmarkStartAuthorized"])
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify(self.base / "receipt.json")

    def test_tool_lifecycle_and_final_response_are_required(self) -> None:
        for mutation in ("observation", "exit", "final"):
            fixture = self._new_fixture()
            path = fixture.trial / "agent" / "trajectory.json"
            trajectory = json.loads(path.read_text())
            if mutation == "observation":
                trajectory["steps"][0].pop("observation")
            elif mutation == "exit":
                trajectory["steps"][0]["extra"]["tool_metadata"]["exit_code"] = 1
            else:
                trajectory["steps"][1]["message"] = "almost"
            _write(path, trajectory)
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(paired.IntegrityError),
            ):
                fixture.verify()

    def test_pilot_authorization_revalidates_evidence_and_credential(self) -> None:
        self.fixture.verify(self.fixture.authorization)
        receipt = json.loads(self.fixture.authorization.read_text())
        receipt["pilotJob"]["configSha256"] = "sha256:" + "0" * 64
        _write(self.fixture.authorization, receipt)
        os.chmod(self.fixture.authorization, 0o600)
        with self.assertRaises(paired.IntegrityError):
            self.fixture.validate_authorization()

        self.fixture = self._new_fixture()
        self.fixture.verify(self.fixture.authorization)
        self.fixture.credential.write_text("replacement-provider-secret-00000000\n")
        with self.assertRaises(paired.IntegrityError):
            self.fixture.validate_authorization()

    def test_active_job_gate_rejects_wrong_path_config_and_locks(self) -> None:
        config = yaml.safe_load(
            (self.fixture.root / self.fixture.entry["config"]).read_text()
        )
        self.fixture.prepared_probe(config).claim_active_trial(self.fixture.trial)

        wrong = self.fixture.root / "alternate-job" / self.fixture.trial.name
        wrong.mkdir(parents=True)
        with self.assertRaisesRegex(paired.IntegrityError, "another Harbor job"):
            self.fixture.prepared_probe(config).claim_active_trial(wrong)

        for target, mutate in (
            (
                "config.json",
                lambda value: value.update({"quiet": True}),
            ),
            (
                "lock.json",
                lambda value: value.update({"n_concurrent_trials": 2}),
            ),
            (
                f"{self.fixture.trial.name}/lock.json",
                lambda value: value["agent"].update({"model_name": "wrong/model"}),
            ),
            (
                f"{self.fixture.trial.name}/config.json",
                lambda value: value.update({"trial_name": "wrong-trial"}),
            ),
        ):
            fixture = self._new_fixture()
            candidate_config = yaml.safe_load(
                (fixture.root / fixture.entry["config"]).read_text()
            )
            path = fixture.job_dir / target
            value = json.loads(path.read_text())
            mutate(value)
            _write(path, value)
            with self.subTest(target=target), self.assertRaises(paired.IntegrityError):
                fixture.prepared_probe(candidate_config).claim_active_trial(
                    fixture.trial
                )

    def test_pilot_active_job_matches_all_frozen_trial_locks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunFixture(Path(raw), "screen-v1")
            record = json.loads((run.root / "run-record.json").read_text())
            entry = next(
                item for item in record["providers"] if item["provider"] == "deepseek"
            )
            provider, job_dir, config, compose, compose_sha256 = (
                paired._validated_job_dir(
                    run.root,
                    entry,
                    run.preflight,
                    paired._digest(run.preflight),
                    list(paired._TASKS),
                    entry["armOrder"],
                )
            )
            prepared = paired.PreparedJob(
                run.root,
                provider,
                entry,
                job_dir,
                run.binding,
                config,
                compose,
                compose_sha256,
                "pilot",
            )
            task = run.tasks[0]
            variant = entry["armOrder"][0]
            trial = run.trials[(provider, task, variant)]
            result = json.loads((trial / "result.json").read_text())
            trial_config = TrialConfig.model_validate(result["config"])
            _write(
                trial / "config.json",
                trial_config.model_dump(mode="json", exclude_defaults=True),
            )
            prepared.claim_active_trial(trial)

            original = json.loads((trial / "lock.json").read_text())
            mutated = json.loads(json.dumps(original))
            mutated["agent"]["model_name"] = "deepseek/wrong-model"
            root_lock_path = job_dir / "lock.json"
            root_lock = json.loads(root_lock_path.read_text())
            index = next(
                index
                for index, item in enumerate(root_lock["trials"])
                if canonical_json(item) == canonical_json(original)
            )
            root_lock["trials"][index] = mutated
            _write(root_lock_path, root_lock)
            _write(trial / "lock.json", mutated)
            with self.assertRaisesRegex(paired.IntegrityError, "job lock differs"):
                prepared.claim_active_trial(trial)

    def test_third_request_is_rejected(self) -> None:
        fixture = self._new_fixture(
            relay_lifecycles=tuple({"output_tokens": 2} for _ in range(3))
        )
        with self.assertRaises(paired.IntegrityError):
            fixture.verify()

    def test_wrong_task_agent_and_unverified_effect_are_rejected(self) -> None:
        record = json.loads((self.fixture.root / "run-record.json").read_text())
        record["liveRouteProbes"][0]["task"] = "terminal-bench/video-processing"
        _write(self.fixture.root / "run-record.json", record)
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify()

        self.fixture = self._new_fixture()
        result_path = self.fixture.trial / "result.json"
        result = json.loads(result_path.read_text())
        result["config"]["agent"]["name"] = "wrong-agent"
        _write(result_path, result)
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify()

        self.fixture = self._new_fixture()
        result_path = self.fixture.trial / "result.json"
        result = json.loads(result_path.read_text())
        result["agent_result"]["metadata"]["open_agent_lab_provider"]["agent_variant"][
            "effect_verified"
        ] = False
        _write(result_path, result)
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify()

    def test_credential_leak_and_expired_cap_are_rejected(self) -> None:
        (self.fixture.trial / "leak.log").write_bytes(
            b"prefix " + self.fixture.credential.read_bytes().strip() + b" suffix"
        )
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify()

        self.fixture = self._new_fixture()
        decoy = self.base / "decoy-credential"
        decoy.write_text("not-the-used-credential\n")
        self.fixture.credential = decoy
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify()

        self.fixture = self._new_fixture()
        self.fixture.write_cap(expired=True)
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify()

    def test_receipt_cannot_be_overwritten(self) -> None:
        output = self.fixture.authorization
        self.fixture.verify(output)
        with self.assertRaises(paired.IntegrityError):
            self.fixture.verify(output)


if __name__ == "__main__":
    unittest.main()
