# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""Inter Branch Reconciliation.

A matrix of what each branch owes each other branch. Rows are the branch
holding the balance, columns the counterparty the account head represents.

Healthy state: A→B and B→A net to zero. A non-zero pair means a manual Journal
Entry was posted to one side only, a companion entry was cancelled without its
source, or a document is still mid-flight. The `Unmatched` column carries that
difference per row so nobody has to eyeball the grid.

The account-to-branch map comes from Inter Branch Configuration, not from
parsing account names, so renaming a head does not silently drop it out of the
report.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Sum
from frappe.utils import flt

from inter_branch.core import accounts as ib_accounts


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = filters.company or frappe.defaults.get_user_default("Company")
	if not company:
		frappe.throw(_("Select a company."))

	account_branch = ib_accounts.inter_branch_accounts(company)
	if not account_branch:
		frappe.throw(
			_("No branch account heads are configured for {0}. Set them on Inter Branch Configuration.").format(
				company
			),
			title=_("Nothing to Reconcile"),
		)

	balances = _balances(company, list(account_branch), filters)

	# matrix[holder][counterparty] = net balance the holder carries
	matrix: dict[str, dict[str, float]] = {}
	for (account, holder), amount in balances.items():
		counterparty = account_branch.get(account)
		if not holder or not counterparty:
			continue
		matrix.setdefault(holder, {}).setdefault(counterparty, 0.0)
		matrix[holder][counterparty] += amount

	branches = sorted(set(account_branch.values()) | set(matrix))
	if filters.hide_empty:
		active = {
			branch
			for branch in branches
			if any(abs(value) > 0.01 for value in matrix.get(branch, {}).values())
			or any(abs(matrix.get(other, {}).get(branch, 0.0)) > 0.01 for other in branches)
		}
		branches = [branch for branch in branches if branch in active]

	columns = _columns(branches)
	data = []
	for holder in branches:
		row = {"holder": holder}
		unmatched = 0.0
		for counterparty in branches:
			value = flt(matrix.get(holder, {}).get(counterparty, 0.0), 2)
			row[_fieldname(counterparty)] = value
			if holder != counterparty:
				mirror = flt(matrix.get(counterparty, {}).get(holder, 0.0), 2)
				unmatched += value + mirror
		row["unmatched"] = flt(unmatched, 2)
		data.append(row)

	return columns, data


def _columns(branches: list[str]) -> list[dict]:
	columns = [
		{
			"label": _("Branch"),
			"fieldname": "holder",
			"fieldtype": "Link",
			"options": "Branch",
			"width": 180,
		}
	]
	for branch in branches:
		columns.append(
			{
				"label": branch,
				"fieldname": _fieldname(branch),
				"fieldtype": "Currency",
				"width": 140,
			}
		)
	columns.append(
		{
			"label": _("Unmatched"),
			"fieldname": "unmatched",
			"fieldtype": "Currency",
			"width": 130,
		}
	)
	return columns


def _fieldname(branch: str) -> str:
	return "br_" + "".join(ch for ch in branch.lower() if ch.isalnum())


def _balances(company: str, accounts: list[str], filters) -> dict[tuple[str, str], float]:
	gl = DocType("GL Entry")
	query = (
		frappe.qb.from_(gl)
		.select(gl.account, gl.branch, Sum(gl.debit - gl.credit).as_("balance"))
		.where((gl.company == company) & (gl.is_cancelled == 0) & (gl.account.isin(accounts)))
		.groupby(gl.account, gl.branch)
	)
	if filters.from_date:
		query = query.where(gl.posting_date >= filters.from_date)
	if filters.to_date:
		query = query.where(gl.posting_date <= filters.to_date)

	return {(row.account, row.branch): flt(row.balance) for row in query.run(as_dict=True)}
