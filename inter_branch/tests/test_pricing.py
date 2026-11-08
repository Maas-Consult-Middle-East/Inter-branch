"""The three pricing bases of an Inter Branch Service Settlement.

This is the arithmetic that decides how much one branch charges another, so it
is worth testing on its own — and it has no business needing a site to run. The
per-row calculation lives in `inter_branch.core.pricing.row_amount(basis, row,
default_markup_percentage)`; the controller's `_row_amount` delegates to it.
That split is what lets these tests run on a site where `inter_branch` is not
installed and our DocTypes therefore do not exist.

Contract pinned here:

* `inter_branch.core.pricing.row_amount(pricing_basis, row, default_markup_percentage=0)`
  — pure, three positional parameters, returns a float.
* `FLAT` / `HOURLY` / `COST_PLUS` keep their exact option strings, which are
  stored on every existing document and hard-coded in the client script.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_pricing
"""

from __future__ import annotations

import unittest

import frappe

from inter_branch.core.pricing import COST_PLUS, FLAT, HOURLY, row_amount
from inter_branch.inter_branch.doctype.inter_branch_service_settlement import (
	inter_branch_service_settlement as settlement_module,
)
from inter_branch.inter_branch.doctype.inter_branch_service_settlement.inter_branch_service_settlement import (
	InterBranchServiceSettlement,
)


def row(**fields):
	base = dict(hours=0, rate=0, cost_amount=0, markup_percentage=0, amount=0)
	base.update(fields)
	return frappe._dict(base)


class TestBasisNames(unittest.TestCase):
	"""The option strings are stored data, not an implementation detail."""

	def test_option_strings_are_unchanged(self):
		self.assertEqual(FLAT, "Flat Amount")
		self.assertEqual(HOURLY, "Hours x Rate")
		self.assertEqual(COST_PLUS, "Cost Plus Percentage")

	def test_the_controller_exposes_the_same_constants(self):
		"""Existing imports go through the controller module; moving the values
		must not change what they are."""
		self.assertEqual(settlement_module.FLAT, FLAT)
		self.assertEqual(settlement_module.HOURLY, HOURLY)
		self.assertEqual(settlement_module.COST_PLUS, COST_PLUS)


class TestRowAmount(unittest.TestCase):
	def test_hours_times_rate(self):
		self.assertEqual(row_amount(HOURLY, row(hours=7.5, rate=120), 0), 900.0)

	def test_hourly_ignores_the_typed_amount(self):
		self.assertEqual(row_amount(HOURLY, row(hours=2, rate=50, amount=9999), 0), 100.0)

	def test_hourly_coerces_blanks_rather_than_raising(self):
		self.assertEqual(row_amount(HOURLY, row(hours=None, rate=120), 0), 0.0)
		self.assertEqual(row_amount(HOURLY, row(hours="7.5", rate="120"), 0), 900.0)

	def test_cost_plus_uses_the_row_markup(self):
		self.assertEqual(row_amount(COST_PLUS, row(cost_amount=1000, markup_percentage=15), 5), 1150.0)

	def test_cost_plus_falls_back_to_the_document_markup(self):
		self.assertEqual(row_amount(COST_PLUS, row(cost_amount=2000, markup_percentage=0), 10), 2200.0)

	def test_cost_plus_with_no_markup_anywhere_is_cost(self):
		self.assertEqual(row_amount(COST_PLUS, row(cost_amount=2000), 0), 2000.0)

	def test_cost_plus_default_markup_is_optional(self):
		"""Called with two arguments the document default is simply absent."""
		self.assertEqual(row_amount(COST_PLUS, row(cost_amount=500, markup_percentage=20)), 600.0)

	def test_cost_plus_handles_fractional_markup(self):
		self.assertAlmostEqual(
			row_amount(COST_PLUS, row(cost_amount=1234.56, markup_percentage=7.5), 0), 1327.152, places=6
		)

	def test_flat_basis_keeps_the_row_amount_untouched(self):
		self.assertEqual(
			row_amount(FLAT, row(hours=3, rate=50, cost_amount=999, markup_percentage=50, amount=400), 0),
			400.0,
		)

	def test_an_unset_basis_keeps_the_row_amount(self):
		"""A draft saved before the basis was picked must not silently recompute."""
		self.assertEqual(row_amount(None, row(hours=3, rate=50, amount=400), 0), 400.0)


class TestControllerDelegates(unittest.TestCase):
	"""`_row_amount` must stay the controller's entry point — it is called from
	`_calculate_amounts` — but hold no arithmetic of its own."""

	def _doc(self, **fields):
		return frappe._dict(fields)

	def test_hourly_through_the_controller(self):
		doc = self._doc(pricing_basis=HOURLY, default_markup_percentage=0)
		self.assertEqual(InterBranchServiceSettlement._row_amount(doc, row(hours=7.5, rate=120)), 900.0)

	def test_cost_plus_row_markup_through_the_controller(self):
		doc = self._doc(pricing_basis=COST_PLUS, default_markup_percentage=5)
		self.assertEqual(
			InterBranchServiceSettlement._row_amount(doc, row(cost_amount=1000, markup_percentage=15)),
			1150.0,
		)

	def test_cost_plus_document_markup_through_the_controller(self):
		doc = self._doc(pricing_basis=COST_PLUS, default_markup_percentage=10)
		self.assertEqual(
			InterBranchServiceSettlement._row_amount(doc, row(cost_amount=2000, markup_percentage=0)),
			2200.0,
		)

	def test_flat_through_the_controller(self):
		doc = self._doc(pricing_basis=FLAT, default_markup_percentage=0)
		self.assertEqual(
			InterBranchServiceSettlement._row_amount(
				doc, row(hours=3, rate=50, cost_amount=999, markup_percentage=50, amount=400)
			),
			400.0,
		)

	def test_the_controller_passes_its_own_default_markup_down(self):
		doc = self._doc(pricing_basis=COST_PLUS, default_markup_percentage=25)
		self.assertEqual(
			InterBranchServiceSettlement._row_amount(doc, row(cost_amount=400, markup_percentage=0)), 500.0
		)
