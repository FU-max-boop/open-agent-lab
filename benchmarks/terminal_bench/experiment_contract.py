"""Immutable wire names and small primitives for the frozen experiment."""

import hashlib
import json
import re
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
CODEX_PROVIDER_RETRY_POLICY = MappingProxyType(
    {
        "request_max_retries": 0,
        "stream_max_retries": 0,
        "unbounded_connection_retries": False,
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
LIVE_ROUTE_PROBE_INSTRUCTION_SHA256 = (
    "sha256:" + hashlib.sha256(LIVE_ROUTE_PROBE_INSTRUCTION.encode()).hexdigest()
)
LIVE_ROUTE_PROBE_COMMAND_SHA256 = (
    "sha256:" + hashlib.sha256(LIVE_ROUTE_PROBE_COMMAND.encode()).hexdigest()
)
LIVE_ROUTE_PROBE_EFFECT_SHA256 = (
    "sha256:" + hashlib.sha256(LIVE_ROUTE_PROBE_EFFECT.encode()).hexdigest()
)
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


def artifact_manifest() -> list[dict[str, str | None]]:
    return [
        dict(zip(_ARTIFACT_FIELDS, entry, strict=True)) for entry in _ARTIFACT_ENTRIES
    ]


def live_route_probe_relay_command(command: object) -> list[str]:
    """Derive the one narrow live-route relay policy from a frozen pilot command."""
    if not isinstance(command, list) or any(
        not isinstance(item, str) for item in command
    ):
        raise ValueError("relay command is invalid")
    result = list(command)
    replacements = {
        "--ttl-seconds": str(LIVE_ROUTE_PROBE_LIMITS["ttlSeconds"]),
        "--max-requests": str(LIVE_ROUTE_PROBE_LIMITS["maxRequests"]),
    }
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
