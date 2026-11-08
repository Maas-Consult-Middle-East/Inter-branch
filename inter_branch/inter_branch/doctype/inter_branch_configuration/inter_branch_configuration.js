// Copyright (c) 2026, Enfono Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Inter Branch Configuration", {
	setup(frm) {
		const ledger_filter = (root_type) => () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
				root_type: root_type,
			},
		});

		frm.set_query("due_from_account", ledger_filter("Asset"));
		frm.set_query("due_to_account", ledger_filter("Liability"));
		frm.set_query("service_recovered_account", ledger_filter("Income"));
		frm.set_query("service_charged_account", ledger_filter("Expense"));

		frm.set_query("default_cost_center", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("warehouse", "warehouses", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("cost_center", "cost_centers", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
	},

	refresh(frm) {
		if (frm.is_new()) return;

		const blank = ["due_from_account", "due_to_account"].filter((f) => !frm.doc[f]);
		if (blank.length) {
			frm.dashboard.set_headline_alert(
				__("Account heads left blank are created automatically on the first inter-branch posting."),
				"blue"
			);
		}
	},
});
