"""Branch lifecycle glue."""

from __future__ import annotations

import frappe
from frappe import _

from inter_branch.core import config as ib_config


def on_branch_insert(doc, method=None) -> None:
	"""Create an Inter Branch Configuration stub for a newly added Branch.

	Only when the site has exactly one enabled company — with more than one,
	which company the branch belongs to is a human decision, and a wrong guess
	would point its account heads at the wrong chart.
	"""
	if frappe.db.exists(ib_config.CONFIG_DOCTYPE, doc.name):
		return

	companies = ib_config.enabled_companies()
	if len(companies) != 1:
		if companies:
			frappe.msgprint(
				_("Create an {0} for branch <b>{1}</b> before posting inter-branch documents.").format(
					_(ib_config.CONFIG_DOCTYPE), doc.name
				),
				title=_("Branch Not Configured"),
				indicator="orange",
			)
		return

	config = frappe.new_doc(ib_config.CONFIG_DOCTYPE)
	config.branch = doc.name
	config.company = companies[0]
	config.flags.ignore_permissions = True
	config.insert(ignore_permissions=True)

	frappe.msgprint(
		_(
			"{0} <b>{1}</b> created. Set its warehouses, cost centre and account heads — "
			"account heads left blank are created on first use."
		).format(_(ib_config.CONFIG_DOCTYPE), config.name),
		title=_("Inter-Branch Configuration Created"),
		indicator="orange",
	)
