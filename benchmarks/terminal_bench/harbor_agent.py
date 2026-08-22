"""A minimal native-Responses profile layered on Harbor's Codex agent."""

import re
from pathlib import Path
from typing import Any, TypedDict, override

from harbor.agents.installed.codex import Codex
from harbor.agents.model_connection import ModelConnectionSpec


class _Profile(TypedDict):
    base_url: str
    env_key: str
    models: frozenset[str]
    reasoning: str
    context_window: int


_PROFILES: dict[str, _Profile] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/",
        "env_key": "DEEPSEEK_API_KEY",
        "models": frozenset({"deepseek-v4-flash", "deepseek-v4-pro"}),
        "reasoning": "high",
        "context_window": 1_048_576,
    },
    "zai": {
        "base_url": "https://api.z.ai/api/v1",
        "env_key": "ZAI_API_KEY",
        "models": frozenset({"glm-5.3"}),
        "reasoning": "max",
        "context_window": 1_000_000,
    },
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_EMPTY_AUTH = Path(__file__).with_name("empty-auth.json")


class OpenAgentLabCodex(Codex):
    """Harbor Codex with a frozen GLM or DeepSeek Responses provider."""

    MODEL_CONNECTION = ModelConnectionSpec(default_provider=None)

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *args: Any,
        config: Path | str | dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        if config is not None:
            raise ValueError("OpenAgentLabCodex owns its benchmark config.")
        if (
            model_name is None
            or "/" not in model_name
            or not _MODEL_ID.fullmatch(model_name)
        ):
            raise ValueError("model_name must be '<deepseek|zai>/<exact-model-id>'.")
        provider, model = model_name.split("/", 1)
        profile = _PROFILES.get(provider)
        if profile is None or model not in profile["models"]:
            supported = ", ".join(
                f"{owner}/{candidate}"
                for owner, value in _PROFILES.items()
                for candidate in sorted(value["models"])
            )
            raise ValueError(f"Unsupported model variant. Choose one of: {supported}.")

        context_window = profile["context_window"]
        env_key = profile["env_key"]
        provider_config = {
            "model_provider": "open-agent-lab",
            "model_context_window": context_window,
            "model_auto_compact_token_limit": context_window * 9 // 10,
            "model_reasoning_effort": profile["reasoning"],
            "model_reasoning_summary": "none",
            "shell_environment_policy": {
                "ignore_default_excludes": False,
                "set": {env_key: ""},
            },
            "model_providers": {
                "open-agent-lab": {
                    "name": f"Open Agent Lab ({provider})",
                    "base_url": profile["base_url"],
                    "env_key": env_key,
                    "wire_api": "responses",
                    "request_max_retries": 4,
                    "stream_max_retries": 5,
                    "stream_idle_timeout_ms": 300_000,
                    "requires_openai_auth": False,
                    "supports_websockets": False,
                }
            },
        }
        agent_env = dict(extra_env or {})
        agent_env["CODEX_AUTH_JSON_PATH"] = str(_EMPTY_AUTH)
        super().__init__(
            logs_dir,
            *args,
            model_name=model_name,
            config=provider_config,
            extra_env=agent_env,
            **kwargs,
        )

    @staticmethod
    @override
    def name() -> str:
        return "open-agent-lab-codex"

    @override
    def _build_effective_config(
        self, openai_base_url: str | None = None
    ) -> dict[str, Any]:
        if openai_base_url is not None:
            raise ValueError("Benchmark provider base URLs are frozen by the adapter.")
        return super()._build_effective_config(None)
