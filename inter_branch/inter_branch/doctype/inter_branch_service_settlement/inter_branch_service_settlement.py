# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""Inter Branch Service Settlement.

A stock transfer moves an asset, so a balance-sheet pair is enough to keep both
branches square. A *service* has no asset: by the time one branch has done work
for another, the cost is already sunk in the performing branch's P&L — salaries,
fuel, subcontractors, all posted against its own branch. Booking only the
balance-sheet pair would leave the performer carrying the whole cost and the
receiver carrying none, while the company total looks right.

So this document posts four legs: the usual `Due from` / `Due to` pair, plus a
recharge pair that carries the P&L effect across the boundary. See
`inter_branch.core.journal.post_service_settlement` for the leg-by-leg map.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from inter_branch.core import accounts as ib_accounts
from inter_branch.core import config as ib_config
from inter_branch.core import journal as ib_journal
from inter_branch.core import pricing as ib_pricing
from inter_branch.core.pricing import COST_PLUS, FLAT, HOURLY

__all__ = ["COST_PLUS", "FLAT", "HOURLY", "InterBranchServiceSettlement"]


class InterBranchServiceSettlement(Document):
	# ------------------------------------------------------------------
	# Validate
	# ------------------------------------------------------------------

	def validate(self):
		ib_config.get_company_setting(self.company, throw=True)
		self._validate_branches()
		self._calculate_amounts()
		self._set_accounts()

	def _validate_branches(self):
		if self.performing_branch == self.receiving_branch:
			frappe.throw(
				_("The performing and receiving branch cannot be the same."),
				title=_("Same Branch"),
			)
		# Both configurations must exist before submit, or the account heads
		# cannot be resolved and the failure lands mid-submit instead of here.
		# `validate_branch_company` loads each one with `throw=True`, so it
		# covers that as well as the one-Branch-one-Company constraint — the
		# settlement posts four legs across these two branches and has exactly
		# the same failure mode as a stock transfer.
		ib_config.validate_branch_company(self.performing_branch, self.company)
		ib_config.validate_branch_company(self.receiving_branch, self.company)

	def _calculate_amounts(self):
		if self.pricing_basis == FLAT:
			self.total_amount = flt(self.flat_amount)
		else:
			total = 0.0
			for item in self.items or []:
				item.amount = self._row_amount(item)
				total += flt(item.amount)
			self.total_amount = total

		if flt(self.total_amount) <= 0:
			frappe.throw(
				_("The settlement amount must be greater than zero."),
				title=_("Nothing to Settle"),
			)

	def _row_amount(self, item) -> float:
		return ib_pricing.row_amount(self.pricing_basis, item, self.default_markup_percentage)

	def _set_accounts(self):
		"""Resolve the recharge accounts onto the document so they are auditable.

		Held here rather than looked up at submit time: an operator who changes
		a branch default afterwards should not silently change what an
		already-submitted settlement posted to.
		"""
		if not self.service_recovered_account:
			self.service_recovered_account = ib_accounts.get_service_recovered_account(
				self.company, self.performing_branch
			)
		if not self.service_charged_account:
			self.service_charged_account = ib_accounts.get_service_charged_account(
				self.company, self.receiving_branch
			)

	# ------------------------------------------------------------------
	# Submit / cancel
	# ------------------------------------------------------------------

	def on_submit(self):
		self.db_set("status", "Submitted")
		je_name = ib_journal.post_service_settlement(
			company=self.company,
			posting_date=self.posting_date,
			performing_branch=self.performing_branch,
			receiving_branch=self.receiving_branch,
			amount=self.total_amount,
			source_doctype=self.doctype,
			source_docname=self.name,
			recovered_account=self.service_recovered_account,
			charged_account=self.service_charged_account,
			remark=self.remarks or None,
		)
		if not je_name:
			frappe.throw(_("Could not post the settlement Journal Entry."))

		self.db_set("journal_entry", je_name, notify=True)
		frappe.msgprint(
			_('Journal Entry <a href="/app/journal-entry/{0}">{0}</a> created').format(je_name),
			alert=True,
			indicator="green",
		)

	def on_cancel(self):
		try:
			ib_journal.cancel_companion_jes(self.doctype, self.name)
		except Exception:
			frappe.log_error(
				title="Inter-branch service settlement JE cancel failed",
				message=frappe.get_traceback(),
			)
			raise
		self.db_set("status", "Cancelled")


@frappe.whitelist()
def get_branch_accounts(company: str, performing_branch: str, receiving_branch: str) -> dict:
	"""Preview the recharge accounts a settlement would post to."""
	frappe.has_permission("Inter Branch Service Settlement", "read", throw=True)
	return {
		"service_recovered_account": ib_accounts.get_service_recovered_account(company, performing_branch),
		"service_charged_account": ib_accounts.get_service_charged_account(company, receiving_branch),
	}
