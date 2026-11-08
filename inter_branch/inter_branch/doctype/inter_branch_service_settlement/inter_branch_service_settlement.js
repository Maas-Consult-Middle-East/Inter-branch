// Copyright (c) 2026, Enfono Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Inter Branch Service Settlement", {
	setup(frm) {
		frm.set_query("service_recovered_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Income" },
		}));
		frm.set_query("service_charged_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
		frm.set_query("cost_center", "items", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.journal_entry) {
			frm.add_custom_button(__("Journal Entry"), () => {
				frappe.set_route("Form", "Journal Entry", frm.doc.journal_entry);
			}, __("View"));
		}

		if (frm.doc.docstatus === 0) {
			frm.dashboard.set_headline_alert(
				__(
					"Posts a recharge, not a sale: the income leg is excluded from turnover and nets off against the expense leg on consolidation."
				),
				"blue"
			);
		}
	},

	company(frm) {
		frm.set_value({ service_recovered_account: null, service_charged_account: null });
	},

	performing_branch(frm) {
		frm.set_value("service_recovered_account", null);
		preview_accounts(frm);
	},

	receiving_branch(frm) {
		frm.set_value("service_charged_account", null);
		preview_accounts(frm);
	},

	pricing_basis(frm) {
		// Amounts are recomputed server-side; clearing here stops a stale
		// total from being read as the new basis's number.
		frm.set_value("total_amount", 0);
	},
});

frappe.ui.form.on("Inter Branch Service Settlement Item", {
	hours: (frm, cdt, cdn) => recalc_row(frm, cdt, cdn),
	rate: (frm, cdt, cdn) => recalc_row(frm, cdt, cdn),
	cost_amount: (frm, cdt, cdn) => recalc_row(frm, cdt, cdn),
	markup_percentage: (frm, cdt, cdn) => recalc_row(frm, cdt, cdn),
});

function recalc_row(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	let amount = 0;

	if (frm.doc.pricing_basis === "Hours x Rate") {
		amount = flt(row.hours) * flt(row.rate);
	} else if (frm.doc.pricing_basis === "Cost Plus Percentage") {
		const markup = flt(row.markup_percentage) || flt(frm.doc.default_markup_percentage);
		amount = flt(row.cost_amount) * (1 + markup / 100);
	}

	frappe.model.set_value(cdt, cdn, "amount", amount);
	frm.set_value(
		"total_amount",
		(frm.doc.items || []).reduce((sum, item) => sum + flt(item.amount), 0)
	);
}

function preview_accounts(frm) {
	if (!frm.doc.company || !frm.doc.performing_branch || !frm.doc.receiving_branch) return;
	if (frm.doc.performing_branch === frm.doc.receiving_branch) return;

	frappe.call({
		method:
			"inter_branch.inter_branch.doctype.inter_branch_service_settlement.inter_branch_service_settlement.get_branch_accounts",
		args: {
			company: frm.doc.company,
			performing_branch: frm.doc.performing_branch,
			receiving_branch: frm.doc.receiving_branch,
		},
		callback: (r) => {
			if (r.message) frm.set_value(r.message);
		},
	});
}
