"""Unit tests for inter-company mirror planning.

Stand-in rows and lookups only — no companies or chart of accounts needed.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_inter_company
"""

import unittest

import frappe

from inter_branch.core.inter_company import _prorate, plan_mirrors

RELATED = [
	frappe._dict(company="Kairoos", related_party_account="Related Party - Allo - K", related_company="Allo"),
	frappe._dict(company="Allo", related_party_account="Related Party - Kairoos - A", related_company="Kairoos"),
]

# Kairoos account -> Allo account, by account name.
ALLO_CHART = {"Software Expenses - K": "Software Expenses - A", "Rent - K": "Rent - A", "Bank - K": "Bank - A"}


def map_account(account, company):
	return ALLO_CHART.get(account) if company == "Allo" else None


def account_company(account):
	return "Allo" if account.endswith(" - A") else "Kairoos"


def leg(idx, account, debit=0.0, credit=0.0, **extra):
	return frappe._dict(idx=idx, account=account, debit=debit, credit=credit, **extra)


def plan(rows):
	return plan_mirrors("Kairoos", rows, RELATED, map_account, account_company)


def summary(legs):
	return sorted((l["account"], l["side"], l["amount"]) for l in legs)


class TestPlanMirrors(unittest.TestCase):
	def test_shared_expense_split_70_30(self):
		result = plan([
			leg(1, "Software Expenses - K", debit=700),
			leg(2, "Related Party - Allo - K", debit=300),
			leg(3, "Bank - K", credit=1000),
		])
		self.assertEqual(list(result), ["Allo"])
		self.assertEqual(
			summary(result["Allo"]),
			[("Related Party - Kairoos - A", "credit", 300), ("Software Expenses - A", "debit", 300)],
		)

	def test_offset_is_prorated_across_same_side_lines(self):
		result = plan([
			leg(1, "Software Expenses - K", debit=400),
			leg(2, "Rent - K", debit=300),
			leg(3, "Related Party - Allo - K", debit=300),
			leg(4, "Bank - K", credit=1000),
		])["Allo"]
		debits = sum(l["amount"] for l in result if l["side"] == "debit")
		self.assertEqual(debits, 300)
		self.assertEqual(
			summary(result),
			[
				("Related Party - Kairoos - A", "credit", 300),
				("Rent - A", "debit", 128.57),
				("Software Expenses - A", "debit", 171.43),
			],
		)

	def test_credit_to_related_party_mirrors_as_debit(self):
		# Allo paid a Kairoos bill: Kairoos books the expense and owes Allo.
		result = plan([
			leg(1, "Software Expenses - K", debit=500),
			leg(2, "Related Party - Allo - K", credit=500, custom_inter_company_account="Bank - A"),
		])["Allo"]
		self.assertEqual(
			summary(result), [("Bank - A", "credit", 500), ("Related Party - Kairoos - A", "debit", 500)]
		)

	def test_override_wins_over_same_side_lines(self):
		result = plan([
			leg(1, "Software Expenses - K", debit=700),
			leg(2, "Related Party - Allo - K", debit=300, custom_inter_company_account="Rent - A"),
			leg(3, "Bank - K", credit=1000),
		])["Allo"]
		self.assertIn(("Rent - A", "debit", 300), summary(result))

	def test_funding_entry_without_override_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			plan([leg(1, "Related Party - Allo - K", debit=300), leg(2, "Bank - K", credit=300)])

	def test_override_in_wrong_company_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			plan([
				leg(1, "Related Party - Allo - K", debit=300, custom_inter_company_account="Bank - K"),
				leg(2, "Bank - K", credit=300),
			])

	def test_unmatched_offset_account_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			plan([
				leg(1, "Travel - K", debit=700),
				leg(2, "Related Party - Allo - K", debit=300),
				leg(3, "Bank - K", credit=1000),
			])

	def test_business_unit_of_the_related_party_line_reaches_every_mirror_leg(self):
		result = plan([
			leg(1, "Software Expenses - K", debit=700, business_unit="BU Kairoos"),
			leg(2, "Related Party - Allo - K", debit=300, business_unit="BU Allo"),
			leg(3, "Bank - K", credit=1000),
		])["Allo"]
		self.assertEqual({l["business_unit"] for l in result}, {"BU Allo"})

	def test_entry_without_related_party_lines_plans_nothing(self):
		self.assertEqual(plan([leg(1, "Software Expenses - K", debit=10), leg(2, "Bank - K", credit=10)]), {})

	def test_auto_inserted_legs_are_not_offsets(self):
		result = plan([
			leg(1, "Software Expenses - K", debit=700),
			leg(2, "Due from X - K", debit=50, custom_auto_inserted=1),
			leg(3, "Related Party - Allo - K", debit=300),
			leg(4, "Bank - K", credit=1050),
		])["Allo"]
		self.assertIn(("Software Expenses - A", "debit", 300), summary(result))


class TestProrate(unittest.TestCase):
	def test_shares_always_sum_to_amount(self):
		shares = _prorate(100, [1, 1, 1])
		self.assertEqual(shares, [33.33, 33.33, 33.34])
		self.assertEqual(round(sum(shares), 2), 100)
