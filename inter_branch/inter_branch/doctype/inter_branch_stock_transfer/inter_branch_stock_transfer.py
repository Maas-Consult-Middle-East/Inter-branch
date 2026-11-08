# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

"""Inter Branch Stock Transfer.

One document that moves stock between two branches and books the obligation
that movement creates:

* a Material Transfer **Stock Entry** for the physical move,
* a two-leg **Journal Entry** so each branch's trial balance squares,
* optionally a **Delivery Note**, when the receiving branch sells the goods
  onward to a customer.

Everything client-specific — target warehouse, price list, whether a Delivery
Note is wanted at all — comes from Inter Branch Settings, never from a constant
in this file.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today

from inter_branch.core import config as ib_config
from inter_branch.core import journal as ib_journal
from inter_branch.core import stock as ib_stock


class InterBranchStockTransfer(Document):
	# ------------------------------------------------------------------
	# Validate
	# ------------------------------------------------------------------

	def validate(self):
		self._apply_company_defaults()
		self._set_branches()
		self._validate_branch_companies()
		self._set_item_warehouses()
		self._set_rates_and_totals()
		self._validate_warehouses()
		if self.is_return and self.return_against:
			self._validate_return_quantities()

	def _apply_company_defaults(self):
		row = ib_config.get_company_setting(self.company, throw=True)

		if not self.to_warehouse and row.default_target_warehouse:
			self.to_warehouse = row.default_target_warehouse
		if not self.price_list and row.transfer_price_list:
			self.price_list = row.transfer_price_list
		if self.is_new() and not self.taxes_and_charges and row.default_taxes_and_charges:
			self.taxes_and_charges = row.default_taxes_and_charges

	def _set_branches(self):
		self.from_branch, self.to_branch = ib_config.resolve_branch_pair(
			self.from_warehouse, self.to_warehouse
		)
		if self.from_branch == self.to_branch:
			frappe.throw(
				_("Source and target warehouse both belong to branch {0}. Use a plain Stock Entry.").format(
					self.from_branch
				),
				title=_("Same Branch"),
			)

	def _validate_branch_companies(self):
		"""Both branches must belong to this document's company.

		Runs immediately after the branches are resolved, and before rates and
		warehouses, so an operator who picked a branch from another company gets
		that as the reason instead of an account-level complaint from deep
		inside Journal Entry validation.
		"""
		ib_config.validate_branch_company(self.from_branch, self.company)
		ib_config.validate_branch_company(self.to_branch, self.company)

	def _set_item_warehouses(self):
		for item in self.items or []:
			if not item.s_warehouse:
				item.s_warehouse = self.from_warehouse
			if not item.t_warehouse:
				item.t_warehouse = self.to_warehouse

	def _set_rates_and_totals(self):
		"""Rate order: what the user typed → price list → valuation.

		Valuation is the honest default for an internal move; a price list is
		only consulted when the client configured one, because transferring at
		selling price would move margin between branches.
		"""
		total_qty = 0.0
		total_amount = 0.0

		for item in self.items or []:
			item.valuation_rate = get_valuation_rate(item.item_code, item.s_warehouse)

			if not flt(item.basic_rate):
				rate = get_price_list_rate(item.item_code, self.price_list) if self.price_list else 0.0
				item.basic_rate = rate or item.valuation_rate

			item.conversion_factor = flt(item.conversion_factor) or 1.0
			item.transfer_qty = flt(item.qty) * item.conversion_factor
			item.basic_amount = flt(item.qty) * flt(item.basic_rate)

			total_qty += flt(item.qty)
			total_amount += flt(item.basic_amount)

		self.total_qty = total_qty
		self.total_amount = total_amount

	def _validate_warehouses(self):
		for item in self.items or []:
			if not item.s_warehouse or not item.t_warehouse:
				frappe.throw(
					_("Row {0}: both source and target warehouse are required for {1}.").format(
						item.idx, item.item_code
					),
					title=_("Warehouse Missing"),
				)
			if item.s_warehouse == item.t_warehouse:
				frappe.throw(
					_("Row {0}: source and target warehouse are the same for {1}.").format(
						item.idx, item.item_code
					),
					title=_("Same Warehouse"),
				)

	def _validate_return_quantities(self):
		original_qty = {
			item.item_code: flt(item.qty)
			for item in frappe.get_doc(self.doctype, self.return_against).items or []
		}
		already_returned = returned_qty_map(self.return_against, exclude=self.name)

		for item in self.items or []:
			remaining = original_qty.get(item.item_code, 0.0) - already_returned.get(item.item_code, 0.0)
			if flt(item.qty) > remaining + 0.001:
				frappe.throw(
					_("Row {0}: return qty {1} for {2} exceeds the {3} still returnable.").format(
						item.idx, item.qty, item.item_code, round(remaining, 3)
					),
					title=_("Return Quantity Exceeded"),
				)

	# ------------------------------------------------------------------
	# Submit
	# ------------------------------------------------------------------

	def before_submit(self):
		self._validate_perpetual_inventory()
		self._validate_stock_accounts()
		self._validate_stock_availability()

	def _validate_perpetual_inventory(self):
		"""Refuse to post when the company keeps inventory on a periodic basis.

		Without perpetual inventory a Material Transfer writes no GL at all, so
		the companion Journal Entry would stand alone: the sending branch gets a
		receivable and the receiving branch a payable, with nothing on either
		side to offset them. Each branch's trial balance would then be out by
		the value of every transfer — the exact failure this app exists to
		prevent. Better to stop here than to book it.
		"""
		if frappe.get_cached_value("Company", self.company, "enable_perpetual_inventory"):
			return

		frappe.throw(
			_(
				"Company {0} does not have <b>Perpetual Inventory</b> enabled, so a stock "
				"transfer posts no stock GL entries. The inter-branch obligation would have "
				"nothing to offset it and both branches' trial balances would be wrong.<br><br>"
				"Enable Perpetual Inventory on the Company (and set its stock accounts) before "
				"using inter-branch stock transfers. Service settlements are unaffected."
			).format(self.company),
			title=_("Perpetual Inventory Required"),
		)

	def _validate_stock_accounts(self):
		"""Refuse to post when the movement would write no stock GL at all.

		Perpetual inventory is necessary but not sufficient. ERPNext writes a
		stock leg only for a warehouse that resolves through the warehouse
		account map, and when both warehouses resolve to the *same* account the
		two legs merge to zero in `process_gl_map` and nothing is written at
		all. Either way the companion Journal Entry stands alone and each
		branch's trial balance is out by the value of the transfer — the exact
		failure this app exists to prevent, so it is refused rather than booked.

		Pairs are built through `_row_warehouses`, so a return is checked in the
		direction the goods actually move and the message names the real source.
		"""
		account_map = ib_stock.get_stock_account_map(self.company)
		broken = ib_stock.unpostable_transfer_pairs(
			[self._row_warehouses(item) for item in self.items or []], account_map
		)
		if not broken:
			return

		header = "<thead><tr><th>{0}</th><th>{1}</th><th>{2}</th></tr></thead>".format(
			_("Source Warehouse"), _("Target Warehouse"), _("Problem")
		)
		rows = []
		for pair in broken:
			if pair["reason"] == "shared_account":
				problem = _(
					"Both warehouses post to account <b>{0}</b>, so the transfer's stock legs "
					"cancel out and ERPNext writes no GL at all. Give each branch's warehouse "
					"its own stock account."
				).format(pair["account"])
			else:
				problem = _(
					"No stock account resolves for <b>{0}</b>. Give that Warehouse its own "
					"<b>Account</b>, or Company <b>{1}</b> a <b>Default Inventory Account</b>."
				).format(", ".join(pair["unmapped"]), self.company)
			rows.append(
				"<tr><td>{0}</td><td>{1}</td><td>{2}</td></tr>".format(
					pair["source_warehouse"], pair["target_warehouse"], problem
				)
			)

		frappe.throw(
			_(
				"This transfer would move stock without writing any stock GL, so its companion "
				"Journal Entry would stand alone and each branch's trial balance would be out "
				"by the value of the transfer:"
			)
			+ "<br><br><table class='table table-bordered table-sm'>"
			+ header
			+ "<tbody>"
			+ "".join(rows)
			+ "</tbody></table>",
			title=_("Stock Account Not Postable"),
		)

	def _validate_stock_availability(self):
		"""Check the warehouse the goods actually leave — which flips on a return."""
		needed: dict[tuple[str, str], float] = defaultdict(float)
		for item in self.items or []:
			warehouse = item.t_warehouse if self.is_return else item.s_warehouse
			if item.item_code and warehouse:
				needed[(warehouse, item.item_code)] += flt(item.qty)

		shortages = []
		for (warehouse, item_code), qty in needed.items():
			available = flt(
				frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
			)
			if available < qty:
				shortages.append((item_code, warehouse, qty, available))

		if not shortages:
			return

		header = "<thead><tr><th>{0}</th><th>{1}</th><th>{2}</th><th>{3}</th></tr></thead>".format(
			_("Item"), _("Warehouse"), _("Required"), _("Available")
		)
		body = "".join(
			"<tr><td>{0}</td><td>{1}</td><td>{2}</td><td style='color:red'>{3}</td></tr>".format(*row)
			for row in shortages
		)
		frappe.throw(
			_("Not enough stock in the source warehouse:")
			+ "<br><br><table class='table table-bordered table-sm'>"
			+ header
			+ "<tbody>"
			+ body
			+ "</tbody></table>",
			title=_("Insufficient Stock"),
		)

	def on_submit(self):
		self.db_set("status", "Return" if self.is_return else "Submitted")
		self._create_stock_entry()

		row = ib_config.get_company_setting(self.company, throw=True)
		if self.customer and row.create_delivery_note:
			self._create_delivery_note()

		self._create_journal_entry()

		if self.is_return and self.return_against:
			frappe.db.set_value(self.doctype, self.return_against, "return_ibst", self.name)

	def _create_stock_entry(self):
		"""Post the physical move, then correct its per-leg branch tags.

		ERPNext stamps one branch on both GL legs of a transfer; the credit
		belongs to the sending branch and the debit to the receiving one.
		"""
		from inter_branch.core.stock import retag_stock_entry_gl

		source_branch, target_branch = self._movement_branches()

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.company = self.company
		se.set_posting_time = 1
		se.posting_date = self.posting_date
		se.posting_time = self.posting_time
		se.branch = source_branch
		se.remarks = _("Created from {0} {1}").format(_(self.doctype), self.name)

		for item in self.items or []:
			source_wh, target_wh = self._row_warehouses(item)
			se.append(
				"items",
				{
					"item_code": item.item_code,
					"qty": item.qty,
					"uom": item.uom,
					"conversion_factor": item.conversion_factor or 1,
					"s_warehouse": source_wh,
					"t_warehouse": target_wh,
					"basic_rate": item.basic_rate,
					"allow_zero_valuation_rate": item.allow_zero_valuation_rate,
					"serial_no": item.serial_no,
					"batch_no": item.batch_no,
					"cost_center": item.cost_center,
					"branch": source_branch,
				},
			)

		# Keep the Stock Entry hook out of this: the transfer posts its own
		# Journal Entry, and two companions for one movement would double the
		# obligation.
		se.flags.from_inter_branch_stock_transfer = True
		se.flags.ignore_permissions = True
		se.insert(ignore_permissions=True)
		se.flags.from_inter_branch_stock_transfer = True
		se.submit()

		retag_stock_entry_gl(se, source_branch, target_branch)

		self.db_set("stock_entry", se.name, notify=True)
		self._sync_values_from_stock_entry(se)
		_created_message(_("Stock Entry"), "stock-entry", se.name)

	def _sync_values_from_stock_entry(self, se) -> None:
		"""Copy the rates ERPNext actually valued the move at back onto this doc.

		The draft prices items from the Bin's `valuation_rate` snapshot, but the
		Stock Entry books them at the real moving-average / FIFO rate — and the
		companion Journal Entry follows the Stock Entry. Left unsynced, the
		transfer would display one total while its Journal Entry posted another.

		Written with `db.set_value` on the child rows: the parent is submitted by
		this point, and saving it again would fight its own timestamp.
		"""
		se_rows = list(se.items or [])
		if len(se_rows) != len(self.items or []):
			return

		total_amount = 0.0
		for item, se_row in zip(self.items, se_rows, strict=False):
			rate = flt(se_row.basic_rate)
			amount = flt(se_row.basic_amount) or rate * flt(item.qty)
			total_amount += amount
			item.basic_rate = rate
			item.basic_amount = amount
			frappe.db.set_value(
				item.doctype,
				item.name,
				{"basic_rate": rate, "basic_amount": amount},
				update_modified=False,
			)

		self.db_set("total_amount", total_amount)

	def _movement_branches(self) -> tuple[str, str]:
		"""(sending branch, receiving branch) — reversed on a return."""
		if self.is_return:
			return self.to_branch, self.from_branch
		return self.from_branch, self.to_branch

	def _row_warehouses(self, item) -> tuple[str, str]:
		if self.is_return:
			return item.t_warehouse or self.to_warehouse, item.s_warehouse or self.from_warehouse
		return item.s_warehouse or self.from_warehouse, item.t_warehouse or self.to_warehouse

	def _create_journal_entry(self) -> str | None:
		"""Book the obligation: the branch that received the value owes the other."""
		source_branch, target_branch = self._movement_branches()
		amount = self._transfer_value()
		if not amount:
			frappe.log_error(
				title="Inter-branch JE skipped — zero value",
				message=(
					f"{self.doctype} {self.name} valued at zero. Give the items a valuation rate in "
					f"the source warehouse, or set a rate on the transfer, before submitting."
				),
			)
			return None

		je_name = ib_journal.post_obligation(
			company=self.company,
			posting_date=self.posting_date,
			debtor_branch=target_branch,
			creditor_branch=source_branch,
			amount=amount,
			source_doctype=self.doctype,
			source_docname=self.name,
		)
		if je_name:
			self.db_set("journal_entry", je_name, notify=True)
			_created_message(_("Journal Entry"), "journal-entry", je_name)
		return je_name

	def _transfer_value(self) -> float:
		"""Value the movement from the submitted Stock Entry where possible.

		ERPNext fills `basic_amount` from the real valuation rate, so reading it
		back keeps the Journal Entry consistent with the stock ledger even when
		the transfer itself was submitted with blank rates.
		"""
		if self.stock_entry:
			rows = frappe.get_all(
				"Stock Entry Detail", filters={"parent": self.stock_entry}, fields=["basic_amount"]
			)
			amount = round(sum(flt(row.basic_amount) for row in rows), 2)
			if amount:
				return amount

		return round(sum(flt(item.basic_amount) for item in self.items or []), 2)

	def _create_delivery_note(self):
		"""Deliver the goods onward from the receiving branch.

		The stock already sits in the target warehouse by this point, so the
		Delivery Note ships out of it — shipping out of the source warehouse
		would deduct the sending branch twice.
		"""
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		cost_center = ib_config.resolve_branch_cost_center(self.to_branch)

		dn = frappe.new_doc("Delivery Note")
		dn.customer = self.customer
		dn.company = self.company
		dn.branch = self.to_branch
		dn.cost_center = cost_center
		dn.set_posting_time = 1
		dn.posting_date = self.posting_date
		dn.posting_time = self.posting_time
		dn.set_warehouse = self.to_warehouse
		dn.is_return = 1 if self.is_return else 0
		if self.price_list:
			dn.selling_price_list = self.price_list
		dn.remarks = _("Created from {0} {1}").format(_(self.doctype), self.name)

		for item in self.items or []:
			dn.append(
				"items",
				{
					"item_code": item.item_code,
					"qty": -abs(flt(item.qty)) if self.is_return else flt(item.qty),
					"uom": item.uom,
					"warehouse": self.to_warehouse,
					"rate": item.basic_rate,
					"cost_center": item.cost_center or cost_center,
					"description": item.description or item.item_name,
					"branch": self.to_branch,
				},
			)

		if self.taxes_and_charges:
			dn.taxes_and_charges = self.taxes_and_charges
			dn.extend(
				"taxes",
				get_taxes_and_charges("Sales Taxes and Charges Template", self.taxes_and_charges),
			)

		dn.flags.ignore_permissions = True
		dn.insert(ignore_permissions=True)
		dn.submit()

		self.db_set("delivery_note", dn.name, notify=True)
		_created_message(_("Delivery Note"), "delivery-note", dn.name)

	# ------------------------------------------------------------------
	# Cancel
	# ------------------------------------------------------------------

	def on_cancel(self):
		# Reverse order of creation: the Journal Entry first, then the
		# documents that produced the stock movement it accounts for.
		self._release_return_backlink()
		self._cancel_companion_je()
		self._cancel_linked("Delivery Note", self.delivery_note)
		self._cancel_linked("Stock Entry", self.stock_entry)
		self.db_set("status", "Cancelled")

	def _release_return_backlink(self):
		"""Clear the original's pointer to this return before Frappe's link check.

		Submitting a return stamps `return_ibst` on the document it returns
		against. That is a Link field, so `check_no_back_links_exist` refuses to
		cancel the return while the pointer is live — the return becomes
		uncancellable, which was hit on the first live run. Runs inside
		`on_cancel`, which Frappe calls before the link check.
		"""
		if not self.is_return or not self.return_against:
			return
		if frappe.db.get_value(self.doctype, self.return_against, "return_ibst") == self.name:
			frappe.db.set_value(self.doctype, self.return_against, "return_ibst", None)

	def _cancel_companion_je(self):
		try:
			ib_journal.cancel_companion_jes(self.doctype, self.name)
		except Exception:
			frappe.log_error(
				title="Inter-branch companion JE cancel failed",
				message=frappe.get_traceback(),
			)
			raise

	def _cancel_linked(self, doctype: str, name: str | None):
		if not name:
			return
		try:
			doc = frappe.get_doc(doctype, name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
		except Exception:
			frappe.log_error(
				title=f"Inter-branch {doctype} cancel failed",
				message=frappe.get_traceback(),
			)
			raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _created_message(label: str, route: str, name: str) -> None:
	frappe.msgprint(
		_('{0} <a href="/app/{1}/{2}">{2}</a> created').format(label, route, name),
		alert=True,
		indicator="green",
	)


def returned_qty_map(transfer: str, exclude: str | None = None) -> dict[str, float]:
	"""{item_code: qty already returned} across submitted returns."""
	names = [
		row.name
		for row in frappe.get_all(
			"Inter Branch Stock Transfer",
			filters={"return_against": transfer, "docstatus": 1},
			fields=["name"],
		)
	]
	if exclude and exclude in names:
		names.remove(exclude)
	if not names:
		return {}

	rows = frappe.get_all(
		"Inter Branch Stock Transfer Item",
		filters={"parent": ["in", names]},
		fields=["item_code", "qty"],
	)
	totals: dict[str, float] = defaultdict(float)
	for row in rows:
		totals[row.item_code] += flt(row.qty)
	return dict(totals)


def get_valuation_rate(item_code: str, warehouse: str | None) -> float:
	if not item_code:
		return 0.0
	rate = 0.0
	if warehouse:
		rate = flt(
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate")
		)
	return rate or flt(frappe.db.get_value("Item", item_code, "valuation_rate"))


def get_price_list_rate(item_code: str, price_list: str) -> float:
	return flt(
		frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list},
			"price_list_rate",
		)
	)


# ---------------------------------------------------------------------------
# Whitelisted endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_item_rates(item_code: str, warehouse: str | None = None, price_list: str | None = None) -> dict:
	"""Rates for the client script's item picker.

	Read-only lookups on Bin / Item / Item Price, which branch-scoped roles
	often cannot query directly through `frappe.client.get_value`.
	"""
	frappe.has_permission("Inter Branch Stock Transfer", "read", throw=True)
	return {
		"valuation_rate": get_valuation_rate(item_code, warehouse),
		"price_list_rate": get_price_list_rate(item_code, price_list) if price_list else 0.0,
	}


@frappe.whitelist()
def make_journal_entry(transfer: str) -> str:
	"""Create the companion Journal Entry for an already-submitted transfer."""
	doc = frappe.get_doc("Inter Branch Stock Transfer", transfer)
	doc.check_permission("submit")

	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted transfer can produce a Journal Entry."))

	existing = ib_journal.find_companion_jes(doc.doctype, doc.name)
	if existing:
		return existing[0]

	je_name = doc._create_journal_entry()
	if not je_name:
		frappe.throw(_("No transfer value to post — check the item rates."))
	return je_name


@frappe.whitelist()
def make_return(transfer: str) -> str:
	"""Draft a return transfer pre-filled with whatever is still returnable."""
	original = frappe.get_doc("Inter Branch Stock Transfer", transfer)
	original.check_permission("submit")

	if original.docstatus != 1:
		frappe.throw(_("Only a submitted transfer can be returned."))
	if original.is_return:
		frappe.throw(_("A return cannot itself be returned."))

	pending = frappe.db.get_value(
		"Inter Branch Stock Transfer", {"return_against": transfer, "docstatus": 0}, "name"
	)
	if pending:
		frappe.throw(
			_("Return {0} is still in draft. Submit or cancel it first.").format(pending),
			title=_("Pending Return Exists"),
		)

	already_returned = returned_qty_map(transfer)

	doc = frappe.new_doc("Inter Branch Stock Transfer")
	doc.is_return = 1
	doc.return_against = transfer
	doc.company = original.company
	doc.posting_date = today()
	doc.from_warehouse = original.from_warehouse
	doc.to_warehouse = original.to_warehouse
	doc.customer = original.customer
	doc.price_list = original.price_list
	doc.taxes_and_charges = original.taxes_and_charges

	for item in original.items or []:
		remaining = flt(item.qty) - already_returned.get(item.item_code, 0.0)
		if remaining <= 0:
			continue
		doc.append(
			"items",
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"qty": remaining,
				"uom": item.uom,
				"conversion_factor": item.conversion_factor or 1,
				"s_warehouse": item.s_warehouse,
				"t_warehouse": item.t_warehouse,
				"basic_rate": item.basic_rate,
			},
		)

	if not doc.items:
		frappe.throw(_("Every item has already been returned."), title=_("Nothing to Return"))

	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist()
def get_return_breakdown(transfer: str) -> dict:
	"""Item-wise original / returned / remaining, plus the return documents."""
	frappe.has_permission("Inter Branch Stock Transfer", "read", doc=transfer, throw=True)

	original = frappe.get_doc("Inter Branch Stock Transfer", transfer)
	returned = returned_qty_map(transfer)

	items = []
	for item in original.items or []:
		returned_qty = returned.get(item.item_code, 0.0)
		items.append(
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"uom": item.uom,
				"original_qty": flt(item.qty),
				"returned_qty": returned_qty,
				"remaining_qty": flt(item.qty) - returned_qty,
			}
		)

	returns = frappe.get_all(
		"Inter Branch Stock Transfer",
		filters={"return_against": transfer, "docstatus": ["!=", 2]},
		fields=["name", "posting_date", "status", "docstatus"],
		order_by="creation desc",
	)
	return {"returns": returns, "items": items}
