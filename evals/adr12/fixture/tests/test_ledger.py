"""Tests for the ledger."""

from ledger import apply_fee


def test_apply_fee_adds_the_rate():
    assert apply_fee(100.0) == 102.5
