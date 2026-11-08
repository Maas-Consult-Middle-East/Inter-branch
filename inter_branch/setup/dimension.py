"""Branch as a mandatory Accounting Dimension.

ERPNext enforces mandatory dimensions at the GL-posting layer: with
`mandatory_for_bs` and `mandatory_for_pl` set on a company's row, every Balance
Sheet and P&L GL Entry for that company is rejected without a Branch. That one
switch is what makes branch-level statements trustworthy — it covers Journal
Entry, Stock Entry, Payment Entry and every other posting doctype at once,
including the ones this app never touches.

Only companies enabled in Inter Branch Settings are affected. Turning the
dimension mandatory on an unprepared company blocks its bookkeeping, so it is
never applied site-wide.
"""

from __future__ import annotations

import frappe

from inter_branch.core import config as ib_config


def ensure_branch_dimension() -> None:
	settings = ib_config.get_settings()
	if not settings.enabled:
		return

	enforced = {
		row.company: bool(row.enforce_branch_dimension)
		for row in (settings.companies or [])
		if row.enabled and row.company
	}
	if not enforced:
		return

	name = frappe.db.get_value("Accounting Dimension", {"document_type": "Branch"})
	if name:
		dimension = frappe.get_doc("Accounting Dimension", name)
	else:
		dimension = frappe.new_doc("Accounting Dimension")
		dimension.document_type = "Branch"
		dimension.insert(ignore_permissions=True)

	if dimension.disabled:
		dimension.disabled = 0

	existing = {row.company: row for row in (dimension.dimension_defaults or [])}

	for company, mandatory in enforced.items():
		row = existing.get(company)
		if row:
			row.reference_document = "Branch"
			row.mandatory_for_bs = 1 if mandatory else 0
			row.mandatory_for_pl = 1 if mandatory else 0
		else:
			dimension.append(
				"dimension_defaults",
				{
					"company": company,
					"reference_document": "Branch",
					"mandatory_for_bs": 1 if mandatory else 0,
					"mandatory_for_pl": 1 if mandatory else 0,
				},
			)

	dimension.save(ignore_permissions=True)
	_ensure_dimension_fields(dimension)


def _ensure_dimension_fields(dimension) -> None:
	"""Create the `branch` field on the accounting doctypes synchronously.

	ERPNext enqueues `make_dimension_in_accounting_doctypes` on the long queue
	after commit. Until that job runs, `tabGL Entry` and `tabJournal Entry
	Account` have no `branch` column — so an inter-branch posting made in the
	gap either loses its branch tags silently, or collides with the job's
	ALTER TABLE and dies with a deadlock. Both were observed on first install.

	Running it inline costs a few seconds once and makes the install
	deterministic. It is idempotent: ERPNext skips doctypes that already carry
	the field.
	"""
	if frappe.get_meta("Journal Entry Account").get_field("branch"):
		return

	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		make_dimension_in_accounting_doctypes,
	)

	try:
		make_dimension_in_accounting_doctypes(doc=dimension)
		frappe.clear_cache()
	except Exception:
		# Not fatal — the queued job still runs. Postings before it lands are
		# the risk, which is what the log is for.
		frappe.log_error(
			title="inter_branch: could not create Branch dimension fields inline",
			message=frappe.get_traceback(),
		)
