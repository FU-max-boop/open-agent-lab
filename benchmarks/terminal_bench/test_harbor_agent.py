import hashlib
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch
from uuid import UUID

import yaml
from harbor.agents.factory import AgentFactory
from harbor.agents.installed.codex import Codex
from harbor.models.agent.context import AgentContext
from harbor.models.job.config import JobConfig
from harbor.models.task.id import PackageTaskId
from harbor.models.trial.config import AgentConfig

from benchmarks.terminal_bench import live_route_probe as live_probe
from benchmarks.terminal_bench import paired_results as paired
from benchmarks.terminal_bench.codex_runtime import (
    CODEX_RUNTIME_ENTRYPOINT,
    CODEX_RUNTIME_SPEC_SHA256,
    HARBOR_CODEX_EXEC_PREFIX,
    build_full_tree_verification_command,
    codex_runtime_spec,
)
from benchmarks.terminal_bench.experiment_contract import (
    LIVE_ROUTE_PROBE_CAP_ENV,
    LIVE_ROUTE_PROBE_COMMAND,
    LIVE_ROUTE_PROBE_INSTRUCTION,
    PILOT_RECEIPT_ENV,
)
from benchmarks.terminal_bench.harbor_agent import (
    _EXPERIMENT_MANIFEST,
    _PROFILES,
    _RELAY_AUTHORIZE_COMMAND,
    _RELAY_BOOTSTRAP_COMMAND,
    _RELAY_TOKEN_COMMAND,
    _REPOSITORY_ROOT,
    _VERIFY_INSTRUCTION,
    _VERIFY_INSTRUCTION_SHA256,
    OpenAgentLabCodex,
    OpenAgentLabCodexLiveRouteProbe,
    OpenAgentLabCodexVerifyInstructionV1,
    _validate_live_source,
)
from benchmarks.terminal_bench.harbor_environment import (
    PinnedRelayDockerEnvironment,
)
from benchmarks.terminal_bench.relay_evidence import _canonical, relay_metadata
from benchmarks.terminal_bench.validate_harbor_e2e import (
    _assert_isolation_call,
    _isolation_command,
)

_DEFAULT_USAGE = object()
_RUN_BINDING = {
    "schema_version": 1,
    "experiment_id": "terminal-bench-2.1-verify-instruction-v1",
    "replication_id": "screen-v1",
    "source_revision": "a" * 40,
    "experiment_manifest_sha256": "sha256:" + "b" * 64,
    "relay_build_sha256": "sha256:" + "d" * 64,
    "relay_image_sha256": "sha256:" + "f" * 64,
    "preflight_sha256": "sha256:" + "c" * 64,
    "task_snapshots_sha256": "sha256:" + "9" * 64,
}
_CAPABILITY_ID = "e" * 64


def _pinned_environment_mock(
    service_exec: object, *, role: str = "fixture"
) -> PinnedRelayDockerEnvironment:
    environment = object.__new__(PinnedRelayDockerEnvironment)
    environment.service_exec = AsyncMock(side_effect=service_exec)
    environment._relay_role = role
    environment._provider_secret_path = None
    environment._provider_credential_identity = None
    environment.trial_paths = SimpleNamespace(trial_dir=Path("/tmp/oal-trial"))
    return environment


def _bootstrap_identity(
    *,
    provider: str = "zai",
    model: str = "glm-5.3",
    capability_id: str = _CAPABILITY_ID,
) -> str:
    return _canonical(
        {
            "schemaVersion": 1,
            "buildId": _RUN_BINDING["relay_build_sha256"],
            "provider": provider,
            "model": model,
            "capabilityId": capability_id,
        }
    )


def _relay_capability(
    capability_id: str = _CAPABILITY_ID, bearer: str = "a" * 64
) -> str:
    return _canonical(
        {"schemaVersion": 1, "capabilityId": capability_id, "bearer": bearer}
    )


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _relay_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_evidence(
    directory: Path,
    *,
    provider_id: str = "zai",
    returned_model: str = "glm-5.3",
    build_id: str = "sha256:" + "b" * 64,
    schema_version: object = 1,
    requests: tuple[tuple[int, str, object], ...] | None = None,
    rejected_requests: dict[str, int] | None = None,
) -> None:
    events = []
    for ordinal, (status, transport_state, usage) in enumerate(
        ((200, "completed", _DEFAULT_USAGE),) if requests is None else requests,
        1,
    ):
        requested_at = datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(
            milliseconds=(ordinal - 1) * 3
        )
        observed_usage = (
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
            if usage is _DEFAULT_USAGE
            else usage
        )
        terminal_response = {
            "id": f"response-test-{ordinal}",
            "model": returned_model,
        }
        if observed_usage is not None:
            terminal_response["usage"] = observed_usage
        response_body = "data:" + _canonical(
            {"type": "response.completed", "response": terminal_response}
        )
        request_body = _canonical({"model": "glm-5.3", "store": False, "stream": True})
        common = {
            "schemaVersion": schema_version,
            "relayVersion": "native-responses-relay-v1",
            "runId": "relay-test",
            "relayInstanceId": "00000000-0000-4000-8000-000000000001",
            "providerId": provider_id,
            "buildId": build_id,
            "ordinal": ordinal,
            "relayRequestId": f"00000000-0000-4000-8000-{ordinal:012d}",
        }
        events.extend(
            [
                {
                    **common,
                    "at": _relay_time(requested_at),
                    "event": "transport.responses.request",
                    "requestedModel": "glm-5.3",
                    "requestBytes": len(request_body.encode()),
                    "requestSha256": _digest(request_body),
                    "clientRequestId": f"client-test-{ordinal}",
                    "stream": True,
                },
                {
                    **common,
                    "at": _relay_time(requested_at + timedelta(milliseconds=1)),
                    "event": "transport.responses.headers",
                    "status": status,
                    "providerRequestId": f"provider-test-{ordinal}",
                    "modelHeader": "glm-5.3",
                    "headersMs": 1,
                },
                {
                    **common,
                    "at": _relay_time(requested_at + timedelta(milliseconds=2)),
                    "event": "transport.responses.closed",
                    "status": status,
                    "providerRequestId": f"provider-test-{ordinal}",
                    "transportState": transport_state,
                    "errorCategory": (
                        None
                        if transport_state == "completed"
                        else "client_disconnected"
                        if transport_state == "aborted"
                        else "upstream_failure"
                    ),
                    "responseBytes": len(response_body.encode()),
                    "responseSha256": _digest(response_body),
                    "durationMs": 2,
                    "firstByteMs": 1,
                    "parseErrors": 0,
                    "metadataConflicts": [],
                    "modelConsistency": "consistent",
                    "modelSources": {
                        "http.openai-model.0": "glm-5.3",
                        "event.response.completed.response.model.1": returned_model,
                    },
                    "returnedModel": returned_model,
                    "responseId": f"response-test-{ordinal}",
                    "systemFingerprint": None,
                    "terminalEvent": "response.completed",
                    "usage": observed_usage,
                },
            ]
        )
    previous = None
    lines = []
    for event in events:
        body = {**event, "previousEventSha256": previous}
        previous = _digest(_canonical(body))
        lines.append(_canonical({**body, "eventSha256": previous}))
    (directory / "provider-metadata.ndjson").write_text(
        "\n".join(lines) + ("\n" if lines else "")
    )
    marker = {
        "schemaVersion": schema_version,
        "relayVersion": "native-responses-relay-v1",
        "runId": "relay-test",
        "relayInstanceId": "00000000-0000-4000-8000-000000000001",
        "providerId": provider_id,
        "buildId": build_id,
        "state": "sealed",
        "expectedModel": "glm-5.3",
        "sealedAt": "2026-08-22T00:00:00.000Z",
        "eventCount": len(events),
        "chainHead": previous,
        "rejectedRequests": rejected_requests or {},
    }
    seal = {**marker, "markerSha256": _digest(_canonical(marker))}
    (directory / "provider-metadata.ndjson.sealed").write_text(_canonical(seal) + "\n")


class RelayMetadataTest(unittest.TestCase):
    def test_canonical_keys_follow_javascript_utf16_order(self) -> None:
        value = _canonical({"\ue000": 2, "\U00010000": 1})
        self.assertLess(value.index("\U00010000"), value.index("\ue000"))

    def test_complete_sealed_lifecycle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory)
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertEqual(metadata["publication_gate"], {"ok": True, "reasons": []})

    def test_raw_evidence_requires_unique_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            journal = directory / "provider-metadata.ndjson"
            seal = directory / "provider-metadata.ndjson.sealed"
            mutations = (
                (
                    journal,
                    lambda value: value.replace(
                        '"input_tokens":1',
                        '"input_tokens":"TOP_SECRET","input_tokens":1',
                        1,
                    ),
                ),
                (journal, lambda value: value.replace('"ordinal":1', '"ordinal": 1')),
                (
                    seal,
                    lambda value: value.replace(
                        '"state":"sealed"',
                        '"state":"TOP_SECRET","state":"sealed"',
                    ),
                ),
                (seal, lambda value: value.rstrip("\n")),
            )
            for path, mutate in mutations:
                with self.subTest(path=path.name, mutation=mutate):
                    _write_evidence(directory)
                    path.write_text(mutate(path.read_text()))
                    with self.assertRaises(ValueError) as caught:
                        relay_metadata(journal, seal)
                    self.assertNotIn("TOP_SECRET", str(caught.exception))

    def test_synthetic_provider_can_validate_transport_but_never_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory, provider_id="synthetic-fixture")
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertEqual(
                metadata["publication_gate"],
                {"ok": False, "reasons": ["synthetic_provider"]},
            )

    def test_post_terminal_disconnect_is_audited_but_not_a_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(
                directory,
                provider_id="synthetic-fixture",
                rejected_requests={"client_disconnected_after_close": 1},
            )
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertEqual(
                metadata["publication_gate"],
                {"ok": False, "reasons": ["synthetic_provider"]},
            )

    def test_real_rejection_still_blocks_with_a_post_terminal_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(
                directory,
                rejected_requests={
                    "client_disconnected_after_close": 1,
                    "invalid_json": 1,
                },
            )
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertEqual(
                metadata["publication_gate"],
                {"ok": False, "reasons": ["relay_rejected_requests"]},
            )

    def test_incomplete_or_wrong_model_evidence_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory, returned_model="other")
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertIn(
                "returned_model_mismatch", metadata["publication_gate"]["reasons"]
            )
            journal = directory / "provider-metadata.ndjson"
            journal.write_text("\n".join(journal.read_text().splitlines()[:2]) + "\n")
            with self.assertRaisesRegex(ValueError, "incomplete lifecycle"):
                relay_metadata(journal, directory / "provider-metadata.ndjson.sealed")

    def test_development_build_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory, build_id="development")
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertEqual(
                metadata["publication_gate"],
                {"ok": False, "reasons": ["unverifiable_relay_build"]},
            )

    def test_failed_request_and_missing_usage_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(
                directory,
                requests=(
                    (200, "completed", _DEFAULT_USAGE),
                    (429, "failed", _DEFAULT_USAGE),
                ),
                rejected_requests={"client_disconnected_after_close": 1},
            )
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertIn(
                "provider_request_incomplete_or_failed",
                metadata["publication_gate"]["reasons"],
            )

            _write_evidence(directory, requests=((200, "completed", None),))
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertIn(
                "usage_missing_or_invalid", metadata["publication_gate"]["reasons"]
            )

    def test_empty_sealed_journal_is_auditable_but_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory, requests=())
            with self.assertRaisesRegex(ValueError, "metadata is empty"):
                relay_metadata(
                    directory / "provider-metadata.ndjson",
                    directory / "provider-metadata.ndjson.sealed",
                )
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
                allow_empty=True,
            )
            self.assertEqual(metadata["event_count"], 0)
            self.assertIsNone(metadata["chain_head"])
            self.assertEqual(
                metadata["publication_gate"],
                {"ok": False, "reasons": ["no_completed_response"]},
            )

    def test_boolean_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory, schema_version=True)
            with self.assertRaisesRegex(ValueError, "Invalid lifecycle"):
                relay_metadata(
                    directory / "provider-metadata.ndjson",
                    directory / "provider-metadata.ndjson.sealed",
                )


class ProfileDriftTest(unittest.TestCase):
    def test_verify_experiment_file_hashes_are_frozen(self) -> None:
        root = Path(__file__).parents[2]
        manifest = json.loads(
            (
                root / "benchmarks/terminal_bench/verify-instruction-v1.experiment.json"
            ).read_text()
        )
        self.assertEqual(
            manifest["experimentId"], "terminal-bench-2.1-verify-instruction-v1"
        )
        self.assertEqual(manifest["runClass"], "development")
        for relative, expected in manifest["fileSha256"].items():
            actual = paired._frozen_file_digest(root, relative)
            self.assertEqual(actual, expected, relative)

    def test_verify_instruction_bytes_are_frozen(self) -> None:
        path = Path(__file__).with_name("verify-instruction-v1.txt")
        content = path.read_bytes()
        self.assertEqual(content.decode(), _VERIFY_INSTRUCTION)
        self.assertEqual(
            "sha256:" + hashlib.sha256(content).hexdigest(), _VERIFY_INSTRUCTION_SHA256
        )
        self.assertTrue(content.endswith(b"\n"))
        self.assertFalse(content.endswith(b"\n\n"))

    def test_provider_free_e2e_rejects_a_degraded_isolation_command(self) -> None:
        secret = b"provider-free-test-key"
        _assert_isolation_call({"cmd": _isolation_command(secret)}, secret)
        with self.assertRaisesRegex(RuntimeError, "isolation command drifted"):
            _assert_isolation_call(
                {"cmd": "printf 'Hello, world!\\n' > /app/hello.txt"}, secret
            )

    def test_typescript_compose_and_pilot_profiles_are_aligned(self) -> None:
        root = Path(__file__).parents[2]
        relay_command = (root / "apps/cli/src/relay-command.ts").read_text()
        experiment = json.loads(_EXPERIMENT_MANIFEST.read_text())
        runtime_tasks = experiment["runtime"]["taskOrder"]
        cases = (
            (
                "deepseek",
                "https://api.deepseek.com/responses",
                "deepseek-v4-pro",
            ),
            ("zai", "https://api.z.ai/api/v1/responses", "glm-5.3"),
        )
        for provider, endpoint, selected_model in cases:
            with self.subTest(provider=provider):
                self.assertIn(f'endpoint: "{endpoint}"', relay_command)
                for model in _PROFILES[provider]["models"]:
                    self.assertIn(f'"{model}"', relay_command)

                compose_name = f"relay.{provider}.compose.yaml"
                compose = yaml.safe_load(
                    (root / "benchmarks/terminal_bench" / compose_name).read_text()
                )
                command = compose["services"]["open-agent-lab-relay"]["command"]
                self.assertEqual(command[command.index("--provider") + 1], provider)
                self.assertEqual(command[command.index("--model") + 1], selected_model)
                self.assertEqual(
                    compose["services"]["open-agent-lab-relay"]["environment"][
                        "OAL_EXPECTED_RELAY_BUILD_ID"
                    ],
                    experiment["relayBuildIds"]["production"],
                )

                pilot = yaml.safe_load(
                    (
                        root / "benchmarks/terminal_bench" / f"pilot-v1.{provider}.yaml"
                    ).read_text()
                )
                self.assertEqual(
                    pilot["environment"]["extra_docker_compose"],
                    [f"benchmarks/terminal_bench/{compose_name}"],
                )
                self.assertEqual(
                    pilot["agents"][0]["model_name"],
                    f"{provider}/{selected_model}",
                )
                self.assertIn(selected_model, _PROFILES[provider]["models"])
                paired = yaml.safe_load(
                    (
                        root / "benchmarks/terminal_bench" / f"pilot-v2.{provider}.yaml"
                    ).read_text()
                )
                self.assertEqual(paired["n_concurrent_trials"], 1)
                self.assertEqual(paired["retry"], {"max_retries": 0})
                self.assertEqual(
                    paired["environment"]["extra_docker_compose"],
                    [f"benchmarks/terminal_bench/{compose_name}"],
                )
                self.assertEqual(
                    [agent["model_name"] for agent in paired["agents"]],
                    [f"{provider}/{selected_model}"] * 2,
                )
                self.assertEqual(
                    [
                        agent["kwargs"]["enable_verify_instruction_v1"]
                        for agent in paired["agents"]
                    ],
                    [False, True] if provider == "deepseek" else [True, False],
                )
                self.assertEqual(
                    [agent["kwargs"]["reasoning_effort"] for agent in paired["agents"]],
                    [_PROFILES[provider]["reasoning"]] * 2,
                )
                self.assertEqual(
                    paired["datasets"][0]["task_names"],
                    pilot["datasets"][0]["task_names"],
                )
                self.assertEqual(paired["datasets"][0]["task_names"], runtime_tasks)

    def test_terminal_bench_task_filters_use_harbor_package_names(self) -> None:
        root = Path(__file__).parents[2]
        runtime_tasks = json.loads(_EXPERIMENT_MANIFEST.read_text())["runtime"][
            "taskOrder"
        ]
        available = [
            PackageTaskId(
                org="terminal-bench",
                name=name.removeprefix("terminal-bench/"),
                ref="sha256:" + f"{index:064x}",
            )
            for index, name in enumerate(runtime_tasks, 1)
        ]
        for provider in _PROFILES:
            with self.subTest(provider=provider):
                config = yaml.safe_load(
                    (
                        root / "benchmarks/terminal_bench" / f"pilot-v2.{provider}.yaml"
                    ).read_text()
                )
                dataset = JobConfig.model_validate(config).datasets[0]
                selected = dataset._filter_task_ids(available)
                self.assertEqual([task.get_name() for task in selected], runtime_tasks)

    def test_provider_free_e2e_is_exact_and_only_overrides_the_entrypoint(self) -> None:
        root = Path(__file__).parents[2]
        benchmark = root / "benchmarks/terminal_bench"
        config = yaml.safe_load((benchmark / "harbor-e2e.yaml").read_text())
        self.assertEqual(
            config["datasets"],
            [
                {
                    "name": "harbor/hello-world",
                    "ref": "sha256:d10e96e201d6816b22553504e06e7de0153a26381e808d11404cbca530b9d388",
                }
            ],
        )
        expected_artifacts = [
            {
                "source": f"/var/lib/open-agent-lab/{name}",
                "destination": name,
                "service": "open-agent-lab-relay",
            }
            for name in (
                "provider-metadata.ndjson",
                "provider-metadata.ndjson.sealed",
            )
        ]
        self.assertEqual(config["artifacts"], expected_artifacts)
        self.assertEqual(
            config["environment"]["extra_docker_compose"],
            [
                "benchmarks/terminal_bench/relay.deepseek.compose.yaml",
                "benchmarks/terminal_bench/relay.fixture.compose.yaml",
            ],
        )
        fixture = yaml.safe_load((benchmark / "relay.fixture.compose.yaml").read_text())
        self.assertEqual(
            fixture,
            {
                "services": {
                    "open-agent-lab-relay": {
                        "build": {"target": "fixture"},
                        "environment": {
                            "OAL_EXPECTED_RELAY_BUILD_ID": json.loads(
                                _EXPERIMENT_MANIFEST.read_text()
                            )["relayBuildIds"]["providerFreeFixture"]
                        },
                        "entrypoint": [
                            "node",
                            "/app/apps/cli/relay-dist/relay-fixture-entry.js",
                        ],
                    }
                }
            },
        )
        self.assertFalse(config["agents"][0]["kwargs"]["enable_verify_instruction_v1"])
        treatment = yaml.safe_load(
            (benchmark / "harbor-verify-instruction-e2e.yaml").read_text()
        )
        self.assertEqual(
            treatment["environment"]["extra_docker_compose"],
            config["environment"]["extra_docker_compose"],
        )
        self.assertEqual(treatment["artifacts"], expected_artifacts)
        self.assertTrue(
            treatment["agents"][0]["kwargs"]["enable_verify_instruction_v1"]
        )
        self.assertEqual(
            treatment["agents"][0]["import_path"],
            "benchmarks.terminal_bench.harbor_agent:"
            "OpenAgentLabCodexVerifyInstructionV1",
        )


class HarborAdapterTest(unittest.IsolatedAsyncioTestCase):
    def test_sealed_relay_profile_does_not_claim_resume_support(self) -> None:
        self.assertFalse(OpenAgentLabCodex.SUPPORTS_RESUME)

    def test_codex_version_is_exact_before_parent_construction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for version in (None, "0.148.0", "latest", True):
                with (
                    self.subTest(version=version),
                    patch(
                        "benchmarks.terminal_bench.harbor_agent._validate_harbor_runtime"
                    ),
                    patch.object(Codex, "__init__", autospec=True) as parent,
                    self.assertRaisesRegex(ValueError, "exactly 0.149.0"),
                ):
                    OpenAgentLabCodex(
                        Path(raw),
                        model_name="zai/glm-5.3",
                        version=version,  # type: ignore[arg-type]
                    )
                parent.assert_not_called()

    def test_verify_instruction_is_opt_in_and_uses_real_codex_config(self) -> None:
        common = {
            "model_name": "zai/glm-5.3",
            "version": "0.149.0",
            "extra_env": {"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
        }
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            control = OpenAgentLabCodex(
                Path(raw) / "control", run_binding=_RUN_BINDING, **common
            )
            treatment = OpenAgentLabCodexVerifyInstructionV1(
                Path(raw) / "treatment", run_binding=_RUN_BINDING, **common
            )

        self.assertNotIn("developer_instructions", control._build_effective_config())
        self.assertEqual(
            control._build_effective_config()["features"],
            {"shell_zsh_fork": False, "unified_exec_zsh_fork": False},
        )
        self.assertEqual(
            treatment._build_effective_config()["developer_instructions"],
            _VERIFY_INSTRUCTION,
        )
        self.assertEqual(control._open_agent_lab_variant["variant_id"], "control-v1")
        self.assertEqual(
            treatment._open_agent_lab_variant,
            {
                "schema_version": 1,
                "variant_id": "verify-instruction-v1",
                "developer_instruction_requested": True,
                "requested_developer_instructions_sha256": _VERIFY_INSTRUCTION_SHA256,
            },
        )

    def test_live_route_probe_is_zero_retry_and_non_scoring(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            probe = OpenAgentLabCodexLiveRouteProbe(
                Path(raw),
                model_name="zai/glm-5.3",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={
                    "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                    LIVE_ROUTE_PROBE_CAP_ENV: str(
                        Path(raw) / "authorizations" / "zai.cap.json"
                    ),
                },
            )
            provider = probe._build_effective_config()["model_providers"][
                "open-agent-lab"
            ]
            self.assertEqual(provider["request_max_retries"], 0)
            self.assertEqual(provider["stream_max_retries"], 0)
            self.assertEqual(
                probe._open_agent_lab_variant["variant_id"], "live-route-probe-v1"
            )
            self.assertFalse(
                probe._open_agent_lab_variant["benchmark_task_instruction_used"]
            )
            self.assertFalse(probe._open_agent_lab_variant["benchmark_reward_used"])
            with self.assertRaisesRegex(ValueError, "cannot enable"):
                OpenAgentLabCodexLiveRouteProbe(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    enable_verify_instruction_v1=True,
                    run_binding=_RUN_BINDING,
                    extra_env={
                        "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                        LIVE_ROUTE_PROBE_CAP_ENV: str(
                            Path(raw) / "authorizations" / "zai.cap.json"
                        ),
                    },
                )

    async def test_live_route_probe_replaces_task_instruction_and_checks_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                probe = OpenAgentLabCodexLiveRouteProbe(
                    Path(raw),
                    model_name="deepseek/deepseek-v4-pro",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={
                        "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                        LIVE_ROUTE_PROBE_CAP_ENV: str(
                            Path(raw) / "authorizations" / "deepseek.cap.json"
                        ),
                    },
                )
            probe._extra_env["OAL_RELAY_TOKEN"] = "a" * 64
            parent_run = AsyncMock(
                side_effect=lambda *_args, **_kwargs: setattr(
                    probe, "_codex_launches", 1
                )
            )
            parent_exec = AsyncMock(return_value=SimpleNamespace(return_code=0))
            retain = AsyncMock()
            environment = _pinned_environment_mock(None, role="live-route-probe")
            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "run", new=parent_run),
                patch.object(Codex, "exec_as_agent", new=parent_exec),
                patch.object(probe, "_seal_and_retain", new=retain),
            ):
                await probe.run(
                    "PRIVATE BENCHMARK INSTRUCTION", environment, AgentContext()
                )
        parent_run.assert_awaited_once_with(
            LIVE_ROUTE_PROBE_INSTRUCTION, environment, ANY
        )
        self.assertNotIn("PRIVATE BENCHMARK", LIVE_ROUTE_PROBE_INSTRUCTION)
        self.assertIn(
            json.dumps(LIVE_ROUTE_PROBE_COMMAND), LIVE_ROUTE_PROBE_INSTRUCTION
        )
        parent_exec.assert_awaited_once()
        self.assertTrue(probe._open_agent_lab_variant["effect_verified"])
        retain.assert_awaited_once()

    def test_verify_instruction_switch_is_strict(self) -> None:
        common = {
            "model_name": "zai/glm-5.3",
            "version": "0.149.0",
            "extra_env": {"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
        }
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                OpenAgentLabCodex(
                    Path(raw), enable_verify_instruction_v1="true", **common
                )  # type: ignore[arg-type]
            with self.assertRaisesRegex(
                ValueError, "requires enable_verify_instruction_v1=true"
            ):
                OpenAgentLabCodexVerifyInstructionV1(
                    Path(raw), enable_verify_instruction_v1=False, **common
                )

    def test_live_run_binding_is_strict_and_copied(self) -> None:
        common = {
            "model_name": "zai/glm-5.3",
            "version": "0.149.0",
            "extra_env": {"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
        }
        with tempfile.TemporaryDirectory() as raw:
            source = dict(_RUN_BINDING)
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(Path(raw), run_binding=source, **common)
            source["replication_id"] = "tampered"
            self.assertEqual(agent._open_agent_lab_run_binding, _RUN_BINDING)

            for invalid in (
                {**_RUN_BINDING, "extra": True},
                {**_RUN_BINDING, "schema_version": True},
                {**_RUN_BINDING, "replication_id": "unknown"},
                {**_RUN_BINDING, "source_revision": "a" * 39},
                {**_RUN_BINDING, "relay_image_sha256": "sha256:bad"},
                {**_RUN_BINDING, "preflight_sha256": "sha256:bad"},
            ):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaisesRegex(ValueError, "run_binding"),
                ):
                    OpenAgentLabCodex(Path(raw), run_binding=invalid, **common)

    def test_bound_profile_validates_source_before_parent_constructor(self) -> None:
        events: list[str] = []
        with (
            tempfile.TemporaryDirectory() as raw,
            patch(
                "benchmarks.terminal_bench.harbor_agent._validate_live_source",
                side_effect=lambda _binding: events.append("source"),
            ),
            patch.object(
                Codex,
                "__init__",
                autospec=True,
                side_effect=lambda *_args, **_kwargs: events.append("parent"),
            ),
        ):
            OpenAgentLabCodex(
                Path(raw),
                model_name="zai/glm-5.3",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            )
        self.assertEqual(events, ["source", "parent"])

    def test_harbor_factory_logger_is_the_only_framework_injection(self) -> None:
        logger = logging.getLogger("open-agent-lab.harbor-shape")
        config = AgentConfig(
            import_path=("benchmarks.terminal_bench.harbor_agent:OpenAgentLabCodex"),
            model_name="zai/glm-5.3",
            kwargs={
                "version": "0.149.0",
                "reasoning_effort": "max",
                "run_binding": _RUN_BINDING,
            },
            env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
        )
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            agent = AgentFactory.create_agent_from_config(
                config, Path(raw), logger=logger
            )
        self.assertIs(agent.logger.parent, logger)
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
            self.assertRaisesRegex(ValueError, "constructor inputs"),
        ):
            AgentFactory.create_agent_from_config(
                config, Path(raw), logger=logger, prompt_template_path="forbidden"
            )

    def test_wrong_or_editable_harbor_fails_before_parent_constructor(self) -> None:
        for version, direct_url in (
            ("0.23.0", None),
            ("0.22.0", '{"dir_info":{"editable":true}}'),
        ):
            package = SimpleNamespace(
                version=version, read_text=lambda _name, value=direct_url: value
            )
            with (
                self.subTest(version=version, direct_url=direct_url),
                tempfile.TemporaryDirectory() as raw,
                patch(
                    "benchmarks.terminal_bench.harbor_agent.distribution",
                    return_value=package,
                ),
                patch.object(Codex, "__init__", autospec=True) as parent,
                self.assertRaisesRegex(RuntimeError, "non-editable 0.22.0"),
            ):
                OpenAgentLabCodex(Path(raw))
            parent.assert_not_called()

    def test_live_source_is_rechecked_before_provider_work(self) -> None:
        build_ids = json.loads(_EXPERIMENT_MANIFEST.read_text())["relayBuildIds"]
        binding = {
            **_RUN_BINDING,
            "experiment_manifest_sha256": (
                "sha256:"
                + hashlib.sha256(_EXPERIMENT_MANIFEST.read_bytes()).hexdigest()
            ),
            "relay_build_sha256": build_ids["providerFreeFixture"],
        }
        clean = [
            SimpleNamespace(returncode=0, stdout=f"{_REPOSITORY_ROOT}\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        with (
            patch.dict(
                "os.environ", {"OPEN_AGENT_LAB_REPO_ROOT": str(_REPOSITORY_ROOT)}
            ),
            patch("subprocess.run", side_effect=clean),
        ):
            _validate_live_source(binding)

        with (
            patch.dict(
                "os.environ", {"OPEN_AGENT_LAB_REPO_ROOT": str(_REPOSITORY_ROOT)}
            ),
            patch("subprocess.run", side_effect=clean),
        ):
            _validate_live_source(
                {**binding, "relay_build_sha256": build_ids["production"]}
            )

        blocked_manifest = json.loads(_EXPERIMENT_MANIFEST.read_text())
        blocked_manifest["runtime"]["hermeticCodexRuntimeReady"] = False
        with tempfile.TemporaryDirectory() as raw:
            blocked_path = Path(raw) / "manifest.json"
            blocked_path.write_text(json.dumps(blocked_manifest))
            blocked_binding = {
                **binding,
                "experiment_manifest_sha256": (
                    "sha256:" + hashlib.sha256(blocked_path.read_bytes()).hexdigest()
                ),
                "relay_build_sha256": build_ids["production"],
            }
            with (
                patch.dict(
                    "os.environ",
                    {"OPEN_AGENT_LAB_REPO_ROOT": str(_REPOSITORY_ROOT)},
                ),
                patch("subprocess.run", side_effect=clean),
                patch(
                    "benchmarks.terminal_bench.harbor_agent._EXPERIMENT_MANIFEST",
                    blocked_path,
                ),
                self.assertRaisesRegex(RuntimeError, "runtime bytes are frozen"),
            ):
                _validate_live_source(blocked_binding)

        dirty = [
            *clean[:2],
            SimpleNamespace(returncode=0, stdout=" M result-aware-edit\n", stderr=""),
        ]
        with (
            patch.dict(
                "os.environ", {"OPEN_AGENT_LAB_REPO_ROOT": str(_REPOSITORY_ROOT)}
            ),
            patch("subprocess.run", side_effect=dirty),
            self.assertRaisesRegex(RuntimeError, "drifted"),
        ):
            _validate_live_source(binding)

        with (
            patch.dict("os.environ", {"OPEN_AGENT_LAB_REPO_ROOT": "/tmp"}),
            self.assertRaisesRegex(RuntimeError, "drifted"),
        ):
            _validate_live_source(binding)

    def test_unbound_profile_and_legacy_fixture_bypass_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            common = {
                "model_name": "zai/glm-5.3",
                "version": "0.149.0",
                "extra_env": {"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            }
            with self.assertRaisesRegex(RuntimeError, "prepared run binding"):
                OpenAgentLabCodex(Path(raw) / "live", **common)
            with self.assertRaisesRegex(ValueError, "prepared source binding"):
                OpenAgentLabCodex(
                    Path(raw) / "fixture", provider_free_fixture=True, **common
                )

    def test_profile_rejects_any_non_sidecar_relay_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for value in (
                "https://attacker.example/v1",
                "http://localhost:8080/v1",
                "http://open-agent-lab-relay:8080/v1/",
            ):
                with (
                    self.subTest(value=value),
                    patch(
                        "benchmarks.terminal_bench.harbor_agent._validate_live_source"
                    ),
                    self.assertRaisesRegex(ValueError, "must be exactly"),
                ):
                    OpenAgentLabCodex(
                        Path(raw),
                        model_name="zai/glm-5.3",
                        version="0.149.0",
                        run_binding=_RUN_BINDING,
                        extra_env={"OAL_RELAY_URL": value},
                    )

    async def test_agent_and_relay_roles_cannot_cross(self) -> None:
        common = {
            "model_name": "zai/glm-5.3",
            "version": "0.149.0",
            "run_binding": _RUN_BINDING,
            "extra_env": {"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
        }
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            pilot = OpenAgentLabCodex(Path(raw) / "pilot", **common)
            probe = OpenAgentLabCodexLiveRouteProbe(
                Path(raw) / "probe",
                **{
                    **common,
                    "extra_env": {
                        "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                        LIVE_ROUTE_PROBE_CAP_ENV: str(
                            Path(raw) / "authorizations" / "zai.cap.json"
                        ),
                    },
                },
            )
        for agent, role in (
            (pilot, "live-route-probe"),
            (probe, "pilot"),
            (probe, "fixture"),
        ):
            with (
                self.subTest(agent=type(agent).__name__, role=role),
                self.assertRaisesRegex(RuntimeError, "policy roles"),
            ):
                await agent.install(_pinned_environment_mock(None, role=role))

    def test_probe_and_pilot_authorizations_are_checked_before_relay_open(self) -> None:
        identity = (1, 2, 3, 4, "sha256:" + "8" * 64)
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            root = Path(raw)
            pilot_path = root / "authorizations" / "zai.json"
            cap_path = root / "authorizations" / "zai.cap.json"
            pilot = OpenAgentLabCodex(
                root / "pilot",
                model_name="zai/glm-5.3",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={
                    "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                    PILOT_RECEIPT_ENV: str(pilot_path),
                },
            )
            probe = OpenAgentLabCodexLiveRouteProbe(
                root / "probe",
                model_name="zai/glm-5.3",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={
                    "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                    LIVE_ROUTE_PROBE_CAP_ENV: str(cap_path),
                },
            )
            unguarded = OpenAgentLabCodex(
                root / "unguarded",
                model_name="zai/glm-5.3",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            )
        environment = _pinned_environment_mock(None, role="pilot")
        environment._provider_secret_path = Path("/run/provider-key")
        environment._provider_credential_identity = identity
        environment.trial_paths = SimpleNamespace(trial_dir=pilot.logs_dir.parent)
        probe_environment = _pinned_environment_mock(None, role="live-route-probe")
        probe_environment._provider_secret_path = Path("/run/provider-key")
        probe_environment._provider_credential_identity = identity
        probe_environment.trial_paths = SimpleNamespace(trial_dir=probe.logs_dir.parent)
        with (
            patch(
                "benchmarks.terminal_bench.harbor_agent._harbor_environment."
                "_credential_identity",
                return_value=identity,
            ),
            patch(
                "benchmarks.terminal_bench.live_route_probe."
                "validate_pilot_authorization"
            ) as validate_pilot,
            patch(
                "benchmarks.terminal_bench.live_route_probe.validate_probe_cap"
            ) as validate_cap,
        ):
            pilot._validate_route_authorization(environment, _RUN_BINDING)
            probe._validate_route_authorization(probe_environment, _RUN_BINDING)
            with self.assertRaisesRegex(RuntimeError, "inputs are unavailable"):
                unguarded._validate_route_authorization(environment, _RUN_BINDING)
        validate_pilot.assert_called_once_with(
            pilot_path,
            "zai",
            "glm-5.3",
            _RUN_BINDING,
            Path("/run/provider-key"),
            environment.trial_paths.trial_dir,
        )
        validate_cap.assert_called_once_with(
            cap_path,
            "zai",
            "glm-5.3",
            _RUN_BINDING,
            Path("/run/provider-key"),
            probe_environment.trial_paths.trial_dir,
        )

    def test_forged_authorization_module_fails_before_validation(self) -> None:
        identity = (1, 2, 3, 4, "sha256:" + "8" * 64)
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            root = Path(raw)
            agent = OpenAgentLabCodex(
                root / "job" / "trial" / "agent",
                model_name="zai/glm-5.3",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={
                    "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                    PILOT_RECEIPT_ENV: str(root / "authorizations" / "zai.json"),
                },
            )
        environment = _pinned_environment_mock(None, role="pilot")
        environment.trial_paths = SimpleNamespace(trial_dir=agent.logs_dir.parent)
        environment._provider_secret_path = Path("/run/provider-key")
        environment._provider_credential_identity = identity
        with (
            patch(
                "benchmarks.terminal_bench.harbor_agent._harbor_environment."
                "_credential_identity",
                return_value=identity,
            ),
            patch.object(live_probe, "__file__", "/tmp/forged-gate.py"),
            patch.object(live_probe, "validate_pilot_authorization") as validate,
            self.assertRaisesRegex(RuntimeError, "Authorization source"),
        ):
            agent._validate_route_authorization(environment, _RUN_BINDING)
        validate.assert_not_called()

    def test_probe_constructor_rejects_runtime_injection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            common = {
                "model_name": "zai/glm-5.3",
                "version": "0.149.0",
                "run_binding": _RUN_BINDING,
                "extra_env": {
                    "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1",
                    LIVE_ROUTE_PROBE_CAP_ENV: str(
                        Path(raw) / "authorizations" / "zai.cap.json"
                    ),
                },
            }
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                for override in (
                    {"reasoning_effort": "low"},
                    {"prompt_template_path": "/tmp/attacker.txt"},
                    {"extra_env": {**common["extra_env"], "ATTACKER": "1"}},
                ):
                    with self.subTest(override=override), self.assertRaises(ValueError):
                        OpenAgentLabCodexLiveRouteProbe(
                            Path(raw) / "probe", **{**common, **override}
                        )

    async def test_runtime_operations_require_the_exact_pinned_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            parent_setup = AsyncMock()
            parent_run = AsyncMock()
            parent_exec = AsyncMock()
            retain = AsyncMock()

            class DerivedPinnedRelayDockerEnvironment(PinnedRelayDockerEnvironment):
                pass

            for environment in (
                object(),
                object.__new__(DerivedPinnedRelayDockerEnvironment),
            ):
                with (
                    patch.object(Codex, "exec_as_agent", new=parent_exec),
                    self.assertRaisesRegex(
                        TypeError, "exact PinnedRelayDockerEnvironment"
                    ),
                ):
                    await agent.install(environment)  # type: ignore[arg-type]
                parent_exec.assert_not_awaited()

                with (
                    self.subTest(environment=type(environment).__name__),
                    patch(
                        "benchmarks.terminal_bench.harbor_agent._validate_live_source"
                    ) as validate_source,
                    patch.object(Codex, "setup", new=parent_setup),
                    self.assertRaisesRegex(
                        TypeError, "exact PinnedRelayDockerEnvironment"
                    ),
                ):
                    await agent.setup(environment)  # type: ignore[arg-type]
                parent_setup.assert_not_awaited()
                validate_source.assert_not_called()

                with (
                    patch(
                        "benchmarks.terminal_bench.harbor_agent._validate_live_source"
                    ) as validate_source,
                    patch.object(Codex, "run", new=parent_run),
                    patch.object(agent, "_seal_and_retain", new=retain),
                    self.assertRaisesRegex(
                        TypeError, "exact PinnedRelayDockerEnvironment"
                    ),
                ):
                    await agent.run(  # type: ignore[arg-type]
                        "instruction", environment, AgentContext()
                    )
                parent_run.assert_not_awaited()
                retain.assert_not_awaited()
                validate_source.assert_not_called()

                with self.assertRaisesRegex(
                    TypeError, "exact PinnedRelayDockerEnvironment"
                ):
                    await agent._seal_and_retain(environment)  # type: ignore[arg-type]

    async def test_install_only_verifies_the_frozen_native_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            parent_exec = AsyncMock()
            environment = _pinned_environment_mock(None)
            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "exec_as_agent", new=parent_exec),
            ):
                await agent.install(environment)

            expected = (
                build_full_tree_verification_command(codex_runtime_spec())
                + f'; test "$({CODEX_RUNTIME_ENTRYPOINT} --version)" = '
                '"codex-cli 0.149.0"'
            )
            parent_exec.assert_awaited_once_with(environment, command=expected)
            self.assertNotIn("npm", expected)
            self.assertNotIn("nvm", expected)
            self.assertEqual(
                agent.get_version_command(), f"{CODEX_RUNTIME_ENTRYPOINT} --version"
            )

    async def test_only_the_exact_harbor_launch_uses_the_frozen_entrypoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            parent_exec = AsyncMock(return_value="sentinel")
            environment = _pinned_environment_mock(None)
            suffix = "--dangerously-bypass-approvals-and-sandbox -- task"
            agent._codex_run_active = True
            with patch.object(Codex, "exec_as_agent", new=parent_exec):
                result = await agent.exec_as_agent(
                    environment,
                    HARBOR_CODEX_EXEC_PREFIX + suffix,
                    env={"CODEX_HOME": "/tmp/codex"},
                    cwd="/root",
                    timeout_sec=7,
                )
                with self.assertRaisesRegex(RuntimeError, "exactly once"):
                    await agent.exec_as_agent(
                        environment, HARBOR_CODEX_EXEC_PREFIX + suffix
                    )
                for command in (
                    "codex exec -- task",
                    "/usr/bin/codex\t\texec -- task",
                    "true; " + HARBOR_CODEX_EXEC_PREFIX + suffix,
                ):
                    with self.assertRaisesRegex(RuntimeError, "ambient Codex"):
                        await agent.exec_as_agent(environment, command)

            expected = (
                build_full_tree_verification_command(codex_runtime_spec())
                + f"; {CODEX_RUNTIME_ENTRYPOINT} exec {suffix}"
            )
            self.assertEqual(result, "sentinel")
            self.assertNotIn("nvm", expected)
            parent_exec.assert_awaited_once_with(
                environment,
                expected,
                env={"CODEX_HOME": "/tmp/codex"},
                cwd="/root",
                timeout_sec=7,
            )

    async def test_successful_parent_run_must_launch_codex_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            agent._extra_env["OAL_RELAY_TOKEN"] = "a" * 64
            retain = AsyncMock()
            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "run", new=AsyncMock()),
                patch.object(agent, "_seal_and_retain", new=retain),
                self.assertRaisesRegex(RuntimeError, "did not launch exactly once"),
            ):
                await agent.run(
                    "instruction", _pinned_environment_mock(None), AgentContext()
                )
            retain.assert_awaited_once()
            self.assertFalse(agent._codex_run_active)

    async def test_setup_fetches_a_per_trial_token_before_agent_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            calls: list[tuple[str, dict[str, object]]] = []
            events: list[str] = []
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    logs,
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )

            async def service_exec(*_args: object, **_kwargs: object) -> object:
                parent_setup.assert_awaited_once()
                calls.append((str(_args[0]), _kwargs))
                events.append(f"exec:{_args[0]}")
                if _args[0] == "cat /app/relay-build-id":
                    stdout = _RUN_BINDING["relay_build_sha256"]
                elif _args[0] == _RELAY_BOOTSTRAP_COMMAND:
                    stdout = _bootstrap_identity()
                elif _args[0] == _RELAY_AUTHORIZE_COMMAND:
                    stdout = ""
                else:
                    stdout = _relay_capability()
                return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

            with (
                patch(
                    "benchmarks.terminal_bench.harbor_agent._validate_live_source",
                    side_effect=lambda _binding: events.append("source"),
                ),
                patch.object(
                    Codex,
                    "setup",
                    new=AsyncMock(
                        side_effect=lambda _environment: events.append("parent")
                    ),
                ) as parent_setup,
            ):
                await agent.setup(_pinned_environment_mock(service_exec))
            parent_setup.assert_awaited_once()
            self.assertEqual(
                calls,
                [
                    (
                        "cat /app/relay-build-id",
                        {
                            "service": "open-agent-lab-relay",
                            "timeout_sec": 10,
                            "user": "1000",
                        },
                    ),
                    (
                        _RELAY_BOOTSTRAP_COMMAND,
                        {
                            "service": "open-agent-lab-relay",
                            "timeout_sec": 10,
                            "user": "0",
                        },
                    ),
                    (
                        _RELAY_AUTHORIZE_COMMAND,
                        {
                            "service": "open-agent-lab-relay",
                            "timeout_sec": 10,
                            "user": "0",
                        },
                    ),
                    (
                        _RELAY_TOKEN_COMMAND,
                        {
                            "service": "open-agent-lab-relay",
                            "timeout_sec": 25,
                            "user": "1000",
                        },
                    ),
                ],
            )
            self.assertEqual(
                events,
                [
                    "source",
                    "parent",
                    "exec:cat /app/relay-build-id",
                    f"exec:{_RELAY_BOOTSTRAP_COMMAND}",
                    "source",
                    f"exec:{_RELAY_AUTHORIZE_COMMAND}",
                    f"exec:{_RELAY_TOKEN_COMMAND}",
                ],
            )
            self.assertEqual(agent.extra_env["OAL_RELAY_TOKEN"], "a" * 64)

    async def test_setup_rejects_the_wrong_relay_build_before_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )

            async def service_exec(*_args: object, **_kwargs: object) -> object:
                return SimpleNamespace(
                    return_code=0, stdout="sha256:" + "f" * 64, stderr=""
                )

            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "setup", new=AsyncMock()),
                self.assertRaisesRegex(RuntimeError, "build identity"),
            ):
                await agent.setup(_pinned_environment_mock(service_exec))
            self.assertNotIn("OAL_RELAY_TOKEN", agent.extra_env)

    async def test_setup_rechecks_source_before_relay_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            commands: list[str] = []

            async def service_exec(command: str, **_kwargs: object) -> object:
                commands.append(command)
                stdout = (
                    _RUN_BINDING["relay_build_sha256"]
                    if command == "cat /app/relay-build-id"
                    else _bootstrap_identity()
                )
                return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

            with (
                patch(
                    "benchmarks.terminal_bench.harbor_agent._validate_live_source",
                    side_effect=(None, RuntimeError("source drifted")),
                ),
                patch.object(Codex, "setup", new=AsyncMock()),
                self.assertRaisesRegex(RuntimeError, "source drifted"),
            ):
                await agent.setup(_pinned_environment_mock(service_exec))
            self.assertEqual(
                commands, ["cat /app/relay-build-id", _RELAY_BOOTSTRAP_COMMAND]
            )
            self.assertNotIn("OAL_RELAY_TOKEN", agent.extra_env)

    async def test_setup_rejects_mixed_provider_or_model_before_authorization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for provider, model in (
                ("zai", "glm-5.3"),
                ("deepseek", "deepseek-v4-flash"),
            ):
                with (
                    self.subTest(provider=provider, model=model),
                    patch(
                        "benchmarks.terminal_bench.harbor_agent._validate_live_source"
                    ),
                ):
                    agent = OpenAgentLabCodex(
                        Path(raw),
                        model_name="deepseek/deepseek-v4-pro",
                        version="0.149.0",
                        run_binding=_RUN_BINDING,
                        extra_env={
                            "OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"
                        },
                    )
                commands: list[str] = []

                async def service_exec(
                    command: str,
                    *,
                    selected_provider: str = provider,
                    selected_model: str = model,
                    selected_commands: list[str] = commands,
                    **_kwargs: object,
                ) -> object:
                    selected_commands.append(command)
                    stdout = (
                        _RUN_BINDING["relay_build_sha256"]
                        if command == "cat /app/relay-build-id"
                        else _bootstrap_identity(
                            provider=selected_provider, model=selected_model
                        )
                    )
                    return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

                with (
                    patch(
                        "benchmarks.terminal_bench.harbor_agent._validate_live_source"
                    ),
                    patch.object(Codex, "setup", new=AsyncMock()),
                    self.assertRaisesRegex(RuntimeError, "does not match this trial"),
                ):
                    await agent.setup(_pinned_environment_mock(service_exec))
                self.assertEqual(
                    commands, ["cat /app/relay-build-id", _RELAY_BOOTSTRAP_COMMAND]
                )
                self.assertNotIn("OAL_RELAY_TOKEN", agent.extra_env)

    async def test_setup_rejects_an_invalid_relay_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )

            async def service_exec(command: str, **_kwargs: object) -> object:
                if command == "cat /app/relay-build-id":
                    stdout = _RUN_BINDING["relay_build_sha256"]
                elif command == _RELAY_BOOTSTRAP_COMMAND:
                    stdout = _bootstrap_identity()
                elif command == _RELAY_AUTHORIZE_COMMAND:
                    stdout = ""
                else:
                    stdout = _relay_capability(capability_id="f" * 64)
                return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "setup", new=AsyncMock()),
                self.assertRaisesRegex(RuntimeError, "per-trial relay capability"),
            ):
                await agent.setup(_pinned_environment_mock(service_exec))
            self.assertNotIn("OAL_RELAY_TOKEN", agent.extra_env)

    async def test_setup_never_reads_a_token_when_authorization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            commands: list[str] = []

            async def service_exec(command: str, **_kwargs: object) -> object:
                commands.append(command)
                return SimpleNamespace(
                    return_code=(
                        0
                        if command
                        in {"cat /app/relay-build-id", _RELAY_BOOTSTRAP_COMMAND}
                        else 1
                    ),
                    stdout=(
                        _RUN_BINDING["relay_build_sha256"]
                        if command == "cat /app/relay-build-id"
                        else _bootstrap_identity()
                        if command == _RELAY_BOOTSTRAP_COMMAND
                        else ""
                    ),
                    stderr="",
                )

            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "setup", new=AsyncMock()),
                self.assertRaisesRegex(RuntimeError, "post-validation authorization"),
            ):
                await agent.setup(_pinned_environment_mock(service_exec))
            self.assertEqual(
                commands,
                [
                    "cat /app/relay-build-id",
                    _RELAY_BOOTSTRAP_COMMAND,
                    _RELAY_AUTHORIZE_COMMAND,
                ],
            )
            self.assertNotIn("OAL_RELAY_TOKEN", agent.extra_env)

    async def test_relay_evidence_is_retained_outside_task_mounted_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    logs,
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )

            async def service_exec(command: str, **_kwargs: object) -> object:
                stdout = "cmV0YWluZWQ=" if command.startswith("base64 ") else ""
                return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

            await agent._seal_and_retain(_pinned_environment_mock(service_exec))
            evidence = trial / "artifacts" / "provider-evidence"
            main_artifact_bind = trial / "artifacts" / "logs" / "artifacts"
            self.assertEqual(agent._provider_evidence_dir, evidence)
            self.assertFalse(evidence.is_relative_to(logs))
            self.assertFalse(evidence.is_relative_to(main_artifact_bind))
            self.assertEqual(
                (evidence / "provider-metadata.ndjson").read_text(), "retained"
            )

    async def test_evidence_failure_fails_an_otherwise_successful_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    Path(raw),
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            agent._extra_env["OAL_RELAY_TOKEN"] = "a" * 64
            agent.logger.disabled = True

            async def one_launch(*_args: object, **_kwargs: object) -> None:
                agent._codex_launches = 1

            with (
                patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
                patch.object(Codex, "run", new=AsyncMock(side_effect=one_launch)),
                patch.object(
                    agent,
                    "_seal_and_retain",
                    new=AsyncMock(side_effect=RuntimeError("evidence failed")),
                ),
                self.assertRaisesRegex(RuntimeError, "evidence failed"),
            ):
                await agent.run(
                    "instruction", _pinned_environment_mock(None), AgentContext()
                )

    def test_invalid_metadata_never_raises_into_the_official_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    logs,
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            agent.logger.disabled = True
            context = AgentContext()
            agent.populate_context_post_run(context)
            gate = context.metadata["open_agent_lab_provider"]["publication_gate"]
            self.assertEqual(
                gate,
                {
                    "ok": False,
                    "reasons": [
                        "provider_metadata_unavailable_or_invalid",
                        "trajectory_session_missing",
                    ],
                },
            )

    def test_binding_keeps_harbor_and_atif_session_namespaces_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            with patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"):
                agent = OpenAgentLabCodex(
                    logs,
                    model_name="zai/glm-5.3",
                    version="0.149.0",
                    run_binding=_RUN_BINDING,
                    extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
                )
            agent._provider_evidence_dir.mkdir(parents=True)
            _write_evidence(agent._provider_evidence_dir)
            (logs / "trajectory.json").write_text(
                json.dumps({"session_id": "codex-rollout-id"})
            )
            agent.session_id = "harbor-trial__agent"
            agent.context_id = UUID("00000000-0000-0000-0000-000000000001")
            context = AgentContext()

            with patch.object(Codex, "populate_context_post_run"):
                agent.populate_context_post_run(context)

            binding = context.metadata["open_agent_lab_provider"]["harbor_binding"]
            self.assertEqual(binding["harbor_session_id"], "harbor-trial__agent")
            self.assertEqual(binding["trajectory_session_id"], "codex-rollout-id")
            self.assertEqual(
                binding["harbor_context_id"],
                "00000000-0000-0000-0000-000000000001",
            )
            self.assertEqual(binding["variant_id"], "control-v1")
            self.assertIsNone(binding["requested_developer_instructions_sha256"])
            self.assertEqual(
                binding["codex_runtime_spec_sha256"], CODEX_RUNTIME_SPEC_SHA256
            )
            self.assertEqual(binding["run_binding"], _RUN_BINDING)
            self.assertEqual(
                context.metadata["open_agent_lab_provider"]["agent_variant"],
                {
                    "schema_version": 1,
                    "variant_id": "control-v1",
                    "developer_instruction_requested": False,
                    "requested_developer_instructions_sha256": None,
                },
            )


if __name__ == "__main__":
    unittest.main()
