"""Wiring tests for the stock path: the submit guard, the GL re-tag, the hook.

The pure decision logic lives in `test_stock_accounts.py`. This module pins the
things that only go wrong once the logic is wired in:

* the Inter Branch Stock Transfer *refuses* to submit rather than posting a
  Journal Entry with nothing to offset it (D1);
* `retag_stock_entry_gl` attributes GL rows from the Stock Entry's own item
  rows and never reverse-looks-up a `Warehouse` by account (D2);
* `on_stock_entry_submit` — which fires on every Material Transfer on the site —
  logs and returns instead of throwing, and books no one-sided obligation.

Controller methods are called unbound against `frappe._dict` stand-ins, because
the site has no `inter_branch` DocTypes.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_stock_hooks
"""

from __future__ import annotations

import contextlib
import unittest
from unittest.mock import patch

import frappe

from inter_branch.core import stock as ib_stock
from inter_branch.inter_branch.doctype.inter_branch_stock_transfer import (
	inter_branch_stock_transfer as ibst_module,
)
from inter_branch.inter_branch.doctype.inter_branch_stock_transfer.inter_branch_stock_transfer import (
	InterBranchStockTransfer,
)
from inter_branch.tests._stubs import (
	DocStub,
	MessageAssertions,
	Recorder,
	account_map,
	item_row,
	stub,
)

COMPANY = "Acme Corp"
SOURCE = "Branch A - AC"
TARGET = "Branch B - AC"
STOCK_A = "Stock In Hand A - AC"
STOCK_B = "Stock In Hand B - AC"
SHARED = "Stock In Hand - AC"
COGS = "Cost of Goods Sold - AC"
STORES = "Stores - AC"
FINISHED = "Finished Goods - AC"


@contextlib.contextmanager
def patched_account_map(amap):
	"""Patch `get_stock_account_map` wherever it has been bound.

	The helper lives in `core.stock`; the controller may hold its own reference
	after a `from … import`. Patching both keeps the test honest either way.
	"""
	with contextlib.ExitStack() as stack:
		stack.enter_context(patch.object(ib_stock, "get_stock_account_map", return_value=amap, create=True))
		if hasattr(ibst_module, "get_stock_account_map"):
			stack.enter_context(
				patch.object(ibst_module, "get_stock_account_map", return_value=amap)
			)
		yield


def transfer(items, is_return=0, company=COMPANY):
	return stub(
		InterBranchStockTransfer,
		methods=("_validate_stock_accounts", "_row_warehouses"),
		company=company,
		items=items,
		is_return=is_return,
		from_warehouse=SOURCE,
		to_warehouse=TARGET,
	)


class TestValidateStockAccounts(unittest.TestCase, MessageAssertions):
	"""`InterBranchStockTransfer._validate_stock_accounts` — the D1 guard."""

	def test_distinct_accounts_submit_without_complaint(self):
		doc = transfer([item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")])
		with patched_account_map(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			self.assertIsNone(doc._validate_stock_accounts())

	def test_shared_account_is_refused_with_the_account_named(self):
		doc = transfer([item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")])
		with patched_account_map(account_map(**{SOURCE: SHARED, TARGET: SHARED})):
			message = self.assert_throws(doc._validate_stock_accounts)
		self.assert_mentions(message, SOURCE, TARGET, SHARED)
		self.assertEqual(message.title, "Stock Account Not Postable")

	def test_shared_account_message_says_why_it_matters(self):
		"""The operator must be told the consequence, not just the rule: with no
		stock GL the companion Journal Entry stands alone and each branch's trial
		balance is out by the value of the transfer."""
		doc = transfer([item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")])
		with patched_account_map(account_map(**{SOURCE: SHARED, TARGET: SHARED})):
			message = self.assert_throws(doc._validate_stock_accounts)
		self.assertIn("Journal Entry", message.plain)

	def test_unmapped_warehouse_is_refused_with_a_remedy(self):
		doc = transfer([item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")])
		with patched_account_map(account_map(**{TARGET: STOCK_B})):
			message = self.assert_throws(doc._validate_stock_accounts)
		self.assert_mentions(message, SOURCE, COMPANY)
		self.assertIn("Journal Entry", message.plain)
		self.assertIn("Default Inventory Account", message.plain)
		self.assertEqual(message.title, "Stock Account Not Postable")

	def test_empty_account_map_is_refused_not_waved_through(self):
		"""`get_stock_account_map` returns `{}` when the upstream builder threw.
		Submitting on that would post the standalone Journal Entry all over again."""
		doc = transfer([item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")])
		with patched_account_map(frappe._dict()):
			self.assert_throws(doc._validate_stock_accounts)

	def test_the_map_is_fetched_once_per_document_not_once_per_row(self):
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=i, item_code=f"ITEM-{i}") for i in range(1, 11)]
		doc = transfer(rows)
		recorder = Recorder(result=account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B}))
		with patch.object(ib_stock, "get_stock_account_map", recorder, create=True):
			if hasattr(ibst_module, "get_stock_account_map"):
				with patch.object(ibst_module, "get_stock_account_map", recorder):
					doc._validate_stock_accounts()
			else:
				doc._validate_stock_accounts()
		self.assertEqual(recorder.count, 1)

	def test_the_map_is_fetched_for_this_document_s_company(self):
		doc = transfer([item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")])
		recorder = Recorder(result=account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B}))
		with patch.object(ib_stock, "get_stock_account_map", recorder, create=True):
			if hasattr(ibst_module, "get_stock_account_map"):
				with patch.object(ibst_module, "get_stock_account_map", recorder):
					doc._validate_stock_accounts()
			else:
				doc._validate_stock_accounts()
		args, kwargs = recorder.calls[0]
		self.assertIn(COMPANY, list(args) + list(kwargs.values()))

	def test_a_ten_row_transfer_reports_the_pair_once(self):
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=i, item_code=f"ITEM-{i}") for i in range(1, 11)]
		doc = transfer(rows)
		with patched_account_map(account_map(**{SOURCE: SHARED, TARGET: SHARED})):
			message = self.assert_throws(doc._validate_stock_accounts)
		self.assertEqual(message.plain.count(SHARED), 1, "one broken pair, one row in the table")

	def test_a_return_is_checked_in_the_direction_the_goods_move(self):
		"""On a return the goods leave `t_warehouse`. The verdict is symmetric,
		but the message must name the real source."""
		doc = transfer(
			[item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")], is_return=1
		)
		with patched_account_map(account_map(**{SOURCE: SHARED, TARGET: SHARED})):
			message = self.assert_throws(doc._validate_stock_accounts)
		self.assert_mentions(message, SOURCE, TARGET, SHARED)

	def test_a_return_of_a_postable_transfer_is_postable(self):
		doc = transfer(
			[item_row(s_warehouse=SOURCE, t_warehouse=TARGET, idx=1, item_code="ITEM-1")], is_return=1
		)
		with patched_account_map(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			self.assertIsNone(doc._validate_stock_accounts())


class TestSubmitOrder(unittest.TestCase):
	"""The guard has to run *before* anything is created, and after the perpetual
	check whose message is the better one when both apply."""

	def _ordered_stub(self, method, names, **fields):
		order: list[str] = []

		def record(name):
			def call(*args, **kwargs):
				order.append(name)

			return call

		doc = stub(InterBranchStockTransfer, methods=(method,), **fields)
		for name in names:
			setattr(doc, name, record(name))
		return doc, order

	def test_before_submit_checks_stock_accounts_after_perpetual_inventory(self):
		doc, order = self._ordered_stub(
			"before_submit",
			("_validate_perpetual_inventory", "_validate_stock_accounts", "_validate_stock_availability"),
		)
		doc.before_submit()
		self.assertIn("_validate_stock_accounts", order)
		self.assertLess(
			order.index("_validate_perpetual_inventory"),
			order.index("_validate_stock_accounts"),
			"perpetual inventory is still the right first check",
		)

	def test_validate_checks_branch_companies_right_after_resolving_branches(self):
		doc, order = self._ordered_stub(
			"validate",
			(
				"_apply_company_defaults",
				"_set_branches",
				"_validate_branch_companies",
				"_set_item_warehouses",
				"_set_rates_and_totals",
				"_validate_warehouses",
				"_validate_return_quantities",
			),
			is_return=0,
			return_against=None,
		)
		doc.validate()
		self.assertEqual(
			order.index("_validate_branch_companies"),
			order.index("_set_branches") + 1,
			"the operator should get the real reason before rates and warehouses",
		)


class CountingResolver:
	"""Warehouse -> branch resolver that remembers every call.

	`resolve_warehouse_branch` is an uncached two-table join and both the stock
	path's callers walk the same warehouses twice over. A plain patch proves the
	answer; only a call count proves it is asked once per warehouse rather than
	once per row, which is the whole of the memoisation.
	"""

	def __init__(self, mapping: dict[str, str]):
		self.mapping = mapping
		self.calls: list[str] = []

	def __call__(self, warehouse):
		self.calls.append(warehouse)
		return self.mapping.get(warehouse)

	@property
	def distinct(self) -> set[str]:
		return set(self.calls)


class TestRetagStockEntryGL(unittest.TestCase):
	"""`retag_stock_entry_gl` — D2.

	The old body asked `frappe.db.get_value("Warehouse", {"account": row.account})`
	which is not a function when warehouses share a stock account: it returned
	whichever row MariaDB handed back first and tagged the GL with an arbitrary
	branch.
	"""

	def setUp(self):
		self.entry = DocStub(
			doctype="Stock Entry",
			name="MAT-STE-2026-00001",
			company=COMPANY,
			items=[item_row(s_warehouse=SOURCE, t_warehouse=TARGET, qty=3, basic_amount=330)],
		)
		self.rows = [
			frappe._dict(name="GLE-A", account=STOCK_A),
			frappe._dict(name="GLE-B", account=STOCK_B),
			frappe._dict(name="GLE-C", account=COGS),
		]

	@contextlib.contextmanager
	def _run(self, amap, rows=None):
		self.set_value = Recorder()
		self.get_all = Recorder(result=rows if rows is not None else self.rows)
		# A `Recorder`, not a bare `patch.object`: the "re-tag incomplete" log is
		# a claim about the two branches' trial balances, so which documents it
		# fires on is behaviour worth asserting either way.
		self.log_error = Recorder()

		def no_warehouse_lookup(doctype, *args, **kwargs):
			raise AssertionError(
				"retag_stock_entry_gl must not reverse-resolve a warehouse from an account; "
				f"it called frappe.db.get_value({doctype!r}, …)"
			)

		self.resolve = CountingResolver({SOURCE: "Riyadh", TARGET: "Jeddah"})

		with patch.object(ib_stock, "get_stock_account_map", return_value=amap, create=True), patch(
			"inter_branch.core.config.resolve_warehouse_branch", self.resolve
		), patch.object(frappe, "get_all", self.get_all), patch.object(
			frappe.db, "set_value", self.set_value
		), patch.object(frappe.db, "get_value", no_warehouse_lookup), patch.object(
			frappe, "log_error", self.log_error
		):
			yield

	def _writes(self) -> dict[str, str]:
		out = {}
		for args, kwargs in self.set_value.calls:
			self.assertEqual(args[0], "GL Entry")
			field = args[2] if len(args) > 2 else kwargs.get("fieldname")
			if isinstance(field, dict):
				out[args[1]] = field.get("branch")
			else:
				self.assertEqual(field, "branch")
				out[args[1]] = args[3] if len(args) > 3 else kwargs.get("val")
		return out

	def test_each_leg_gets_the_branch_that_owns_its_warehouse(self):
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertEqual(self._writes(), {"GLE-A": "Riyadh", "GLE-B": "Jeddah"})

	def test_each_warehouse_is_resolved_once_however_many_rows(self):
		"""Four rows over one warehouse pair must cost two branch lookups, not eight."""
		self.entry.items = [
			item_row(s_warehouse=SOURCE, t_warehouse=TARGET, qty=1, basic_amount=110)
			for _ in range(4)
		]
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")

		self.assertEqual(self.resolve.distinct, {SOURCE, TARGET})
		self.assertEqual(
			len(self.resolve.calls),
			len(self.resolve.distinct),
			f"resolved once per row instead of once per warehouse: {self.resolve.calls}",
		)

	def test_rows_on_other_accounts_are_left_alone(self):
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertNotIn("GLE-C", self._writes(), "expense and rounding legs are not ours to re-tag")

	def test_a_shared_account_leaves_every_row_untouched(self):
		"""Ambiguous attribution: better ERPNext's header branch on both legs than
		a coin-flip between them."""
		with self._run(account_map(**{SOURCE: SHARED, TARGET: SHARED}), rows=[frappe._dict(name="GLE-S", account=SHARED)]):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertEqual(self._writes(), {})

	def test_an_empty_account_map_touches_nothing(self):
		with self._run(frappe._dict()):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertEqual(self._writes(), {})

	def test_a_branch_outside_the_pair_is_not_written(self):
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Dammam")
		self.assertEqual(self._writes(), {"GLE-A": "Riyadh"})

	def test_only_live_gl_rows_of_this_voucher_are_read(self):
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		args, kwargs = self.get_all.calls[0]
		self.assertEqual(args[0], "GL Entry")
		filters = kwargs.get("filters") or (args[1] if len(args) > 1 else {})
		self.assertEqual(filters.get("voucher_no"), self.entry.name)
		self.assertEqual(filters.get("is_cancelled"), 0, "reversal rows must never be re-tagged")

	def test_the_write_does_not_bump_modified(self):
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		for _args, kwargs in self.set_value.calls:
			self.assertIs(kwargs.get("update_modified"), False)

	def test_a_complete_attribution_leaves_no_error_log(self):
		"""Both legs were re-tagged; there is nothing wrong to report. A log line
		that fires on success is a log line nobody reads."""
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertFalse(self.log_error.called)

	def test_a_row_that_belongs_to_no_branch_is_not_reported_as_unattributed(self):
		"""The mixed document the submit guard exists to permit: a branch-crossing
		row plus a plain `Stores → Finished Goods` move on the company default
		account. Both cross-branch legs attribute; the stranger's account is not
		ours to attribute, ERPNext writes no GL row for it at all (one account,
		both sides, netted to nothing), and it touches neither branch's trial
		balance. Reporting it would train people to ignore the log."""
		self.entry.items = [
			item_row(s_warehouse=SOURCE, t_warehouse=TARGET, qty=3, basic_amount=330),
			item_row(s_warehouse=STORES, t_warehouse=FINISHED, qty=1, basic_amount=50),
		]
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B, STORES: SHARED, FINISHED: SHARED})):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertEqual(self._writes(), {"GLE-A": "Riyadh", "GLE-B": "Jeddah"})
		self.assertFalse(self.log_error.called, "the stranger's pair is not evidence of a broken trial balance")

	def test_a_shared_account_is_reported_because_the_legs_keep_the_wrong_branch(self):
		"""Here the unattributed account *is* one of ours: two branch warehouses on
		one stock account, which is the case the log exists for."""
		with self._run(
			account_map(**{SOURCE: SHARED, TARGET: SHARED}), rows=[frappe._dict(name="GLE-S", account=SHARED)]
		):
			ib_stock.retag_stock_entry_gl(self.entry, "Riyadh", "Jeddah")
		self.assertTrue(self.log_error.called, "a leg left on the wrong branch is unauditable if unlogged")


class TestOnStockEntrySubmitGuard(unittest.TestCase):
	"""The direct Stock Entry hook fires on every Material Transfer on the site.
	It must never throw on a stranger's document — and must not book an
	obligation that has no stock GL to offset it."""

	def setUp(self):
		self.doc = DocStub(
			doctype="Stock Entry",
			name="MAT-STE-2026-00002",
			company=COMPANY,
			purpose="Material Transfer",
			posting_date="2026-08-16",
			flags=frappe._dict(),
			items=[item_row(s_warehouse=SOURCE, t_warehouse=TARGET, qty=3, basic_rate=110, basic_amount=330)],
		)

	@contextlib.contextmanager
	def _run(self, amap):
		self.resolve = CountingResolver({SOURCE: "Riyadh", TARGET: "Jeddah"})
		self.post_obligation = Recorder(result="ACC-JV-0001")
		self.retag = Recorder()
		self.log_error = Recorder()
		with patch(
			"inter_branch.core.config.get_settings",
			return_value=frappe._dict(enabled=1, handle_direct_stock_entry=1),
		), patch("inter_branch.core.config.is_pre_cut_over", return_value=False), patch(
			"inter_branch.core.config.resolve_warehouse_branch", self.resolve
		), patch("inter_branch.core.journal.find_companion_jes", return_value=[]), patch(
			"inter_branch.core.journal.post_obligation", self.post_obligation
		), patch.object(ib_stock, "get_stock_account_map", return_value=amap, create=True), patch.object(
			ib_stock, "retag_stock_entry_gl", self.retag
		), patch.object(frappe, "get_cached_value", return_value=1), patch.object(
			frappe, "log_error", self.log_error
		):
			yield

	def test_a_postable_transfer_still_books_its_obligation(self):
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.on_stock_entry_submit(self.doc)
		self.assertTrue(self.post_obligation.called)
		self.assertEqual(self.post_obligation.calls[0][1].get("amount"), 330.0)
		self.assertTrue(self.retag.called)

	def test_the_scoping_pass_resolves_each_warehouse_once(self):
		"""The guard's own row-scoping walk is memoised.

		`stock_entry_branch_pair` runs first and resolves both warehouses of
		every row — two per row, unchanged and out of scope here. What this pins
		is the pass that follows it: it must add one lookup per DISTINCT
		warehouse, not another two per row. This hook fires on every Material
		Transfer on the site, so the difference is real.
		"""
		rows = 3
		self.doc.items = [
			item_row(s_warehouse=SOURCE, t_warehouse=TARGET, qty=1, basic_rate=110, basic_amount=110)
			for _ in range(rows)
		]
		with self._run(account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})):
			ib_stock.on_stock_entry_submit(self.doc)

		expected = 2 * rows + len({SOURCE, TARGET})
		self.assertEqual(
			len(self.resolve.calls),
			expected,
			"the scoping pass resolved per row instead of per warehouse: "
			f"{len(self.resolve.calls)} calls, expected {expected} — {self.resolve.calls}",
		)

	def test_an_unrelated_row_does_not_veto_the_branch_crossing_one(self):
		"""One Material Transfer may carry a branch-crossing row *and* a row that
		belongs to no branch at all — a plain `Stores → Finished Goods` move on
		the company's single default inventory account.

		The obligation is computed from the branch-crossing row only, so the
		guard must be scoped to the same row. Judging the whole document would
		refuse the posting on a stranger's warehouse pair: the cross-branch stock
		GL is already written, the re-tag is skipped and no companion Journal
		Entry is booked, leaving each branch out by the value of the transfer —
		the original defect, reached from the other direction.
		"""
		self.doc.items = [
			item_row(s_warehouse=SOURCE, t_warehouse=TARGET, qty=3, basic_rate=110, basic_amount=330),
			item_row(s_warehouse=STORES, t_warehouse=FINISHED, qty=1, basic_rate=50, basic_amount=50),
		]
		amap = account_map(
			**{SOURCE: STOCK_A, TARGET: STOCK_B, STORES: SHARED, FINISHED: SHARED}
		)
		with self._run(amap):
			ib_stock.on_stock_entry_submit(self.doc)
		self.assertTrue(
			self.post_obligation.called,
			"the branch-crossing row is postable; an unrelated pair must not suppress it",
		)
		self.assertEqual(self.post_obligation.calls[0][1].get("amount"), 330.0)
		self.assertTrue(self.retag.called, "the cross-branch GL legs still need their branches")

	def test_a_shared_account_transfer_books_nothing_and_does_not_throw(self):
		with self._run(account_map(**{SOURCE: SHARED, TARGET: SHARED})):
			ib_stock.on_stock_entry_submit(self.doc)
		self.assertFalse(
			self.post_obligation.called,
			"a one-sided obligation is exactly the bug; skip the whole posting",
		)
		self.assertFalse(self.retag.called)

	def test_a_shared_account_transfer_leaves_an_error_log(self):
		with self._run(account_map(**{SOURCE: SHARED, TARGET: SHARED})):
			ib_stock.on_stock_entry_submit(self.doc)
		self.assertTrue(self.log_error.called, "a silent skip is unauditable")

	def test_an_unmapped_warehouse_books_nothing_and_does_not_throw(self):
		with self._run(account_map(**{SOURCE: STOCK_A})):
			ib_stock.on_stock_entry_submit(self.doc)
		self.assertFalse(self.post_obligation.called)
		self.assertTrue(self.log_error.called)

	def test_an_empty_account_map_books_nothing_and_does_not_throw(self):
		with self._run(frappe._dict()):
			ib_stock.on_stock_entry_submit(self.doc)
		self.assertFalse(self.post_obligation.called)
