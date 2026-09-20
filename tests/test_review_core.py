"""Unit tests for review_core.py — the provider/transport-agnostic scoring,
parsing, and rendering logic shared by the GitHub Actions script and the
GitHub App. Extracted from carlos_review.py so a rubric or rendering change
only needs to happen in one place; these tests protect that extraction from
silently drifting behavior.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".github", "scripts"))

import review_core  # noqa: E402


def _base_review(**overrides):
    review = {
        "summary": "does a thing",
        "issues": [],
        "edge_cases": [],
        "policy_violations": [],
        "potential_bugs": [],
        "tests": {"estimated_changed_line_coverage_pct": 100},
        "scores": {"correctness": 40, "tests": 25, "security": 20, "style": 15},
        "confidence": "high",
    }
    review.update(overrides)
    return review


class ApplyPolicyTests(unittest.TestCase):
    def test_perfect_score_auto_merges_with_zero_required_approvals(self):
        policy = review_core.apply_policy(_base_review(), ["src/foo.py"], [])
        self.assertEqual(policy["score"], 100)
        self.assertEqual(policy["required"], 0)
        self.assertTrue(policy["auto_merge"])

    def test_blocker_issue_caps_score_at_49(self):
        review = _base_review(issues=[{"severity": "blocker", "file": "a.py", "title": "t", "detail": "d"}])
        policy = review_core.apply_policy(review, ["a.py"], [])
        self.assertLessEqual(policy["score"], 49)
        self.assertEqual(policy["required"], 2)

    def test_must_not_violation_caps_at_49(self):
        review = _base_review(policy_violations=[{"rule_id": "WB-SEC-01", "level": "MUST NOT",
                                                    "file": "a.py", "evidence": "e", "fix": "f"}])
        policy = review_core.apply_policy(review, ["a.py"], [])
        self.assertLessEqual(policy["score"], 49)

    def test_must_violation_caps_at_79_when_no_must_not(self):
        review = _base_review(policy_violations=[{"rule_id": "WB-REL-03", "level": "MUST",
                                                    "file": "a.py", "evidence": "e", "fix": "f"}])
        policy = review_core.apply_policy(review, ["a.py"], [])
        self.assertLessEqual(policy["score"], 79)
        self.assertEqual(policy["required"], 1)

    def test_low_coverage_subtracts_fifteen_points(self):
        review = _base_review(tests={"estimated_changed_line_coverage_pct": 10})
        policy = review_core.apply_policy(review, ["a.py"], [])
        self.assertEqual(policy["score"], 85)  # 100 - 15

    def test_protected_path_blocks_auto_merge_even_at_perfect_score(self):
        policy = review_core.apply_policy(_base_review(), ["auth/login.py"], ["auth/"])
        self.assertEqual(policy["score"], 100)
        self.assertFalse(policy["auto_merge"])
        self.assertEqual(policy["required"], 1)
        self.assertTrue(any("protected" in g for g in policy["guard"]))

    def test_low_confidence_blocks_auto_merge(self):
        policy = review_core.apply_policy(_base_review(confidence="low"), ["a.py"], [])
        self.assertFalse(policy["auto_merge"])
        self.assertEqual(policy["required"], 1)

    def test_score_between_50_and_94_requires_one_approval(self):
        review = _base_review(scores={"correctness": 30, "tests": 15, "security": 10, "style": 5})
        policy = review_core.apply_policy(review, ["a.py"], [])
        self.assertEqual(policy["score"], 60)
        self.assertEqual(policy["required"], 1)

    def test_score_below_50_requires_two_approvals(self):
        review = _base_review(scores={"correctness": 10, "tests": 5, "security": 5, "style": 0})
        policy = review_core.apply_policy(review, ["a.py"], [])
        self.assertEqual(policy["score"], 20)
        self.assertEqual(policy["required"], 2)


class NormalizeReviewTests(unittest.TestCase):
    def test_fills_in_missing_optional_fields(self):
        review = review_core.normalize_review({"scores": {}})
        self.assertEqual(review["issues"], [])
        self.assertEqual(review["scores"], {"correctness": 0, "tests": 0, "security": 0, "style": 0})
        self.assertEqual(review["confidence"], "medium")

    def test_severity_aliases_map_to_canonical_values(self):
        review = review_core.normalize_review(
            {"issues": [{"severity": "critical"}, {"severity": "warning"}, {"severity": "bogus"}]}
        )
        self.assertEqual([i["severity"] for i in review["issues"]], ["blocker", "minor", "minor"])


class ParseReviewTests(unittest.TestCase):
    def test_strips_markdown_fences(self):
        text = '```json\n{"summary": "ok"}\n```'
        self.assertEqual(review_core.parse_review(text), {"summary": "ok"})

    def test_extracts_json_from_surrounding_prose(self):
        text = 'Sure, here is the review:\n{"summary": "ok"}\nHope that helps!'
        self.assertEqual(review_core.parse_review(text), {"summary": "ok"})


class RenderTests(unittest.TestCase):
    def test_render_includes_provider_and_bot_name_in_footer(self):
        review = review_core.normalize_review(_base_review())
        review["_whitebook_source"] = ""
        policy = review_core.apply_policy(review, [], [])
        body = review_core.render(review, policy, approvals=0, bot_name="carlos", provider_name="claude")
        self.assertIn(review_core.MARKER, body)
        self.assertIn("powered by claude", body)
        self.assertIn("carlos review", body)


if __name__ == "__main__":
    unittest.main()
