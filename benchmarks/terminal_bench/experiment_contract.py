"""Immutable wire names and small primitives for the frozen experiment."""

import hashlib
import json
import re
from types import MappingProxyType

EXPERIMENT_ID = "terminal-bench-2.1-verify-instruction-v1"
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
