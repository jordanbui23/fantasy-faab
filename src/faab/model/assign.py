"""Optimal slot assignment.

Filling a lineup greedily is wrong, and the review that found it gave the counterexample.
Give a league a `WRRB` slot taking a back or a receiver, and a `FLEX` slot also taking a
tight end. Hand it a back worth 20 and a tight end worth 19. Greedy fills the slots in
turn: `FLEX` takes the back, `WRRB` then has only the tight end available and cannot use
him, so one slot goes empty and the lineup scores 20. Assigning both at once starts the
tight end at `FLEX` and the back at `WRRB`, and scores 39.

So this is an assignment problem, not a sort. It is solved exactly, with the Hungarian
algorithm, because the inputs are tiny: a dozen slots against two dozen players. There is
no reason to approximate something this small.

Filling a slot always beats scoring points in it, which is what `FILL_BONUS` encodes. An
empty slot scores zero for the week, so any eligible player is better than none.
"""

from __future__ import annotations

# Dominates any plausible points total, so the solver fills every slot it can before it
# considers who to put where.
FILL_BONUS = 1_000_000.0

# Breaks a tie toward the earlier player in the caller's order, which is sorted best
# first. Small enough that it cannot outweigh a real difference in points.
_TIE_BREAK = 1e-6

_INELIGIBLE = float("-inf")


def assign_max_weight(weights: list[list[float]]) -> list[int]:
    """Assign rows to columns to maximize total weight.

    `weights[row][col]` is the value of giving that row that column, or negative
    infinity when the pairing is not allowed. Returns one column index per row, or -1
    for a row left unassigned. Each column is used at most once.
    """
    rows = len(weights)
    if rows == 0:
        return []
    cols = len(weights[0])
    if cols == 0:
        return [-1] * rows

    # The solver minimizes, so costs are negated weights. A disallowed pairing gets a
    # cost far above any real one rather than infinity, which keeps the arithmetic
    # finite; such pairings are discarded after the fact.
    finite = [w for row in weights for w in row if w != _INELIGIBLE]
    forbidden = (max(abs(w) for w in finite) + 1.0) * (rows + cols) + 1.0 if finite else 1.0
    cost = [
        [forbidden if w == _INELIGIBLE else -w for w in row] + [0.0] * rows
        for row in weights
    ]
    padded_cols = cols + rows

    inf = float("inf")
    potential_row = [0.0] * (rows + 1)
    potential_col = [0.0] * (padded_cols + 1)
    match = [0] * (padded_cols + 1)
    path = [0] * (padded_cols + 1)

    for row in range(1, rows + 1):
        match[0] = row
        col = 0
        slack = [inf] * (padded_cols + 1)
        visited = [False] * (padded_cols + 1)
        while True:
            visited[col] = True
            current_row = match[col]
            delta = inf
            next_col = -1
            for candidate in range(1, padded_cols + 1):
                if visited[candidate]:
                    continue
                reduced = (
                    cost[current_row - 1][candidate - 1]
                    - potential_row[current_row]
                    - potential_col[candidate]
                )
                if reduced < slack[candidate]:
                    slack[candidate] = reduced
                    path[candidate] = col
                if slack[candidate] < delta:
                    delta = slack[candidate]
                    next_col = candidate
            for candidate in range(padded_cols + 1):
                if visited[candidate]:
                    potential_row[match[candidate]] += delta
                    potential_col[candidate] -= delta
                else:
                    slack[candidate] -= delta
            col = next_col
            if match[col] == 0:
                break
        while col:
            previous = path[col]
            match[col] = match[previous]
            col = previous

    assignment = [-1] * rows
    for candidate in range(1, padded_cols + 1):
        row = match[candidate]
        if row and candidate <= cols and weights[row - 1][candidate - 1] != _INELIGIBLE:
            assignment[row - 1] = candidate - 1
    return assignment


def assign_slots(
    slots: list[str],
    player_values: list[float],
    accepts: list[list[bool]],
) -> list[int]:
    """Fill slots with players to maximize filled slots first, then total points.

    `accepts[slot][player]` says whether that player is eligible for that slot.
    `player_values` is ordered best first, which decides ties. Returns one player index
    per slot, or -1 for a slot nothing can fill.
    """
    weights = [
        [
            FILL_BONUS + player_values[player] - _TIE_BREAK * player
            if accepts[slot][player]
            else _INELIGIBLE
            for player in range(len(player_values))
        ]
        for slot in range(len(slots))
    ]
    return assign_max_weight(weights)
