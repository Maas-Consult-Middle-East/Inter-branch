"""Unit tests for the stock-account preconditions (D1) and GL attribution (D2).

EFTSD-001: an Inter Branch Stock Transfer on a perpetual-inventory company wrote
**no** stock GL, because ERPNext only writes stock legs for warehouses that
resolve through `erpnext.stock.get_warehouse_account_map`, and when both
warehouses resolve to the *same* account the debit and credit merge to zero in
`process_gl_map`. The companion Journal Entry then stood alone and each branch's
trial balance was out by the value of the transfer.

Everything here is pure: hand-built maps, `frappe._dict` stand-ins, no company,
no chart of accounts, no fixture. The site does not have `inter_branch`
installed, so no test may construct one of our documents.

Run:
    bench --site <site> run-tests --module inter_branch.tests.test_stock_accounts
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from inter_branch.core.stock import (
	account_branch_map,
	get_stock_account_map,
	stock_account,
	unpostable_transfer_pairs,
)
from inter_branch.tests._stubs import (
	MessageAssertions,
	Recorder,
	account_map,
	item_row,
	resolver,
	wh_row,
)

SOURCE = "Branch A - AC"
TARGET = "Branch B - AC"
STOCK_A = "Stock In Hand A - AC"
STOCK_B = "Stock In Hand B - AC"
SHARED = "Stock In Hand - AC"


class TestStockAccount(unittest.TestCase):
	"""`stock_account(warehouse, account_map)` — the only sanctioned way to read
	an account for a warehouse. Reading `Warehouse.account` directly would miss
	the parent-warehouse and company-default fallbacks upstream applies."""

	def test_mapped_warehouse_returns_its_account(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		self.assertEqual(stock_account(SOURCE, amap), STOCK_A)
		self.assertEqual(stock_account(TARGET, amap), STOCK_B)

	def test_row_without_an_account_is_unresolved(self):
		amap = frappe._dict({SOURCE: wh_row(SOURCE, None)})
		self.assertIsNone(stock_account(SOURCE, amap))

	def test_row_with_a_blank_account_is_unresolved(self):
		amap = frappe._dict({SOURCE: wh_row(SOURCE, "")})
		self.assertIsNone(stock_account(SOURCE, amap))

	def test_absent_warehouse_is_unresolved(self):
		amap = account_map(**{SOURCE: STOCK_A})
		self.assertIsNone(stock_account(TARGET, amap))

	def test_empty_map_is_unresolved(self):
		self.assertIsNone(stock_account(SOURCE, {}))
		self.assertIsNone(stock_account(SOURCE, frappe._dict()))

	def test_falsy_warehouse_is_unresolved(self):
		amap = account_map(**{SOURCE: STOCK_A})
		self.assertIsNone(stock_account(None, amap))
		self.assertIsNone(stock_account("", amap))

	def test_company_keyed_fallback_shape_is_not_mistaken_for_a_warehouse_map(self):
		"""Upstream returns `frappe.flags.warehouse_account_map` — the whole
		*company*-keyed dict — when a company's sub-map comes back empty. Reading
		that as if it were warehouse-keyed would hand back a nested dict and be
		treated as a resolved account."""
		company_keyed = frappe._dict({"Acme Corp": account_map(**{SOURCE: STOCK_A})})
		self.assertIsNone(stock_account(SOURCE, company_keyed))

	def test_plain_dict_rows_are_accepted(self):
		"""`stock_controller` reads `warehouse_account[wh]["account"]`; a plain
		dict row must behave like the `frappe._dict` one."""
		self.assertEqual(stock_account(SOURCE, {SOURCE: {"account": STOCK_A}}), STOCK_A)


class TestUnpostableTransferPairs(unittest.TestCase):
	"""`unpostable_transfer_pairs(pairs, account_map)` — the whole of D1's
	decision logic. `pairs` is `[(source_warehouse, target_warehouse), …]` in the
	direction the goods actually move. Each returned dict carries
	`reason` ("unmapped" | "shared_account"), `source_warehouse`,
	`target_warehouse`, `unmapped` (the warehouses that did not resolve) and,
	for `shared_account`, `account`."""

	def test_two_distinct_accounts_are_postable(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		self.assertEqual(unpostable_transfer_pairs([(SOURCE, TARGET)], amap), [])

	def test_source_without_an_account_is_unmapped(self):
		amap = account_map(**{TARGET: STOCK_B})
		result = unpostable_transfer_pairs([(SOURCE, TARGET)], amap)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["reason"], "unmapped")
		self.assertEqual(result[0]["source_warehouse"], SOURCE)
		self.assertEqual(result[0]["target_warehouse"], TARGET)
		self.assertEqual(list(result[0]["unmapped"]), [SOURCE])

	def test_target_without_an_account_is_unmapped(self):
		amap = account_map(**{SOURCE: STOCK_A})
		result = unpostable_transfer_pairs([(SOURCE, TARGET)], amap)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["reason"], "unmapped")
		self.assertEqual(list(result[0]["unmapped"]), [TARGET])

	def test_both_sides_unmapped_report_once_naming_both(self):
		result = unpostable_transfer_pairs([(SOURCE, TARGET)], {})
		self.assertEqual(len(result), 1, "one broken pair is one report, not one per side")
		self.assertEqual(result[0]["reason"], "unmapped")
		self.assertEqual(sorted(result[0]["unmapped"]), sorted([SOURCE, TARGET]))

	def test_shared_account_is_the_eftsd_001_failure(self):
		"""Both warehouses resolve, to the *same* account: ERPNext merges the two
		legs to zero in `process_gl_map` and writes nothing at all."""
		amap = account_map(**{SOURCE: SHARED, TARGET: SHARED})
		result = unpostable_transfer_pairs([(SOURCE, TARGET)], amap)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["reason"], "shared_account")
		self.assertEqual(result[0]["account"], SHARED)
		self.assertEqual(result[0]["source_warehouse"], SOURCE)
		self.assertEqual(result[0]["target_warehouse"], TARGET)

	def test_inherited_parent_account_is_still_a_shared_account(self):
		"""Two warehouses under one parent inherit the parent's account. The map
		has already resolved that, so it must be caught, not waved through — this
		is the most likely real-world hit after the fix ships."""
		amap = account_map(**{SOURCE: SHARED, TARGET: SHARED})
		self.assertEqual(unpostable_transfer_pairs([(SOURCE, TARGET)], amap)[0]["reason"], "shared_account")

	def test_unmapped_wins_over_shared_account(self):
		"""A pair cannot be both; an unresolved side is the more actionable fault."""
		amap = account_map(**{SOURCE: SHARED})
		result = unpostable_transfer_pairs([(SOURCE, TARGET)], amap)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["reason"], "unmapped")

	def test_ten_rows_over_one_warehouse_pair_report_once(self):
		amap = account_map(**{SOURCE: SHARED, TARGET: SHARED})
		result = unpostable_transfer_pairs([(SOURCE, TARGET)] * 10, amap)
		self.assertEqual(len(result), 1, "a ten-row transfer must not shout ten times")

	def test_distinct_broken_pairs_are_reported_separately(self):
		amap = account_map(**{SOURCE: SHARED, TARGET: SHARED, "Branch C - AC": SHARED})
		result = unpostable_transfer_pairs(
			[(SOURCE, TARGET), (SOURCE, "Branch C - AC"), (SOURCE, TARGET)], amap
		)
		self.assertEqual(len(result), 2)

	def test_a_pair_with_a_falsy_warehouse_is_skipped_not_crashed_on(self):
		"""`_validate_warehouses` already refuses a blank warehouse with a better
		message; this helper must not blow up in front of it."""
		amap = account_map(**{SOURCE: STOCK_A})
		self.assertEqual(unpostable_transfer_pairs([("", TARGET)], amap), [])
		self.assertEqual(unpostable_transfer_pairs([(SOURCE, None)], amap), [])
		self.assertEqual(unpostable_transfer_pairs([(None, None)], amap), [])

	def test_no_pairs_is_postable(self):
		self.assertEqual(unpostable_transfer_pairs([], account_map(**{SOURCE: STOCK_A})), [])

	def test_verdict_is_symmetric_so_a_return_of_a_good_transfer_is_good(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		self.assertEqual(unpostable_transfer_pairs([(TARGET, SOURCE)], amap), [])

	def test_a_return_of_a_shared_account_transfer_is_still_refused(self):
		amap = account_map(**{SOURCE: SHARED, TARGET: SHARED})
		result = unpostable_transfer_pairs([(TARGET, SOURCE)], amap)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["reason"], "shared_account")
		self.assertEqual(result[0]["source_warehouse"], TARGET, "the message must follow the flipped direction")


class TestAccountBranchMap(unittest.TestCase):
	"""`account_branch_map(item_rows, account_map, branches, resolve_branch)` — D2.

	Attribution comes from the Stock Entry's own item rows, never from a reverse
	`Warehouse` lookup by account. The branch resolver is the **fourth positional
	parameter**, defaulting to `ib_config.resolve_warehouse_branch`, so the
	function stays pure and needs no patching to test.
	"""

	def setUp(self):
		self.branches = ("Riyadh", "Jeddah")
		self.resolve = resolver({SOURCE: "Riyadh", TARGET: "Jeddah"})

	def test_two_warehouses_two_accounts_two_branches(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET)]
		self.assertEqual(
			account_branch_map(rows, amap, self.branches, self.resolve),
			{STOCK_A: "Riyadh", STOCK_B: "Jeddah"},
		)

	def test_shared_account_is_left_untouched(self):
		"""The heart of D2. One account, two branches: attribution is not a
		function, so the row keeps whatever branch ERPNext stamped rather than
		getting an arbitrary one from whichever `Warehouse` row MariaDB returned
		first."""
		amap = account_map(**{SOURCE: SHARED, TARGET: SHARED})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET)]
		self.assertEqual(account_branch_map(rows, amap, self.branches, self.resolve), {})

	def test_branch_outside_the_pair_is_excluded(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		resolve = resolver({SOURCE: "Riyadh", TARGET: "Dammam"})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET)]
		self.assertEqual(
			account_branch_map(rows, amap, self.branches, resolve), {STOCK_A: "Riyadh"}
		)

	def test_warehouse_missing_from_the_account_map_is_excluded(self):
		amap = account_map(**{SOURCE: STOCK_A})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET)]
		self.assertEqual(account_branch_map(rows, amap, self.branches, self.resolve), {STOCK_A: "Riyadh"})

	def test_warehouse_with_no_branch_is_excluded_without_raising(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		resolve = resolver({SOURCE: "Riyadh"})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET)]
		self.assertEqual(account_branch_map(rows, amap, self.branches, resolve), {STOCK_A: "Riyadh"})

	def test_empty_item_list(self):
		self.assertEqual(account_branch_map([], account_map(**{SOURCE: STOCK_A}), self.branches, self.resolve), {})
		self.assertEqual(account_branch_map(None, account_map(**{SOURCE: STOCK_A}), self.branches, self.resolve), {})

	def test_rows_with_only_one_warehouse_are_tolerated(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		rows = [item_row(s_warehouse=SOURCE), item_row(t_warehouse=TARGET)]
		self.assertEqual(
			account_branch_map(rows, amap, self.branches, self.resolve),
			{STOCK_A: "Riyadh", STOCK_B: "Jeddah"},
		)

	def test_many_rows_over_the_same_pair_agree(self):
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET) for _ in range(10)]
		self.assertEqual(
			account_branch_map(rows, amap, self.branches, self.resolve),
			{STOCK_A: "Riyadh", STOCK_B: "Jeddah"},
		)

	def test_two_warehouses_of_one_branch_sharing_an_account_are_unambiguous(self):
		"""Ambiguity is about *branches*, not warehouses: two Riyadh warehouses on
		one account still resolve to Riyadh."""
		amap = account_map(**{SOURCE: STOCK_A, "Riyadh Spare - AC": STOCK_A, TARGET: STOCK_B})
		resolve = resolver({SOURCE: "Riyadh", "Riyadh Spare - AC": "Riyadh", TARGET: "Jeddah"})
		rows = [
			item_row(s_warehouse=SOURCE, t_warehouse=TARGET),
			item_row(s_warehouse="Riyadh Spare - AC", t_warehouse=TARGET),
		]
		self.assertEqual(
			account_branch_map(rows, amap, self.branches, resolve),
			{STOCK_A: "Riyadh", STOCK_B: "Jeddah"},
		)

	def test_default_resolver_is_the_apps_own_warehouse_mapping(self):
		"""Called with three arguments it must fall back to
		`ib_config.resolve_warehouse_branch` — the app's authoritative mapping."""
		amap = account_map(**{SOURCE: STOCK_A, TARGET: STOCK_B})
		rows = [item_row(s_warehouse=SOURCE, t_warehouse=TARGET)]
		with patch(
			"inter_branch.core.config.resolve_warehouse_branch",
			side_effect=lambda wh: {SOURCE: "Riyadh", TARGET: "Jeddah"}.get(wh),
		):
			self.assertEqual(
				account_branch_map(rows, amap, self.branches),
				{STOCK_A: "Riyadh", STOCK_B: "Jeddah"},
			)


class TestGetStockAccountMap(unittest.TestCase, MessageAssertions):
	"""`get_stock_account_map(company)` wraps the upstream builder, which
	`frappe.throw`s for a non-group warehouse with no resolvable account. We want
	our own message, before any document is created."""

	def test_upstream_map_is_returned_unchanged(self):
		amap = account_map(**{SOURCE: STOCK_A})
		with patch("inter_branch.core.stock.get_warehouse_account_map", return_value=amap):
			self.assertEqual(get_stock_account_map("Acme Corp"), amap)

	def test_company_is_passed_through(self):
		recorder = Recorder(result=frappe._dict())
		with patch("inter_branch.core.stock.get_warehouse_account_map", recorder):
			get_stock_account_map("Acme Corp")
		self.assertTrue(recorder.called)
		args, kwargs = recorder.calls[0]
		self.assertIn("Acme Corp", list(args) + list(kwargs.values()))

	def test_upstream_throw_becomes_an_empty_map_not_an_exception(self):
		def boom(company=None):
			frappe.throw("Please set Account in Warehouse X or Default Inventory Account in Company Y")

		frappe.clear_messages()
		with patch("inter_branch.core.stock.get_warehouse_account_map", boom), patch.object(
			frappe, "log_error"
		):
			result = get_stock_account_map("Acme Corp")
		self.assertEqual(result, {})

	def test_upstreams_raw_message_is_cleared_so_only_ours_is_shown(self):
		def boom(company=None):
			frappe.throw("Please set Account in Warehouse X or Default Inventory Account in Company Y")

		frappe.clear_messages()
		with patch("inter_branch.core.stock.get_warehouse_account_map", boom), patch.object(
			frappe, "log_error"
		):
			get_stock_account_map("Acme Corp")
		leftovers = [m.get("message") or "" for m in frappe.get_message_log()]
		frappe.clear_messages()
		self.assertFalse(
			[m for m in leftovers if "Please set Account in Warehouse" in m],
			"ERPNext's own message must not stack on top of ours",
		)

	def test_upstream_failure_is_logged(self):
		def boom(company=None):
			frappe.throw("no account")

		frappe.clear_messages()
		logger = Recorder()
		with patch("inter_branch.core.stock.get_warehouse_account_map", boom), patch.object(
			frappe, "log_error", logger
		):
			get_stock_account_map("Acme Corp")
		frappe.clear_messages()
		self.assertTrue(logger.called, "a swallowed upstream failure must leave an Error Log")

	def test_the_log_is_deferred_so_the_callers_throw_cannot_roll_it_back(self):
		"""The IBST caller answers an empty map with a `frappe.throw`. A plain
		`log_error` inserts inside that transaction, so the rollback discards the
		one diagnostic explaining why the map was empty."""

		def boom(company=None):
			frappe.throw("no account")

		frappe.clear_messages()
		logger = Recorder()
		with patch("inter_branch.core.stock.get_warehouse_account_map", boom), patch.object(
			frappe, "log_error", logger
		):
			get_stock_account_map("Acme Corp")
		frappe.clear_messages()
		_args, kwargs = logger.calls[0]
		self.assertIs(kwargs.get("defer_insert"), True)
