"""A minimal isolated-Responses profile layered on Harbor's Codex agent."""

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, TypedDict, override
from urllib.parse import urlsplit

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from . import harbor_environment as _harbor_environment
from .harbor_environment import (
    PinnedRelayDockerEnvironment as _PinnedRelayDockerEnvironment,
)
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
_RELAY_BOOTSTRAP_FILE = f"{_RELAY_SIDECAR}.bootstrap-ready"
_RELAY_BUILD_ID_FILE = "/app/relay-build-id"
_RELAY_AUTHORIZE_COMMAND = "kill -USR1 1"
_RELAY_BOOTSTRAP_COMMAND = f"cat {_RELAY_BOOTSTRAP_FILE}"
_RELAY_TOKEN_COMMAND = (
    f"i=0; while [ ! -f {_RELAY_TOKEN_FILE} ]; do i=$((i+1)); "
    f'[ "$i" -lt 200 ] || exit 1; sleep 0.1; done; cat {_RELAY_TOKEN_FILE}'
)
_RELAY_TOKEN_ENV = "OAL_RELAY_TOKEN"
_RELAY_URL_ENV = "OAL_RELAY_URL"
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
_EXPERIMENT_ID = "terminal-bench-2.1-verify-instruction-v1"
_HARBOR_VERSION = "0.22.0"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPABILITY = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_RUN_BINDING_KEYS = {
    "schema_version",
    "experiment_id",
    "replication_id",
    "source_revision",
    "experiment_manifest_sha256",
    "relay_build_sha256",
    "relay_image_sha256",
    "preflight_sha256",
    "task_snapshots_sha256",
}
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_EXPERIMENT_MANIFEST = (
    _REPOSITORY_ROOT / "benchmarks/terminal_bench/verify-instruction-v1.experiment.json"
)


def _run_binding(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _RUN_BINDING_KEYS:
        raise ValueError("run_binding has an invalid schema.")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["experiment_id"] != _EXPERIMENT_ID
        or value["replication_id"] not in {"screen-v1", "mirror-v1"}
        or not isinstance(value["source_revision"], str)
        or not _SOURCE_REVISION.fullmatch(value["source_revision"])
        or any(
            not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key])
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
        != {"schemaVersion", "buildId", "provider", "model", "capabilityId"}
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or not isinstance(value["buildId"], str)
        or not _DIGEST.fullmatch(value["buildId"])
        or not isinstance(value["provider"], str)
        or not isinstance(value["model"], str)
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
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or value["capabilityId"] != capability_id
        or not isinstance(value["bearer"], str)
        or not _CAPABILITY.fullmatch(value["bearer"])
    ):
        raise RuntimeError("Relay capability is invalid.")
    return value["bearer"]


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
            not isinstance(value, str) or not _DIGEST.fullmatch(value)
            for value in allowed_relay_build_ids
        ):
            raise ValueError("relayBuildIds has invalid values")
        if (
            not isinstance(runtime, dict)
            or type(runtime.get("hermeticCodexRuntimeReady")) is not bool
        ):
            raise ValueError("hermetic Codex runtime gate is invalid")
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
        enable_verify_instruction_v1: bool = False,
        run_binding: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        _validate_harbor_runtime()
        if config is not None:
            raise ValueError("OpenAgentLabCodex owns its benchmark config.")
        if "provider_free_fixture" in kwargs:
            raise ValueError("Provider-free runs require a prepared source binding.")
        if type(enable_verify_instruction_v1) is not bool:
            raise ValueError(
                "enable_verify_instruction_v1 must be a boolean experiment switch."
            )
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
        binding = _run_binding(run_binding)
        if binding is None:
            raise RuntimeError("Live provider work requires a prepared run binding.")
        _validate_live_source(binding)

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
        if enable_verify_instruction_v1:
            provider_config["developer_instructions"] = _VERIFY_INSTRUCTION
        agent_env["CODEX_AUTH_JSON_PATH"] = str(_EMPTY_AUTH)
        self._open_agent_lab_provider = provider
        self._open_agent_lab_model = model
        self._open_agent_lab_run_binding = binding
        self._open_agent_lab_variant = {
            "schema_version": 1,
            "variant_id": (
                "verify-instruction-v1"
                if enable_verify_instruction_v1
                else "control-v1"
            ),
            "developer_instruction_requested": enable_verify_instruction_v1,
            "requested_developer_instructions_sha256": (
                _VERIFY_INSTRUCTION_SHA256 if enable_verify_instruction_v1 else None
            ),
        }
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
        binding = self._validate_request_source()
        await super().setup(environment)
        build_result = await environment.service_exec(
            f"cat {_RELAY_BUILD_ID_FILE}",
            service=_RELAY_SERVICE,
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
            service=_RELAY_SERVICE,
            timeout_sec=10,
            user="0",
        )
        if bootstrap_result.return_code != 0:
            raise RuntimeError("Relay bootstrap identity is unavailable.")
        identity = _bootstrap_identity(bootstrap_result.stdout or "")
        if any(
            identity[key] != expected
            for key, expected in {
                "schemaVersion": 1,
                "buildId": binding["relay_build_sha256"],
                "provider": self._open_agent_lab_provider,
                "model": self._open_agent_lab_model,
            }.items()
        ):
            raise RuntimeError("Relay bootstrap identity does not match this trial.")
        self._validate_request_source()
        authorization = await environment.service_exec(
            _RELAY_AUTHORIZE_COMMAND,
            service=_RELAY_SERVICE,
            timeout_sec=10,
            user="0",
        )
        if authorization.return_code != 0:
            raise RuntimeError("Relay refused post-validation authorization.")
        token_result = await environment.service_exec(
            _RELAY_TOKEN_COMMAND,
            service=_RELAY_SERVICE,
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
        self._extra_env[_RELAY_TOKEN_ENV] = relay_token

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        primary_error: BaseException | None = None
        try:
            self._validate_request_source()
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
                if primary_error is None:
                    raise

    def _validate_request_source(self) -> dict[str, Any]:
        if self._open_agent_lab_run_binding is None:
            raise RuntimeError("Live provider work requires a prepared run binding.")
        _validate_live_source(self._open_agent_lab_run_binding)
        return self._open_agent_lab_run_binding

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

            async def retain(source: str, target: Path) -> None:
                encoded = await environment.service_exec(
                    f"base64 -w0 {source}",
                    service=_RELAY_SERVICE,
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

            await retain(_RELAY_SIDECAR, journal_path)
            await retain(_RELAY_SEAL, seal_path)
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
