"""Risk control on multiple dimensions: wanted score plus three risk layers.

Reference implementation of the policy lab (index.html). Every candidate has
one wanted score and three risk scores (fraud, credit, compliance). A policy
is a weight and a hard cap per risk layer, and the pick runs in three stages:

1. Wanted stage: keep candidates whose wanted score is at or above the
   high-group mean (the same threshold as the original two-layer demo).
2. Cap stage: block anyone over the cap on any risk layer.
3. Distance stage: fetch the survivor with the smallest
   (max wanted - wanted)^2 + sum(weight_k * risk_k^2). Ties are broken by a
   seeded draw.

The data generator and the drift between rounds use the same random streams
as the web page, so the same arguments give the same numbers on both.

Examples
--------
Run the default pool with the balanced policy::

    python risk_lab.py

Reproduce a state of the web page and ask why candidate 3 was not fetched::

    python risk_lab.py --num-elements 36 --seed 112 --policy averse --why 3

Use your own weights and caps (a cap of 50 means no cap)::

    python risk_lab.py --weights 1,0.5,2 --caps 50,40,15 --volatility 2 --round 6
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass, field
from typing import Sequence

SCORE_MAX = 50
CAP_OFF = 50  # a cap of 50 can never be exceeded, so it means "no cap"
ROUND_COUNT = 12
LAYERS = ("fraud", "credit", "compliance")
TAG_LAYER = 0x6C617972  # derived streams for the extra layers
TAG_DRIFT = 0x64726966  # derived streams for round-to-round drift

PRESETS = {
    "original": ((1.0, 0.0, 0.0), (50, 50, 50)),
    "balanced": ((1.0, 1.0, 0.5), (50, 50, 30)),
    "averse": ((1.5, 1.5, 2.0), (35, 35, 12)),
    "growth": ((0.2, 0.15, 0.05), (50, 50, 40)),
}

# Status codes, as in the web page.
BELOW, BLOCKED, SURVIVOR, FETCHED = 0, 1, 2, 3
STATUS_NAMES = {BELOW: "below wanted", BLOCKED: "blocked", SURVIVOR: "survivor", FETCHED: "fetched"}


@dataclass(frozen=True)
class Policy:
    weights: tuple[float, float, float]
    caps: tuple[int, int, int]

    @classmethod
    def preset(cls, name: str) -> "Policy":
        weights, caps = PRESETS[name]
        return cls(tuple(weights), tuple(caps))


@dataclass
class Pool:
    wanted: list[int]
    risks: list[list[int]]  # [fraud, credit, compliance]


@dataclass
class World:
    n: int
    seed: int
    base: Pool
    tie_state: tuple
    noise: list[list[list[float]]] = field(repr=False)


@dataclass
class Decision:
    threshold: float
    max_wanted: int
    considered: list[int]
    survivors: list[int]
    finalists: list[int]
    fetch: int | None
    best: float | None
    status: list[int]
    distance: dict[int, float]
    terms: dict[int, tuple[float, float, float, float]]
    violations: dict[int, list[int]]
    ranking: list[int]


def js_round(x: float) -> int:
    """JavaScript Math.round: halves round up (Python's round() rounds to even)."""

    r = math.floor(x)
    return int(r) + 1 if x - r >= 0.5 else int(r)


def clamp_score(value: int) -> int:
    return min(SCORE_MAX, max(0, value))


def derived_stream(seed: int, tag: int, index: int) -> random.Random:
    """Independent stream keyed by (seed, tag, index).

    random.Random(int) seeds MT19937 from the 32-bit words of the integer, so
    this is the stream the web page builds from the key [lo, hi, tag, index].
    """

    s = abs(int(seed))
    lo, hi = s % 2**32, s // 2**32
    return random.Random(lo + (hi << 32) + (tag << 64) + (index << 96))


def gaussian(rng: random.Random) -> float:
    u1 = 1 - rng.random()
    u2 = rng.random()
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def wanted_threshold(values: Sequence[int]) -> float:
    """High-group threshold of the original demo (multi-dimensional-choices.py)."""

    ordered = sorted(values)
    if len(ordered) < 4 or len(set(ordered)) == 1:
        return float(min(ordered))
    minimum, maximum = ordered[0], ordered[-1]
    middle = ordered[1:-1]
    if len(middle) < 2:
        return float(minimum)

    def average_squared_distance(group: Sequence[int], center: int) -> float:
        return sum((value - center) ** 2 for value in group) / len(group)

    objective = {
        split: average_squared_distance(middle[: split + 1], minimum)
        + average_squared_distance(middle[split + 1 :], maximum)
        for split in range(len(middle) - 1)
    }
    best_split = min(objective, key=objective.get)
    higher = middle[best_split + 1 :]
    return sum(higher) / len(higher)


def generate_world(num_elements: int, seed: int) -> World:
    """Base pool plus the drift noise for every later round.

    wanted and fraud come from random.Random(seed) exactly as in the original
    two-layer demo. Credit risk leans against fraud risk (so the layers trade
    off and the weights matter) and a little with wanted; compliance is mostly
    clean with a heavy tail of serious flags.
    """

    if num_elements < 1:
        raise ValueError("num_elements must be at least 1")
    rng = random.Random(seed)
    wanted = [rng.randint(0, SCORE_MAX) for _ in range(num_elements)]
    fraud = [rng.randint(0, SCORE_MAX) for _ in range(num_elements)]
    tie_state = rng.getstate()

    credit_stream = derived_stream(seed, TAG_LAYER, 1)
    credit = [
        clamp_score(js_round(0.25 * w + 0.65 * (SCORE_MAX - f) + 18 * credit_stream.random() - 8))
        for w, f in zip(wanted, fraud)
    ]
    compliance_stream = derived_stream(seed, TAG_LAYER, 2)
    compliance = []
    for _ in wanted:
        r = compliance_stream.random()
        compliance.append(clamp_score(js_round(SCORE_MAX * (r * r * r))))

    noise = []
    for t in range(1, ROUND_COUNT):
        drift = derived_stream(seed, TAG_DRIFT, t)
        noise.append([[gaussian(drift) for _ in range(num_elements)] for _ in range(4)])
    return World(num_elements, seed, Pool(wanted, [fraud, credit, compliance]), tie_state, noise)


def build_rounds(world: World, volatility: float) -> list[Pool]:
    """Round 1 is the base pool; each later round is a bounded random walk."""

    rounds = [world.base]
    for t in range(1, ROUND_COUNT):
        prev = rounds[-1]
        z = world.noise[t - 1]

        def step(series: list[int], s: int) -> list[int]:
            return [clamp_score(js_round(v + volatility * z[s][i])) for i, v in enumerate(series)]

        rounds.append(Pool(step(prev.wanted, 0), [step(series, k + 1) for k, series in enumerate(prev.risks)]))
    return rounds


def decide(pool: Pool, policy: Policy, tie_state: tuple) -> Decision:
    wanted, risks = pool.wanted, pool.risks
    threshold = wanted_threshold(wanted)
    max_wanted = max(wanted)
    considered = [i for i, w in enumerate(wanted) if w >= threshold]
    if not considered:
        considered = [i for i, w in enumerate(wanted) if w == max_wanted]

    status = [BELOW] * len(wanted)
    distance: dict[int, float] = {}
    terms: dict[int, tuple[float, float, float, float]] = {}
    violations: dict[int, list[int]] = {}
    survivors = []
    for i in considered:
        gap2 = (max_wanted - wanted[i]) ** 2
        layer_terms = [policy.weights[k] * risks[k][i] ** 2 for k in range(3)]
        d = gap2
        for term in layer_terms:  # same summation order as the web page
            d += term
        distance[i] = d
        terms[i] = (gap2, *layer_terms)
        over = [k for k in range(3) if policy.caps[k] < CAP_OFF and risks[k][i] > policy.caps[k]]
        if over:
            violations[i] = over
            status[i] = BLOCKED
        else:
            status[i] = SURVIVOR
            survivors.append(i)

    best = min((distance[i] for i in survivors), default=None)
    finalists = [i for i in survivors if distance[i] == best]
    fetch = None
    if finalists:
        tie_rng = random.Random()
        tie_rng.setstate(tie_state)
        fetch = tie_rng.choice(finalists)
        status[fetch] = FETCHED
    ranking = sorted(survivors, key=lambda i: (distance[i], i != fetch, i))
    return Decision(threshold, max_wanted, considered, survivors, finalists, fetch, best, status,
                    distance, terms, violations, ranking)


def explain(decision: Decision, pool: Pool, policy: Policy, candidate: int) -> str:
    """Plain-language reason a candidate was or was not fetched."""

    st = decision.status[candidate]
    if st == FETCHED:
        others = [i for i in decision.finalists if i != candidate]
        if others:
            return f"#{candidate} was fetched: tied with {others} at distance {decision.best:g}; the seeded draw picked it."
        return f"#{candidate} was fetched: closest survivor to the ideal point (distance {decision.best:g})."
    if st == BELOW:
        return (f"#{candidate} stopped at the wanted stage: wanted {pool.wanted[candidate]} "
                f"is below the threshold {decision.threshold:.2f}.")
    if st == BLOCKED:
        items = "; ".join(
            f"{LAYERS[k]} {pool.risks[k][candidate]} is over the cap {policy.caps[k]}"
            for k in decision.violations[candidate]
        )
        d = decision.distance[candidate]
        if decision.best is None or d < decision.best:
            tail = "" if decision.best is None else f" Without the cap it would have been fetched ({d:g} against {decision.best:g})."
        else:
            rank = sum(decision.distance[i] < d for i in decision.survivors) + 1
            tail = f" Without the cap it would rank {rank} of {len(decision.survivors) + 1}."
        return f"#{candidate} was blocked at the cap stage: {items}.{tail}"
    rank = decision.ranking.index(candidate) + 1
    return (f"#{candidate} passed both gates but sits farther from the ideal point: "
            f"{decision.distance[candidate]:g} against {decision.best:g}, rank {rank} of {len(decision.survivors)}.")


def run(num_elements: int, seed: int, policy: Policy, volatility: float) -> tuple[World, list[Pool], list[Decision]]:
    world = generate_world(num_elements, seed)
    rounds = build_rounds(world, volatility)
    return world, rounds, [decide(pool, policy, world.tie_state) for pool in rounds]


def _parse_floats(text: str) -> tuple[float, float, float]:
    values = tuple(float(v) for v in text.split(","))
    if len(values) != 3:
        raise argparse.ArgumentTypeError("expected three comma-separated values")
    return values


def _parse_caps(text: str) -> tuple[int, int, int]:
    values = tuple(int(v) for v in text.split(","))
    if len(values) != 3 or not all(0 <= v <= CAP_OFF for v in values):
        raise argparse.ArgumentTypeError("expected three integers between 0 and 50")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Wanted score plus three risk layers, with weights and caps.")
    parser.add_argument("--num-elements", type=int, default=36, help="number of candidates (default: 36)")
    parser.add_argument("--seed", type=int, default=112, help="random seed (default: 112)")
    parser.add_argument("--policy", choices=sorted(PRESETS), default="balanced", help="preset policy")
    parser.add_argument("--weights", type=_parse_floats, help="custom weights, e.g. 1,1,0.5 (overrides --policy)")
    parser.add_argument("--caps", type=_parse_caps, help="custom caps, e.g. 50,50,30; 50 means no cap")
    parser.add_argument("--volatility", type=float, default=4, help="drift per round (default: 4)")
    parser.add_argument("--round", type=int, default=1, help=f"round to report, 1 to {ROUND_COUNT} (default: 1)")
    parser.add_argument("--why", type=int, help="explain why this candidate was or was not fetched")
    args = parser.parse_args()

    if not 1 <= args.round <= ROUND_COUNT:
        parser.error(f"--round must be between 1 and {ROUND_COUNT}")
    weights, caps = PRESETS[args.policy]
    policy = Policy(tuple(args.weights or weights), tuple(args.caps or caps))
    world, rounds, decisions = run(args.num_elements, args.seed, policy, args.volatility)
    pool, decision = rounds[args.round - 1], decisions[args.round - 1]

    print(f"candidates: {args.num_elements}  seed: {args.seed}  round: {args.round} of {ROUND_COUNT}")
    print(f"weights (fraud, credit, compliance): {policy.weights}  caps: {policy.caps}")
    print(f"wanted threshold: {decision.threshold:.2f}  passed: {len(decision.considered)}  "
          f"blocked: {len(decision.violations)}  survivors: {len(decision.survivors)}")
    if decision.fetch is None:
        print("fetch: nobody survives the caps")
    else:
        f = decision.fetch
        print(f"fetch: #{f}  wanted {pool.wanted[f]}  "
              + "  ".join(f"{LAYERS[k]} {pool.risks[k][f]}" for k in range(3))
              + f"  distance {decision.best:g}")
    strip = " ".join("-" if d.fetch is None else f"#{d.fetch}" for d in decisions)
    print(f"fetch by round: {strip}")
    if args.why is not None:
        if not 0 <= args.why < args.num_elements:
            parser.error("--why must be a candidate id")
        print(explain(decision, pool, policy, args.why))


if __name__ == "__main__":
    main()
