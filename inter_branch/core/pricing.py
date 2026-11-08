"""How much one branch charges another for a service.

Held apart from the Inter Branch Service Settlement controller so the
arithmetic can be tested without a site: it is a pure function of the pricing
basis and one row, and needs no DocType, no company and no chart of accounts.
The controller stays the entry point — `_row_amount` delegates here — and
re-exports the three basis names, which are stored on every existing document
and hard-coded in the client script.
"""

from __future__ import annotations

from frappe.utils import flt

FLAT = "Flat Amount"
HOURLY = "Hours x Rate"
COST_PLUS = "Cost Plus Percentage"


def row_amount(pricing_basis: str | None, row, default_markup_percentage=0) -> float:
	"""The amount one settlement row charges, under `pricing_basis`.

	Everything is coerced with `flt`, so a blank hour count or a rate that
	arrived as a string is worth zero rather than raising. An unrecognised or
	not-yet-picked basis keeps the row's own amount: a draft saved before the
	basis was chosen must not silently recompute itself to zero.
	"""
	if pricing_basis == HOURLY:
		return flt(row.hours) * flt(row.rate)

	if pricing_basis == COST_PLUS:
		markup = flt(row.markup_percentage) or flt(default_markup_percentage)
		return flt(row.cost_amount) * (1 + markup / 100.0)

	return flt(row.amount)
