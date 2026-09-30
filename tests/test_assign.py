"""Tests for the slot assignment solver.

The solver claims to be exact, so most of these tests check it against brute force over
every possible assignment. That oracle is the only honest way to test an optimizer: a
hand-written expected answer just restates what the author believed.
"""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.model.assign import assign_max_weight, assign_slots  # noqa: E402

NEG = float("-inf")


def _value(weights: list[list[float]], assignment: list[int]) -> float:
    return sum(
        weights[row][col] for row, col in enumerate(assignment) if col >= 0
    )


def _brute_force_best(weights: list[list[float]]) -> float:
    """The best achievable total, found by trying every assignment."""
    rows = len(weights)
    cols = len(weights[0]) if rows else 0
    best = 0.0
    for size in range(min(rows, cols) + 1):
        for chosen_rows in itertools.combinations(range(rows), size):
            for chosen_cols in itertools.permutations(range(cols), size):
                total = 0.0
                allowed = True
                for row, col in zip(chosen_rows, chosen_cols):
                    if weights[row][col] == NEG:
                        allowed = False
                        break
                    total += weights[row][col]
                if allowed:
                    best = max(best, total)
    return best


def _assert_optimal(weights):
    assignment = assign_max_weight(weights)
    assert len(assignment) == len(weights)

    used = [col for col in assignment if col >= 0]
    assert len(used) == len(set(used)), "a column was assigned twice"
    for row, col in enumerate(assignment):
        if col >= 0:
            assert weights[row][col] != NEG, "an ineligible pairing was assigned"

    assert _value(weights, assignment) == pytest.approx(_brute_force_best(weights))
    return assignment


# --- the counterexample that prompted this module ------------------------------


def test_the_greedy_counterexample_is_solved_optimally():
    """FLEX takes RB/WR/TE, WRRB takes RB/WR. An RB worth 20, a TE worth 19.

    Greedy gives FLEX the back and leaves WRRB empty for 20 points. Assigning both at
    once starts the tight end at FLEX and the back at WRRB for 39.
    """
    slots = ["FLEX", "WRRB"]
    values = [20.0, 19.0]
    accepts = [[True, True], [True, False]]

    assignment = assign_slots(slots, values, accepts)
    assert assignment == [1, 0], "the greedy answer left a slot empty"
    assert -1 not in assignment


def test_a_slot_is_filled_even_when_it_costs_points():
    """An empty slot scores zero, so filling it always wins."""
    slots = ["RB", "FLEX"]
    values = [30.0, 0.1]
    accepts = [[True, True], [True, True]]
    assert sorted(assign_slots(slots, values, accepts)) == [0, 1]


# --- exact optimality ----------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_matches_brute_force_on_random_small_problems(seed):
    rng = random.Random(seed)
    rows = rng.randint(1, 4)
    cols = rng.randint(1, 5)
    weights = [
        [
            NEG if rng.random() < 0.3 else round(rng.uniform(0.0, 30.0), 2)
            for _ in range(cols)
        ]
        for _ in range(rows)
    ]
    _assert_optimal(weights)


@pytest.mark.parametrize("seed", range(15))
def test_matches_brute_force_when_most_pairings_are_forbidden(seed):
    """Sparse eligibility is the case greedy gets wrong, so test it hardest."""
    rng = random.Random(1000 + seed)
    rows = rng.randint(2, 4)
    cols = rng.randint(2, 4)
    weights = [
        [
            round(rng.uniform(1.0, 20.0), 2) if rng.random() < 0.35 else NEG
            for _ in range(cols)
        ]
        for _ in range(rows)
    ]
    _assert_optimal(weights)


def test_more_slots_than_players_leaves_slots_unassigned():
    weights = [[5.0], [3.0], [1.0]]
    assignment = _assert_optimal(weights)
    assert assignment.count(-1) == 2
    assert assignment[0] == 0, "the best pairing must be the one kept"


def test_more_players_than_slots_uses_the_best():
    weights = [[1.0, 50.0, 3.0]]
    assert _assert_optimal(weights) == [1]


def test_a_row_with_no_eligible_column_is_unassigned():
    weights = [[NEG, NEG], [4.0, 7.0]]
    assignment = _assert_optimal(weights)
    assert assignment[0] == -1
    assert assignment[1] == 1


def test_all_pairings_forbidden_assigns_nothing():
    assert assign_max_weight([[NEG, NEG], [NEG, NEG]]) == [-1, -1]


def test_empty_inputs_are_handled():
    assert assign_max_weight([]) == []
    assert assign_max_weight([[]]) == [-1]


def test_a_negative_pairing_is_declined_because_maximizing_means_declining_it():
    """Taking a negative weight lowers the total, so the maximum is to take nothing.

    Lineup building never reaches this case: every real weight is FILL_BONUS plus
    points, which is hugely positive, so a slot is always worth filling. The behaviour
    is pinned here because `assign_max_weight` is a general solver and this is what
    maximizing actually means.
    """
    assert _assert_optimal([[-5.0, -1.0]]) == [-1]


# --- tie breaking --------------------------------------------------------------


def test_a_tie_goes_to_the_earlier_player():
    """Callers sort best first, so ties must resolve that way and stay stable."""
    slots = ["FLEX"]
    values = [10.0, 10.0, 10.0]
    accepts = [[True, True, True]]
    assert assign_slots(slots, values, accepts) == [0]


def test_tie_breaking_cannot_outweigh_a_real_difference():
    slots = ["FLEX"]
    values = [10.0, 10.5]
    accepts = [[True, True]]
    assert assign_slots(slots, values, accepts) == [1]


def test_the_result_is_deterministic_across_runs():
    slots = ["FLEX", "WRRB", "RB"]
    values = [12.0, 12.0, 12.0, 12.0]
    accepts = [[True] * 4, [True, True, False, False], [True, False, True, False]]
    first = assign_slots(slots, values, accepts)
    for _ in range(5):
        assert assign_slots(slots, values, accepts) == first


# --- scale ---------------------------------------------------------------------


def test_a_full_sized_lineup_respects_eligibility_and_fills_every_slot():
    """A real problem is a dozen slots against two dozen players.

    Checking only the length and for duplicates let a scale-only defect through, because
    an assignment of forbidden pairings satisfies both. Eligibility is asserted here, and
    so is the known optimum: with far more eligible players than slots, every slot fills.
    """
    rng = random.Random(7)
    slots = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]
    values = [round(rng.uniform(0.0, 40.0), 2) for _ in range(24)]
    accepts = [[rng.random() < 0.5 for _ in values] for _ in slots]

    assignment = assign_slots(slots, values, accepts)

    assert len(assignment) == len(slots)
    used = [c for c in assignment if c >= 0]
    assert len(used) == len(set(used)), "a player filled two slots"
    for slot, player in enumerate(assignment):
        if player >= 0:
            assert accepts[slot][player], f"slot {slot} took an ineligible player"
    assert -1 not in assignment, "a slot was left empty despite eligible players"


def test_a_full_sized_lineup_maximizes_points_not_just_fill():
    """Every slot takes a distinct position, so the best eligible player must win each."""
    slots = ["QB", "RB", "WR", "TE"]
    positions = ["QB", "RB", "WR", "TE"]
    values = []
    accepts = [[] for _ in slots]
    # Three players per position, the third of each being the best.
    for position in positions:
        for points in (1.0, 5.0, 20.0):
            values.append(points)
            for slot_index, slot in enumerate(slots):
                accepts[slot_index].append(slot == position)

    assignment = assign_slots(slots, values, accepts)

    # The total alone is not enough: an assignment reusing one player can reach it.
    used = [player for player in assignment if player >= 0]
    assert len(used) == len(set(used)), "a player filled two slots"
    for slot, player in enumerate(assignment):
        assert player >= 0, f"slot {slots[slot]} was left empty"
        assert accepts[slot][player], f"slot {slots[slot]} took an ineligible player"
    total = sum(values[player] for player in used)
    assert total == pytest.approx(80.0), "it did not take the best player per slot"
