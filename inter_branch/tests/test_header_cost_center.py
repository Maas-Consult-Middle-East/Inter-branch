"""Unit tests for the site-added mandatory header Cost Center on Journal Entry.

flame.enfonoerp.com (EFTSP-014) carries a client Custom Field
`Journal Entry.custom_cost_center` (Link → Cost Center) plus a Server Script on
Before Insert that throws `Value missing for Journal Entry: Cost Center` when it
is empty. That script only fills the field from a referenced Sales or Purchase
Invoice, and an inter-branch companion Journal Entry references neither — so
`Inter Branch Service Settlement` and `Inter Branch Stock Transfer` could not be
submitted at all: the throw landed inside `je.insert()`, mid-submit.

Dropping `reqd` on the Custom Field does not help — the Server Script is what
throws, and it does not read `reqd`. Filling the field does, and it is also the
honest answer: the voucher belongs to a branch, so it gets that branch's Cost
Center.

Filling it also gets the *name* right on that site: flame names Journal Entries
by Document Naming Rule keyed on `voucher_type` + `custom_cost_center`, so a
settlement performed by a warehouse branch now names itself `JVWH…`. The same
rules are why `naming_series` is mandatory there with no default — the second
half of this module.

Pure tests: a stand-in document with a hand-built meta, no site, no DocType.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_header_cost_center
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from inter_branch.core import journal as ib_journal
from inter_branch.core.journal import (
	_header_cost_center_fields,
	_set_header_cost_center,
	_set_required_naming_series,
)
from inter_branch.tests._stubs import MessageAssertions, Recorder

PERFORMER = "Logistics RYD"
RECEIVER = "WH 01 RYD"
PERFORMER_CC = "Logistics RYD - FL"
RECEIVER_CC = "01 - WH 01 RYD - FL"


def field(fieldname, fieldtype="Link", options="Cost Center", reqd=0, mandatory_depends_on=None, label=None):
	return frappe._dict(
		fieldname=fieldname,
		fieldtype=fieldtype,
		options=options,
		reqd=reqd,
		mandatory_depends_on=mandatory_depends_on,
		label=label or fieldname.replace("_", " ").title(),
	)


class JeStub:
	"""A Journal Entry stand-in: a meta with fields, and get/set on values."""

	def __init__(self, fields, doctype="Journal Entry", **values):
		self.doctype = doctype
		self.meta = frappe._dict(fields=fields)
		self.meta.get_field = lambda fieldname: next(
			(df for df in fields if df.fieldname == fieldname), None
		)
		self._values = dict(values)

	def get(self, fieldname, default=None):
		value = self._values.get(fieldname)
		return default if value is None else value

	def set(self, fieldname, value):
		self._values[fieldname] = value


class TestHeaderCostCenterDiscovery(unittest.TestCase):
	def test_a_mandatory_cost_center_link_is_found(self):
		je = JeStub([field("custom_cost_center", reqd=1, label="Cost Center")])
		self.assertEqual([df.fieldname for df in _header_cost_center_fields(je)], ["custom_cost_center"])

	def test_a_conditionally_mandatory_one_is_found_too(self):
		je = JeStub([field("custom_cost_center", mandatory_depends_on="eval:doc.company")])
		self.assertEqual([df.fieldname for df in _header_cost_center_fields(je)], ["custom_cost_center"])

	def test_an_optional_cost_center_counts_too(self):
		"""`reqd` is not the trigger: flame enforces this field from a Server Script,
		and its Document Naming Rules read it whether or not it carries an asterisk."""
		je = JeStub([field("custom_cost_center")])
		self.assertEqual([df.fieldname for df in _header_cost_center_fields(je)], ["custom_cost_center"])

	def test_a_link_to_something_else_is_left_out(self):
		je = JeStub([field("custom_division", options="Project", reqd=1)])
		self.assertEqual(_header_cost_center_fields(je), [])

	def test_a_plain_bench_has_none(self):
		"""Core Journal Entry has no header Cost Center — nothing to do there."""
		je = JeStub([
			field("company", options="Company", reqd=1),
			field("posting_date", fieldtype="Date", options=None, reqd=1),
		])
		self.assertEqual(_header_cost_center_fields(je), [])


class TestHeaderCostCenterFill(unittest.TestCase, MessageAssertions):
	def test_the_field_is_filled_with_the_branch_cost_center(self):
		je = JeStub([field("custom_cost_center", reqd=1)])
		_set_header_cost_center(je, PERFORMER_CC, PERFORMER)
		self.assertEqual(je.get("custom_cost_center"), PERFORMER_CC)

	def test_an_optional_field_is_filled_as_well(self):
		"""The live regression: `reqd` was cleared by hand on flame to dodge the
		throw, which would have left us skipping the field the naming rules read."""
		je = JeStub([field("custom_cost_center")])
		_set_header_cost_center(je, PERFORMER_CC, PERFORMER)
		self.assertEqual(je.get("custom_cost_center"), PERFORMER_CC)

	def test_an_optional_field_with_no_cost_center_stays_empty_without_throwing(self):
		je = JeStub([field("custom_cost_center")])
		_set_header_cost_center(je, None, PERFORMER)
		self.assertIsNone(je.get("custom_cost_center"))

	def test_a_value_already_there_is_not_overwritten(self):
		je = JeStub([field("custom_cost_center", reqd=1)], custom_cost_center=RECEIVER_CC)
		_set_header_cost_center(je, PERFORMER_CC, PERFORMER)
		self.assertEqual(je.get("custom_cost_center"), RECEIVER_CC)

	def test_no_such_field_and_no_cost_center_is_not_an_error(self):
		"""The common case: a bench without the client field. Never throw there."""
		je = JeStub([field("company", options="Company", reqd=1)])
		_set_header_cost_center(je, None, None)
		self.assertIsNone(je.get("custom_cost_center"))

	def test_a_mandatory_field_with_no_resolvable_cost_center_throws_our_message(self):
		"""Better our message naming the branch than the Server Script's naming the JE."""
		je = JeStub([field("custom_cost_center", reqd=1, label="Cost Center")])
		message = self.assert_throws(_set_header_cost_center, je, None, PERFORMER)
		self.assert_mentions(message, "Cost Center", PERFORMER, "Inter Branch Configuration")


class TestRequiredNamingSeries(unittest.TestCase):
	"""flame names Journal Entries by Document Naming Rule, so `naming_series` is
	mandatory with no default and a programmatic insert has nobody to pick one."""

	SERIES = frappe._dict(
		fieldname="naming_series",
		fieldtype="Select",
		options="\nJV.YY.MM.###\nJVLG.YY.MM.###\nJVWH.YY.MM.###",
		reqd=1,
		mandatory_depends_on=None,
		label="Series",
	)

	def test_the_first_option_is_taken(self):
		je = JeStub([self.SERIES], doctype="Journal Entry")
		_set_required_naming_series(je)
		self.assertEqual(je.get("naming_series"), "JV.YY.MM.###")

	def test_a_series_already_chosen_is_kept(self):
		je = JeStub([self.SERIES], doctype="Journal Entry", naming_series="JVLG.YY.MM.###")
		_set_required_naming_series(je)
		self.assertEqual(je.get("naming_series"), "JVLG.YY.MM.###")

	def test_an_optional_field_is_left_to_the_site(self):
		"""A site with a default on the field has already answered this."""
		optional = self.SERIES.copy()
		optional.reqd = 0
		je = JeStub([frappe._dict(optional)], doctype="Journal Entry")
		_set_required_naming_series(je)
		self.assertIsNone(je.get("naming_series"))

	def test_no_such_field_is_not_an_error(self):
		je = JeStub([field("custom_cost_center", reqd=1)], doctype="Journal Entry")
		_set_required_naming_series(je)
		self.assertIsNone(je.get("naming_series"))


class TestCallersPassTheOriginatingBranch(unittest.TestCase):
	"""Which branch the header carries is a decision, so pin it down.

	Obligation → the creditor (the branch that gave up the stock).
	Settlement  → the performer (the branch that did the work).
	"""

	def _patched(self, new_je):
		return (
			patch.object(ib_journal, "_new_journal_entry", new_je),
			patch.object(ib_journal, "_add_leg", Recorder()),
			patch.object(ib_journal, "_submit", Recorder(result="JV-TEST-0001")),
			patch.object(
				ib_journal.ib_config,
				"resolve_branch_cost_center",
				lambda branch: {PERFORMER: PERFORMER_CC, RECEIVER: RECEIVER_CC}.get(branch),
			),
		)

	def test_obligation_header_follows_the_creditor(self):
		new_je = Recorder(result=frappe._dict(company="Acme Corp"))
		accounts = patch.multiple(
			ib_journal.ib_accounts,
			get_due_from_account=lambda company, branch: f"Due From {branch}",
			get_due_to_account=lambda company, branch: f"Due To {branch}",
		)
		with accounts:
			for context in self._patched(new_je):
				context.start()
				self.addCleanup(context.stop)
			ib_journal.post_obligation(
				company="Acme Corp",
				posting_date="2026-08-25",
				debtor_branch=RECEIVER,
				creditor_branch=PERFORMER,
				amount=100,
				source_doctype="Inter Branch Stock Transfer",
				source_docname="IBST-2026-00001",
			)
		_, kwargs = new_je.calls[0]
		self.assertEqual(kwargs["cost_center"], PERFORMER_CC)
		self.assertEqual(kwargs["branch"], PERFORMER)

	def test_settlement_header_follows_the_performer(self):
		new_je = Recorder(result=frappe._dict(company="Acme Corp"))
		accounts = patch.multiple(
			ib_journal.ib_accounts,
			get_due_from_account=lambda company, branch: f"Due From {branch}",
			get_due_to_account=lambda company, branch: f"Due To {branch}",
		)
		with accounts:
			for context in self._patched(new_je):
				context.start()
				self.addCleanup(context.stop)
			ib_journal.post_service_settlement(
				company="Acme Corp",
				posting_date="2026-08-25",
				performing_branch=PERFORMER,
				receiving_branch=RECEIVER,
				amount=100,
				source_doctype="Inter Branch Service Settlement",
				source_docname="IBSS-2026-00001",
				recovered_account="Service Recovered - AC",
				charged_account="Service Charged - AC",
			)
		_, kwargs = new_je.calls[0]
		self.assertEqual(kwargs["cost_center"], PERFORMER_CC)
		self.assertEqual(kwargs["branch"], PERFORMER)
