import copy
import unittest

from benchmarks.terminal_bench.experiment_contract import (
    CODEX_PROVIDER_RETRY_POLICY,
    ENVIRONMENT_IMPORT,
    EXPERIMENT_ID,
    LIVE_ROUTE_PROBE_EGRESS_NETWORK,
    LIVE_ROUTE_PROBE_INTERNAL_NETWORK,
    LIVE_ROUTE_PROBE_LIMITS,
    PILOT_RELAY_TTL_SECONDS,
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
    live_route_probe_networks,
    live_route_probe_relay_command,
    provider_control_window,
    same_json,
)

_PILOT_RELAY_COMMAND = [
    "open-agent-lab",
    "relay",
    "--provider",
    "deepseek",
    "--ttl-seconds",
    str(PILOT_RELAY_TTL_SECONDS),
    "--max-requests",
    "256",
    "--build-id-file",
    "/app/relay-build-id",
]

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
    def test_provider_control_is_an_exact_discriminated_union(self) -> None:
        common = {
            "scope": "campaign",
            "observedAt": "2026-08-24T00:00:00Z",
            "expiresAt": "2026-08-24T04:00:00Z",
            "evidenceSha256": "sha256:" + "a" * 64,
            "assertedBy": "fixture operator",
        }
        deepseek = {
            **common,
            "controlClass": "provider_hard_spend_cap_usd",
            "limitUsd": 2,
            "sourceUrls": {"providerControl": "https://platform.deepseek.com/"},
        }
        zai = {
            **common,
            "controlClass": "coding_plan_subscription_quota_no_balance_deduction",
            "baseUrl": "https://api.z.ai/api/v1",
            "protocol": "openai_responses",
            "plan": "zai_coding_plan",
            "noBalanceDeduction": True,
            "quotaSnapshot": {
                "fiveHour": {
                    "remainingPercent": 80,
                    "resetsAt": "2026-08-24T05:00:00Z",
                },
                "weekly": {
                    "remainingPercent": 60,
                    "resetsAt": "2026-08-31T00:00:00Z",
                },
            },
            "sourceUrls": {
                "endpointProtocol": "https://docs.z.ai/devpack/tool/others",
                "providerControl": "https://docs.z.ai/devpack/faq",
            },
        }
        self.assertEqual(provider_control_window(deepseek, "deepseek")[0], deepseek)
        self.assertEqual(provider_control_window(zai, "zai")[0], zai)
        with self.assertRaises(TypeError):
            provider_control_window([], "deepseek")
        with self.assertRaises(ValueError):
            provider_control_window({"spendCap": {"limitUsd": 2}}, "deepseek")

        missing = object()
        cases = (
            ("deepseek", "controlClass", "wrong"),
            ("deepseek", "estimatedUsd", 1),
            ("deepseek", "limitUsd", True),
            ("deepseek", "limitUsd", 10**1000),
            (
                "deepseek",
                "sourceUrls.providerControl",
                "https://api-docs.deepseek.com/",
            ),
            ("zai", "limitUsd", 1),
            ("zai", "quotaSnapshot.fiveHour.remainingPercent", 0),
            ("zai", "quotaSnapshot.weekly.remainingPercent", False),
            ("zai", "quotaSnapshot.weekly.remainingPercent", 10**1000),
            (
                "zai",
                "quotaSnapshot.fiveHour.resetsAt",
                common["expiresAt"],
            ),
            (
                "zai",
                "sourceUrls.endpointProtocol",
                "https://docs.z.ai/devpack/tool/codex",
            ),
            ("deepseek", "expiresAt", "2026-08-25T00:00:01Z"),
            ("zai", "plan", missing),
        )
        for provider, path, replacement in cases:
            candidate = copy.deepcopy(deepseek if provider == "deepseek" else zai)
            keys = path.split(".")
            target = candidate
            for key in keys[:-1]:
                target = target[key]
            if replacement is missing:
                target.pop(keys[-1])
            else:
                target[keys[-1]] = replacement
            with (
                self.subTest(provider=provider, path=path),
                self.assertRaises(ValueError),
            ):
                provider_control_window(candidate, provider)

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
        self.assertEqual(
            dict(CODEX_PROVIDER_RETRY_POLICY),
            {
                "request_max_retries": 0,
                "stream_max_retries": 0,
                "unbounded_connection_retries": False,
            },
        )
        with self.assertRaises(TypeError):
            CODEX_PROVIDER_RETRY_POLICY["request_max_retries"] = 1  # type: ignore[index]

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
        self.assertFalse(
            same_json({"request_max_retries": 0}, {"request_max_retries": False})
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

    def test_live_route_probe_relay_command_is_exact_and_non_mutating(self) -> None:
        command = list(_PILOT_RELAY_COMMAND)

        result = live_route_probe_relay_command(command)

        self.assertEqual(command, _PILOT_RELAY_COMMAND)
        self.assertIsNot(result, command)
        self.assertEqual(
            result,
            [
                "open-agent-lab",
                "relay",
                "--provider",
                "deepseek",
                "--ttl-seconds",
                "600",
                "--max-requests",
                "2",
                "--max-request-bytes",
                str(512 * 1024),
                "--max-response-bytes",
                str(512 * 1024),
                "--connect-timeout-ms",
                "30000",
                "--idle-timeout-ms",
                "180000",
                "--build-id-file",
                "/app/relay-build-id",
            ],
        )
        self.assertEqual(
            dict(LIVE_ROUTE_PROBE_LIMITS),
            {
                "ttlSeconds": 600,
                "maxRequests": 2,
                "maxRequestBytes": 512 * 1024,
                "maxResponseBytes": 512 * 1024,
                "connectTimeoutMs": 30_000,
                "idleTimeoutMs": 180_000,
                "codexTimeoutSeconds": 480,
            },
        )

    def test_live_route_probe_networks_isolates_main_from_egress(self) -> None:
        compose = {
            "services": {"main": {}, "open-agent-lab-relay": {"image": "relay"}},
            "secrets": {"provider-api-key": {"file": "/key"}},
        }

        result = live_route_probe_networks(compose)

        self.assertEqual(compose["services"]["main"], {})
        self.assertEqual(
            result["services"]["main"]["networks"],
            {LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {}},
        )
        self.assertEqual(
            result["services"]["open-agent-lab-relay"]["networks"],
            {
                LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {
                    "aliases": ["open-agent-lab-relay"]
                },
                LIVE_ROUTE_PROBE_EGRESS_NETWORK: {},
            },
        )
        self.assertEqual(
            result["networks"],
            {
                LIVE_ROUTE_PROBE_INTERNAL_NETWORK: {"internal": True},
                LIVE_ROUTE_PROBE_EGRESS_NETWORK: {"internal": False},
            },
        )

    def test_live_route_probe_networks_rejects_existing_network_policy(self) -> None:
        for compose in (
            None,
            {"services": {}},
            {
                "services": {
                    "main": {"network_mode": "host"},
                    "open-agent-lab-relay": {},
                }
            },
            {
                "services": {"main": {}, "open-agent-lab-relay": {}},
                "networks": {"default": {}},
            },
        ):
            with (
                self.subTest(compose=compose),
                self.assertRaises((TypeError, ValueError)),
            ):
                live_route_probe_networks(compose)

    def test_live_route_probe_relay_command_rejects_invalid_shapes(self) -> None:
        for command in (None, "relay", tuple(_PILOT_RELAY_COMMAND), ["relay", 1]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                live_route_probe_relay_command(command)

    def test_live_route_probe_relay_command_rejects_missing_policy_flags(self) -> None:
        for flag in ("--ttl-seconds", "--max-requests", "--build-id-file"):
            command = list(_PILOT_RELAY_COMMAND)
            index = command.index(flag)
            del command[index : index + 2]
            original = list(command)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                live_route_probe_relay_command(command)
            self.assertEqual(command, original)

    def test_live_route_probe_relay_command_rejects_duplicate_policy_flags(
        self,
    ) -> None:
        for flag in ("--ttl-seconds", "--max-requests", "--build-id-file"):
            command = list(_PILOT_RELAY_COMMAND)
            index = command.index(flag)
            command[index:index] = command[index : index + 2]
            original = list(command)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                live_route_probe_relay_command(command)
            self.assertEqual(command, original)


if __name__ == "__main__":
    unittest.main()
