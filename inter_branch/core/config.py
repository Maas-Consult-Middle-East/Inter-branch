"""Configuration lookups for the Inter Branch app.

Everything the posting layer needs to know about a site — which companies are
live, what the cut-over date is, which warehouse belongs to which branch, and
which account heads a branch posts to — is read through this module. Nothing
below it is allowed to hardcode an account name, a warehouse, or a price list;
that is the whole reason this app exists as a product rather than a client
customisation.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder import DocType, Order
from frappe.utils import getdate

SETTINGS_DOCTYPE = "Inter Branch Settings"
CONFIG_DOCTYPE = "Inter Branch Configuration"


def get_settings():
	"""Return the Inter Branch Settings single doc (cached)."""
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def is_enabled() -> bool:
	return bool(get_settings().enabled)


def get_company_setting(company: str, throw: bool = False):
	"""Return the company row from Inter Branch Settings, or None.

	`throw` turns a missing / disabled row into an actionable error instead of
	a silent no-op — used by the document controllers, where the operator has
	explicitly asked for an inter-branch posting.
	"""
	if not company:
		return None

	settings = get_settings()
	if not settings.enabled:
		if throw:
			frappe.throw(
				_("Inter-branch postings are switched off in {0}.").format(_(SETTINGS_DOCTYPE)),
				title=_("Inter-Branch Disabled"),
			)
		return None

	for row in settings.companies or []:
		if row.company == company:
			if not row.enabled:
				if throw:
					frappe.throw(
						_("Company {0} is disabled in {1}.").format(company, _(SETTINGS_DOCTYPE)),
						title=_("Inter-Branch Disabled"),
					)
				return None
			return row

	if throw:
		frappe.throw(
			_("Company {0} has no row in {1}. Add one before posting inter-branch documents.").format(
				company, _(SETTINGS_DOCTYPE)
			),
			title=_("Company Not Configured"),
		)
	return None


def enabled_companies() -> list[str]:
	settings = get_settings()
	if not settings.enabled:
		return []
	return [row.company for row in (settings.companies or []) if row.enabled and row.company]


def is_pre_cut_over(company: str, posting_date=None) -> bool:
	"""True when the document predates the company's cut-over date.

	A blank cut-over date means the automatic posting layer is off for that
	company — the same convention the shipped RMAX implementation uses, so a
	half-configured site never starts writing surprise Journal Entries.
	"""
	row = get_company_setting(company)
	if not row or not row.cut_over_date:
		return True
	if not posting_date:
		return False
	return getdate(posting_date) < getdate(row.cut_over_date)


def get_bridge_branch(company: str) -> str | None:
	row = get_company_setting(company)
	return row.bridge_branch if row else None


def trusted_roles() -> set[str]:
	"""Roles whose Branch choice is never auto-corrected from the warehouse."""
	raw = get_settings().trusted_roles or ""
	return {line.strip() for line in raw.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# Branch configuration
# ---------------------------------------------------------------------------


def get_branch_config(branch: str, throw: bool = False):
	"""Return the Inter Branch Configuration doc for a branch, or None."""
	if not branch:
		return None
	if not frappe.db.exists(CONFIG_DOCTYPE, branch):
		if throw:
			frappe.throw(
				_("Branch {0} has no {1}. Create one and set its account heads.").format(
					branch, _(CONFIG_DOCTYPE)
				),
				title=_("Branch Not Configured"),
			)
		return None

	doc = frappe.get_cached_doc(CONFIG_DOCTYPE, branch)
	if doc.disabled:
		if throw:
			frappe.throw(
				_("{0} for branch {1} is disabled.").format(_(CONFIG_DOCTYPE), branch),
				title=_("Branch Disabled"),
			)
		return None
	return doc


def validate_branch_company(branch: str, company: str) -> None:
	"""Refuse a document whose branch belongs to a different company.

	There is one Inter Branch Configuration per Branch — the DocType is
	autonamed `field:branch` — and it carries a company. So a Branch belongs to
	exactly one Company. Left unchecked this surfaces much later as a confusing
	"account belongs to company X, not Y" from inside Journal Entry validation,
	which sends the operator off to fix the account instead of the branch.
	"""
	if not branch:
		# `get_branch_config` returns None *without* throwing for a falsy branch,
		# so falling through would render "Branch None is configured for company
		# (not set)…" — a description of something that did not happen.
		frappe.throw(
			_(
				"This document is on company {0} but carries no Branch, so there is no {1} "
				"to check it against."
			).format(company, _(CONFIG_DOCTYPE)),
			title=_("Branch Missing"),
		)

	config = get_branch_config(branch, throw=True)
	configured_company = config.company if config else None
	if configured_company and configured_company == company:
		return

	frappe.throw(
		_(
			"Branch {0} is configured for company {1} under {2}, but this document is on "
			"company {3}.<br><br>"
			"A Branch belongs to exactly one company — there is one {2} per Branch. Pick a "
			"Branch that belongs to {3}, or create one."
		).format(branch, configured_company or _("(not set)"), _(CONFIG_DOCTYPE), company),
		title=_("Branch Belongs To Another Company"),
	)


def resolve_warehouse_branch(warehouse: str) -> str | None:
	"""Return the Branch a warehouse belongs to, via Inter Branch Configuration.

	The most recently modified configuration wins when a warehouse is listed on
	more than one branch — shared-warehouse setups are legal but ambiguous, and
	silently picking one beats throwing inside an unrelated stock posting.
	"""
	if not warehouse:
		return None

	config = DocType(CONFIG_DOCTYPE)
	child = DocType("Inter Branch Configuration Warehouse")

	rows = (
		frappe.qb.from_(child)
		.join(config)
		.on(config.name == child.parent)
		.select(config.branch)
		.where((child.warehouse == warehouse) & (config.disabled == 0))
		.orderby(config.modified, order=Order.desc)
		.limit(1)
	).run()

	return rows[0][0] if rows else None


def resolve_branch_cost_center(branch: str) -> str | None:
	"""Default Cost Center for a branch: the explicit field, else the first row."""
	config = get_branch_config(branch)
	if not config:
		return None
	if config.default_cost_center:
		return config.default_cost_center
	for row in config.cost_centers or []:
		if row.cost_center:
			return row.cost_center
	return None


def branch_warehouses(branch: str) -> list[str]:
	config = get_branch_config(branch)
	if not config:
		return []
	return [row.warehouse for row in (config.warehouses or []) if row.warehouse]


def resolve_branch_pair(from_warehouse: str, to_warehouse: str) -> tuple[str, str]:
	"""Resolve both warehouses to branches, or throw an actionable error.

	Called before any stock document is created. Without this the failure
	surfaces much later as ERPNext's opaque "Accounting Dimension Branch is
	required" deep inside GL posting.
	"""
	source = resolve_warehouse_branch(from_warehouse)
	target = resolve_warehouse_branch(to_warehouse)

	missing = []
	if not source:
		missing.append(from_warehouse or "—")
	if not target:
		missing.append(to_warehouse or "—")

	if missing:
		frappe.throw(
			_(
				"No Branch is mapped to warehouse(s): <b>{0}</b>.<br><br>"
				"Open <b>{1}</b> for the branch that owns the warehouse and add it to the "
				"<b>Warehouses</b> table."
			).format(", ".join(missing), _(CONFIG_DOCTYPE)),
			title=_("Warehouse Not Mapped to Branch"),
		)
	return source, target
