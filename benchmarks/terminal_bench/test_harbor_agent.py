import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import yaml
from harbor.agents.installed.codex import Codex
from harbor.models.agent.context import AgentContext

from benchmarks.terminal_bench.harbor_agent import _PROFILES, OpenAgentLabCodex
from benchmarks.terminal_bench.relay_evidence import relay_metadata
from benchmarks.terminal_bench.validate_harbor_e2e import (
    _assert_isolation_call,
    _isolation_command,
)

_DEFAULT_USAGE = object()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


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
        requests or ((200, "completed", _DEFAULT_USAGE),), 1
    ):
        common = {
            "schemaVersion": schema_version,
            "relayVersion": "native-responses-relay-v1",
            "runId": "relay-test",
            "relayInstanceId": "instance-test",
            "providerId": provider_id,
            "buildId": build_id,
            "ordinal": ordinal,
            "relayRequestId": f"request-test-{ordinal}",
        }
        events.extend(
            [
                {
                    **common,
                    "event": "transport.responses.request",
                    "requestedModel": "glm-5.3",
                },
                {
                    **common,
                    "event": "transport.responses.headers",
                    "status": status,
                    "providerRequestId": f"provider-test-{ordinal}",
                },
                {
                    **common,
                    "event": "transport.responses.closed",
                    "status": status,
                    "providerRequestId": f"provider-test-{ordinal}",
                    "transportState": transport_state,
                    "parseErrors": 0,
                    "metadataConflicts": [],
                    "modelConsistency": "consistent",
                    "returnedModel": returned_model,
                    "responseId": f"response-test-{ordinal}",
                    "terminalEvent": "response.completed",
                    "usage": (
                        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
                        if usage is _DEFAULT_USAGE
                        else usage
                    ),
                },
            ]
        )
    previous = None
    lines = []
    for event in events:
        body = {**event, "previousEventSha256": previous}
        previous = _digest(_canonical(body))
        lines.append(_canonical({**body, "eventSha256": previous}))
    (directory / "provider-metadata.ndjson").write_text("\n".join(lines) + "\n")
    marker = {
        "schemaVersion": schema_version,
        "relayVersion": "native-responses-relay-v1",
        "runId": "relay-test",
        "relayInstanceId": "instance-test",
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
    def test_complete_sealed_lifecycle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory)
            metadata = relay_metadata(
                directory / "provider-metadata.ndjson",
                directory / "provider-metadata.ndjson.sealed",
            )
            self.assertEqual(metadata["publication_gate"], {"ok": True, "reasons": []})

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

    def test_pre_accept_rejection_prevents_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_evidence(directory, rejected_requests={"invalid_json": 1})
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
                        "entrypoint": [
                            "node",
                            "/app/apps/cli/relay-dist/relay-fixture-entry.js",
                        ],
                    }
                }
            },
        )


class HarborAdapterTest(unittest.IsolatedAsyncioTestCase):
    def test_sealed_relay_profile_does_not_claim_resume_support(self) -> None:
        self.assertFalse(OpenAgentLabCodex.SUPPORTS_RESUME)

    async def test_setup_fetches_a_per_trial_token_before_agent_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            agent = OpenAgentLabCodex(
                logs,
                model_name="zai/glm-5.3",
                version="0.149.0",
                extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            )

            class Environment:
                async def service_exec(
                    self, *_args: object, **_kwargs: object
                ) -> object:
                    parent_setup.assert_awaited_once()
                    return SimpleNamespace(return_code=0, stdout="a" * 64, stderr="")

            with patch.object(Codex, "setup", new=AsyncMock()) as parent_setup:
                await agent.setup(Environment())  # type: ignore[arg-type]
            parent_setup.assert_awaited_once()
            self.assertEqual(agent.extra_env["OAL_RELAY_TOKEN"], "a" * 64)

    async def test_relay_evidence_is_retained_outside_task_mounted_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            agent = OpenAgentLabCodex(
                logs,
                model_name="zai/glm-5.3",
                version="0.149.0",
                extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            )

            class Environment:
                async def service_exec(self, command: str, **_kwargs: object) -> object:
                    stdout = "cmV0YWluZWQ=" if command.startswith("base64 ") else ""
                    return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

            await agent._seal_and_retain(Environment())  # type: ignore[arg-type]
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
            agent = OpenAgentLabCodex(
                Path(raw),
                model_name="zai/glm-5.3",
                version="0.149.0",
                extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            )
            agent._extra_env["OAL_RELAY_TOKEN"] = "a" * 64
            agent.logger.disabled = True
            with (
                patch.object(Codex, "run", new=AsyncMock()),
                patch.object(
                    agent,
                    "_seal_and_retain",
                    new=AsyncMock(side_effect=RuntimeError("evidence failed")),
                ),
                self.assertRaisesRegex(RuntimeError, "evidence failed"),
            ):
                await agent.run("instruction", object(), AgentContext())  # type: ignore[arg-type]

    def test_invalid_metadata_never_raises_into_the_official_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            logs = trial / "agent"
            logs.mkdir()
            agent = OpenAgentLabCodex(
                logs,
                model_name="zai/glm-5.3",
                version="0.149.0",
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
            agent = OpenAgentLabCodex(
                logs,
                model_name="zai/glm-5.3",
                version="0.149.0",
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


if __name__ == "__main__":
    unittest.main()
