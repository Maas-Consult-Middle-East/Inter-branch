"""Stock-side glue: branch tagging, GL re-tagging, direct Stock Entry support.

The Branch accounting dimension is enforced per line, so a stock document whose
item rows carry no branch is rejected deep inside GL posting with an unhelpful
message. These hooks fill the dimension from the warehouse mapping before that
happens, and correct the two GL legs of a cross-branch transfer afterwards,
because ERPNext stamps both of them with the header branch.
"""

from __future__ import annotations

import frappe
from erpnext.stock import get_warehouse_account_map
from frappe import _
from frappe.utils import flt

from inter_branch.core import config as ib_config
from inter_branch.core import journal as ib_journal

# Only the single-purpose transfer is handled automatically. Repacks and
# manufacturing entries move value inside one branch or need a human decision.
INTER_BRANCH_SE_PURPOSES = ("Material Transfer",)


def auto_set_branch_from_warehouse(doc, method=None) -> None:
	"""Fill each item row's `branch` from the warehouse it moves stock in or out of.

	The warehouse is authoritative by default: an operator who picks a branch
	that contradicts the warehouse gets it replaced, because the GL entry hangs
	off the warehouse's account either way. Roles listed in Inter Branch
	Settings keep their choice.
	"""
	items = doc.get("items")
	if not items:
		return

	settings = ib_config.get_settings()
	if not settings.enabled or not settings.auto_tag_branch_from_warehouse:
		return

	user = frappe.session.user
	if user == "Administrator":
		return
	if set(frappe.get_roles(user)) & ib_config.trusted_roles():
		return

	authoritative = bool(settings.warehouse_is_authoritative)
	resolved: set[str] = set()
	first: str | None = None
	overridden = False

	for item in items:
		warehouse = item.get("warehouse") or item.get("s_warehouse") or item.get("t_warehouse")
		if not warehouse:
			continue
		branch = ib_config.resolve_warehouse_branch(warehouse)
		if not branch:
			continue

		previous = item.get("branch")
		if previous and previous != branch:
			if not authoritative:
				resolved.add(previous)
				continue
			overridden = True
		item.branch = branch
		resolved.add(branch)
		if first is None:
			first = branch

	# The header branch is only safe to set when every row agrees; a mixed
	# document keeps its per-row dimension and leaves the header alone.
	if first and len(resolved) == 1:
		if doc.get("branch") and doc.get("branch") != first:
			overridden = True
		doc.branch = first

	if overridden and not doc.flags.get("inter_branch_correction_notified"):
		doc.flags.inter_branch_correction_notified = True
		frappe.msgprint(
			_(
				"Branch was set from the warehouse mapping to keep GL entries consistent. "
				"Pick a different warehouse if you meant another branch."
			),
			indicator="orange",
			title=_("Branch Adjusted"),
			alert=True,
		)


def propagate_header_branch_to_items(doc, method=None) -> None:
	"""Copy the header branch into item rows that are still blank.

	ERPNext's dimension-aware reports filter on the child row, not the header,
	so a row left blank by an unmapped warehouse silently disappears from them
	even though its GL entry carries the branch. Runs after
	`auto_set_branch_from_warehouse`, and never overwrites a resolved value.
	"""
	settings = ib_config.get_settings()
	if not settings.enabled or not settings.propagate_header_branch:
		return

	header_branch = doc.get("branch")
	if not header_branch:
		return

	for item in doc.get("items") or []:
		if item.meta.get_field("branch") and not item.get("branch"):
			item.branch = header_branch


# ---------------------------------------------------------------------------
# Stock accounts
# ---------------------------------------------------------------------------


def get_stock_account_map(company: str):
	"""Warehouse → account map for a company, without ERPNext's own throw.

	`get_warehouse_account_map` resolves a warehouse's stock account through
	four fallbacks (the warehouse, its nearest ancestor, the company's default
	inventory account, then any Stock ledger account of the company) and
	`frappe.throw`s when a non-group warehouse still has none. Letting that
	surface would show the operator ERPNext's message instead of ours, from a
	call we only made to decide whether posting is safe at all — so it is caught
	here and the caller decides what to say. `clear_last_message` drops the raw
	message, which `frappe.throw` has already pushed onto the message log, so it
	cannot stack on top of the one we raise next.

	Always pass the company: a bare call returns every company's warehouses.

	The log is deferred: the IBST caller answers an empty map with a
	`frappe.throw`, and a plain `log_error` inserts inside that transaction, so
	the rollback would discard the one diagnostic that explains *why* the
	upstream builder failed — on exactly the path an operator raises a ticket
	about. `defer_insert` queues it out of band instead.
	"""
	try:
		return get_warehouse_account_map(company)
	except frappe.ValidationError:
		frappe.clear_last_message()
		frappe.log_error(
			title="Inter-branch stock account map unavailable",
			message=(
				f"Could not build the warehouse account map for company {company}:\n\n"
				f"{frappe.get_traceback()}"
			),
			defer_insert=True,
		)
		return {}


def stock_account(warehouse: str | None, account_map) -> str | None:
	"""The stock account a warehouse posts to, or None when it does not resolve.

	The only sanctioned way to read an account for a warehouse: reading
	`Warehouse.account` directly would miss the parent-warehouse and
	company-default fallbacks that upstream applies.

	Never iterate `account_map`. When a company's sub-map comes back empty,
	`get_warehouse_account_map` returns the whole *company*-keyed dict instead
	of a warehouse-keyed one, so only `.get(warehouse)` is safe and anything
	falsy — including a row with no account — counts as unresolved.
	"""
	if not warehouse or not account_map:
		return None

	row = account_map.get(warehouse)
	if not row:
		return None

	# `stock_controller` reads this as `warehouse_account[wh]["account"]`, so a
	# plain dict row behaves like the `frappe._dict` one upstream builds. A
	# company-keyed row is a nested warehouse map and simply has no account.
	return row.get("account") or None


def unpostable_transfer_pairs(pairs, account_map) -> list[dict]:
	"""The warehouse pairs that would move stock without writing any stock GL.

	ERPNext writes a stock leg only for a warehouse that resolves through the
	account map, and when both sides resolve to the *same* account the debit and
	credit merge to zero in `process_gl_map` and nothing is written at all.
	Either way the companion Journal Entry stands alone and both branches'
	trial balances are out by the value of the transfer.

	`pairs` is `[(source_warehouse, target_warehouse), …]` in the direction the
	goods actually move. Returns one dict per distinct broken pair, in input
	order, with `reason` ("unmapped" | "shared_account"), both warehouses, the
	`unmapped` ones, and the shared `account` where that is the fault.
	"""
	broken: list[dict] = []
	seen: set[tuple[str, str]] = set()

	for source_wh, target_wh in pairs or []:
		# A blank warehouse is `_validate_warehouses`' business, and its message
		# is the better one; say nothing rather than shout over it.
		if not source_wh or not target_wh:
			continue
		if (source_wh, target_wh) in seen:
			continue
		seen.add((source_wh, target_wh))

		source_account = stock_account(source_wh, account_map)
		target_account = stock_account(target_wh, account_map)

		unmapped = [
			warehouse
			for warehouse, account in ((source_wh, source_account), (target_wh, target_account))
			if not account
		]
		if unmapped:
			# An unresolved side is the more actionable fault, and a pair cannot
			# be both: without two accounts there is nothing to compare.
			broken.append(
				{
					"reason": "unmapped",
					"source_warehouse": source_wh,
					"target_warehouse": target_wh,
					"unmapped": unmapped,
				}
			)
		elif source_account == target_account:
			broken.append(
				{
					"reason": "shared_account",
					"source_warehouse": source_wh,
					"target_warehouse": target_wh,
					"unmapped": [],
					"account": source_account,
				}
			)

	return broken


def account_branch_map(item_rows, account_map, branches, resolve_branch=None) -> dict[str, str]:
	"""{account: branch} for the stock accounts this movement can attribute.

	Built from the document's own warehouse rows, which is the only place the
	attribution actually exists. An account is kept only when every warehouse
	that reaches it belongs to one branch, and that branch is one of `branches`
	— anything ambiguous is dropped so the caller leaves the row alone.

	`resolve_branch` defaults to the app's own warehouse mapping; it is looked
	up when called rather than bound as a default argument, so a caller (and a
	test) can substitute a resolver.
	"""
	resolve = resolve_branch or ib_config.resolve_warehouse_branch
	allowed = {branch for branch in (branches or ()) if branch}

	owners: dict[str, set[str]] = {}
	for row in item_rows or []:
		for warehouse in (row.get("s_warehouse"), row.get("t_warehouse")):
			account = stock_account(warehouse, account_map)
			if not account:
				continue
			branch = resolve(warehouse)
			if not branch:
				continue
			owners.setdefault(account, set()).add(branch)

	return {
		account: next(iter(branch_set))
		for account, branch_set in owners.items()
		if len(branch_set) == 1 and branch_set <= allowed
	}


# ---------------------------------------------------------------------------
# Direct Stock Entry
# ---------------------------------------------------------------------------


def stock_entry_branch_pair(stock_entry) -> tuple[str | None, str | None, float]:
	"""Return (source_branch, target_branch, value) for a single-pair transfer.

	A Stock Entry that crosses several branch pairs at once is skipped and
	logged: splitting it is a business decision, and guessing which pair owes
	which would post a wrong obligation.
	"""
	pairs: dict[tuple[str, str], float] = {}

	for item in stock_entry.items or []:
		source_wh = item.get("s_warehouse")
		target_wh = item.get("t_warehouse")
		if not source_wh or not target_wh:
			continue
		source_branch = ib_config.resolve_warehouse_branch(source_wh)
		target_branch = ib_config.resolve_warehouse_branch(target_wh)
		if not source_branch or not target_branch:
			continue
		amount = flt(item.get("basic_amount")) or flt(item.get("qty")) * flt(item.get("basic_rate"))
		pairs[(source_branch, target_branch)] = pairs.get((source_branch, target_branch), 0.0) + amount

	if not pairs:
		return None, None, 0.0

	if len(pairs) > 1:
		frappe.log_error(
			title="Inter-branch Stock Entry skipped (multiple branch pairs)",
			message=(
				f"Stock Entry {stock_entry.name} moves stock across several branch pairs: "
				f"{sorted(pairs)}. Split it into one pair per document to get the "
				f"inter-branch Journal Entry."
			),
		)
		return None, None, 0.0

	(source, target), total = next(iter(pairs.items()))
	return source, target, round(total, 2)


def retag_stock_entry_gl(stock_entry, source_branch: str, target_branch: str) -> None:
	"""Point each GL leg at the branch that owns its warehouse.

	ERPNext copies one branch onto every GL row of the entry. For a
	cross-branch transfer the credit belongs to the source branch and the debit
	to the target, or neither branch's trial balance stands up on its own.

	Attribution comes from the entry's own item rows. It used to come from
	`frappe.db.get_value("Warehouse", {"account": row.account})`, but that
	reverse lookup is not a function: warehouses routinely share a stock account
	(the company-default fallback), and it silently returned whichever row
	MariaDB handed back first — tagging the GL with an arbitrary branch on
	exactly the sites where getting it right matters most.

	Only accounts that attribute unambiguously are written. Everything else —
	expense heads, rounding differences, additional-cost legs, an account both
	branches reach — keeps whatever ERPNext stamped, which is a better answer
	than a coin-flip between two branches.
	"""
	account_map = get_stock_account_map(stock_entry.company)
	if not account_map:
		frappe.log_error(
			title="Inter-branch GL re-tag skipped — no warehouse account map",
			message=(
				f"No stock accounts resolved for company {stock_entry.company}, so the GL rows of "
				f"{stock_entry.doctype} {stock_entry.name} were left with the branch ERPNext "
				f"stamped on them."
			),
		)
		return

	# Resolve each DISTINCT warehouse once. `resolve_warehouse_branch` is a query
	# per call and both the attribution below and the unattributed sweep walk the
	# same warehouses, so a ten-row entry over one pair was costing twenty
	# lookups for two answers. Side-effect-free, so hoisting it changes nothing
	# but the query count.
	branch_of = {
		warehouse: ib_config.resolve_warehouse_branch(warehouse)
		for warehouse in {
			warehouse
			for row in stock_entry.items or []
			for warehouse in (row.get("s_warehouse"), row.get("t_warehouse"))
			if warehouse
		}
	}

	mapping = account_branch_map(
		stock_entry.items,
		account_map,
		(source_branch, target_branch),
		resolve_branch=branch_of.get,
	)

	# Every leg left unattributed keeps the single branch ERPNext stamped on the
	# whole voucher — which is the defect this function exists to correct — so a
	# partial or empty attribution is worth a line in the log even though there
	# is nothing safe to write. Only accounts reached by a warehouse of *this*
	# branch pair are named — the same scoping the submit guard applies to its
	# rows. Expense heads and rounding legs are never ours, and neither is a row
	# that moves stock between warehouses of no branch at all: it rides on the
	# same document by coincidence, ERPNext's stamp on it is not wrong, and
	# naming it here would raise an alarm about trial balances it cannot affect.
	# An empty `mapping` with nothing to name means no warehouse resolved to an
	# account at all, which is how the company-keyed map shape arrives here —
	# it reads as truthy above, so it never trips the empty-map branch.
	own: set[str] = set()
	for row in stock_entry.items or []:
		for warehouse in (row.get("s_warehouse"), row.get("t_warehouse")):
			account = stock_account(warehouse, account_map)
			# `mapping` first: an attributed account needs no branch lookup, so
			# the healthy document costs no query here at all.
			if not account or account in mapping:
				continue
			if branch_of.get(warehouse) in (source_branch, target_branch):
				own.add(account)

	unattributed = sorted(own)
	if unattributed or not mapping:
		frappe.log_error(
			title="Inter-branch GL re-tag incomplete — accounts not attributed",
			message=(
				f"{stock_entry.doctype} {stock_entry.name}: "
				f"{', '.join(unattributed) or 'no warehouse resolved to a stock account'} — "
				f"not owned by exactly one of ({source_branch}, {target_branch}). Those GL rows "
				f"keep the branch ERPNext stamped on them, so the two branches' trial balances "
				f"will not stand up on their own. Two branch warehouses sharing one stock "
				f"account is the usual cause."
			),
		)

	if not mapping:
		return

	rows = frappe.get_all(
		"GL Entry",
		filters={
			"voucher_type": stock_entry.doctype,
			"voucher_no": stock_entry.name,
			"is_cancelled": 0,
		},
		fields=["name", "account"],
	)
	for row in rows:
		branch = mapping.get(row.account)
		if not branch:
			continue
		frappe.db.set_value("GL Entry", row.name, "branch", branch, update_modified=False)


def on_stock_entry_submit(doc, method=None) -> None:
	"""Post the companion JE for a cross-branch Material Transfer.

	Skipped when the entry came from an Inter Branch Stock Transfer (that
	controller posts its own JE), when the company is pre-cut-over, or when a
	companion already exists for this entry.
	"""
	if doc.doctype != "Stock Entry" or not doc.company:
		return
	if doc.flags.get("from_inter_branch_stock_transfer"):
		return
	if doc.purpose not in INTER_BRANCH_SE_PURPOSES:
		return

	settings = ib_config.get_settings()
	if not settings.enabled or not settings.handle_direct_stock_entry:
		return
	if ib_config.is_pre_cut_over(doc.company, doc.posting_date):
		return
	if not frappe.get_cached_value("Company", doc.company, "enable_perpetual_inventory"):
		# No stock GL is written without perpetual inventory, so a companion JE
		# would leave both branches unbalanced. Skip rather than throw — this
		# hook fires on Stock Entries that have nothing to do with us.
		frappe.log_error(
			title="Inter-branch Stock Entry skipped — perpetual inventory off",
			message=(
				f"Stock Entry {doc.name} crosses branches, but company {doc.company} keeps "
				f"inventory on a periodic basis. No inter-branch Journal Entry was posted: "
				f"it would have no stock legs to offset."
			),
		)
		return
	if ib_journal.find_companion_jes(doc.doctype, doc.name):
		return

	source_branch, target_branch, amount = stock_entry_branch_pair(doc)
	if not source_branch or not target_branch or source_branch == target_branch:
		return

	# Only the rows the obligation was computed from: both warehouses resolving
	# to a branch, and to *this* pair — exactly what `stock_entry_branch_pair`
	# counted. A row that moves stock between warehouses of no branch at all
	# rides on the same document by coincidence; vetoing on its account would
	# suppress a legitimate companion Journal Entry and leave the cross-branch
	# stock GL unoffset, which is this guard's own bug arrived at from the
	# other side.
	#
	# `resolve_warehouse_branch` is an uncached two-table join, so it is asked
	# once per *distinct* warehouse rather than twice per row: this hook fires on
	# every Material Transfer on the site.
	branch_of = {
		warehouse: ib_config.resolve_warehouse_branch(warehouse)
		for warehouse in {
			w for item in doc.items or [] for w in (item.get("s_warehouse"), item.get("t_warehouse")) if w
		}
	}
	own_pairs = [
		(item.get("s_warehouse"), item.get("t_warehouse"))
		for item in doc.items or []
		if branch_of.get(item.get("s_warehouse")) == source_branch
		and branch_of.get(item.get("t_warehouse")) == target_branch
	]
	broken = unpostable_transfer_pairs(own_pairs, get_stock_account_map(doc.company))
	if broken:
		# Perpetual inventory is on but no stock GL will be written anyway:
		# either a warehouse resolves to no account, or both resolve to the same
		# one and the two legs cancel out. Booking the obligation on top of that
		# is the bug itself. Skip rather than throw — this hook fires on every
		# Material Transfer on the site, same reasoning as the branch above.
		frappe.log_error(
			title="Inter-branch Stock Entry skipped — stock accounts not postable",
			message=(
				f"Stock Entry {doc.name} crosses branches, but its warehouses write no stock GL: "
				f"{broken}. No inter-branch Journal Entry was posted; it would have nothing to "
				f"offset it. Give each branch's warehouse its own stock account."
			),
		)
		return

	retag_stock_entry_gl(doc, source_branch, target_branch)
	ib_journal.post_obligation(
		company=doc.company,
		posting_date=doc.posting_date,
		debtor_branch=target_branch,
		creditor_branch=source_branch,
		amount=amount,
		source_doctype=doc.doctype,
		source_docname=doc.name,
	)


def on_stock_entry_cancel(doc, method=None) -> None:
	"""Cancel the companion JE when the Stock Entry is cancelled."""
	if doc.doctype != "Stock Entry":
		return
	try:
		ib_journal.cancel_companion_jes(doc.doctype, doc.name)
	except Exception:
		frappe.log_error(
			title="Inter-branch companion JE cancel failed (Stock Entry)",
			message=frappe.get_traceback(),
		)
		raise
