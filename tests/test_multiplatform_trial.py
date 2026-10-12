import unittest

from multiplatform_trial import eligible_candidates


class MultiPlatformTrialTests(unittest.TestCase):
    def test_minimum_score_never_uses_low_relevance_to_fill_quota(self):
        rows = [
            {"id": "high", "combined_score": 0.81},
            {"id": "edge", "combined_score": 0.50},
            {"id": "low", "combined_score": 0.49},
        ]
        self.assertEqual([row["id"] for row in eligible_candidates(rows, 0.50)], ["high", "edge"])
