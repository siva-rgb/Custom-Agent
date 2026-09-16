"""Ledger maths."""

FEE_RATE = 0.025


def apply_fee(amount):
    """Apply the standard fee to one amount."""
    return round(amount * (1 + FEE_RATE), 2)


def total(amounts):
    return round(sum(amounts), 2)
