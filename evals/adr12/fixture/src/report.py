"""Reporting helpers."""

from ledger import total

TAX_RATE = 0.19


def summarise(amounts):
    gross = total(amounts)
    return {"gross": gross, "tax": round(gross * TAX_RATE, 2)}
