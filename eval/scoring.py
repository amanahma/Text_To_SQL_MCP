# """Correctness scoring for evaluation runs.

# OWNER WRITES THIS. The harness calls :func:`is_correct` with the executed result
# set of the model's SQL and of the gold SQL, and expects a bool. The comparison
# policy (row/column order, type coercion, set vs. multiset, floating-point
# tolerance) is a judgement call the owner makes deliberately — so this ships as a
# stub. Until it is implemented the harness records ``correct = null``.
# """

# from __future__ import annotations

# from typing import Any

# Row = tuple[Any, ...]


# def is_correct(pred_rows: list[Row], gold_rows: list[Row]) -> bool:
#     """Return True iff the predicted result set matches the gold result set.

#     Args:
#         pred_rows: Rows returned by the model's SQL, each a tuple of column values.
#         gold_rows: Rows returned by the reference (gold) SQL, same shape.
#     """
#     raise NotImplementedError(
#         "eval/scoring.py is owner-written; is_correct is not implemented yet."
#     )

"""Decide whether a predicted SQL result matches the gold result.

Rules (BIRD-style execution accuracy, plus number rounding):
- Results are compared as SETS of rows: row order and duplicate rows are ignored.
- Column order matters (rows are tuples); column names are ignored.
- Integers are compared exactly.
- Floats and Decimals are rounded to 6 significant digits, because BIRD's gold SQL
  often uses CAST(... AS REAL), a 4-byte float with ~7 significant digits, while a
  model may compute the same value with exact NUMERIC math.
- None equals None. Booleans stay booleans (True is not treated as 1).
- Everything else (text, dates, ...) is compared exactly.

Known weaknesses (documented on purpose):
- "Top N" questions: order is ignored, so the right rows in the wrong order still pass.
- Empty vs empty counts as correct.
- Two different numbers that agree to 6 significant digits count as equal.
- Values sitting right on a rounding edge can still split (rare: ~1 in 400 for REAL results).
"""

from decimal import Decimal

SIG_DIGITS = 5


def _round_sig(d: Decimal) -> Decimal:
    """Round a finite Decimal to SIG_DIGITS significant digits."""
    if d == 0:
        return Decimal(0)
    exponent = d.adjusted() - SIG_DIGITS + 1
    return d.quantize(Decimal(1).scaleb(exponent))


def _norm_value(v):
    if v is None or isinstance(v, bool):  # bool BEFORE int: bool is a subclass of int
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, (float, Decimal)):
        d = Decimal(str(v)) if isinstance(v, float) else v
        if not d.is_finite():  # NaN / Infinity
            return str(d)
        return _round_sig(d)
    if isinstance(v, (list, tuple)):  # Postgres arrays come back as lists (unhashable)
        return tuple(_norm_value(x) for x in v)
    return v


def _norm_row(row) -> tuple:
    return tuple(_norm_value(v) for v in row)


def is_correct(pred_rows, gold_rows) -> bool:
    pred = {_norm_row(r) for r in pred_rows}
    gold = {_norm_row(r) for r in gold_rows}
    return pred == gold
