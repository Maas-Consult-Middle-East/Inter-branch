"""Chart-of-Accounts plumbing for inter-branch postings.

Two families of accounts:

* **Balance sheet pair** — `Due from <Branch>` (Asset) and `Due to <Branch>`
  (Liability). One pair per branch, keyed by the *counterparty*. Stock and
  service documents both move these, which is why the reconciliation report
  needs no knowledge of the document that produced a balance.
* **Recharge pair** — `Inter-Branch Service Recovered` (Income) and
  `Inter-Branch Service Charged` (Expense). Equal and opposite by
  construction, so they eliminate structurally at consolidation. Recovered is
  **not turnover**; both are stamped with `custom_exclude_from_turnover` so a
  revenue KPI can filter them out instead of relying on a close checklist.

Every account head is configurable. When a Branch has none set, the app
creates one and writes it back to the branch's Inter Branch Configuration, so
what got created is visible and re-pointable afterwards.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder import DocType

from inter_branch.core import config as ib_config

ROOT_PARENT_FALLBACK = {
	"Asset": "Current Assets",
	"Liability": "Current Liabilities",
	"Income": "Indirect Income",
	"Expense": "Indirect Expenses",
}


def company_abbr(company: str) -> str:
	return frappe.get_cached_value("Company", company, "abbr")


def company_currency(company: str) -> str:
	return frappe.get_cached_value("Company", company, "default_currency")


def find_parent_group(company: str, root_type: str, fallback_label: str | None = None) -> str:
	"""Best parent group for a new account of `root_type`.

	Order: `<fallback_label> - <abbr>` → any group with that account_name →
	the company's root group for the root type. Charts that number their
	accounts ("1100-1600 - Current Assets - CL") only match on the second try,
	which is why the plain name lookup is not enough on its own.
	"""
	fallback_label = fallback_label or ROOT_PARENT_FALLBACK.get(root_type)
	abbr = company_abbr(company)

	if fallback_label:
		candidate = f"{fallback_label} - {abbr}"
		if frappe.db.exists("Account", candidate):
			return candidate

		by_account_name = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_name": fallback_label,
				"is_group": 1,
				"root_type": root_type,
			},
			"name",
		)
		if by_account_name:
			return by_account_name

	account = DocType("Account")
	root = (
		frappe.qb.from_(account)
		.select(account.name)
		.where(
			(account.company == company)
			& (account.root_type == root_type)
			& (account.is_group == 1)
			& (account.parent_account.isnull() | (account.parent_account == ""))
		)
		.limit(1)
	).run()

	if root:
		return root[0][0]

	frappe.throw(
		_("Cannot locate a root {0} group for company {1}.").format(root_type, company),
		title=_("Chart of Accounts Incomplete"),
	)


def _slug(text: str) -> str:
	"""Keep account labels to characters ERPNext is happy to name a doc with."""
	return "".join(ch for ch in text if ch.isalnum() or ch == " ").strip()


def ensure_group_account(company: str, label: str, root_type: str, parent: str) -> str:
	abbr = company_abbr(company)
	name = f"{label} - {abbr}"
	if frappe.db.exists("Account", name):
		return name

	account = frappe.new_doc("Account")
	account.account_name = label
	account.company = company
	account.parent_account = parent
	account.is_group = 1
	account.root_type = root_type
	account.insert(ignore_permissions=True)
	return account.name


def ensure_leaf_account(
	company: str,
	label: str,
	root_type: str,
	parent: str,
	exclude_from_turnover: bool = False,
) -> str:
	abbr = company_abbr(company)
	name = f"{label} - {abbr}"
	if frappe.db.exists("Account", name):
		return name

	account = frappe.new_doc("Account")
	account.account_name = label
	account.company = company
	account.parent_account = parent
	account.is_group = 0
	account.root_type = root_type
	# account_type is deliberately left blank: a Receivable / Payable typed
	# account forces a Party on every Journal Entry line, and inter-branch
	# legs have no party.
	account.account_currency = company_currency(company)
	if exclude_from_turnover and account.meta.get_field("custom_exclude_from_turnover"):
		account.custom_exclude_from_turnover = 1
	account.insert(ignore_permissions=True)
	return account.name


def ensure_inter_branch_groups(company: str) -> dict[str, str]:
	"""Create (idempotently) the parent groups the leaves hang under.

	Honours the per-company overrides on Inter Branch Settings; falls back to
	Current Assets / Current Liabilities the way a stock ERPNext chart is laid
	out.
	"""
	settings = ib_config.get_settings()
	row = ib_config.get_company_setting(company)

	receivable_parent = (row.receivable_parent_account if row else None) or ensure_group_account(
		company,
		settings.receivable_group_label or "Inter-Branch Receivable",
		"Asset",
		find_parent_group(company, "Asset"),
	)
	payable_parent = (row.payable_parent_account if row else None) or ensure_group_account(
		company,
		settings.payable_group_label or "Inter-Branch Payable",
		"Liability",
		find_parent_group(company, "Liability"),
	)
	return {"receivable": receivable_parent, "payable": payable_parent}


# ---------------------------------------------------------------------------
# Branch account heads
# ---------------------------------------------------------------------------


def _write_back(branch: str, fieldname: str, value: str) -> None:
	"""Persist an auto-created head onto the branch configuration.

	A direct `db.set_value`, not `save()` — the caller is usually mid-submit on
	another document and a full save here would fight its timestamp.
	"""
	frappe.db.set_value(ib_config.CONFIG_DOCTYPE, branch, fieldname, value)
	frappe.clear_document_cache(ib_config.CONFIG_DOCTYPE, branch)


def _auto_create_allowed(company: str) -> bool:
	row = ib_config.get_company_setting(company)
	return bool(row and row.auto_create_accounts)


def get_due_from_account(company: str, branch: str) -> str:
	"""Asset head debited by a counterparty when `branch` owes it."""
	config = ib_config.get_branch_config(branch, throw=True)
	if config.due_from_account:
		return config.due_from_account

	if not _auto_create_allowed(company):
		frappe.throw(
			_("Branch {0} has no Due-from Account and auto-creation is off for company {1}.").format(
				branch, company
			),
			title=_("Account Head Missing"),
		)

	settings = ib_config.get_settings()
	groups = ensure_inter_branch_groups(company)
	prefix = settings.due_from_prefix or "Due from"
	label = "{0} {1}".format(prefix, _slug(config.branch_abbr or branch))
	account = ensure_leaf_account(company, label, "Asset", groups["receivable"])
	_write_back(branch, "due_from_account", account)
	return account


def get_due_to_account(company: str, branch: str) -> str:
	"""Liability head credited by a counterparty when `branch` is owed."""
	config = ib_config.get_branch_config(branch, throw=True)
	if config.due_to_account:
		return config.due_to_account

	if not _auto_create_allowed(company):
		frappe.throw(
			_("Branch {0} has no Due-to Account and auto-creation is off for company {1}.").format(
				branch, company
			),
			title=_("Account Head Missing"),
		)

	settings = ib_config.get_settings()
	groups = ensure_inter_branch_groups(company)
	prefix = settings.due_to_prefix or "Due to"
	label = "{0} {1}".format(prefix, _slug(config.branch_abbr or branch))
	account = ensure_leaf_account(company, label, "Liability", groups["payable"])
	_write_back(branch, "due_to_account", account)
	return account


def get_service_recovered_account(company: str, performing_branch: str) -> str:
	"""Income-side recharge head credited on the branch that did the work."""
	config = ib_config.get_branch_config(performing_branch)
	if config and config.service_recovered_account:
		return config.service_recovered_account

	row = ib_config.get_company_setting(company, throw=True)
	if row.service_recovered_account:
		return row.service_recovered_account

	if not row.auto_create_accounts:
		frappe.throw(
			_("No Service Recovered Account is configured for company {0}.").format(company),
			title=_("Account Head Missing"),
		)

	settings = ib_config.get_settings()
	return ensure_leaf_account(
		company,
		settings.service_recovered_label or "Inter-Branch Service Recovered",
		"Income",
		find_parent_group(company, "Income"),
		exclude_from_turnover=True,
	)


def get_service_charged_account(company: str, receiving_branch: str) -> str:
	"""Expense-side recharge head debited on the branch that benefited."""
	config = ib_config.get_branch_config(receiving_branch)
	if config and config.service_charged_account:
		return config.service_charged_account

	row = ib_config.get_company_setting(company, throw=True)
	if row.service_charged_account:
		return row.service_charged_account

	if not row.auto_create_accounts:
		frappe.throw(
			_("No Service Charged Account is configured for company {0}.").format(company),
			title=_("Account Head Missing"),
		)

	settings = ib_config.get_settings()
	return ensure_leaf_account(
		company,
		settings.service_charged_label or "Inter-Branch Service Charged",
		"Expense",
		find_parent_group(company, "Expense"),
		exclude_from_turnover=True,
	)


def account_currency(company: str, account: str) -> str:
	return frappe.get_cached_value("Account", account, "account_currency") or company_currency(company)


def inter_branch_accounts(company: str) -> dict[str, str]:
	"""Map every configured branch head to the branch it represents.

	The reconciliation report reads this instead of parsing account names, so a
	client that renamed the heads still reconciles.
	"""
	config = DocType(ib_config.CONFIG_DOCTYPE)
	rows = (
		frappe.qb.from_(config)
		.select(config.branch, config.due_from_account, config.due_to_account)
		.where(config.company == company)
	).run(as_dict=True)

	mapping: dict[str, str] = {}
	for row in rows:
		for account in (row.due_from_account, row.due_to_account):
			if account:
				mapping[account] = row.branch
	return mapping
