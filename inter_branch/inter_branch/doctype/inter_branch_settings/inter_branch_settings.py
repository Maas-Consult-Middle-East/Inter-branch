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
