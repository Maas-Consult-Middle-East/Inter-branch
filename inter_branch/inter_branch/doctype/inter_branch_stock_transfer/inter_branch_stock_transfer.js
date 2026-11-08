// Copyright (c) 2026, Enfono Technologies and contributors
// For license information, please see license.txt

const METHOD_PATH =
	"inter_branch.inter_branch.doctype.inter_branch_stock_transfer.inter_branch_stock_transfer";

frappe.ui.form.on("Inter Branch Stock Transfer", {
	setup(frm) {
		const warehouse_query = () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		});

		frm.set_query("from_warehouse", warehouse_query);
		frm.set_query("to_warehouse", warehouse_query);
		frm.set_query("s_warehouse", "items", warehouse_query);
		frm.set_query("t_warehouse", "items", warehouse_query);
		frm.set_query("cost_center", "items", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("taxes_and_charges", () => ({
			filters: { company: frm.doc.company },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		["stock_entry", "journal_entry", "delivery_note"].forEach((field) => {
			if (!frm.doc[field]) return;
			const doctype = frappe.meta.get_docfield(frm.doctype, field).options;
			frm.add_custom_button(__(doctype), () => {
				frappe.set_route("Form", doctype, frm.doc[field]);
			}, __("View"));
		});

		if (!frm.doc.journal_entry) {
			// The transfer posted its stock but not its obligation — usually a
			// zero valuation at submit time. Offer the retry rather than
			// leaving the branches unsettled.
			frm.add_custom_button(__("Create Journal Entry"), () => {
				frappe.call({
					method: `${METHOD_PATH}.make_journal_entry`,
					args: { transfer: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		}

		if (!frm.doc.is_return) {
			frm.add_custom_button(__("Return"), () => {
				frappe.call({
					method: `${METHOD_PATH}.make_return`,
					args: { transfer: frm.doc.name },
					freeze: true,
					callback: (r) => {
						if (r.message) frappe.set_route("Form", frm.doctype, r.message);
					},
				});
			}, __("Create"));

			show_return_status(frm);
		}
	},

	company(frm) {
		frm.set_value({ from_warehouse: null, to_warehouse: null });
	},

	from_warehouse(frm) {
		(frm.doc.items || []).forEach((row) => {
			if (!row.s_warehouse) row.s_warehouse = frm.doc.from_warehouse;
		});
		frm.refresh_field("items");
	},
});

frappe.ui.form.on("Inter Branch Stock Transfer Item", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;

		frappe.call({
			method: `${METHOD_PATH}.get_item_rates`,
			args: {
				item_code: row.item_code,
				warehouse: row.s_warehouse || frm.doc.from_warehouse,
				price_list: frm.doc.price_list,
			},
			callback: (r) => {
				if (!r.message) return;
				frappe.model.set_value(cdt, cdn, "valuation_rate", r.message.valuation_rate);
				if (!row.basic_rate) {
					frappe.model.set_value(
						cdt,
						cdn,
						"basic_rate",
						r.message.price_list_rate || r.message.valuation_rate
					);
				}
			},
		});
	},

	qty: (frm, cdt, cdn) => set_amount(cdt, cdn),
	basic_rate: (frm, cdt, cdn) => set_amount(cdt, cdn),
});

function set_amount(cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "basic_amount", flt(row.qty) * flt(row.basic_rate));
}

function show_return_status(frm) {
	frappe.call({
		method: `${METHOD_PATH}.get_return_breakdown`,
		args: { transfer: frm.doc.name },
		callback: (r) => {
			if (!r.message || !r.message.returns.length) return;

			frm.dashboard.add_indicator(
				__("{0} return(s)", [r.message.returns.length]),
				"orange"
			);
			frm.add_custom_button(__("Returns"), () => {
				const rows = r.message.items
					.map(
						(item) =>
							`<tr><td>${item.item_code}</td><td>${item.original_qty}</td>` +
							`<td>${item.returned_qty}</td><td>${item.remaining_qty}</td></tr>`
					)
					.join("");

				frappe.msgprint({
					title: __("Return Breakdown"),
					message:
						`<table class="table table-bordered table-sm"><thead><tr>` +
						`<th>${__("Item")}</th><th>${__("Transferred")}</th>` +
						`<th>${__("Returned")}</th><th>${__("Remaining")}</th>` +
						`</tr></thead><tbody>${rows}</tbody></table>`,
					wide: true,
				});
			}, __("View"));
		},
	});
}
