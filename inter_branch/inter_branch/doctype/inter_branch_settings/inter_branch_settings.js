// Copyright (c) 2026, Enfono Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Inter Branch Settings", {
	setup(frm) {
		frm.set_query("related_party_account", "related_parties", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, is_group: 0 } };
		});
		frm.set_query("related_company", "related_parties", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { name: ["!=", row.company || ""] } };
		});
	},
});
