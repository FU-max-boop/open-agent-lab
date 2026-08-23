"""Publication-policy tests for retained relay evidence."""

import unittest

from benchmarks.terminal_bench import paired_results
from benchmarks.terminal_bench.relay_evidence import _publication_reasons

_BUILD_ID = f"sha256:{'0' * 64}"
_MODEL = "deepseek-chat"


class PublicationReasonsTest(unittest.TestCase):
    def _reasons(self, rejected_requests: dict[str, int]) -> set[str]:
        return _publication_reasons(
            [], rejected_requests, "deepseek", _BUILD_ID, _MODEL
        )

    def test_requested_model_rejection_gets_an_unwaivable_reason(self) -> None:
        reasons = self._reasons({"model_mismatch": 1})

        self.assertEqual(
            reasons,
            {
                "no_completed_response",
                "relay_rejected_requests",
                "requested_model_mismatch",
            },
        )
        with self.assertRaisesRegex(
            paired_results.IntegrityError, "relay publication gate failed"
        ):
            paired_results._relay_gate_is_complete(
                {"ok": False, "reasons": sorted(reasons)}, allow_incomplete=True
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
