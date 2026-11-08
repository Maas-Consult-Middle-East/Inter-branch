"""Install / migrate entrypoints.

Everything here is idempotent: `after_migrate` runs on every bench migrate, and
a half-applied setup on a production site is worse than no setup at all.
"""

from __future__ import annotations

import frappe

from inter_branch.core import accounts as ib_accounts
from inter_branch.core import config as ib_config
from inter_branch.setup.custom_fields import ensure_branch_columns, setup_custom_fields
from inter_branch.setup.dimension import ensure_branch_dimension


def after_install() -> None:
	setup_custom_fields()
	_ensure_settings_exists()
	frappe.db.commit()


def after_migrate() -> None:
	setup_custom_fields()
	_ensure_settings_exists()
	ensure_branch_columns()
	ensure_branch_dimension()
	_ensure_company_accounts()
	frappe.db.commit()


def _ensure_settings_exists() -> None:
	"""Materialise the single doc so its defaults are readable straight away.

	Until a Single is saved once, `tabSingles` holds nothing for it and every
	default reads back as empty — which would look like "inter-branch is off"
	on a freshly installed site.
	"""
	if frappe.db.get_singles_dict("Inter Branch Settings"):
		return

	settings = frappe.get_single("Inter Branch Settings")
	settings.flags.ignore_permissions = True
	settings.save(ignore_permissions=True)


def _ensure_company_accounts() -> None:
	"""Create the inter-branch parent groups for every enabled company.

	Leaves stay lazy — they are created on first use, or when the operator sets
	them by hand on Inter Branch Configuration.
	"""
	for company in ib_config.enabled_companies():
		try:
			ib_accounts.ensure_inter_branch_groups(company)
		except Exception:
			frappe.log_error(
				title=f"inter_branch: could not create account groups for {company}",
				message=frappe.get_traceback(),
			)
