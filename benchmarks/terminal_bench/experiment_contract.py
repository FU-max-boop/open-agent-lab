"""Immutable wire names and small primitives for the frozen experiment."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from .codex_runtime import (
    CODEX_RUNTIME_INSTALL_ROOT,
    CODEX_RUNTIME_PREPARED_RELATIVE,
)

EXPERIMENT_ID = "terminal-bench-2.1-verify-instruction-v1"
CODEX_VERSION = "0.149.0"
ENVIRONMENT_IMPORT = (
    "benchmarks.terminal_bench.harbor_environment:PinnedRelayDockerEnvironment"
)
RELAY_SERVICE = "open-agent-lab-relay"
RELAY_BUILD_ID_PATH = "/app/relay-build-id"
RELAY_JOURNAL_PATH = "/var/lib/open-agent-lab/provider-metadata.ndjson"
RELAY_SEAL_PATH = f"{RELAY_JOURNAL_PATH}.sealed"
RELAY_ARTIFACT_LIMITS = MappingProxyType(
    {RELAY_JOURNAL_PATH: 4 * 1024 * 1024, RELAY_SEAL_PATH: 64 * 1024}
)
RELAY_CLAIM_FIELDS = frozenset(
    {
        "schemaVersion",
        "proofClass",
        "provider",
        "policySha256",
        "jobId",
        "jobDir",
        "trialLockSha256",
        "claimedAt",
    }
)
LIVE_ROUTE_PROBE_TASK = "open-agent-lab/live-route-probe"
LIVE_ROUTE_PROBE_AGENT = "open-agent-lab-codex-live-route-probe-v1"
LIVE_ROUTE_PROBE_AGENT_IMPORT = (
    "benchmarks.terminal_bench.harbor_agent:OpenAgentLabCodexLiveRouteProbe"
)
PILOT_RECEIPT_ENV = "OAL_PILOT_RECEIPT_FILE"
LIVE_ROUTE_PROBE_CAP_ENV = "OAL_SPEND_CAP_ATTESTATION_FILE"
LIVE_ROUTE_PROBE_INTERNAL_NETWORK = "live-route-probe-internal"
LIVE_ROUTE_PROBE_EGRESS_NETWORK = "live-route-probe-egress"
PILOT_RELAY_TTL_SECONDS = 14_400
LIVE_ROUTE_PROBE_LIMITS = MappingProxyType(
    {
        "ttlSeconds": 600,
        "maxRequests": 2,
        "maxRequestBytes": 512 * 1024,
        "maxResponseBytes": 512 * 1024,
        "connectTimeoutMs": 30_000,
        "idleTimeoutMs": 180_000,
        "codexTimeoutSeconds": 480,
    }
)
SCORED_SLOT_OUTPUT_TOKEN_LIMIT = 50_000
SCORED_PROVIDER_OUTPUT_TOKEN_LIMIT = 1_000_000
SCORED_CAMPAIGN_OUTPUT_TOKEN_LIMIT = 2_000_000
ZAI_ROUTE_PROBE_OUTPUT_BUDGET = MappingProxyType(
    {
        "slotOutputTokenLimit": 8_448,
        "roundOutputTokenLimits": (8_192, 256),
        "minimumRequestedRound2OutputTokens": 1_024,
    }
)
CODEX_PROVIDER_RETRY_POLICY = MappingProxyType(
    {
        "request_max_retries": 0,
        "stream_max_retries": 0,
        "unbounded_connection_retries": False,
    }
)
DEEPSEEK_PROVIDER_CONTROL_SOURCES = MappingProxyType(
    {"providerControl": "https://platform.deepseek.com/"}
)
ZAI_PROVIDER_CONTROL_SOURCES = MappingProxyType(
    {
        "endpointProtocol": "https://docs.z.ai/devpack/tool/others",
        "providerControl": "https://docs.z.ai/devpack/faq",
    }
)
_PROVIDER_CONTROL_COMMON_FIELDS = frozenset(
    {
        "controlClass",
        "scope",
        "observedAt",
        "expiresAt",
        "evidenceSha256",
        "sourceUrls",
        "assertedBy",
    }
)
LIVE_ROUTE_PROBE_COMMAND = """test -z "${DEEPSEEK_API_KEY+x}" && \\
test -z "${ZAI_API_KEY+x}" && \\
test -z "${OAL_RELAY_TOKEN:-}" && \\
test ! -e /run/secrets/provider-api-key && \\
printf 'live-route-probe-v1\\n' > /tmp/open-agent-lab-live-route-probe"""
LIVE_ROUTE_PROBE_EFFECT = "live-route-probe-v1\n"
LIVE_ROUTE_PROBE_INSTRUCTION = f"""This is a non-scoring provider route probe.
Call exec_command exactly once with this exact JSON argument and no other tool:
{{"cmd":{json.dumps(LIVE_ROUTE_PROBE_COMMAND)}}}
After that command succeeds, reply only LIVE_ROUTE_PROBE_OK.
"""
ZAI_LIVE_ROUTE_PROBE_INSTRUCTION = f"""This is a non-scoring provider route probe.
Call exec_command exactly once with this exact JSON argument and no other tool:
{{"cmd":{json.dumps(LIVE_ROUTE_PROBE_COMMAND)}}}
After that command succeeds, use no more tools. Produce a neutral final response.
It must contain at least 1024 output tokens. The response must be unrelated to benchmark tasks, the tool
result, and any intermediate or final answer; repeat simple numbered observations about
ordinary geometric shapes until the requested length is reached.
"""
LIVE_ROUTE_PROBE_INSTRUCTION_SHA256 = (
    "sha256:" + hashlib.sha256(LIVE_ROUTE_PROBE_INSTRUCTION.encode()).hexdigest()
)
ZAI_LIVE_ROUTE_PROBE_INSTRUCTION_SHA256 = (
    "sha256:" + hashlib.sha256(ZAI_LIVE_ROUTE_PROBE_INSTRUCTION.encode()).hexdigest()
)
LIVE_ROUTE_PROBE_COMMAND_SHA256 = (
    "sha256:" + hashlib.sha256(LIVE_ROUTE_PROBE_COMMAND.encode()).hexdigest()
)
LIVE_ROUTE_PROBE_EFFECT_SHA256 = (
    "sha256:" + hashlib.sha256(LIVE_ROUTE_PROBE_EFFECT.encode()).hexdigest()
)


def live_route_probe_instruction(provider: str) -> tuple[str, str]:
    """Return the exact provider-specific non-scoring probe instruction and digest."""
    if provider == "deepseek":
        return LIVE_ROUTE_PROBE_INSTRUCTION, LIVE_ROUTE_PROBE_INSTRUCTION_SHA256
    if provider == "zai":
        return ZAI_LIVE_ROUTE_PROBE_INSTRUCTION, ZAI_LIVE_ROUTE_PROBE_INSTRUCTION_SHA256
    raise ValueError("unknown live-route provider")


def live_route_probe_variant(
    provider: str, *, effect_verified: bool
) -> dict[str, object]:
    """Return the retained agent variant for one provider-specific route probe."""
    _, instruction_sha256 = live_route_probe_instruction(provider)
    return {
        "schema_version": 1,
        "variant_id": "live-route-probe-v1",
        "developer_instruction_requested": False,
        "requested_developer_instructions_sha256": None,
        "benchmark_task_instruction_used": False,
        "benchmark_reward_used": False,
        "instruction_sha256": instruction_sha256,
        "command_sha256": LIVE_ROUTE_PROBE_COMMAND_SHA256,
        "effect_sha256": LIVE_ROUTE_PROBE_EFFECT_SHA256,
        "effect_verified": effect_verified,
        **CODEX_PROVIDER_RETRY_POLICY,
        "limits": dict(LIVE_ROUTE_PROBE_LIMITS),
    }


RUN_BINDING_KEYS = frozenset(
    """schema_version experiment_id replication_id source_revision
    experiment_manifest_sha256 relay_build_sha256 relay_image_sha256
    preflight_sha256 task_snapshots_sha256""".split()  # noqa: SIM905
)
PREFLIGHT_KEYS = frozenset(
    """schemaVersion experimentId replicationId sourceRevision
    experimentManifestSha256 relayBuildSha256 relayImageSha256
    taskSnapshotsSha256 cleanTree createdAt""".split()  # noqa: SIM905
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_FIELDS = ("source", "destination", "type", "status", "service")
_ARTIFACT_ENTRIES = (
    ("/logs/artifacts", "artifacts/logs/artifacts", "directory", "empty", None),
    (
        RELAY_JOURNAL_PATH,
        "artifacts/provider-metadata.ndjson",
        "file",
        "ok",
        RELAY_SERVICE,
    ),
    (
        RELAY_SEAL_PATH,
        "artifacts/provider-metadata.ndjson.sealed",
        "file",
        "ok",
        RELAY_SERVICE,
    ),
)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def canonical_digest(value: object) -> str:
    return digest_bytes(canonical_json(value))


def same_json(left: object, right: object) -> bool:
    return canonical_json(left) == canonical_json(right)


def relay_claim_name(provider: str, role: str, trial_lock_sha256: str) -> str:
    if (
        provider not in {"deepseek", "zai"}
        or role not in {"probe", "pilot"}
        or not is_digest(trial_lock_sha256)
    ):
        raise ValueError("invalid relay claim identity")
    digest = trial_lock_sha256.removeprefix("sha256:")
    return f"{provider}.{role}.{digest}.claim.json"


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def is_revision(value: object) -> bool:
    return isinstance(value, str) and _REVISION.fullmatch(value) is not None


def is_strict_int(value: object) -> bool:
    return type(value) is int


def provider_control(value: object, provider: str) -> dict[str, object]:
    """Validate and copy the exact provider-specific launch-control union."""
    if not isinstance(value, dict):
        raise TypeError("providerControl must be an object")
    asserted_by = value.get("assertedBy")
    common_invalid = (
        value.get("scope") != "campaign"
        or not is_digest(value.get("evidenceSha256"))
        or not isinstance(asserted_by, str)
        or not asserted_by.strip()
        or len(asserted_by.encode()) > 256
    )
    if provider == "deepseek":
        limit = value.get("limitUsd")
        invalid = (
            set(value) != _PROVIDER_CONTROL_COMMON_FIELDS | {"limitUsd"}
            or value.get("controlClass") != "provider_hard_spend_cap_usd"
            or value.get("sourceUrls") != dict(DEEPSEEK_PROVIDER_CONTROL_SOURCES)
            or type(limit) not in (int, float)
            or not 0 < limit <= 2
        )
    elif provider == "zai":
        quota = value.get("quotaSnapshot")
        invalid = (
            set(value)
            != _PROVIDER_CONTROL_COMMON_FIELDS
            | {"baseUrl", "protocol", "plan", "noBalanceDeduction", "quotaSnapshot"}
            or value.get("controlClass")
            != "coding_plan_subscription_quota_no_balance_deduction"
            or value.get("baseUrl") != "https://api.z.ai/api/v1"
            or value.get("protocol") != "openai_responses"
            or value.get("plan") != "zai_coding_plan"
            or value.get("noBalanceDeduction") is not True
            or value.get("sourceUrls") != dict(ZAI_PROVIDER_CONTROL_SOURCES)
            or not isinstance(quota, dict)
            or set(quota) != {"fiveHour", "weekly"}
        )
        if not invalid:
            for period in ("fiveHour", "weekly"):
                snapshot = quota[period]
                remaining = (
                    snapshot.get("remainingPercent")
                    if isinstance(snapshot, dict)
                    else None
                )
                if (
                    not isinstance(snapshot, dict)
                    or set(snapshot) != {"remainingPercent", "resetsAt"}
                    or type(remaining) not in (int, float)
                    or not 0 < remaining <= 100
                ):
                    invalid = True
                    break
    else:
        raise ValueError("unknown providerControl provider")
    if common_invalid or invalid:
        raise ValueError("providerControl policy drifted")
    return json.loads(canonical_json(value))


def provider_control_window(
    value: object, provider: str
) -> tuple[dict[str, object], datetime, datetime]:
    """Validate the bounded UTC window, including ZAI quota reset coverage."""
    control = provider_control(value, provider)

    def utc(field: str, source: dict[str, object] = control) -> datetime:
        raw = source.get(field)
        if not isinstance(raw, str) or not raw.endswith("Z"):
            raise ValueError(f"providerControl {field} must be UTC")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"providerControl {field} must be UTC") from error
        if parsed.utcoffset() != timedelta(0):
            raise ValueError(f"providerControl {field} must be UTC")
        return parsed

    observed, expires = utc("observedAt"), utc("expiresAt")
    if not timedelta(0) < expires - observed <= timedelta(hours=24):
        raise ValueError("providerControl window must be positive and at most 24 hours")
    if provider == "zai":
        quota = control["quotaSnapshot"]
        for period, duration in (
            ("fiveHour", timedelta(hours=5)),
            ("weekly", timedelta(days=7)),
        ):
            reset = utc("resetsAt", quota[period])
            if not expires < reset or not observed < reset <= observed + duration:
                raise ValueError("providerControl quota reset is outside its period")
    return control, observed, expires


def provider_control_identity(
    value: object, provider: str, model: object, credential_sha256: object
) -> dict[str, object]:
    """Extract the stable policy identity, excluding root-local attestations."""
    control = provider_control_window(value, provider)[0]
    if not isinstance(model, str) or not model or not is_digest(credential_sha256):
        raise ValueError("providerControl identity is invalid")
    identity = {
        key: control[key]
        for key in ("controlClass", "scope", "evidenceSha256", "sourceUrls")
    }
    identity.update(
        {
            "provider": provider,
            "model": model,
            "providerCredentialSha256": credential_sha256,
        }
    )
    for key in (
        ("limitUsd",)
        if provider == "deepseek"
        else ("baseUrl", "protocol", "plan", "noBalanceDeduction")
    ):
        identity[key] = control[key]
    return json.loads(canonical_json(identity))


def artifact_manifest() -> list[dict[str, str | None]]:
    return [
        dict(zip(_ARTIFACT_FIELDS, entry, strict=True)) for entry in _ARTIFACT_ENTRIES
    ]


def live_route_probe_relay_command(command: object, provider: str) -> list[str]:
    """Derive the one narrow live-route relay policy from a frozen pilot command."""
    if not isinstance(command, list) or any(
        not isinstance(item, str) for item in command
    ):
        raise ValueError("relay command is invalid")
    result = list(command)
    replacements = {
        "--ttl-seconds": str(LIVE_ROUTE_PROBE_LIMITS["ttlSeconds"]),
        "--max-requests": str(LIVE_ROUTE_PROBE_LIMITS["maxRequests"]),
        "--budget-class": (
            "zai_route_probe" if provider == "zai" else "unmetered_route_probe"
        ),
    }
    if provider not in {"deepseek", "zai"}:
        raise ValueError("relay provider is invalid")
    strict_flags = (
        "--max-request-bytes",
        "--max-response-bytes",
        "--connect-timeout-ms",
        "--idle-timeout-ms",
    )
    if any(flag in result for flag in strict_flags):
        raise ValueError("relay command policy drifted")
    positions: dict[str, int] = {}
    for flag in (*replacements, "--build-id-file"):
        matches = [index for index, item in enumerate(result) if item == flag]
        if (
            len(matches) != 1
            or matches[0] + 1 >= len(result)
            or result[matches[0] + 1].startswith("--")
        ):
            raise ValueError("relay command policy drifted")
        positions[flag] = matches[0]
    for flag, value in replacements.items():
        result[positions[flag] + 1] = value
    insertion = positions["--build-id-file"]
    result[insertion:insertion] = [
        "--max-request-bytes",
        str(LIVE_ROUTE_PROBE_LIMITS["maxRequestBytes"]),
        "--max-response-bytes",
        str(LIVE_ROUTE_PROBE_LIMITS["maxResponseBytes"]),
        "--connect-timeout-ms",
        str(LIVE_ROUTE_PROBE_LIMITS["connectTimeoutMs"]),
        "--idle-timeout-ms",
        str(LIVE_ROUTE_PROBE_LIMITS["idleTimeoutMs"]),
    ]
    return result


def live_route_probe_networks(compose: object) -> dict[str, object]:
    """Give the task only a private path to the relay while the relay can egress."""
    if not isinstance(compose, dict):
        raise TypeError("relay Compose is invalid")
    result = json.loads(json.dumps(compose))
    services = result.get("services")
    main = services.get("main") if isinstance(services, dict) else None
    relay = services.get(RELAY_SERVICE) if isinstance(services, dict) else None
    if (
        not isinstance(main, dict)
        or not isinstance(relay, dict)
        or "networks" in result
        or any(
            "networks" in service or "network_mode" in service
            for service in (main, relay)
        )
    ):
        raise ValueError("relay Compose network policy drifted")
    main["networks"] = {LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {}}
    relay["networks"] = {
        LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {"aliases": [RELAY_SERVICE]},
        LIVE_ROUTE_PROBE_EGRESS_NETWORK: {},
    }
    result["networks"] = {
        LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {"internal": True},
        LIVE_ROUTE_PROBE_EGRESS_NETWORK: {"internal": False},
    }
    return result


def live_route_probe_config(
    output: Path,
    binding: dict[str, object],
    provider: str,
    model: str,
    reasoning: str,
    compose_path: Path,
    compose_sha256: str,
) -> dict[str, object]:
    """Return the one exact non-scoring probe job authorized for a prepared run."""
    if not output.is_absolute() or not compose_path.is_absolute():
        raise ValueError("live-route probe paths must be absolute")
    job_name = f"open-agent-lab-{binding['replication_id']}-{provider}-live-route-probe"
    return {
        "job_name": job_name,
        "jobs_dir": str(output / "live-route-jobs" / provider),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "quiet": False,
        "timeout_multiplier": 1.0,
        "retry": {"max_retries": 0},
        "verifier": {"disable": True},
        "artifacts": [
            {
                "source": RELAY_JOURNAL_PATH,
                "destination": Path(RELAY_JOURNAL_PATH).name,
                "service": RELAY_SERVICE,
            },
            {
                "source": RELAY_SEAL_PATH,
                "destination": Path(RELAY_SEAL_PATH).name,
                "service": RELAY_SERVICE,
            },
        ],
        "environment": {
            "type": "docker",
            "delete": True,
            "mounts": [
                {
                    "type": "bind",
                    "source": str(output / CODEX_RUNTIME_PREPARED_RELATIVE),
                    "target": CODEX_RUNTIME_INSTALL_ROOT,
                    "read_only": True,
                }
            ],
            "extra_docker_compose": [str(compose_path)],
            "import_path": ENVIRONMENT_IMPORT,
            "kwargs": {
                "relay_compose_sha256": compose_sha256,
                "run_binding": binding,
            },
        },
        "agents": [
            {
                "name": LIVE_ROUTE_PROBE_AGENT,
                "import_path": LIVE_ROUTE_PROBE_AGENT_IMPORT,
                "model_name": f"{provider}/{model}",
                "kwargs": {
                    "version": CODEX_VERSION,
                    "reasoning_effort": reasoning,
                    "enable_verify_instruction_v1": False,
                    "run_binding": binding,
                },
                "env": {
                    "OAL_RELAY_URL": f"http://{RELAY_SERVICE}:8080/v1",
                    LIVE_ROUTE_PROBE_CAP_ENV: str(
                        output / "authorizations" / f"{provider}.cap.json"
                    ),
                },
            }
        ],
        "tasks": [
            {
                "path": str(output / "tasks" / LIVE_ROUTE_PROBE_TASK.rsplit("/", 1)[1]),
                "source": LIVE_ROUTE_PROBE_TASK,
            }
        ],
    }
