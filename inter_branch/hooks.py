app_name = "inter_branch"
app_title = "Inter Branch"
app_publisher = "Enfono Technologies"
app_description = "Inter-branch receivables, payables, stock transfers and service settlements for ERPNext"
app_email = "info@enfono.com"
app_license = "proprietary"

required_apps = ["frappe/erpnext"]

# ---------------------------------------------------------------------------
# Install / migrate
# ---------------------------------------------------------------------------

after_install = "inter_branch.install.after_install"
after_migrate = "inter_branch.install.after_migrate"

# The desk Workspace ships as a fixture so it is recreated on every install and
# every migrate, on every site — the app's DocTypes are otherwise reachable only
# by search or a direct URL.
fixtures = [
	{"dt": "Workspace", "filters": [["module", "=", "Inter Branch"]]},
]

# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------

# Stock-side doctypes whose item rows must carry the Branch dimension. The
# branch is resolved from each row's warehouse, then any row still blank
# inherits the header value.
_STOCK_BRANCH_EVENTS = {
	"validate": [
		"inter_branch.core.stock.auto_set_branch_from_warehouse",
		"inter_branch.core.stock.propagate_header_branch_to_items",
	]
}

doc_events = {
	"Journal Entry": {
		"before_validate": "inter_branch.core.inter_company.split_related_party_allocations",
		"validate": [
			"inter_branch.core.journal.auto_inject_inter_branch_legs",
			"inter_branch.core.inter_company.validate_related_party_legs",
		],
		"on_submit": "inter_branch.core.inter_company.post_related_party_mirrors",
		"on_cancel": "inter_branch.core.inter_company.cancel_related_party_mirrors",
	},
	"Branch": {
		"after_insert": "inter_branch.core.branch.on_branch_insert",
	},
	"Stock Entry": {
		"validate": [
			"inter_branch.core.stock.auto_set_branch_from_warehouse",
			"inter_branch.core.stock.propagate_header_branch_to_items",
		],
		"on_submit": "inter_branch.core.stock.on_stock_entry_submit",
		"on_cancel": "inter_branch.core.stock.on_stock_entry_cancel",
	},
	"Stock Reconciliation": _STOCK_BRANCH_EVENTS,
	"Delivery Note": _STOCK_BRANCH_EVENTS,
	"Purchase Receipt": _STOCK_BRANCH_EVENTS,
	"Sales Invoice": _STOCK_BRANCH_EVENTS,
	"Purchase Invoice": _STOCK_BRANCH_EVENTS,
	"Material Request": _STOCK_BRANCH_EVENTS,
}
