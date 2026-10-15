from decimal import Decimal

from eval.scoring import is_correct


def test_identical_results_are_correct():
    rows = [(1, "a"), (2, "b")]
    assert is_correct(rows, rows) is True


def test_row_order_is_ignored():
    assert is_correct([(1,), (2,)], [(2,), (1,)]) is True


def test_duplicate_rows_are_ignored():
    assert is_correct([(1,), (1,), (2,)], [(1,), (2,)]) is True


def test_int_float_decimal_are_equal():
    assert is_correct([(3,)], [(3.0,)]) is True
    assert is_correct([(3,)], [(Decimal(3),)]) is True


def test_real_vs_numeric_precision_is_equal():
    # gold uses CAST(... AS REAL) (4-byte float); the model used exact NUMERIC math
    assert is_correct([(Decimal("66.666666666666666667"),)], [(66.66666412353516,)]) is True


def test_clearly_different_numbers_are_not_equal():
    assert is_correct([(66.67,)], [(66.66,)]) is False


def test_none_equals_none():
    assert is_correct([(None, "x")], [(None, "x")]) is True


def test_extra_column_is_wrong():
    assert is_correct([(1, "a")], [(1,)]) is False


def test_column_order_matters():
    assert is_correct([("a", 1)], [(1, "a")]) is False


def test_empty_vs_nonempty_is_wrong():
    assert is_correct([], [(1,)]) is False


def test_empty_vs_empty_is_correct():
    # Known weakness, kept for BIRD comparability
    assert is_correct([], []) is True


def test_known_weakness_rounding_edge():
    # 1.23455 rounds up, 1.2345499 rounds down -> marked different. Documented, rare.
    assert is_correct([(Decimal("1.23455"),)], [(Decimal("1.2345499"),)]) is False
