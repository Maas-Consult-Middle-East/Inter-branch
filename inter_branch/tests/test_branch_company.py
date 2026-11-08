"""One Branch, one Company (D3).

There is one Inter Branch Configuration per Branch — the DocType is autonamed
`field:branch` — and that configuration carries a `company`. So a Branch belongs
to exactly one Company. Today that only surfaces indirectly, as a confusing
"account belongs to company X, not Y" thrown from somewhere else entirely, or as
a Journal Entry that fails validation mid-submit. These tests pin the decision
being stated once, in one place, in a message that describes the model.

Pure: `get_branch_config` is stubbed, controller methods are called unbound
against `frappe._dict` stand-ins. No fixture, no DocType.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_branch_company
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from inter_branch.core.config import validate_branch_company
from inter_branch.inter_branch.doctype.inter_branch_configuration.inter_branch_configuration import (
	ACCOUNT_ROOT_TYPES,
	InterBranchConfiguration,
)
from inter_branch.inter_branch.doctype.inter_branch_service_settlement.inter_branch_service_settlement import (
	InterBranchServiceSettlement,
)
from inter_branch.tests._stubs import (
	DocStub,
	MessageAssertions,
	Recorder,
	ThrowingRecorder,
	stub,
)

RIYADH = "Riyadh"
JEDDAH = "Jeddah"
ACME = "Acme Corp"
OTHER = "Globex Trading"


def config_for(company, branch=RIYADH):
	return frappe._dict(doctype="Inter Branch Configuration", name=branch, branch=branch, company=company)


class TestValidateBranchCompany(unittest.TestCase, MessageAssertions):
	"""`validate_branch_company(branch, company)`."""

	def test_matching_company_passes_quietly(self):
		with patch("inter_branch.core.config.get_branch_config", return_value=config_for(ACME)):
			self.assertIsNone(validate_branch_company(RIYADH, ACME))

	def test_mismatched_company_is_refused(self):
		with patch("inter_branch.core.config.get_branch_config", return_value=config_for(OTHER)):
			message = self.assert_throws(validate_branch_company, RIYADH, ACME)
		self.assert_mentions(message, RIYADH, ACME, OTHER)
		self.assertEqual(message.title, "Branch Belongs To Another Company")

	def test_the_message_states_the_model_not_just_the_symptom(self):
		"""An operator who reads "belongs to company X, not Y" tries to fix the
		company. The fix is a different Branch — say so."""
		with patch("inter_branch.core.config.get_branch_config", return_value=config_for(OTHER)):
			message = self.assert_throws(validate_branch_company, RIYADH, ACME)
		self.assertIn("exactly one company", message.plain.lower())
		self.assertIn("Inter Branch Configuration", message.plain)

	def test_a_missing_configuration_is_refused_by_the_loader(self):
		"""`get_branch_config(branch, throw=True)` already has the better message
		for this; it must not be swallowed."""
		raiser = ThrowingRecorder(frappe.ValidationError("Branch Riyadh has no Inter Branch Configuration."))
		with patch("inter_branch.core.config.get_branch_config", raiser):
			with self.assertRaises(frappe.ValidationError):
				validate_branch_company(RIYADH, ACME)
		self.assertTrue(raiser.called)

	def test_a_configuration_that_loads_as_none_is_still_refused(self):
		"""Defensive: a disabled configuration returns None when `throw` is not
		honoured. Posting on it would resolve no account heads at all."""
		with patch("inter_branch.core.config.get_branch_config", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				validate_branch_company(RIYADH, ACME)

	def test_a_legacy_configuration_with_no_company_is_refused(self):
		with patch("inter_branch.core.config.get_branch_config", return_value=config_for(None)):
			with self.assertRaises(frappe.ValidationError):
				validate_branch_company(RIYADH, ACME)

	def test_a_blank_branch_is_refused_rather_than_silently_passing(self):
		"""The loader returns None *without* throwing for a falsy branch, so the
		fall-through would raise a `ValidationError` too — from the mismatch check,
		describing a branch that was never given. `assertRaises` alone cannot tell
		the two apart: assert the refusal happened *before* the loader was reached,
		and that it is our message."""
		loader = ThrowingRecorder(frappe.ValidationError("Branch Not Configured"))
		with patch("inter_branch.core.config.get_branch_config", loader):
			message = self.assert_throws(validate_branch_company, None, ACME)
		self.assertFalse(loader.called, "a blank branch has nothing to look up")
		self.assertEqual(message.title, "Branch Missing")


class TestStockTransferBranchCompanies(unittest.TestCase):
	"""`InterBranchStockTransfer._validate_branch_companies` checks both ends."""

	def test_both_branches_are_checked_against_the_document_company(self):
		from inter_branch.inter_branch.doctype.inter_branch_stock_transfer.inter_branch_stock_transfer import (
			InterBranchStockTransfer,
		)

		doc = stub(
			InterBranchStockTransfer,
			methods=("_validate_branch_companies",),
			company=ACME,
			from_branch=RIYADH,
			to_branch=JEDDAH,
		)
		checker = Recorder()
		with patch("inter_branch.core.config.validate_branch_company", checker):
			doc._validate_branch_companies()

		checked = {tuple(args) or tuple(kwargs.values()) for args, kwargs in checker.calls}
		self.assertEqual(checker.count, 2)
		self.assertIn((RIYADH, ACME), checked)
		self.assertIn((JEDDAH, ACME), checked)


class TestServiceSettlementBranchCompanies(unittest.TestCase):
	"""The settlement posts four legs across the same two branches, so it has the
	same constraint and the same failure mode."""

	def test_both_branches_are_checked_against_the_document_company(self):
		doc = stub(
			InterBranchServiceSettlement,
			methods=("_validate_branches",),
			company=ACME,
			performing_branch=RIYADH,
			receiving_branch=JEDDAH,
		)
		checker = Recorder()
		with patch("inter_branch.core.config.validate_branch_company", checker), patch(
			"inter_branch.core.config.get_branch_config", return_value=config_for(ACME)
		):
			doc._validate_branches()

		checked = {tuple(args) or tuple(kwargs.values()) for args, kwargs in checker.calls}
		self.assertIn((RIYADH, ACME), checked)
		self.assertIn((JEDDAH, ACME), checked)

	def test_the_same_branch_twice_is_still_the_first_thing_refused(self):
		doc = stub(
			InterBranchServiceSettlement,
			methods=("_validate_branches",),
			company=ACME,
			performing_branch=RIYADH,
			receiving_branch=RIYADH,
		)
		with patch("inter_branch.core.config.validate_branch_company", Recorder()), patch(
			"inter_branch.core.config.get_branch_config", return_value=config_for(ACME)
		):
			with self.assertRaises(frappe.ValidationError):
				doc._validate_branches()


class _ConfigStub(DocStub):
	"""Stands in for a saved Inter Branch Configuration document."""

	def is_new(self):
		return bool(self.get("_is_new"))

	def has_value_changed(self, fieldname):
		return fieldname in (self.get("_changed") or ())


class TestConfigurationCompanyIsStable(unittest.TestCase, MessageAssertions):
	"""`InterBranchConfiguration._validate_company_is_stable`.

	Changing `company` on a configured branch silently orphans everything on it:
	the account heads, the warehouses and the cost centres all belong to the old
	company. Allowed only while the configuration is still empty — a fresh record
	with a typo must stay fixable, which is why this is a validation and not
	`set_only_once` on the field.
	"""

	def _doc(self, **fields):
		base = dict(
			name=RIYADH,
			branch=RIYADH,
			company=ACME,
			warehouses=[],
			cost_centers=[],
			_is_new=False,
			_changed=("company",),
		)
		base.update(fields)
		doc = _ConfigStub(**base)
		doc._validate_company_is_stable = InterBranchConfiguration._validate_company_is_stable.__get__(doc)
		return doc

	def test_a_new_configuration_may_be_given_any_company(self):
		doc = self._doc(_is_new=True, due_from_account="Due From Jeddah - AC")
		self.assertIsNone(doc._validate_company_is_stable())

	def test_an_unchanged_company_is_never_questioned(self):
		doc = self._doc(_changed=(), due_from_account="Due From Jeddah - AC")
		self.assertIsNone(doc._validate_company_is_stable())

	def test_an_empty_configuration_may_still_change_company(self):
		doc = self._doc()
		self.assertIsNone(doc._validate_company_is_stable())

	def test_an_account_head_blocks_the_company_change(self):
		for fieldname in ACCOUNT_ROOT_TYPES:
			with self.subTest(fieldname=fieldname):
				doc = self._doc(**{fieldname: "Some Account - AC"})
				message = self.assert_throws(doc._validate_company_is_stable)
				self.assertTrue(message.plain.strip())

	def test_a_mapped_warehouse_blocks_the_company_change(self):
		doc = self._doc(warehouses=[frappe._dict(warehouse="Branch A - AC", idx=1)])
		self.assert_throws(doc._validate_company_is_stable)

	def test_a_mapped_cost_center_blocks_the_company_change(self):
		doc = self._doc(cost_centers=[frappe._dict(cost_center="Riyadh - AC", idx=1)])
		self.assert_throws(doc._validate_company_is_stable)

	def test_the_refusal_explains_what_would_be_orphaned(self):
		doc = self._doc(warehouses=[frappe._dict(warehouse="Branch A - AC", idx=1)])
		message = self.assert_throws(doc._validate_company_is_stable)
		self.assertIn("company", message.plain.lower())


class TestConfigurationValidateWiring(unittest.TestCase):
	def test_validate_runs_the_company_stability_check(self):
		order: list[str] = []

		def record(name):
			def call(*args, **kwargs):
				order.append(name)

			return call

		doc = stub(InterBranchConfiguration, methods=("validate",))
		for name in (
			"_validate_accounts",
			"_validate_shared_heads",
			"_validate_warehouses",
			"_validate_cost_centers",
			"_validate_company_is_stable",
		):
			setattr(doc, name, record(name))
		doc.validate()
		self.assertIn("_validate_company_is_stable", order)
