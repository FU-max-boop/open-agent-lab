import unittest

from benchmarks.terminal_bench.experiment_contract import (
    ENVIRONMENT_IMPORT,
    EXPERIMENT_ID,
    PREFLIGHT_KEYS,
    RELAY_ARTIFACT_LIMITS,
    RELAY_BUILD_ID_PATH,
    RELAY_JOURNAL_PATH,
    RELAY_SEAL_PATH,
    RELAY_SERVICE,
    RUN_BINDING_KEYS,
    artifact_manifest,
    canonical_json,
    digest_bytes,
    is_digest,
    is_revision,
    is_strict_int,
)

_EXPECTED_MANIFEST = [
    {
        "source": "/logs/artifacts",
        "destination": "artifacts/logs/artifacts",
        "type": "directory",
        "status": "empty",
        "service": None,
    },
    {
        "source": "/var/lib/open-agent-lab/provider-metadata.ndjson",
        "destination": "artifacts/provider-metadata.ndjson",
        "type": "file",
        "status": "ok",
        "service": "open-agent-lab-relay",
    },
    {
        "source": "/var/lib/open-agent-lab/provider-metadata.ndjson.sealed",
        "destination": "artifacts/provider-metadata.ndjson.sealed",
        "type": "file",
        "status": "ok",
        "service": "open-agent-lab-relay",
    },
]


class ExperimentContractTest(unittest.TestCase):
    def test_wire_literals_are_pinned(self) -> None:
        self.assertEqual(EXPERIMENT_ID, "terminal-bench-2.1-verify-instruction-v1")
        self.assertEqual(
            ENVIRONMENT_IMPORT,
            "benchmarks.terminal_bench.harbor_environment:PinnedRelayDockerEnvironment",
        )
        self.assertEqual(RELAY_SERVICE, "open-agent-lab-relay")
        self.assertEqual(RELAY_BUILD_ID_PATH, "/app/relay-build-id")
        self.assertEqual(
            RELAY_JOURNAL_PATH,
            "/var/lib/open-agent-lab/provider-metadata.ndjson",
        )
        self.assertEqual(
            RELAY_SEAL_PATH,
            "/var/lib/open-agent-lab/provider-metadata.ndjson.sealed",
        )
        self.assertEqual(
            dict(RELAY_ARTIFACT_LIMITS),
            {
                "/var/lib/open-agent-lab/provider-metadata.ndjson": 4 * 1024 * 1024,
                "/var/lib/open-agent-lab/provider-metadata.ndjson.sealed": 64 * 1024,
            },
        )
        self.assertEqual(
            RUN_BINDING_KEYS,
            frozenset(
                {
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
            ),
        )
        self.assertEqual(
            PREFLIGHT_KEYS,
            frozenset(
                {
                    "schemaVersion",
                    "experimentId",
                    "replicationId",
                    "sourceRevision",
                    "experimentManifestSha256",
                    "relayBuildSha256",
                    "relayImageSha256",
                    "taskSnapshotsSha256",
                    "cleanTree",
                    "createdAt",
                }
            ),
        )
        with self.assertRaises(TypeError):
            RELAY_ARTIFACT_LIMITS[RELAY_JOURNAL_PATH] = 0  # type: ignore[index]

    def test_artifact_manifest_is_exact_and_fresh(self) -> None:
        first = artifact_manifest()
        second = artifact_manifest()
        self.assertEqual(first, _EXPECTED_MANIFEST)
        self.assertEqual(second, _EXPECTED_MANIFEST)
        self.assertIsNot(first, second)
        self.assertIsNot(first[1], second[1])

        first[1]["source"] = "tampered"
        first.append({"source": "extra"})
        self.assertEqual(artifact_manifest(), _EXPECTED_MANIFEST)

    def test_canonical_json_is_utf8_sorted_and_compact(self) -> None:
        self.assertEqual(
            canonical_json({"z": [True, None], "a": "雪"}),
            '{"a":"雪","z":[true,null]}'.encode(),
        )

    def test_canonical_json_rejects_non_finite_numbers(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_json({"value": value})

    def test_digest_uses_prefixed_lowercase_sha256(self) -> None:
        self.assertEqual(
            digest_bytes(b"contract"),
            "sha256:cc8321d6375c494d043fdd0260f21bc0ec51dacc9f6abb7f909cdcd3041b78bf",
        )
        self.assertTrue(is_digest("sha256:" + "a" * 64))
        for value in ("a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63, 1):
            with self.subTest(value=value):
                self.assertFalse(is_digest(value))

    def test_revision_is_exactly_40_lowercase_hex_characters(self) -> None:
        self.assertTrue(is_revision("0123456789abcdef" * 2 + "01234567"))
        for value in ("a" * 39, "a" * 41, "A" * 40, "g" * 40, None):
            with self.subTest(value=value):
                self.assertFalse(is_revision(value))

    def test_strict_int_rejects_bool(self) -> None:
        self.assertTrue(is_strict_int(0))
        self.assertTrue(is_strict_int(-1))
        self.assertFalse(is_strict_int(True))
        self.assertFalse(is_strict_int(False))
        self.assertFalse(is_strict_int(1.0))


if __name__ == "__main__":
    unittest.main()
