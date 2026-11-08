"""Journal Entry construction for inter-branch postings.

Three jobs:

1. Build the companion Journal Entry a stock transfer or service settlement
   needs (`post_obligation`, `post_service_settlement`).
2. Cancel those companions when the source document is cancelled.
3. Balance *manual* Journal Entries that touch more than one branch, by
   injecting the missing inter-branch legs on validate.

Every leg this module writes carries `custom_auto_inserted`,
`custom_source_doctype` and `custom_source_docname`, which is what makes the
injection idempotent and the cancellation targeted.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import flt

from inter_branch.core import accounts as ib_accounts
from inter_branch.core import config as ib_config

AUTO_FLAG = "custom_auto_inserted"
SOURCE_DT = "custom_source_doctype"
SOURCE_DN = "custom_source_docname"


def _header_cost_center_fields(je) -> list:
	"""Site-added header Cost Center fields on Journal Entry.

	Core Journal Entry carries no header Cost Center — the field belongs on the
	leg. Sites add one anyway, to group a voucher under one branch, and then build
	on it: flame.enfonoerp.com has `custom_cost_center` (Link → Cost Center), a
	Server Script on Before Insert that throws when it is empty, and four Document
	Naming Rules that read it to prefix the voucher (JVLG / JVWH / JVLT / JVLM).

	An inter-branch companion Journal Entry references no invoice, so nothing fills
	such a field: the settlement either dies at insert, or — with the throw
	removed — gets named by whichever rule an empty value happens to satisfy,
	which on flame is every `!=` condition in the JVWH rule. So fill it whenever
	the field exists, not only when it is marked mandatory. Whether the field is
	mandatory decides how hard we fail, not whether we fill it: a site can enforce
	it from a Server Script, which no `reqd` flag reflects.
	"""
	return [df for df in je.meta.fields if df.fieldtype == "Link" and df.options == "Cost Center"]


def _set_header_cost_center(je, cost_center: str | None, branch: str | None) -> None:
	"""Fill any header Cost Center field this site added to Journal Entry.

	`cost_center` is the originating branch's — the creditor of an obligation, the
	performer of a settlement — because that is the branch the voucher belongs to.
	The legs keep their own per-branch cost centers regardless; this is a header
	grouping field only.
	"""
	fields = _header_cost_center_fields(je)
	if not fields:
		return

	if not cost_center:
		# Only a field the site made mandatory is worth failing over; an optional one
		# simply stays empty, exactly as it would on a hand-made Journal Entry.
		mandatory = [df for df in fields if df.reqd and not je.get(df.fieldname)]
		if mandatory:
			message = _(
				"{0} is mandatory on Journal Entry, but branch {1} has no Cost Center in its "
				"Inter Branch Configuration. Set a Default Cost Center there and try again."
			)
			frappe.throw(
				message.format(_(mandatory[0].label), branch or _("(not set)")),
				title=_("Branch Cost Center Missing"),
			)
		return

	for df in fields:
		if not je.get(df.fieldname):
			je.set(df.fieldname, cost_center)


def _set_required_naming_series(je) -> None:
	"""Give the Journal Entry a naming series when the site kept that field mandatory.

	Core keeps `naming_series` mandatory because Journal Entry autonames from it. A
	site that names its vouchers by Document Naming Rule instead leaves the field
	with no default — flame.enfonoerp.com has eight rules keyed on `voucher_type`
	plus the header Cost Center — so a desk user picks a series from the list and a
	programmatic insert has nobody to pick. The failure is confusing when it lands:
	`[Journal Entry, JVWH2608001]: naming_series`, thrown *after* the naming rule
	has already named the document.

	Take the default series — the field's own default if it has one (`new_doc` has
	already applied it by the time we look), else the first option. The naming rule
	still decides the actual name; this only satisfies the field.
	"""
	df = je.meta.get_field("naming_series")
	if not df or not df.reqd or je.get("naming_series"):
		return

	options = [option.strip() for option in (df.options or "").split("\n") if option.strip()]
	if not options:
		return

	je.set("naming_series", options[0])


def _new_journal_entry(
	company: str,
	posting_date,
	source_doctype: str,
	source_docname: str,
	remark: str,
	cost_center: str | None = None,
	branch: str | None = None,
):
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = posting_date
	je.user_remark = remark
	if je.meta.get_field(SOURCE_DT):
		je.set(SOURCE_DT, source_doctype)
		je.set(SOURCE_DN, source_docname)
	_set_header_cost_center(je, cost_center, branch)
	_set_required_naming_series(je)
	return je


def _add_leg(
	je,
	account: str,
	branch: str,
	amount: float,
	side: str,
	source_doctype: str,
	source_docname: str,
	cost_center: str | None = None,
	remark: str | None = None,
) -> None:
	"""Append one leg. `side` is "debit" or "credit"; the caller balances the set."""
	if side not in ("debit", "credit"):
		raise ValueError(f"side must be 'debit' or 'credit', got: {side!r}")

	currency = ib_accounts.account_currency(je.company, account)
	row = {
		"account": account,
		"account_currency": currency,
		"exchange_rate": 1,
		"branch": branch,
		AUTO_FLAG: 1,
		SOURCE_DT: source_doctype,
		SOURCE_DN: source_docname,
	}
	if cost_center:
		row["cost_center"] = cost_center
	if remark:
		row["user_remark"] = remark

	if side == "debit":
		row["debit_in_account_currency"] = amount
		row["debit"] = amount
	else:
		row["credit_in_account_currency"] = amount
		row["credit"] = amount

	je.append("accounts", row)


def _submit(je) -> str:
	je.flags.skip_inter_branch_injection = True
	je.flags.ignore_permissions = True
	je.insert(ignore_permissions=True)
	je.submit()
	return je.name


# ---------------------------------------------------------------------------
# Stock-side obligation — two legs, balance sheet only
# ---------------------------------------------------------------------------


def post_obligation(
	company: str,
	posting_date,
	debtor_branch: str,
	creditor_branch: str,
	amount: float,
	source_doctype: str,
	source_docname: str,
	remark: str | None = None,
) -> str | None:
	"""Record that `debtor_branch` owes `creditor_branch` `amount`.

	    Dr  Due from <debtor>    branch = creditor
	    Cr  Due to <creditor>    branch = debtor

	The underlying stock movement already posted the Stock-in-Hand legs on each
	branch, so this pair is what squares each branch's trial balance.
	"""
	amount = flt(amount, 2)
	if amount <= 0 or debtor_branch == creditor_branch:
		return None

	receivable = ib_accounts.get_due_from_account(company, debtor_branch)
	payable = ib_accounts.get_due_to_account(company, creditor_branch)

	creditor_cc = ib_config.resolve_branch_cost_center(creditor_branch)
	debtor_cc = ib_config.resolve_branch_cost_center(debtor_branch)

	remark = remark or _("Inter-branch obligation from {0} {1}").format(_(source_doctype), source_docname)
	# Header cost center follows the creditor: the branch that gave up the stock.
	je = _new_journal_entry(
		company, posting_date, source_doctype, source_docname, remark,
		cost_center=creditor_cc, branch=creditor_branch,
	)

	_add_leg(
		je, receivable, creditor_branch, amount, "debit", source_doctype, source_docname,
		cost_center=creditor_cc,
		remark=_("Receivable from {0}").format(debtor_branch),
	)
	_add_leg(
		je, payable, debtor_branch, amount, "credit", source_doctype, source_docname,
		cost_center=debtor_cc,
		remark=_("Payable to {0}").format(creditor_branch),
	)
	return _submit(je)


# ---------------------------------------------------------------------------
# Service settlement — four legs, recharge across the P&L boundary
# ---------------------------------------------------------------------------


def post_service_settlement(
	company: str,
	posting_date,
	performing_branch: str,
	receiving_branch: str,
	amount: float,
	source_doctype: str,
	source_docname: str,
	recovered_account: str | None = None,
	charged_account: str | None = None,
	remark: str | None = None,
) -> str | None:
	"""Move the cost of work done by one branch onto the branch that benefited.

	    Cr  Service Recovered   branch = performer   (Income, not turnover)
	    Dr  Due from <receiver> branch = performer   (Asset)
	    Dr  Service Charged     branch = receiver    (Expense)
	    Cr  Due to <performer>  branch = receiver    (Liability)

	Deliberately not an internal Sales Invoice: the recharge pair is equal and
	opposite by construction, so it eliminates at consolidation without anyone
	remembering to eliminate it.
	"""
	amount = flt(amount, 2)
	if amount <= 0 or performing_branch == receiving_branch:
		return None

	recovered = recovered_account or ib_accounts.get_service_recovered_account(company, performing_branch)
	charged = charged_account or ib_accounts.get_service_charged_account(company, receiving_branch)
	receivable = ib_accounts.get_due_from_account(company, receiving_branch)
	payable = ib_accounts.get_due_to_account(company, performing_branch)

	performer_cc = ib_config.resolve_branch_cost_center(performing_branch)
	receiver_cc = ib_config.resolve_branch_cost_center(receiving_branch)

	remark = remark or _("Inter-branch service recharge from {0} {1}").format(
		_(source_doctype), source_docname
	)
	# Header cost center follows the performer: the branch that did the work.
	je = _new_journal_entry(
		company, posting_date, source_doctype, source_docname, remark,
		cost_center=performer_cc, branch=performing_branch,
	)

	_add_leg(
		je, recovered, performing_branch, amount, "credit", source_doctype, source_docname,
		cost_center=performer_cc,
		remark=_("Service recovered from {0}").format(receiving_branch),
	)
	_add_leg(
		je, receivable, performing_branch, amount, "debit", source_doctype, source_docname,
		cost_center=performer_cc,
		remark=_("Receivable from {0}").format(receiving_branch),
	)
	_add_leg(
		je, charged, receiving_branch, amount, "debit", source_doctype, source_docname,
		cost_center=receiver_cc,
		remark=_("Service charged by {0}").format(performing_branch),
	)
	_add_leg(
		je, payable, receiving_branch, amount, "credit", source_doctype, source_docname,
		cost_center=receiver_cc,
		remark=_("Payable to {0}").format(performing_branch),
	)
	return _submit(je)


# ---------------------------------------------------------------------------
# Companion lookup / cancellation
# ---------------------------------------------------------------------------


def find_companion_jes(source_doctype: str, source_docname: str, docstatus: int = 1) -> list[str]:
	je = DocType("Journal Entry")
	jea = DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(jea)
		.join(je)
		.on(je.name == jea.parent)
		.select(je.name)
		.distinct()
		.where(
			(jea[SOURCE_DT] == source_doctype)
			& (jea[SOURCE_DN] == source_docname)
			& (je.docstatus == docstatus)
		)
	).run()
	return [row[0] for row in rows]


def cancel_companion_jes(source_doctype: str, source_docname: str) -> list[str]:
	"""Cancel every submitted companion JE for a source document."""
	cancelled = []
	for name in find_companion_jes(source_doctype, source_docname):
		je = frappe.get_doc("Journal Entry", name)
		je.flags.skip_inter_branch_injection = True
		je.flags.ignore_permissions = True
		je.cancel()
		cancelled.append(name)
	return cancelled


# ---------------------------------------------------------------------------
# Manual Journal Entry auto-injection
# ---------------------------------------------------------------------------


def _per_branch_imbalance(je) -> dict[str, float]:
	"""{branch: signed imbalance}; positive = excess debit, negative = excess credit."""
	totals: dict[str, float] = {}
	for row in je.accounts or []:
		branch = (row.branch or "").strip()
		if not branch:
			continue
		totals[branch] = totals.get(branch, 0.0) + flt(row.debit_in_account_currency) - flt(
			row.credit_in_account_currency
		)
	return {branch: round(value, 2) for branch, value in totals.items() if abs(value) >= 0.01}


def _strip_auto_legs(je) -> None:
	je.accounts = [row for row in (je.accounts or []) if not row.get(AUTO_FLAG)]


def _inject_pair(je, debtor: str, creditor: str, amount: float, source_dt: str, source_dn: str) -> None:
	receivable = ib_accounts.get_due_from_account(je.company, debtor)
	payable = ib_accounts.get_due_to_account(je.company, creditor)

	_add_leg(
		je, receivable, creditor, amount, "debit", source_dt or "Journal Entry",
		source_dn or je.name or "",
		remark=_("Auto-injected: receivable from {0}").format(debtor),
	)
	_add_leg(
		je, payable, debtor, amount, "credit", source_dt or "Journal Entry",
		source_dn or je.name or "",
		remark=_("Auto-injected: {0} owes {1}").format(debtor, creditor),
	)


def auto_inject_inter_branch_legs(doc, method=None) -> None:
	"""Journal Entry.validate hook — balance every branch inside one JE.

	Two branches: the counterparty is unambiguous, so the pair is inferred from
	the imbalance. Three or more: the company's bridge branch is the implicit
	counterparty for all the others, because any other choice would be a guess
	about who really owes whom.
	"""
	if doc.doctype != "Journal Entry":
		return
	if doc.flags.get("skip_inter_branch_injection"):
		return
	if not doc.company:
		return
	if not ib_config.is_enabled():
		return
	if ib_config.is_pre_cut_over(doc.company, doc.get("posting_date")):
		return

	_strip_auto_legs(doc)
	imbalance = _per_branch_imbalance(doc)

	if len(imbalance) < 2:
		# Nothing to bridge; a single unbalanced branch is ERPNext's problem.
		return

	if round(sum(imbalance.values()), 2) != 0:
		frappe.throw(
			_("Journal Entry totals are unbalanced before inter-branch injection. Per-branch deltas: {0}").format(
				imbalance
			),
			title=_("Unbalanced Journal Entry"),
		)

	source_dt, source_dn = "", ""
	for row in doc.accounts:
		if row.get(SOURCE_DT):
			source_dt = row.get(SOURCE_DT)
			source_dn = row.get(SOURCE_DN) or ""
			break

	if len(imbalance) == 2:
		branch_a, branch_b = sorted(imbalance)
		if imbalance[branch_a] > imbalance[branch_b]:
			debtor, creditor, amount = branch_a, branch_b, imbalance[branch_a]
		else:
			debtor, creditor, amount = branch_b, branch_a, imbalance[branch_b]
		_inject_pair(doc, debtor, creditor, amount, source_dt, source_dn)
	else:
		bridge = ib_config.get_bridge_branch(doc.company)
		if not bridge:
			frappe.throw(
				_(
					"This Journal Entry touches {0} branches: <b>{1}</b>. Multi-branch entries need a "
					"<b>Bridge Branch</b> on the company row of Inter Branch Settings, or split the "
					"entry into two-branch Journal Entries."
				).format(len(imbalance), ", ".join(sorted(imbalance))),
				title=_("Bridge Branch Not Configured"),
			)
		if bridge not in imbalance:
			frappe.throw(
				_(
					"This Journal Entry touches branches <b>{0}</b>, but the configured bridge branch "
					"<b>{1}</b> is not among them. Add a line on <b>{1}</b>, or split the entry."
				).format(", ".join(sorted(imbalance)), bridge),
				title=_("Bridge Branch Missing From Journal Entry"),
			)

		for branch, delta in sorted(imbalance.items()):
			if branch == bridge:
				continue
			if delta > 0:
				debtor, creditor, amount = branch, bridge, delta
			else:
				debtor, creditor, amount = bridge, branch, abs(delta)
			_inject_pair(doc, debtor, creditor, amount, source_dt, source_dn)

	remaining = _per_branch_imbalance(doc)
	if remaining:
		frappe.throw(
			_("Auto-injection failed to balance branches: {0}").format(remaining),
			title=_("Inter-Branch Auto-Injection Error"),
		)
