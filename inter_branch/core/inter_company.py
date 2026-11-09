"""Inter-company mirroring of Journal Entries through Related Party Accounts.

Each company keeps one Related Party Account per related company (Inter Branch
Settings → Related Party Accounts), optionally tied to the related company's
Business Unit. Two steps:

1. **Split** (before validate). A line that picks a Business Unit Allocation
   whose matrix names a related company's Business Unit is turned into explicit
   lines — the related share moves onto the Related Party Account:

       Dr Software Expenses 1000  [allocation: Kairoos 70 / Allo 30]
     → Dr Software Expenses        700  [BU Kairoos]
       Dr Related Party - Allo     300  [BU Allo]

   The allocation is consumed, so saving again does not split again, and the
   Business Unit Allocation app sees plain Business Unit lines afterwards.

2. **Mirror** (on submit). Every Related Party leg gets a *draft* Journal Entry
   in the related company, for its accountants to review and submit:

       Allo:  Dr Software Expenses - A          300
              Cr Related Party - Kairoos - A    300

   The leg flips side onto the related company's account for this company. Its
   offset is the line's `custom_inter_company_account` (filled by the split, or
   by hand), else the entry's other same-side lines, prorated and matched to the
   related company's chart by account name. A pure funding entry
   (Dr Related Party / Cr Bank) has neither, so the user is asked on save.

ERPNext's own "Inter Company Journal Entry" voucher type is not used: it insists
both vouchers carry equal totals, which a cost split never does.
"""

from __future__ import annotations

from collections.abc import Callable

import frappe
from frappe import _
from frappe.utils import flt

from inter_branch.core import config as ib_config
from inter_branch.core import journal as ib_journal

OVERRIDE_FIELD = "custom_inter_company_account"
NATIVE_INTER_COMPANY = "Inter Company Journal Entry"

# Fields owned by the Business Unit Allocation app, when installed.
BU_FIELD = "business_unit"
BU_ALLOCATION_FIELD = "business_unit_allocation"
BU_SNAPSHOT_FIELD = "business_unit_allocation_snapshot"

AMOUNT_FIELDS = ("debit_in_account_currency", "credit_in_account_currency", "debit", "credit")


def related_party_rows() -> list:
	settings = ib_config.get_settings()
	if not settings.enabled:
		return []
	return list(settings.get("related_parties") or [])


def _net(row) -> float:
	return flt(row.debit) - flt(row.credit)


def _prorate(amount: float, weights: list[float]) -> list[float]:
	"""Split `amount` by `weights` to the cent; the rounding residue lands on the last share."""
	total = sum(weights)
	shares = [flt(amount * weight / total, 2) for weight in weights[:-1]]
	shares.append(flt(amount - sum(shares), 2))
	return shares


def _skip(doc) -> bool:
	# A generated entry (mirror, obligation, settlement) never mirrors again — that
	# is what stops A → B → A. A native inter-company pair is booked by hand on both sides.
	return bool(
		doc.flags.get("skip_inter_company_mirror")
		or doc.get(ib_journal.SOURCE_DT)
		or doc.voucher_type == NATIVE_INTER_COMPANY
	)


# ---------------------------------------------------------------------------
# Step 1 — split a Business Unit Allocation into a Related Party line
# ---------------------------------------------------------------------------


def split_related_party_allocations(doc, method=None) -> None:
	"""Journal Entry.before_validate — see module docstring, step 1."""
	if _skip(doc) or not frappe.get_meta("Journal Entry Account").has_field(BU_ALLOCATION_FIELD):
		return
	units = {
		row.business_unit: row
		for row in related_party_rows()
		if row.company == doc.company and row.get("business_unit")
	}
	if not units:
		return

	for row in list(doc.accounts or []):
		rule_name = row.get(BU_ALLOCATION_FIELD)
		if not rule_name:
			continue
		rule = frappe.get_doc("Business Unit Allocation", rule_name)
		matrix = [(m.business_unit, flt(m.allocation_percentage)) for m in rule.contribution_matrix]
		if not any(unit in units for unit, _pct in matrix):
			continue  # purely internal allocation: the Business Unit Allocation app handles it

		if rule.disabled or rule.company != doc.company:
			frappe.throw(
				_("Row {0}: Business Unit Allocation {1} is disabled or belongs to another company.").format(
					row.idx, rule.name
				),
				title=_("Invalid Allocation"),
			)
		if rule.get("expense_account") and rule.expense_account != row.account:
			frappe.throw(
				_("Row {0}: Business Unit Allocation {1} is restricted to account {2}.").format(
					row.idx, rule.name, rule.expense_account
				),
				title=_("Invalid Allocation"),
			)
		_split_row(doc, row, matrix, units)

	for idx, row in enumerate(doc.accounts, start=1):
		row.idx = idx


def _split_row(doc, row, matrix: list[tuple[str, float]], units: dict) -> None:
	weights = [pct for _unit, pct in matrix]
	shares = {field: _prorate(flt(row.get(field)), weights) for field in AMOUNT_FIELDS}
	template = row.as_dict(no_default_fields=True, no_child_table_fields=True)
	source_account = row.account
	position = doc.accounts.index(row)

	for i, (unit, _pct) in enumerate(matrix):
		amounts = {field: shares[field][i] for field in AMOUNT_FIELDS}
		if i and not any(amounts.values()):
			continue
		if i:
			line = doc.append("accounts", dict(template))
			doc.accounts.remove(line)
			position += 1
			doc.accounts.insert(position, line)
		else:
			line = row

		line.update(amounts)
		line.set(BU_FIELD, unit)
		line.set(BU_ALLOCATION_FIELD, None)
		line.set(BU_SNAPSHOT_FIELD, None)

		setting = units.get(unit)
		if setting:
			line.account = setting.related_party_account
			line.account_currency = frappe.get_cached_value("Account", line.account, "account_currency")
			line.party_type = setting.party_type
			line.party = setting.party
			line.set(OVERRIDE_FIELD, _map_account(source_account, setting.related_company))


# ---------------------------------------------------------------------------
# Step 2 — plan the mirror entries
# ---------------------------------------------------------------------------


def plan_mirrors(
	company: str,
	rows: list,
	related_parties: list,
	map_account: Callable[[str, str], str | None],
	account_company: Callable[[str], str | None],
) -> dict[str, list[dict]]:
	"""{related company: balanced mirror legs} for one Journal Entry's `rows`.

	Each leg is {account, side, amount, party_type, party, business_unit}. Pure apart
	from the two lookups, so the allocation rules are unit-testable without a chart.
	"""
	own = {row.related_party_account: row for row in related_parties if row.company == company}
	reverse = {row.company: row for row in related_parties if row.related_company == company}

	peers = [
		row for row in rows
		if row.account not in own and not row.get(ib_journal.AUTO_FLAG) and abs(_net(row)) >= 0.005
	]

	plan: dict[str, list[dict]] = {}
	for row in rows:
		override = row.get(OVERRIDE_FIELD)
		setting = own.get(row.account)
		if not setting:
			if override:
				frappe.throw(
					_("Row {0}: Related Company Account is only used on a Related Party Account line.").format(
						row.idx
					),
					title=_("Not a Related Party Line"),
				)
			continue

		net = _net(row)
		if abs(net) < 0.005:
			continue

		target = setting.related_company
		counter = reverse.get(target)
		if not counter:
			frappe.throw(
				_("{0} has no Related Party Account for {1} in Inter Branch Settings.").format(target, company),
				title=_("Missing Reverse Related Party"),
			)

		amount = flt(abs(net), 2)
		party_side, offset_side = ("credit", "debit") if net > 0 else ("debit", "credit")

		if override:
			if account_company(override) != target:
				frappe.throw(
					_("Row {0}: Related Company Account {1} must belong to {2}.").format(row.idx, override, target),
					title=_("Wrong Company"),
				)
			offsets = [(override, amount)]
		else:
			same_side = [peer for peer in peers if (_net(peer) > 0) == (net > 0)]
			if not same_side:
				frappe.throw(
					_(
						"Row {0}: nothing on this entry shows what {1} should book against {2}. "
						"Set <b>Related Company Account</b> on the line."
					).format(row.idx, target, row.account),
					title=_("Related Company Account Needed"),
				)
			offsets = []
			for peer, share in zip(same_side, _prorate(amount, [abs(_net(peer)) for peer in same_side])):
				mapped = map_account(peer.account, target)
				if not mapped:
					frappe.throw(
						_(
							"Row {0}: {1} has no ledger account matching {2}. Create it, or set "
							"<b>Related Company Account</b> on the Related Party line (row {3})."
						).format(peer.idx, target, peer.account, row.idx),
						title=_("No Matching Account"),
					)
				offsets.append((mapped, share))

		unit = row.get(BU_FIELD)
		legs = plan.setdefault(target, [])
		legs.append({
			"account": counter.related_party_account,
			"side": party_side,
			"amount": amount,
			"party_type": counter.party_type,
			"party": counter.party,
			"business_unit": unit,
		})
		legs.extend(
			{
				"account": account, "side": offset_side, "amount": share,
				"party_type": None, "party": None, "business_unit": unit,
			}
			for account, share in offsets
			if share
		)

	return plan


# ---------------------------------------------------------------------------
# Database lookups
# ---------------------------------------------------------------------------


def _map_account(account: str, company: str) -> str | None:
	"""The ledger in `company` with the same account name as `account`."""
	account_name = frappe.get_cached_value("Account", account, "account_name")
	return frappe.db.get_value(
		"Account", {"company": company, "account_name": account_name, "is_group": 0, "disabled": 0}
	)


def _account_company(account: str) -> str | None:
	return frappe.get_cached_value("Account", account, "company")


def _plan(doc) -> dict[str, list[dict]]:
	if _skip(doc):
		return {}
	related = related_party_rows()
	if not related:
		return {}
	return plan_mirrors(doc.company, doc.accounts or [], related, _map_account, _account_company)


# ---------------------------------------------------------------------------
# Journal Entry hooks
# ---------------------------------------------------------------------------


def validate_related_party_legs(doc, method=None) -> None:
	"""Journal Entry.validate — fail on save, not submit, when a mirror can't be built."""
	_plan(doc)


def post_related_party_mirrors(doc, method=None) -> None:
	"""Journal Entry.on_submit — leave one draft mirror Journal Entry per related company."""
	has_unit = frappe.get_meta("Journal Entry Account").has_field(BU_FIELD)
	for target, legs in _plan(doc).items():
		cost_center = frappe.get_cached_value("Company", target, "cost_center")
		branch = ib_config.get_bridge_branch(target)
		remark = _("Inter-company mirror of {0} Journal Entry {1}").format(doc.company, doc.name)
		if doc.user_remark:
			remark += f": {doc.user_remark}"

		je = ib_journal._new_journal_entry(
			target, doc.posting_date, doc.doctype, doc.name, remark, cost_center=cost_center, branch=branch
		)
		for leg in legs:
			ib_journal._add_leg(
				je, leg["account"], branch, leg["amount"], leg["side"], doc.doctype, doc.name,
				cost_center=cost_center, party_type=leg["party_type"], party=leg["party"], auto=False,
			)
			if has_unit and leg["business_unit"]:
				je.accounts[-1].set(BU_FIELD, leg["business_unit"])

		je.flags.skip_inter_branch_injection = True
		je.insert(ignore_permissions=True)
		frappe.msgprint(
			_("Draft inter-company Journal Entry {0} created in {1}.").format(
				frappe.utils.get_link_to_form("Journal Entry", je.name), target
			),
			alert=True,
		)


def cancel_related_party_mirrors(doc, method=None) -> None:
	"""Journal Entry.on_cancel — cancel submitted mirrors, delete drafts.

	Runs before Frappe's back-link check, so a submitted mirror never blocks the
	cancellation; a draft is deleted so nobody submits a mirror of a cancelled entry.
	"""
	ib_journal.cancel_companion_jes(doc.doctype, doc.name, exclude=doc.name)
	for name in ib_journal.find_companion_jes(doc.doctype, doc.name, docstatus=0):
		frappe.delete_doc("Journal Entry", name, ignore_permissions=True)
