"""A minimal isolated-Responses profile layered on Harbor's Codex agent."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, Literal, TypedDict, cast, override

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from . import codex_runtime as _codex_runtime
from . import experiment_contract as _experiment_contract
from . import harbor_environment as _harbor_environment
from . import live_route_probe as _live_route_probe
from .codex_runtime import (
    CODEX_RUNTIME_ENTRYPOINT,
    CODEX_RUNTIME_SPEC_SHA256,
    HARBOR_CODEX_EXEC_PREFIX,
    build_full_tree_verification_command,
    codex_runtime_spec,
    rewrite_harbor_launch,
    validate_codex_runtime_spec,
)
from .experiment_contract import (
    CODEX_PROVIDER_RETRY_POLICY,
    EXPERIMENT_ID,
    LIVE_ROUTE_PROBE_CAP_ENV,
    LIVE_ROUTE_PROBE_LIMITS,
    PILOT_RECEIPT_ENV,
    RELAY_BUILD_ID_PATH,
    RELAY_JOURNAL_PATH,
    RELAY_SEAL_PATH,
    RELAY_SERVICE,
    RUN_BINDING_KEYS,
    is_digest,
    is_revision,
    is_strict_int,
    live_route_probe_instruction,
    live_route_probe_variant,
)
from .harbor_environment import (
    PinnedRelayDockerEnvironment as _PinnedRelayDockerEnvironment,
)
from .relay_evidence import relay_metadata


class _Profile(TypedDict):
    models: frozenset[str]
    reasoning: str
    context_window: int
    truncation_mode: Literal["bytes", "tokens"]


_PROFILES: dict[str, _Profile] = {
    "deepseek": {
        "models": frozenset({"deepseek-v4-flash", "deepseek-v4-pro"}),
        "reasoning": "high",
        "context_window": 1_048_576,
        "truncation_mode": "tokens",
    },
    "zai": {
        "models": frozenset({"glm-5.3"}),
        "reasoning": "max",
        "context_window": 1_000_000,
        "truncation_mode": "bytes",
    },
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_AMBIENT_CODEX_EXEC = re.compile(
    r"(?<![A-Za-z0-9._/-])(?:[A-Za-z0-9._/-]*/)?codex\s+exec(?:\s|$)"
)
_EMPTY_AUTH = Path(__file__).with_name("empty-auth.json")
_RELAY_TOKEN_FILE = f"{RELAY_JOURNAL_PATH}.client-token"
_RELAY_BOOTSTRAP_FILE = f"{RELAY_JOURNAL_PATH}.bootstrap-ready"
_RELAY_AUTHORIZE_COMMAND = "kill -USR1 1"
_RELAY_BOOTSTRAP_COMMAND = f"cat {_RELAY_BOOTSTRAP_FILE}"
_RELAY_TOKEN_COMMAND = (
    f"i=0; while [ ! -f {_RELAY_TOKEN_FILE} ]; do i=$((i+1)); "
    f'[ "$i" -lt 200 ] || exit 1; sleep 0.1; done; cat {_RELAY_TOKEN_FILE}'
)
_RELAY_TOKEN_ENV = "OAL_RELAY_TOKEN"
_RELAY_URL_ENV = "OAL_RELAY_URL"
_RELAY_URL = f"http://{RELAY_SERVICE}:8080/v1"
_VERIFY_INSTRUCTION_PATH = Path(__file__).with_name("verify-instruction-v1.txt")
_VERIFY_INSTRUCTION_SHA256 = (
    "sha256:9f855e1e34702265ed0ff4c4fcfb2483cb9777c5f37d8c29daccd2c454f84e4a"
)
_VERIFY_INSTRUCTION_BYTES = _VERIFY_INSTRUCTION_PATH.read_bytes()
if (
    "sha256:" + hashlib.sha256(_VERIFY_INSTRUCTION_BYTES).hexdigest()
    != _VERIFY_INSTRUCTION_SHA256
    or not _VERIFY_INSTRUCTION_BYTES.endswith(b"\n")
    or _VERIFY_INSTRUCTION_BYTES.endswith(b"\n\n")
):
    raise RuntimeError("verify-instruction-v1.txt drifted from its frozen bytes.")
_VERIFY_INSTRUCTION = _VERIFY_INSTRUCTION_BYTES.decode("utf-8")
_DELIVERABLE_FIRST_INSTRUCTION_PATH = Path(__file__).with_name(
    "deliverable-first-v1.txt"
)
_DELIVERABLE_FIRST_INSTRUCTION_SHA256 = (
    "sha256:5f1249f118ea229f35c1822c038e6ac8e671326f7e37e5079393a5c111c54be6"
)
_DELIVERABLE_FIRST_INSTRUCTION_BYTES = _DELIVERABLE_FIRST_INSTRUCTION_PATH.read_bytes()
if (
    "sha256:" + hashlib.sha256(_DELIVERABLE_FIRST_INSTRUCTION_BYTES).hexdigest()
    != _DELIVERABLE_FIRST_INSTRUCTION_SHA256
    or not _DELIVERABLE_FIRST_INSTRUCTION_BYTES.endswith(b"\n")
    or _DELIVERABLE_FIRST_INSTRUCTION_BYTES.endswith(b"\n\n")
):
    raise RuntimeError("deliverable-first-v1.txt drifted from its frozen bytes.")
_DELIVERABLE_FIRST_INSTRUCTION = _DELIVERABLE_FIRST_INSTRUCTION_BYTES.decode("utf-8")
_CODEX_BASE_INSTRUCTIONS_PATH = Path(__file__).with_name(
    "codex-0.149.0-base-instructions.md"
)
_CODEX_BASE_INSTRUCTIONS_SHA256 = (
    "sha256:ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807"
)
_CODEX_BASE_INSTRUCTIONS_BYTES = _CODEX_BASE_INSTRUCTIONS_PATH.read_bytes()
if (
    "sha256:" + hashlib.sha256(_CODEX_BASE_INSTRUCTIONS_BYTES).hexdigest()
    != _CODEX_BASE_INSTRUCTIONS_SHA256
    or not _CODEX_BASE_INSTRUCTIONS_BYTES.endswith(b"\n")
):
    raise RuntimeError("The pinned Codex base instructions drifted.")
_CODEX_BASE_INSTRUCTIONS = _CODEX_BASE_INSTRUCTIONS_BYTES.decode("utf-8")
_CODEX_MODEL_CATALOG_PATH = "/tmp/codex-home/model-catalog.json"
_HARBOR_VERSION = "0.22.0"
_CODEX_VERSION = "0.149.0"
_CAPABILITY = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_EXPERIMENT_MANIFEST = (
    _REPOSITORY_ROOT / "benchmarks/terminal_bench/verify-instruction-v1.experiment.json"
)


def _pinned_environment(
    environment: BaseEnvironment,
) -> _PinnedRelayDockerEnvironment:
    if type(environment) is not _PinnedRelayDockerEnvironment:
        raise TypeError(
            "OpenAgentLabCodex requires the exact PinnedRelayDockerEnvironment runtime."
        )
    return cast(_PinnedRelayDockerEnvironment, environment)


def _run_binding(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != RUN_BINDING_KEYS:
        raise ValueError("run_binding has an invalid schema.")
    if (
        not is_strict_int(value["schema_version"])
        or value["schema_version"] != 1
        or value["experiment_id"] != EXPERIMENT_ID
        or value["replication_id"] not in {"screen-v1", "mirror-v1"}
        or not is_revision(value["source_revision"])
        or any(
            not is_digest(value[key])
            for key in (
                "experiment_manifest_sha256",
                "relay_build_sha256",
                "relay_image_sha256",
                "preflight_sha256",
                "task_snapshots_sha256",
            )
        )
    ):
        raise ValueError("run_binding has invalid values.")
    return dict(value)


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate relay control key")
        value[key] = item
    return value


def _bootstrap_identity(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("Relay bootstrap identity is invalid.") from error
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schemaVersion",
            "buildId",
            "provider",
            "model",
            "budgetClass",
            "capabilityId",
        }
        or not is_strict_int(value["schemaVersion"])
        or value["schemaVersion"] != 2
        or not is_digest(value["buildId"])
        or not isinstance(value["provider"], str)
        or not isinstance(value["model"], str)
        or value["budgetClass"]
        not in {"scored_slot", "zai_route_probe", "unmetered_route_probe"}
        or not isinstance(value["capabilityId"], str)
        or not _CAPABILITY.fullmatch(value["capabilityId"])
    ):
        raise RuntimeError("Relay bootstrap identity is invalid.")
    return value


def _validate_harbor_runtime() -> None:
    try:
        package = distribution("harbor")
        direct_url = package.read_text("direct_url.json")
        metadata = (
            json.loads(direct_url, object_pairs_hook=_unique_json)
            if direct_url is not None
            else {}
        )
        directory = metadata.get("dir_info", {})
        editable = isinstance(directory, dict) and directory.get("editable") is True
    except (PackageNotFoundError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("The frozen Harbor runtime is unavailable.") from error
    if package.version != _HARBOR_VERSION or editable:
        raise RuntimeError("Harbor must be the non-editable 0.22.0 distribution.")


def _relay_capability(raw: str, capability_id: str) -> str:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("Relay capability is invalid.") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "capabilityId", "bearer"}
        or not is_strict_int(value["schemaVersion"])
        or value["schemaVersion"] != 1
        or value["capabilityId"] != capability_id
        or not isinstance(value["bearer"], str)
        or not _CAPABILITY.fullmatch(value["bearer"])
    ):
        raise RuntimeError("Relay capability is invalid.")
    return value["bearer"]


def _validate_authorization_source() -> None:
    root = _REPOSITORY_ROOT / "benchmarks" / "terminal_bench"
    try:
        trusted = (
            Path(_live_route_probe.__file__).resolve(strict=True)
            == root / "live_route_probe.py"
        )
    except (OSError, TypeError) as error:
        raise RuntimeError("Authorization source is unavailable.") from error
    if not trusted:
        raise RuntimeError("Authorization source drifted.")


def _validate_live_source(binding: dict[str, Any]) -> None:
    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=_REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError("Live source identity could not be verified.")
        return completed.stdout.strip()

    try:
        configured_root = (
            Path(os.environ["OPEN_AGENT_LAB_REPO_ROOT"])
            .expanduser()
            .resolve(strict=True)
        )
        manifest_bytes = _EXPERIMENT_MANIFEST.read_bytes()
        manifest = json.loads(manifest_bytes)
        relay_build_ids = manifest["relayBuildIds"]
        runtime = manifest["runtime"]
        if not isinstance(relay_build_ids, dict) or set(relay_build_ids) != {
            "production",
            "providerFreeFixture",
        }:
            raise TypeError("relayBuildIds has an invalid schema")
        allowed_relay_build_ids = set(relay_build_ids.values())
        if len(allowed_relay_build_ids) != 2 or any(
            not is_digest(value) for value in allowed_relay_build_ids
        ):
            raise ValueError("relayBuildIds has invalid values")
        if (
            not isinstance(runtime, dict)
            or type(runtime.get("hermeticCodexRuntimeReady")) is not bool
        ):
            raise ValueError("hermetic Codex runtime gate is invalid")
        validate_codex_runtime_spec(runtime["codexRuntime"])
        _validate_authorization_source()
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise RuntimeError("Live source identity could not be verified.") from error
    manifest_sha = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if (
        configured_root != _REPOSITORY_ROOT
        or git("rev-parse", "--show-toplevel") != str(_REPOSITORY_ROOT)
        or git("rev-parse", "--verify", "HEAD^{commit}") != binding["source_revision"]
        or git("status", "--porcelain=v1", "--untracked-files=all")
        or manifest_sha != binding["experiment_manifest_sha256"]
        or binding["relay_build_sha256"] not in allowed_relay_build_ids
        or Path(__file__).resolve(strict=True)
        != configured_root / "benchmarks/terminal_bench/harbor_agent.py"
        or Path(_harbor_environment.__file__).resolve(strict=True)
        != configured_root / "benchmarks/terminal_bench/harbor_environment.py"
        or Path(_experiment_contract.__file__).resolve(strict=True)
        != configured_root / "benchmarks/terminal_bench/experiment_contract.py"
        or Path(_codex_runtime.__file__).resolve(strict=True)
        != configured_root / "benchmarks/terminal_bench/codex_runtime.py"
        or _PinnedRelayDockerEnvironment.__module__
        != "benchmarks.terminal_bench.harbor_environment"
    ):
        raise RuntimeError("Live source drifted after the clean preflight.")
    if (
        binding["relay_build_sha256"] == relay_build_ids["production"]
        and not runtime["hermeticCodexRuntimeReady"]
    ):
        raise RuntimeError("Live work is blocked until Codex runtime bytes are frozen.")


def _relay_url(env: dict[str, str]) -> str:
    value = env.get(_RELAY_URL_ENV)
    if value != _RELAY_URL:
        raise ValueError(f"{_RELAY_URL_ENV} must be exactly {_RELAY_URL}.")
    return value


def _policy_path(env: dict[str, str], name: str, *, required: bool) -> Path | None:
    value = env.pop(name, None)
    if value is None:
        if required:
            raise ValueError(f"{name} is required for this run class.")
        return None
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path.")
    return Path(value)


def _provider_profile(model_name: str | None) -> tuple[str, str, _Profile]:
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
    return provider, model, profile


def _validate_constructor_options(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    version: str | None,
    config: Path | str | dict[str, Any] | None,
    enable_verification: bool,
    live_route_probe: bool,
    profile: _Profile,
) -> None:
    if version != _CODEX_VERSION:
        raise ValueError(f"version must be exactly {_CODEX_VERSION}.")
    if config is not None:
        raise ValueError("OpenAgentLabCodex owns its benchmark config.")
    if "provider_free_fixture" in kwargs:
        raise ValueError("Provider-free runs require a prepared source binding.")
    if type(enable_verification) is not bool:
        raise ValueError(
            "enable_verify_instruction_v1 must be a boolean experiment switch."
        )
    if live_route_probe and enable_verification:
        raise ValueError("The live-route probe cannot enable an experiment arm.")
    reasoning_effort = kwargs.pop("reasoning_effort", None)
    if args or kwargs:
        raise ValueError("Unsupported Codex constructor inputs are forbidden.")
    if reasoning_effort not in (None, profile["reasoning"]):
        raise ValueError("reasoning_effort must match the frozen provider profile.")


def _agent_environment(
    extra_env: dict[str, str] | None, *, live_route_probe: bool
) -> tuple[dict[str, str], str, Path | None]:
    agent_env = dict(extra_env or {})
    forbidden = {"DEEPSEEK_API_KEY", "ZAI_API_KEY", _RELAY_TOKEN_ENV}.intersection(
        agent_env
    )
    if forbidden:
        raise ValueError(
            "Provider keys and per-trial relay tokens belong only in the relay service."
        )
    policy_name = LIVE_ROUTE_PROBE_CAP_ENV if live_route_probe else PILOT_RECEIPT_ENV
    expected = {_RELAY_URL_ENV, policy_name}
    if not live_route_probe and policy_name not in agent_env:
        expected.remove(policy_name)
    if set(agent_env) != expected:
        raise ValueError("Agent environment contains an unsupported input.")
    relay_url = _relay_url(agent_env)
    policy_file = _policy_path(agent_env, policy_name, required=live_route_probe)
    return agent_env, relay_url, policy_file


def _provider_config(
    provider: str,
    profile: _Profile,
    relay_url: str,
    *,
    live_route_probe: bool,
    developer_instruction: str | None,
) -> dict[str, Any]:
    context_window = profile["context_window"]
    config: dict[str, Any] = {
        "model_provider": "open-agent-lab",
        "model_catalog_json": _CODEX_MODEL_CATALOG_PATH,
        "model_context_window": context_window,
        "model_auto_compact_token_limit": context_window * 9 // 10,
        "model_reasoning_effort": profile["reasoning"],
        "model_reasoning_summary": "none",
        "features": {
            "shell_zsh_fork": False,
            "unified_exec_zsh_fork": False,
            "unbounded_connection_retries": CODEX_PROVIDER_RETRY_POLICY[
                "unbounded_connection_retries"
            ],
        },
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
                "request_max_retries": CODEX_PROVIDER_RETRY_POLICY[
                    "request_max_retries"
                ],
                "stream_max_retries": CODEX_PROVIDER_RETRY_POLICY["stream_max_retries"],
                "stream_idle_timeout_ms": (
                    LIVE_ROUTE_PROBE_LIMITS["idleTimeoutMs"]
                    if live_route_probe
                    else 300_000
                ),
                "requires_openai_auth": False,
                "supports_websockets": False,
            }
        },
    }
    if developer_instruction is not None:
        config["developer_instructions"] = developer_instruction
    return config


def _model_catalog(model: str, profile: _Profile) -> str:
    context_window = profile["context_window"]
    catalog = {
        "models": [
            {
                "slug": model,
                "display_name": model,
                "default_reasoning_level": profile["reasoning"],
                "supported_reasoning_levels": [
                    {
                        "effort": profile["reasoning"],
                        "description": "Frozen Open Agent Lab effort",
                    }
                ],
                "shell_type": "shell_command",
                "visibility": "none",
                "supported_in_api": True,
                "priority": 0,
                "model_messages": {
                    "instructions_template": _CODEX_BASE_INSTRUCTIONS,
                },
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": False,
                "include_apps_usage_instructions": False,
                "supports_reasoning_summary_parameter": False,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {
                    "mode": profile["truncation_mode"],
                    "limit": 10_000,
                },
                "context_window": context_window,
                "max_context_window": context_window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
            }
        ]
    }
    return (
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )


def _variant(
    provider: str,
    *,
    live_route_probe: bool,
    variant_id: str | None,
    instruction_sha256: str | None,
) -> dict[str, Any]:
    if live_route_probe:
        return live_route_probe_variant(provider, effect_verified=False)
    if (variant_id is None) != (instruction_sha256 is None):
        raise RuntimeError("The developer instruction variant is incomplete.")
    return {
        "schema_version": 1,
        "variant_id": variant_id or "control-v1",
        "developer_instruction_requested": variant_id is not None,
        "requested_developer_instructions_sha256": instruction_sha256,
        **CODEX_PROVIDER_RETRY_POLICY,
    }


class OpenAgentLabCodex(Codex):
    """Harbor Codex with a frozen GLM or DeepSeek Responses provider."""

    MODEL_CONNECTION = None
    SUPPORTS_RESUME = False
    _LIVE_ROUTE_PROBE = False
    _VARIANT_ID = "verify-instruction-v1"
    _DEVELOPER_INSTRUCTION = _VERIFY_INSTRUCTION
    _DEVELOPER_INSTRUCTION_SHA256 = _VERIFY_INSTRUCTION_SHA256

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *args: Any,
        logger: logging.Logger | None = None,
        config: Path | str | dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        version: str | None = None,
        enable_verify_instruction_v1: bool = False,
        run_binding: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        _validate_harbor_runtime()
        provider, model, profile = _provider_profile(model_name)
        _validate_constructor_options(
            args,
            kwargs,
            version=version,
            config=config,
            enable_verification=enable_verify_instruction_v1,
            live_route_probe=self._LIVE_ROUTE_PROBE,
            profile=profile,
        )
        agent_env, relay_url, self._policy_file = _agent_environment(
            extra_env, live_route_probe=self._LIVE_ROUTE_PROBE
        )
        binding = _run_binding(run_binding)
        if binding is None:
            raise RuntimeError("Live provider work requires a prepared run binding.")
        _validate_live_source(binding)
        developer_instruction = (
            self._DEVELOPER_INSTRUCTION if enable_verify_instruction_v1 else None
        )
        provider_config = _provider_config(
            provider,
            profile,
            relay_url,
            live_route_probe=self._LIVE_ROUTE_PROBE,
            developer_instruction=developer_instruction,
        )
        agent_env["CODEX_AUTH_JSON_PATH"] = str(_EMPTY_AUTH)
        self._open_agent_lab_provider = provider
        self._open_agent_lab_model = model
        self._open_agent_lab_budget_class = (
            "zai_route_probe"
            if self._LIVE_ROUTE_PROBE and provider == "zai"
            else "unmetered_route_probe"
            if self._LIVE_ROUTE_PROBE
            else "scored_slot"
        )
        self._codex_model_catalog = _model_catalog(model, profile)
        self._open_agent_lab_run_binding = binding
        self._codex_runtime_spec = codex_runtime_spec()
        self._codex_launches = 0
        self._codex_launch_task: asyncio.Task[Any] | None = None
        self._codex_run_active = False
        self._open_agent_lab_variant = _variant(
            provider,
            live_route_probe=self._LIVE_ROUTE_PROBE,
            variant_id=self._VARIANT_ID if developer_instruction is not None else None,
            instruction_sha256=(
                self._DEVELOPER_INSTRUCTION_SHA256
                if developer_instruction is not None
                else None
            ),
        )
        self._provider_evidence_dir = (
            logs_dir.parent / "artifacts" / "provider-evidence"
        )
        super().__init__(
            logs_dir,
            *args,
            model_name=model_name,
            logger=logger,
            config=provider_config,
            extra_env=agent_env,
            version=version,
            **kwargs,
        )

    @override
    async def _upload_effective_config(
        self,
        environment: BaseEnvironment,
        config: dict[str, Any],
        remote_path: str,
    ) -> None:
        if config.get("model_catalog_json") != _CODEX_MODEL_CATALOG_PATH:
            raise RuntimeError("The effective Codex model catalog path drifted.")
        providers = config.get("model_providers")
        provider = (
            providers.get("open-agent-lab") if isinstance(providers, dict) else None
        )
        features = config.get("features")
        observed = {
            "request_max_retries": (
                provider.get("request_max_retries")
                if isinstance(provider, dict)
                else None
            ),
            "stream_max_retries": (
                provider.get("stream_max_retries")
                if isinstance(provider, dict)
                else None
            ),
            "unbounded_connection_retries": (
                features.get("unbounded_connection_retries")
                if isinstance(features, dict)
                else None
            ),
        }
        if config.get("model_provider") != "open-agent-lab" or any(
            type(observed[key]) is not type(expected) or observed[key] != expected
            for key, expected in CODEX_PROVIDER_RETRY_POLICY.items()
        ):
            raise RuntimeError("The effective Codex retry policy drifted.")
        await self._upload_config_text(
            environment,
            content=self._codex_model_catalog,
            remote_path=_CODEX_MODEL_CATALOG_PATH,
            filename="model-catalog.json",
        )
        await super()._upload_effective_config(environment, config, remote_path)

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        environment = self._validated_environment(environment)
        self._validate_request_source()
        command = (
            build_full_tree_verification_command(self._codex_runtime_spec)
            + f'; test "$({CODEX_RUNTIME_ENTRYPOINT} --version)" = '
            f'"codex-cli {_CODEX_VERSION}"'
        )
        await super().exec_as_agent(environment, command=command)

    @override
    def get_version_command(self) -> str | None:
        return f"{CODEX_RUNTIME_ENTRYPOINT} --version"

    @override
    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        if command.startswith(HARBOR_CODEX_EXEC_PREFIX):
            if (
                not self._codex_run_active
                or self._codex_launch_task is not asyncio.current_task()
                or self._codex_launches
            ):
                raise RuntimeError("Codex must launch exactly once inside agent.run().")
            command = (
                build_full_tree_verification_command(self._codex_runtime_spec)
                + "; "
                + rewrite_harbor_launch(command)
            )
            self._codex_launches += 1
        elif _AMBIENT_CODEX_EXEC.search(command):
            raise RuntimeError("Unexpected ambient Codex launch command.")
        return await super().exec_as_agent(
            environment,
            command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        environment = self._validated_environment(environment)
        self._validate_request_source()
        await super().setup(environment)

    async def _authorize_relay(self, environment: _PinnedRelayDockerEnvironment) -> str:
        binding = self._validate_request_source()
        build_result = await environment.service_exec(
            f"cat {RELAY_BUILD_ID_PATH}",
            service=RELAY_SERVICE,
            timeout_sec=10,
            user="1000",
        )
        if (
            build_result.return_code != 0
            or (build_result.stdout or "").strip() != binding["relay_build_sha256"]
        ):
            raise RuntimeError("Relay build identity does not match the preflight.")
        bootstrap_result = await environment.service_exec(
            _RELAY_BOOTSTRAP_COMMAND,
            service=RELAY_SERVICE,
            timeout_sec=10,
            user="0",
        )
        if bootstrap_result.return_code != 0:
            raise RuntimeError("Relay bootstrap identity is unavailable.")
        identity = _bootstrap_identity(bootstrap_result.stdout or "")
        if any(
            identity[key] != expected
            for key, expected in {
                "schemaVersion": 2,
                "buildId": binding["relay_build_sha256"],
                "provider": self._open_agent_lab_provider,
                "model": self._open_agent_lab_model,
                "budgetClass": self._open_agent_lab_budget_class,
            }.items()
        ):
            raise RuntimeError("Relay bootstrap identity does not match this trial.")
        self._validate_request_source()
        self._validate_route_authorization(environment, binding)
        authorization = await environment.service_exec(
            _RELAY_AUTHORIZE_COMMAND,
            service=RELAY_SERVICE,
            timeout_sec=10,
            user="0",
        )
        if authorization.return_code != 0:
            raise RuntimeError("Relay refused post-validation authorization.")
        token_result = await environment.service_exec(
            _RELAY_TOKEN_COMMAND,
            service=RELAY_SERVICE,
            timeout_sec=25,
            user="1000",
        )
        try:
            relay_token = _relay_capability(
                token_result.stdout or "", identity["capabilityId"]
            )
        except RuntimeError as error:
            raise RuntimeError(
                "Failed to obtain the per-trial relay capability."
            ) from error
        if token_result.return_code != 0:
            raise RuntimeError("Failed to obtain the per-trial relay capability.")
        return relay_token

    def _validate_route_authorization(
        self,
        environment: _PinnedRelayDockerEnvironment,
        binding: dict[str, Any],
    ) -> None:
        role = environment._relay_role
        if role == "fixture":
            if self._policy_file is not None:
                raise RuntimeError("Fixture runs cannot carry live authorization.")
            return
        active_trial_dir = Path(environment.trial_paths.trial_dir)
        if not active_trial_dir.is_absolute() or Path(
            os.path.abspath(active_trial_dir)
        ) != Path(os.path.abspath(self.logs_dir.parent)):
            raise RuntimeError("Agent and environment trial directories disagree.")
        credential_path = getattr(environment, "_provider_secret_path", None)
        credential_identity = getattr(
            environment, "_provider_credential_identity", None
        )
        if (
            self._policy_file is None
            or credential_path is None
            or credential_identity is None
            or _harbor_environment._credential_identity(credential_path)
            != credential_identity
        ):
            raise RuntimeError("Live provider authorization inputs are unavailable.")
        _validate_authorization_source()
        if self._LIVE_ROUTE_PROBE:
            _live_route_probe.validate_probe_cap(
                self._policy_file,
                self._open_agent_lab_provider,
                self._open_agent_lab_model,
                binding,
                credential_path,
                active_trial_dir,
            )
        else:
            _live_route_probe.validate_pilot_authorization(
                self._policy_file,
                self._open_agent_lab_provider,
                self._open_agent_lab_model,
                binding,
                credential_path,
                active_trial_dir,
            )

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        environment = self._validated_environment(environment)
        if self._codex_run_active:
            raise RuntimeError("Concurrent Codex runs are not supported.")
        self._codex_run_active = True
        self._codex_launches = 0
        try:
            relay_token = await self._authorize_relay(environment)
            primary_error: BaseException | None = None
            try:
                with environment.scoped_exec_env({_RELAY_TOKEN_ENV: relay_token}):
                    self._codex_launch_task = asyncio.current_task()
                    try:
                        await self._run_once(instruction, environment, context)
                    finally:
                        self._codex_launch_task = None
            except BaseException as error:
                primary_error = error
                raise
            finally:
                await self._retain_after_run(environment, primary_error)
        finally:
            self._codex_launch_task = None
            self._codex_run_active = False

    async def _run_once(
        self,
        instruction: str,
        environment: _PinnedRelayDockerEnvironment,
        context: AgentContext,
    ) -> None:
        self._validate_request_source()
        if self._LIVE_ROUTE_PROBE:
            instruction, _ = live_route_probe_instruction(self._open_agent_lab_provider)
            async with asyncio.timeout(LIVE_ROUTE_PROBE_LIMITS["codexTimeoutSeconds"]):
                await super().run(instruction, environment, context)
        else:
            await super().run(instruction, environment, context)
        if self._codex_launches != 1:
            raise RuntimeError("Codex did not launch exactly once.")
        if not self._LIVE_ROUTE_PROBE:
            return
        effect = await super().exec_as_agent(
            environment,
            command=(
                'test "$(cat /tmp/open-agent-lab-live-route-probe)" = '
                "live-route-probe-v1"
            ),
            timeout_sec=10,
        )
        if effect.return_code != 0:
            raise RuntimeError("Live-route probe effect is unavailable.")
        self._open_agent_lab_variant["effect_verified"] = True

    async def _retain_after_run(
        self,
        environment: _PinnedRelayDockerEnvironment,
        primary_error: BaseException | None,
    ) -> None:
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
            if primary_error is None:
                raise

    def _validate_request_source(self) -> dict[str, Any]:
        if self._open_agent_lab_run_binding is None:
            raise RuntimeError("Live provider work requires a prepared run binding.")
        _validate_live_source(self._open_agent_lab_run_binding)
        return self._open_agent_lab_run_binding

    def _validated_environment(
        self, environment: BaseEnvironment
    ) -> _PinnedRelayDockerEnvironment:
        pinned = _pinned_environment(environment)
        allowed = (
            {"live-route-probe"} if self._LIVE_ROUTE_PROBE else {"pilot", "fixture"}
        )
        if getattr(pinned, "_relay_role", None) not in allowed:
            raise RuntimeError("Agent and relay policy roles do not match.")
        return pinned

    async def _seal_and_retain(self, environment: BaseEnvironment) -> None:
        environment = self._validated_environment(environment)
        command = (
            f"kill -USR2 1 && i=0; while [ ! -f {RELAY_SEAL_PATH} ]; do "
            'i=$((i+1)); [ "$i" -lt 150 ] || exit 1; sleep 0.1; done'
        )
        async with asyncio.timeout(20):
            result = await environment.service_exec(
                command,
                service=RELAY_SERVICE,
                timeout_sec=20,
                user="1000",
            )
            if result.return_code != 0:
                detail = result.stderr or result.stdout or "no output"
                raise RuntimeError(f"Relay seal failed: {detail[-500:]}")
            self._provider_evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            journal_path = self._provider_evidence_dir / "provider-metadata.ndjson"
            seal_path = self._provider_evidence_dir / "provider-metadata.ndjson.sealed"

            async def retain(source: str, target: Path) -> None:
                encoded = await environment.service_exec(
                    f"base64 -w0 {source}",
                    service=RELAY_SERVICE,
                    timeout_sec=10,
                    user="1000",
                )
                if encoded.return_code != 0:
                    detail = encoded.stderr or encoded.stdout or "no output"
                    raise RuntimeError(f"Relay evidence read failed: {detail[-500:]}")
                try:
                    content = base64.b64decode(
                        (encoded.stdout or "").strip(), validate=True
                    )
                except ValueError as error:
                    raise RuntimeError(
                        "Relay evidence was not valid base64."
                    ) from error
                target.write_bytes(content)

            await retain(RELAY_JOURNAL_PATH, journal_path)
            await retain(RELAY_SEAL_PATH, seal_path)
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
                allow_empty=True,
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
            "variant_id": self._open_agent_lab_variant["variant_id"],
            "requested_developer_instructions_sha256": self._open_agent_lab_variant[
                "requested_developer_instructions_sha256"
            ],
            "codex_runtime_spec_sha256": CODEX_RUNTIME_SPEC_SHA256,
            "run_binding": self._open_agent_lab_run_binding,
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
        metadata["agent_variant"] = dict(self._open_agent_lab_variant)
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


class OpenAgentLabCodexVerifyInstructionV1(OpenAgentLabCodex):
    """Named Harbor treatment arm for the frozen verification instruction."""

    def __init__(
        self,
        *args: Any,
        enable_verify_instruction_v1: bool = True,
        **kwargs: Any,
    ) -> None:
        if enable_verify_instruction_v1 is not True:
            raise ValueError(
                "The treatment agent requires enable_verify_instruction_v1=true."
            )
        super().__init__(*args, enable_verify_instruction_v1=True, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        return "open-agent-lab-codex-verify-instruction-v1"


class OpenAgentLabCodexDeliverableFirstV1(OpenAgentLabCodexVerifyInstructionV1):
    """Named Harbor treatment arm for the observed deliverable-first instruction."""

    _VARIANT_ID = "deliverable-first-v1"
    _DEVELOPER_INSTRUCTION = _DELIVERABLE_FIRST_INSTRUCTION
    _DEVELOPER_INSTRUCTION_SHA256 = _DELIVERABLE_FIRST_INSTRUCTION_SHA256

    @staticmethod
    @override
    def name() -> str:
        return "open-agent-lab-codex-deliverable-first-v1"


class OpenAgentLabCodexLiveRouteProbe(OpenAgentLabCodex):
    """One fixed, non-scoring tool round through the isolated production relay."""

    _LIVE_ROUTE_PROBE = True

    @staticmethod
    @override
    def name() -> str:
        return "open-agent-lab-codex-live-route-probe-v1"
