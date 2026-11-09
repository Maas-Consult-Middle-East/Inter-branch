"""Custom Fields this app owns.

Created programmatically rather than shipped as fixtures: a product app has to
install cleanly onto a site that already has its own fixtures, and
`create_custom_fields` is idempotent on every migrate.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field, create_custom_fields

SOURCE_FIELDS = [
	{
		"fieldname": "custom_inter_branch_section",
		"fieldtype": "Section Break",
		"label": "Inter-Branch",
		"collapsible": 1,
		"insert_after": "user_remark",
	},
	{
		"fieldname": "custom_source_doctype",
		"fieldtype": "Link",
		"label": "Inter-Branch Source Document Type",
		"options": "DocType",
		"read_only": 1,
		"no_copy": 1,
		"insert_after": "custom_inter_branch_section",
	},
	{
		"fieldname": "custom_source_docname",
		"fieldtype": "Dynamic Link",
		"label": "Inter-Branch Source Document",
		"options": "custom_source_doctype",
		"read_only": 1,
		"no_copy": 1,
		"insert_after": "custom_source_doctype",
	},
]

CUSTOM_FIELDS = {
	"Journal Entry": SOURCE_FIELDS,
	"Journal Entry Account": [
		{
			"fieldname": "custom_inter_company_account",
			"fieldtype": "Link",
			"label": "Related Company Account",
			"options": "Account",
			"print_hide": 1,
			"insert_after": "account",
			"description": (
				"Only for a Related Party Account line: the Related Company's account the mirror "
				"entry books against. Leave blank to use this entry's other lines on the same side."
			),
		},
		{
			"fieldname": "custom_auto_inserted",
			"fieldtype": "Check",
			"label": "Auto-inserted Inter-Branch Leg",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "user_remark",
		},
		{
			"fieldname": "custom_source_doctype",
			"fieldtype": "Link",
			"label": "Inter-Branch Source Document Type",
			"options": "DocType",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "custom_auto_inserted",
		},
		{
			"fieldname": "custom_source_docname",
			"fieldtype": "Dynamic Link",
			"label": "Inter-Branch Source Document",
			"options": "custom_source_doctype",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "custom_source_doctype",
		},
	],
	"Account": [
		{
			"fieldname": "custom_exclude_from_turnover",
			"fieldtype": "Check",
			"label": "Exclude from Turnover",
			"insert_after": "account_type",
			"description": (
				"Set on inter-branch recharge accounts. Revenue KPIs and statutory reports "
				"that sum Income must filter these out — a recharge is not turnover."
			),
		}
	],
}

# Stock-side child tables that must carry the Branch dimension column. ERPNext
# adds these when the Accounting Dimension is saved, but it has been observed
# to skip individual child doctypes; without the column the value is set in
# memory, never persisted, and per-line dimension enforcement is bypassed.
BRANCH_COLUMN_TARGETS = [
	("Sales Invoice Item", "warehouse"),
	("Delivery Note Item", "warehouse"),
	("Purchase Invoice Item", "warehouse"),
	("Purchase Receipt Item", "warehouse"),
	("Stock Entry Detail", "t_warehouse"),
	("Stock Reconciliation Item", "warehouse"),
	("Material Request Item", "warehouse"),
	("POS Invoice Item", "warehouse"),
]


def setup_custom_fields() -> None:
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def ensure_branch_columns() -> None:
	"""Add a `branch` Custom Field to stock child tables that lack one."""
	for doctype, insert_after in BRANCH_COLUMN_TARGETS:
		if not frappe.db.exists("DocType", doctype):
			continue
		if "branch" in frappe.db.get_table_columns(doctype):
			continue
		try:
			create_custom_field(
				doctype,
				{
					"fieldname": "branch",
					"label": "Branch",
					"fieldtype": "Link",
					"options": "Branch",
					"insert_after": insert_after,
				},
			)
		except Exception:
			frappe.log_error(
				title=f"inter_branch: could not add branch field on {doctype}",
				message=frappe.get_traceback(),
			)
