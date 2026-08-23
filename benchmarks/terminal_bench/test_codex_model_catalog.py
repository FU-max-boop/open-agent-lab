import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from harbor.agents.installed.codex import Codex

from benchmarks.terminal_bench.harbor_agent import (
    _CODEX_BASE_INSTRUCTIONS_SHA256,
    _CODEX_MODEL_CATALOG_PATH,
    OpenAgentLabCodex,
)
from benchmarks.terminal_bench.validate_harbor_e2e import (
    _EFFECTIVE_MODEL_CONTEXT_WINDOW,
    _assert_codex_model_metadata,
)

_RUN_BINDING = {
    "schema_version": 1,
    "experiment_id": "terminal-bench-2.1-verify-instruction-v1",
    "replication_id": "screen-v1",
    "source_revision": "a" * 40,
    "experiment_manifest_sha256": "sha256:" + "b" * 64,
    "relay_build_sha256": "sha256:" + "c" * 64,
    "relay_image_sha256": "sha256:" + "d" * 64,
    "preflight_sha256": "sha256:" + "e" * 64,
    "task_snapshots_sha256": "sha256:" + "f" * 64,
}
_MODEL_FIELDS = {
    "apply_patch_tool_type",
    "context_window",
    "default_reasoning_level",
    "default_reasoning_summary",
    "display_name",
    "effective_context_window_percent",
    "experimental_supported_tools",
    "include_apps_usage_instructions",
    "include_plugin_usage_instructions",
    "include_skills_usage_instructions",
    "input_modalities",
    "max_context_window",
    "model_messages",
    "priority",
    "shell_type",
    "slug",
    "support_verbosity",
    "supported_in_api",
    "supported_reasoning_levels",
    "supports_reasoning_summary_parameter",
    "truncation_policy",
    "visibility",
}


class CodexModelCatalogTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, logs_dir: Path, provider: str, model: str) -> OpenAgentLabCodex:
        with (
            patch("benchmarks.terminal_bench.harbor_agent._validate_harbor_runtime"),
            patch("benchmarks.terminal_bench.harbor_agent._validate_live_source"),
        ):
            return OpenAgentLabCodex(
                logs_dir,
                model_name=f"{provider}/{model}",
                version="0.149.0",
                run_binding=_RUN_BINDING,
                extra_env={"OAL_RELAY_URL": "http://open-agent-lab-relay:8080/v1"},
            )

    def test_catalog_preserves_the_exact_model_prompt_tools_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for provider, model, reasoning, context_window in (
                ("deepseek", "deepseek-v4-pro", "high", 1_048_576),
                ("zai", "glm-5.3", "max", 1_000_000),
            ):
                with self.subTest(provider=provider):
                    agent = self._agent(Path(raw) / provider, provider, model)
                    config = agent._build_effective_config()
                    catalog = json.loads(agent._codex_model_catalog)
                    self.assertEqual(
                        config["model_catalog_json"], _CODEX_MODEL_CATALOG_PATH
                    )
                    self.assertEqual(config["model_context_window"], context_window)
                    self.assertEqual(
                        config["model_auto_compact_token_limit"],
                        context_window * 9 // 10,
                    )
                    self.assertEqual(len(catalog["models"]), 1)
                    metadata = catalog["models"][0]
                    self.assertEqual(set(metadata), _MODEL_FIELDS)
                    self.assertEqual(metadata["slug"], model)
                    self.assertEqual(metadata["default_reasoning_level"], reasoning)
                    self.assertEqual(metadata["apply_patch_tool_type"], "freeform")
                    self.assertEqual(metadata["context_window"], context_window)
                    self.assertEqual(metadata["max_context_window"], context_window)
                    self.assertEqual(metadata["effective_context_window_percent"], 95)
                    prompt = metadata["model_messages"][
                        "instructions_template"
                    ].encode()
                    self.assertEqual(
                        "sha256:" + hashlib.sha256(prompt).hexdigest(),
                        _CODEX_BASE_INSTRUCTIONS_SHA256,
                    )
                    self.assertEqual(
                        agent._codex_model_catalog,
                        json.dumps(
                            catalog,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n",
                    )

    async def test_catalog_upload_is_internal_and_precedes_the_parent_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            agent = self._agent(Path(raw), "zai", "glm-5.3")
            environment = object()
            config = agent._build_effective_config()
            upload = AsyncMock()
            parent = AsyncMock()
            with (
                patch.object(agent, "_upload_config_text", new=upload),
                patch.object(Codex, "_upload_effective_config", new=parent),
            ):
                await agent._upload_effective_config(
                    environment,  # type: ignore[arg-type]
                    config,
                    "/tmp/codex-home/config.toml",
                )
            upload.assert_awaited_once_with(
                environment,
                content=agent._codex_model_catalog,
                remote_path=_CODEX_MODEL_CATALOG_PATH,
                filename="model-catalog.json",
            )
            parent.assert_awaited_once_with(
                environment, config, "/tmp/codex-home/config.toml"
            )

            parent.reset_mock()
            upload.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "catalog path drifted"):
                await agent._upload_effective_config(
                    environment,  # type: ignore[arg-type]
                    {**config, "model_catalog_json": "/tmp/caller-catalog.json"},
                    "/tmp/codex-home/config.toml",
                )
            upload.assert_not_awaited()
            parent.assert_not_awaited()

    def test_fixture_gate_rejects_fallback_context_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trial = Path(raw)
            session = trial / "agent" / "sessions" / "2026" / "08" / "24"
            session.mkdir(parents=True)
            output = trial / "agent" / "codex.txt"
            rollout = session / "rollout-2026-08-24T00-00-00-test.jsonl"

            def write_context(context_window: int) -> None:
                rollout.write_text(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "task_started",
                                "model_context_window": context_window,
                            },
                        }
                    )
                    + "\n"
                )

            output.write_text('{"type":"thread.started"}\n')
            write_context(_EFFECTIVE_MODEL_CONTEXT_WINDOW)
            _assert_codex_model_metadata(trial)

            write_context(258_400)
            with self.assertRaisesRegex(RuntimeError, "frozen DeepSeek context"):
                _assert_codex_model_metadata(trial)


if __name__ == "__main__":
    unittest.main()
