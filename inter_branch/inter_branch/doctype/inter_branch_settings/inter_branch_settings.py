# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# fieldname -> (required root_type, must_be_group)
ACCOUNT_EXPECTATIONS = {
	"receivable_parent_account": ("Asset", 1),
	"payable_parent_account": ("Liability", 1),
	"service_recovered_account": ("Income", 0),
	"service_charged_account": ("Expense", 0),
}


class InterBranchSettings(Document):
	def validate(self):
		self._validate_unique_companies()
		self._validate_accounts()
		self._validate_related_parties()

	def on_update(self):
		# Enabling a company, or flipping the dimension switch, has to reach the
		# Accounting Dimension immediately — waiting for the next migrate would
		# leave GL postings unguarded in between.
		from inter_branch.setup.dimension import ensure_branch_dimension

		frappe.clear_document_cache(self.doctype)
		ensure_branch_dimension()

	def _validate_unique_companies(self):
		seen = set()
		for row in self.companies or []:
			if row.company in seen:
				frappe.throw(
					_("Row {0}: company {1} is listed twice.").format(row.idx, row.company),
					title=_("Duplicate Company"),
				)
			seen.add(row.company)

	def _validate_accounts(self):
		for row in self.companies or []:
			for fieldname, (root_type, is_group) in ACCOUNT_EXPECTATIONS.items():
				account = row.get(fieldname)
				if not account:
					continue

				meta = frappe.db.get_value(
					"Account", account, ["company", "root_type", "is_group"], as_dict=True
				)
				if meta.company != row.company:
					frappe.throw(
						_("Row {0}: account {1} belongs to company {2}, not {3}.").format(
							row.idx, account, meta.company, row.company
						),
						title=_("Wrong Company"),
					)
				if meta.root_type != root_type:
					frappe.throw(
						_("Row {0}: account {1} is {2}, but a {3} account is required here.").format(
							row.idx, account, meta.root_type, root_type
						),
						title=_("Wrong Account Type"),
					)
				if meta.is_group != is_group:
					wanted = _("a group account") if is_group else _("a ledger account")
					frappe.throw(
						_("Row {0}: {1} must be {2}.").format(row.idx, account, wanted),
						title=_("Wrong Account Type"),
					)

	def _validate_related_parties(self):
		"""One Related Party Account per (Company, Related Company) pair, both directions present.

		A pair may span several rows — one per Business Unit of the related company —
		as long as every row names the same account and party.
		"""
		pairs = {}
		account_pair = {}
		units = set()
		for row in self.related_parties or []:
			if row.company == row.related_company:
				frappe.throw(
					_("Row {0}: Related Company must differ from Company.").format(row.idx),
					title=_("Invalid Related Party"),
				)
			pair = (row.company, row.related_company)
			first = pairs.get(pair)
			if first and (first.related_party_account, first.party_type, first.party) != (
				row.related_party_account, row.party_type, row.party
			):
				frappe.throw(
					_("Row {0}: {1} already uses account {2} for {3}; every row of a pair must use the "
					  "same account and party.").format(row.idx, row.company, first.related_party_account, row.related_company),
					title=_("Duplicate Related Party"),
				)
			if account_pair.setdefault(row.related_party_account, pair) != pair:
				frappe.throw(
					_("Row {0}: account {1} already represents another related company.").format(
						row.idx, row.related_party_account
					),
					title=_("Duplicate Related Party"),
				)
			if row.business_unit and (row.company, row.business_unit) in units:
				frappe.throw(
					_("Row {0}: Business Unit {1} is already mapped for {2}.").format(
						row.idx, row.business_unit, row.company
					),
					title=_("Duplicate Related Party"),
				)
			pairs.setdefault(pair, row)
			units.add((row.company, row.business_unit))

			meta = frappe.db.get_value(
				"Account", row.related_party_account, ["company", "is_group", "account_type"], as_dict=True
			)
			if meta.company != row.company:
				frappe.throw(
					_("Row {0}: account {1} belongs to company {2}, not {3}.").format(
						row.idx, row.related_party_account, meta.company, row.company
					),
					title=_("Wrong Company"),
				)
			if meta.is_group:
				frappe.throw(
					_("Row {0}: {1} must be a ledger account.").format(row.idx, row.related_party_account),
					title=_("Wrong Account Type"),
				)
			if meta.account_type in ("Receivable", "Payable") and not (row.party_type and row.party):
				frappe.throw(
					_("Row {0}: {1} is a {2} account, so Party Type and Party are required.").format(
						row.idx, row.related_party_account, _(meta.account_type)
					),
					title=_("Party Required"),
				)

			currencies = {
				frappe.get_cached_value("Company", company, "default_currency")
				for company in (row.company, row.related_company)
			}
			if len(currencies) > 1:
				frappe.throw(
					_("Row {0}: {1} and {2} have different currencies; inter-company mirroring "
					  "only supports companies sharing one currency.").format(
						row.idx, row.company, row.related_company
					),
					title=_("Currency Mismatch"),
				)

		for (company, related_company), row in pairs.items():
			if (related_company, company) not in pairs:
				frappe.throw(
					_("Row {0}: add the reverse row too — {1} needs a Related Party Account for {2}, "
					  "which the mirror entry credits or debits.").format(row.idx, related_company, company),
					title=_("Missing Reverse Related Party"),
				)
