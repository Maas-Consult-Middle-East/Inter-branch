"""Unit tests for the posting rules that decide who owes whom, and how much.

These run against stand-in objects rather than real documents, so they need no
company, chart of accounts or branch configuration and stay fast. What they pin
down is the part that is expensive to get wrong in production: the per-branch
imbalance arithmetic behind Journal Entry auto-injection.

The service settlement's three pricing bases used to live here, driven through
the controller. They now drive the pure calculator instead — see
`inter_branch.tests.test_pricing` — because instantiating a controller needs the
DocType to exist on the site, which it does not on a plain frappe+erpnext bench.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_posting_logic
"""

import unittest

import frappe

from inter_branch.core.journal import _per_branch_imbalance, _strip_auto_legs


def leg(branch, debit=0.0, credit=0.0, auto=0):
	return frappe._dict(
		branch=branch,
		debit_in_account_currency=debit,
		credit_in_account_currency=credit,
		custom_auto_inserted=auto,
	)


class TestBranchImbalance(unittest.TestCase):
	def test_balanced_branch_is_not_reported(self):
		je = frappe._dict(accounts=[leg("Riyadh", debit=100), leg("Riyadh", credit=100)])
		self.assertEqual(_per_branch_imbalance(je), {})

	def test_two_branches_report_equal_and_opposite_deltas(self):
		je = frappe._dict(accounts=[leg("Riyadh", debit=250), leg("Jeddah", credit=250)])
		self.assertEqual(_per_branch_imbalance(je), {"Riyadh": 250.0, "Jeddah": -250.0})

	def test_rows_without_a_branch_are_ignored(self):
		je = frappe._dict(accounts=[leg("", debit=90), leg(None, credit=90)])
		self.assertEqual(_per_branch_imbalance(je), {})

	def test_sub_cent_differences_are_not_an_imbalance(self):
		"""Rounding noise inside one branch is not a bridge to build.

		The imbalance is measured per branch, so the two legs have to be on the
		same branch for the sub-cent rule to be what is under test — the earlier
		version of this test put them on different branches, where each branch is
		individually out by a full 100 and the rule never comes into play.
		"""
		je = frappe._dict(accounts=[leg("Riyadh", debit=100.004), leg("Riyadh", credit=100)])
		self.assertEqual(_per_branch_imbalance(je), {})

	def test_a_sub_cent_leg_across_two_branches_is_still_a_full_imbalance(self):
		"""Each branch is out by ~100 here; only the *rounding* is sub-cent."""
		je = frappe._dict(accounts=[leg("Riyadh", debit=100.004), leg("Jeddah", credit=100)])
		self.assertEqual(_per_branch_imbalance(je), {"Riyadh": 100.0, "Jeddah": -100.0})

	def test_reported_deltas_are_rounded_to_the_cent(self):
		je = frappe._dict(accounts=[leg("Riyadh", debit=100.006), leg("Jeddah", credit=100)])
		self.assertEqual(_per_branch_imbalance(je), {"Riyadh": 100.01, "Jeddah": -100.0})

	def test_auto_legs_are_stripped_before_recomputing(self):
		je = frappe._dict(
			accounts=[
				leg("Riyadh", debit=100),
				leg("Jeddah", credit=100, auto=1),
			]
		)
		_strip_auto_legs(je)
		self.assertEqual(len(je.accounts), 1)
		self.assertEqual(_per_branch_imbalance(je), {"Riyadh": 100.0})
