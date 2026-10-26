"""Checks for risk_lab.py.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import risk_lab  # noqa: E402

REFERENCE = json.loads((ROOT / "tests" / "web_reference.json").read_text(encoding="utf-8"))


def checksum(values):
    acc = 17
    for i, v in enumerate(values):
        acc = (acc * 31 + v * (i + 7)) % 1000000007
    return acc


def original_choose_candidate(wanted, unwanted, rng):
    """The two-layer rule of multi-dimensional-choices.py, kept here for comparison."""

    threshold = risk_lab.wanted_threshold(list(wanted.values()))
    considered = sorted(c for c, w in wanted.items() if w >= threshold)
    if not considered:
        top = max(wanted.values())
        considered = [c for c, w in wanted.items() if w == top]
    top = max(wanted.values())
    distances = {c: (top - wanted[c]) ** 2 + unwanted[c] ** 2 for c in considered}
    best = min(distances.values())
    finalists = sorted(c for c, d in distances.items() if d == best)
    return rng.choice(finalists), considered, threshold, finalists


class MatchesWebPage(unittest.TestCase):
    """Same inputs give the same numbers as the mechanism inside index.html."""

    def test_reference_cases(self):
        policies = {
            name: risk_lab.Policy(tuple(p["weights"]), tuple(p["caps"]))
            for name, p in REFERENCE["policies"].items()
        }
        for case in REFERENCE["cases"]:
            world = risk_lab.generate_world(case["n"], case["seed"])
            with self.subTest(n=case["n"], seed=case["seed"], part="layers"):
                if "credit" in case:
                    self.assertEqual(world.base.risks[1], case["credit"])
                    self.assertEqual(world.base.risks[2], case["compliance"])
                self.assertEqual(checksum(world.base.risks[1]), case["credit_checksum"])
                self.assertEqual(checksum(world.base.risks[2]), case["compliance_checksum"])
            for run in case["runs"]:
                rounds = risk_lab.build_rounds(world, run["volatility"])
                with self.subTest(n=case["n"], seed=case["seed"], volatility=run["volatility"], part="drift"):
                    got = [[checksum(r.wanted)] + [checksum(series) for series in r.risks] for r in rounds]
                    self.assertEqual(got, run["round_checksums"])
                for name, expected_rounds in run["policies"].items():
                    for r, (pool, expected) in enumerate(zip(rounds, expected_rounds)):
                        d = risk_lab.decide(pool, policies[name], world.tie_state)
                        with self.subTest(n=case["n"], seed=case["seed"], volatility=run["volatility"], policy=name, round=r + 1):
                            self.assertEqual([d.fetch, d.threshold, len(d.survivors), d.finalists, d.best], expected)


class MatchesOriginalDemo(unittest.TestCase):
    """With the original rule, round 1 is exactly the two-layer demo."""

    def test_original_rule(self):
        policy = risk_lab.Policy.preset("original")
        for n in (1, 2, 3, 4, 5, 7, 12, 36, 100):
            for seed in range(60):
                rng = random.Random(seed)
                wanted = {c: rng.randint(0, 50) for c in range(n)}
                unwanted = {c: rng.randint(0, 50) for c in range(n)}
                fetch, considered, threshold, finalists = original_choose_candidate(wanted, unwanted, rng)
                world = risk_lab.generate_world(n, seed)
                d = risk_lab.decide(world.base, policy, world.tie_state)
                with self.subTest(n=n, seed=seed):
                    self.assertEqual(world.base.wanted, [wanted[c] for c in range(n)])
                    self.assertEqual(world.base.risks[0], [unwanted[c] for c in range(n)])
                    self.assertEqual((d.fetch, d.considered, d.threshold, d.finalists), (fetch, considered, threshold, finalists))


class Mechanism(unittest.TestCase):
    def test_js_round(self):
        self.assertEqual([risk_lab.js_round(v) for v in (2.5, 3.5, -2.5, -2.6, 0.49, -0.5)], [3, 4, -2, -3, 0, 0])

    def test_caps_block_and_nobody_survives(self):
        world = risk_lab.generate_world(36, 112)
        d = risk_lab.decide(world.base, risk_lab.Policy((1.0, 1.0, 1.0), (5, 5, 5)), world.tie_state)
        self.assertIsNone(d.fetch)
        self.assertEqual(d.survivors, [])
        self.assertTrue(all(i in d.violations for i in d.considered))

    def test_fetch_is_the_closest_survivor(self):
        world = risk_lab.generate_world(100, 42)
        for name in risk_lab.PRESETS:
            d = risk_lab.decide(world.base, risk_lab.Policy.preset(name), world.tie_state)
            with self.subTest(policy=name):
                self.assertIn(d.fetch, d.survivors)
                self.assertTrue(all(d.distance[d.fetch] <= d.distance[i] for i in d.survivors))
                self.assertTrue(all(d.status[i] == risk_lab.BLOCKED for i in d.violations))

    def test_explain_mentions_the_cap(self):
        world = risk_lab.generate_world(36, 112)
        policy = risk_lab.Policy.preset("averse")
        d = risk_lab.decide(world.base, policy, world.tie_state)
        blocked = next(iter(d.violations))
        self.assertIn("over the cap", risk_lab.explain(d, world.base, policy, blocked))


if __name__ == "__main__":
    unittest.main()
