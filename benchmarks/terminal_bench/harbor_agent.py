"""A minimal isolated-Responses profile layered on Harbor's Codex agent."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, TypedDict, override
from urllib.parse import urlsplit

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .relay_evidence import relay_metadata


class _Profile(TypedDict):
    models: frozenset[str]
    reasoning: str
    context_window: int


_PROFILES: dict[str, _Profile] = {
    "deepseek": {
        "models": frozenset({"deepseek-v4-flash", "deepseek-v4-pro"}),
        "reasoning": "high",
        "context_window": 1_048_576,
    },
    "zai": {
        "models": frozenset({"glm-5.3"}),
        "reasoning": "max",
        "context_window": 1_000_000,
    },
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_EMPTY_AUTH = Path(__file__).with_name("empty-auth.json")
_RELAY_SERVICE = "open-agent-lab-relay"
_RELAY_SIDECAR = "/var/lib/open-agent-lab/provider-metadata.ndjson"
_RELAY_SEAL = f"{_RELAY_SIDECAR}.sealed"
_RELAY_TOKEN_FILE = f"{_RELAY_SIDECAR}.client-token"
_RELAY_TOKEN_ENV = "OAL_RELAY_TOKEN"
_RELAY_URL_ENV = "OAL_RELAY_URL"


def _relay_url(env: dict[str, str]) -> str:
    value = env.get(_RELAY_URL_ENV, "")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise ValueError(f"{_RELAY_URL_ENV} must be one fixed /v1 endpoint.")
    if parsed.scheme == "http" and parsed.hostname not in {
        _RELAY_SERVICE,
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(
            f"{_RELAY_URL_ENV} must use HTTPS outside the private relay service."
        )
    return value.rstrip("/")


class OpenAgentLabCodex(Codex):
    """Harbor Codex with a frozen GLM or DeepSeek Responses provider."""

    MODEL_CONNECTION = None
    SUPPORTS_RESUME = False

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

        agent_env = dict(extra_env or {})
        forbidden = {
            "DEEPSEEK_API_KEY",
            "ZAI_API_KEY",
            _RELAY_TOKEN_ENV,
        }.intersection(agent_env)
        if forbidden:
            raise ValueError(
                "Provider keys and per-trial relay tokens belong only in the relay service."
            )
        relay_url = _relay_url(agent_env)

        context_window = profile["context_window"]
        provider_config = {
            "model_provider": "open-agent-lab",
            "model_context_window": context_window,
            "model_auto_compact_token_limit": context_window * 9 // 10,
            "model_reasoning_effort": profile["reasoning"],
            "model_reasoning_summary": "none",
            "shell_environment_policy": {
                "ignore_default_excludes": False,
                "set": {_RELAY_TOKEN_ENV: ""},
            },
            "model_providers": {
                "open-agent-lab": {
                    "name": f"Open Agent Lab ({provider})",
                    "base_url": relay_url,
                    "env_key": _RELAY_TOKEN_ENV,
                    "wire_api": "responses",
                    "request_max_retries": 4,
                    "stream_max_retries": 5,
                    "stream_idle_timeout_ms": 300_000,
                    "requires_openai_auth": False,
                    "supports_websockets": False,
                }
            },
        }
        agent_env["CODEX_AUTH_JSON_PATH"] = str(_EMPTY_AUTH)
        self._open_agent_lab_provider = provider
        self._open_agent_lab_model = model
        self._provider_evidence_dir = (
            logs_dir.parent / "artifacts" / "provider-evidence"
        )
        super().__init__(
            logs_dir,
            *args,
            model_name=model_name,
            config=provider_config,
            extra_env=agent_env,
            **kwargs,
        )

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        token_result = await environment.service_exec(
            f"cat {_RELAY_TOKEN_FILE}",
            service=_RELAY_SERVICE,
            timeout_sec=10,
            user="1000",
        )
        relay_token = (token_result.stdout or "").strip()
        if (
            token_result.return_code != 0
            or len(relay_token.encode()) != 64
            or not re.fullmatch(r"[0-9a-f]{64}", relay_token)
        ):
            raise RuntimeError("Failed to obtain the per-trial relay capability.")
        self._extra_env[_RELAY_TOKEN_ENV] = relay_token

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        primary_error: BaseException | None = None
        try:
            if _RELAY_TOKEN_ENV not in self._extra_env:
                raise RuntimeError("Relay capability was not initialized during setup.")
            await super().run(instruction, environment, context)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            retained = asyncio.create_task(self._seal_and_retain(environment))
            try:
                await asyncio.shield(retained)
            except asyncio.CancelledError:
                try:
                    await retained
                except Exception:
                    self.logger.exception("Failed to retain provider metadata")
                raise
            except Exception:
                self.logger.exception(
                    "Failed to retain provider metadata%s",
                    " after agent failure" if primary_error is not None else "",
                )

    async def _seal_and_retain(self, environment: BaseEnvironment) -> None:
        command = (
            f"kill -USR2 1 && i=0; while [ ! -f {_RELAY_SEAL} ]; do "
            'i=$((i+1)); [ "$i" -lt 150 ] || exit 1; sleep 0.1; done'
        )
        async with asyncio.timeout(20):
            result = await environment.service_exec(
                command,
                service=_RELAY_SERVICE,
                timeout_sec=20,
                user="1000",
            )
            if result.return_code != 0:
                detail = result.stderr or result.stdout or "no output"
                raise RuntimeError(f"Relay seal failed: {detail[-500:]}")
            self._provider_evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            journal_path = self._provider_evidence_dir / "provider-metadata.ndjson"
            seal_path = self._provider_evidence_dir / "provider-metadata.ndjson.sealed"
            await environment.service_download_file(
                _RELAY_SIDECAR,
                journal_path,
                service=_RELAY_SERVICE,
            )
            await environment.service_download_file(
                _RELAY_SEAL,
                seal_path,
                service=_RELAY_SERVICE,
            )
            journal_path.chmod(0o600)
            seal_path.chmod(0o600)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        context.metadata = dict(context.metadata or {})
        try:
            metadata = relay_metadata(
                self._provider_evidence_dir / "provider-metadata.ndjson",
                self._provider_evidence_dir / "provider-metadata.ndjson.sealed",
            )
            reasons = metadata["publication_gate"]["reasons"]
            seal = metadata["seal"]
            if seal.get("providerId") != self._open_agent_lab_provider:
                reasons.append("provider_mismatch")
            if seal.get("expectedModel") != self._open_agent_lab_model:
                reasons.append("requested_model_mismatch")
            metadata["publication_gate"] = {
                "ok": not reasons,
                "reasons": sorted(set(reasons)),
            }
        except Exception as error:
            self.logger.exception("Provider metadata is not publication-safe")
            metadata = {
                "schema_version": 1,
                "publication_gate": {
                    "ok": False,
                    "reasons": ["provider_metadata_unavailable_or_invalid"],
                },
                "error": f"{type(error).__name__}: {error}"[:500],
            }
        try:
            trajectory = json.loads((self.logs_dir / "trajectory.json").read_text())
            trajectory_session_id = trajectory.get("session_id")
            if not isinstance(trajectory_session_id, str) or not trajectory_session_id:
                raise ValueError("ATIF session identity is missing")
        except (AttributeError, OSError, TypeError, ValueError):
            trajectory_session_id = None
            reasons = metadata["publication_gate"]["reasons"]
            metadata["publication_gate"] = {
                "ok": False,
                "reasons": sorted({*reasons, "trajectory_session_missing"}),
            }
        seal = metadata.get("seal", {})
        binding = {
            "schema_version": 1,
            "harbor_context_id": (
                str(self.context_id) if self.context_id is not None else None
            ),
            "harbor_session_id": self.session_id,
            "trajectory_session_id": trajectory_session_id,
            "relay_instance_id": seal.get("relayInstanceId"),
            "relay_build_id": seal.get("buildId"),
            "relay_marker_sha256": seal.get("markerSha256"),
            "provider_id": self._open_agent_lab_provider,
            "requested_model": self._open_agent_lab_model,
        }
        binding_hash = hashlib.sha256(
            json.dumps(
                binding,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        metadata["harbor_binding"] = {
            **binding,
            "binding_sha256": f"sha256:{binding_hash}",
        }
        context.metadata["open_agent_lab_provider"] = metadata

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
