# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# fieldname -> required root_type. Everything here must be a ledger account:
# a group account cannot be posted to, and the failure would only surface on
# the first cross-branch document.
ACCOUNT_ROOT_TYPES = {
	"due_from_account": "Asset",
	"due_to_account": "Liability",
	"service_recovered_account": "Income",
	"service_charged_account": "Expense",
}


def _one_company_note(doctype: str, company: str) -> str:
	"""The one-Branch-one-Company rule, appended to every wrong-company refusal.

	On its own "belongs to company X, not Y" reads as a mis-picked account and
	sends the operator off to change it. The constraint is the other way round:
	this configuration is autonamed `field:branch`, so there is one per Branch
	and a Branch therefore belongs to exactly one Company.
	"""
	return _(
		"<br><br>A Branch belongs to exactly one company — there is one {0} per Branch, and "
		"this one is on {1}. If the same physical branch operates under two companies, create "
		"a Branch per company."
	).format(_(doctype), company)


class InterBranchConfiguration(Document):
	def validate(self):
		self._validate_company_is_stable()
		self._validate_accounts()
		self._validate_shared_heads()
		self._validate_warehouses()
		self._validate_cost_centers()

	def _validate_company_is_stable(self):
		"""Refuse a company change once the configuration has been used.

		The account heads, the mapped warehouses and the cost centres all belong
		to the company this record was set up under, so switching it underneath
		them orphans every one of them — and the damage only surfaces on the
		next cross-branch document. Deliberately a validation rather than
		`set_only_once` on the field: a fresh record with a typo must stay
		fixable while it is still empty.
		"""
		if self.is_new() or not self.has_value_changed("company"):
			return

		in_use = (
			any(self.get(fieldname) for fieldname in ACCOUNT_ROOT_TYPES)
			or bool(self.warehouses)
			or bool(self.cost_centers)
		)
		if not in_use:
			return

		frappe.throw(
			_(
				"The company cannot be changed on a configuration that is already in use: its "
				"account heads, warehouses and cost centres all belong to the previous "
				"company.<br><br>"
				"Clear them first, or create a separate Branch for the other company — a Branch "
				"belongs to exactly one company."
			),
			title=_("Company Locked"),
		)

	def on_update(self):
		frappe.clear_document_cache(self.doctype, self.name)

	def _validate_accounts(self):
		for fieldname, root_type in ACCOUNT_ROOT_TYPES.items():
			account = self.get(fieldname)
			if not account:
				continue

			meta = frappe.db.get_value(
				"Account", account, ["company", "root_type", "is_group"], as_dict=True
			)
			label = _(self.meta.get_label(fieldname))

			if meta.company != self.company:
				frappe.throw(
					_("{0}: account {1} belongs to company {2}, not {3}.").format(
						label, account, meta.company, self.company
					)
					+ _one_company_note(self.doctype, self.company),
					title=_("Wrong Company"),
				)
			if meta.is_group:
				frappe.throw(
					_("{0}: {1} is a group account. Pick a ledger account.").format(label, account),
					title=_("Wrong Account Type"),
				)
			if meta.root_type != root_type:
				frappe.throw(
					_("{0}: {1} is {2}, but a {3} account is required.").format(
						label, account, meta.root_type, root_type
					),
					title=_("Wrong Account Type"),
				)

	def _validate_shared_heads(self):
		"""Two branches must not share a Due-from / Due-to head.

		The reconciliation report reads a balance's counterparty from the
		account it sits on. Point two branches at one control account and that
		mapping is no longer a function — balances would be attributed to
		whichever branch was saved last.
		"""
		for fieldname in ("due_from_account", "due_to_account"):
			account = self.get(fieldname)
			if not account:
				continue

			clash = frappe.db.get_value(
				self.doctype,
				{fieldname: account, "name": ["!=", self.name], "company": self.company},
				"name",
			)
			if clash:
				frappe.throw(
					_("{0} {1} is already used by branch {2}. Each branch needs its own head.").format(
						_(self.meta.get_label(fieldname)), account, clash
					),
					title=_("Account Head Already Used"),
				)

	def _validate_warehouses(self):
		seen = set()
		for row in self.warehouses or []:
			if row.warehouse in seen:
				frappe.throw(
					_("Row {0}: warehouse {1} is listed twice.").format(row.idx, row.warehouse),
					title=_("Duplicate Warehouse"),
				)
			seen.add(row.warehouse)

			company = frappe.get_cached_value("Warehouse", row.warehouse, "company")
			if company != self.company:
				frappe.throw(
					_("Row {0}: warehouse {1} belongs to company {2}, not {3}.").format(
						row.idx, row.warehouse, company, self.company
					)
					+ _one_company_note(self.doctype, self.company),
					title=_("Wrong Company"),
				)

			other = _other_branch_for_warehouse(row.warehouse, self.name)
			if other:
				# Legal, but worth saying out loud: whichever configuration was
				# saved last wins when a stock document resolves this warehouse.
				frappe.msgprint(
					_("Warehouse {0} is also mapped to branch {1}.").format(row.warehouse, other),
					title=_("Warehouse Mapped Twice"),
					indicator="orange",
				)

	def _validate_cost_centers(self):
		listed = [row.cost_center for row in self.cost_centers or [] if row.cost_center]

		if self.default_cost_center and listed and self.default_cost_center not in listed:
			frappe.throw(
				_("Default Cost Center {0} is not in the Cost Centers table.").format(
					self.default_cost_center
				),
				title=_("Cost Center Not Listed"),
			)

		to_check = listed + ([self.default_cost_center] if self.default_cost_center else [])
		for cost_center in to_check:
			company = frappe.get_cached_value("Cost Center", cost_center, "company")
			if company != self.company:
				frappe.throw(
					_("Cost Center {0} belongs to company {1}, not {2}.").format(
						cost_center, company, self.company
					)
					+ _one_company_note(self.doctype, self.company),
					title=_("Wrong Company"),
				)


def _other_branch_for_warehouse(warehouse: str, exclude: str) -> str | None:
	rows = frappe.get_all(
		"Inter Branch Configuration Warehouse",
		filters={"warehouse": warehouse, "parent": ["!=", exclude]},
		fields=["parent"],
		limit=1,
	)
	return rows[0].parent if rows else None
