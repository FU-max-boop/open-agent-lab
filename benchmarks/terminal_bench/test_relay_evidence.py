"""Publication-policy tests for retained relay evidence."""

import unittest

from benchmarks.terminal_bench import paired_results
from benchmarks.terminal_bench.relay_evidence import (
    _REJECTION_CODES,
    _publication_reasons,
)

_BUILD_ID = f"sha256:{'0' * 64}"
_MODEL = "deepseek-chat"


class PublicationReasonsTest(unittest.TestCase):
    def _reasons(self, rejected_requests: dict[str, int]) -> set[str]:
        return _publication_reasons(
            [], rejected_requests, "deepseek", _BUILD_ID, _MODEL
        )

    def test_security_rejections_get_unwaivable_reasons(self) -> None:
        cases = (
            ({"model_mismatch": 1}, "requested_model_mismatch"),
            ({"upstream_secret_echo": 1}, "upstream_secret_echo"),
            ({"invalid_turn_state": 1}, "invalid_turn_state"),
        )
        self.assertIn("invalid_turn_state", _REJECTION_CODES)
        for rejected_requests, unwaivable_reason in cases:
            with self.subTest(rejected_requests=rejected_requests):
                reasons = self._reasons(rejected_requests)
                self.assertEqual(
                    reasons,
                    {
                        "no_completed_response",
                        "relay_rejected_requests",
                        unwaivable_reason,
                    },
                )
                with self.assertRaisesRegex(
                    paired_results.IntegrityError, "relay publication gate failed"
                ):
                    paired_results._relay_gate_is_complete(
                        {"ok": False, "reasons": sorted(reasons)},
                        allow_incomplete=True,
                    )

    def test_other_rejections_keep_the_existing_publication_semantics(self) -> None:
        cases = (
            ({}, {"no_completed_response"}),
            (
                {"client_disconnected_after_close": 1},
                {"no_completed_response"},
            ),
            (
                {"invalid_json": 1},
                {"no_completed_response", "relay_rejected_requests"},
            ),
        )
        for rejected_requests, expected in cases:
            with self.subTest(rejected_requests=rejected_requests):
                self.assertEqual(self._reasons(rejected_requests), expected)


if __name__ == "__main__":
    unittest.main()
