"""Shared stand-ins for the unit tests.

The site these tests run on does not have `inter_branch` installed, so no
DocType of ours exists there. Every test therefore drives plain functions, or
drives a controller method *unbound* against a `frappe._dict` stand-in, and
never calls `frappe.get_doc` / `frappe.new_doc`. That is deliberate: the logic
under test is arithmetic and decision-making, and a fixture would only make it
slower and site-dependent without testing anything more.
"""

from __future__ import annotations

import re
from types import MethodType

import frappe

TAGS = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
	"""Message text with HTML stripped, for substring assertions."""
	return TAGS.sub(" ", text or "")


def wh_row(warehouse: str, account: str | None, **extra):
	"""One row of `erpnext.stock.get_warehouse_account_map`'s return value.

	Upstream builds `frappe._dict(name=…, account=…, parent_warehouse=…,
	company=…, is_group=…, account_currency=…)` — the tests only ever need
	`account`, but the extra keys are carried so a helper that reads them keeps
	working.
	"""
	return frappe._dict(
		name=warehouse,
		account=account,
		parent_warehouse=extra.get("parent_warehouse"),
		company=extra.get("company", "Acme Corp"),
		is_group=extra.get("is_group", 0),
		account_currency=extra.get("account_currency", "SAR"),
	)


def account_map(**warehouses):
	"""`{warehouse: row}` map keyed the way upstream keys it.

	Call as `account_map(**{"Branch A - AC": "Stock A - AC"})`.
	"""
	return frappe._dict(
		{warehouse: wh_row(warehouse, acc) for warehouse, acc in warehouses.items()}
	)


def item_row(s_warehouse=None, t_warehouse=None, **extra):
	"""A Stock Entry Detail stand-in."""
	return frappe._dict(s_warehouse=s_warehouse, t_warehouse=t_warehouse, **extra)


def resolver(mapping: dict[str, str | None]):
	"""A `resolve_warehouse_branch` stand-in backed by a plain dict."""

	def resolve(warehouse):
		return mapping.get(warehouse)

	return resolve


class DocStub:
	"""Stand-in for a Document: attribute access, `.get()`, missing name → None.

	Deliberately *not* a `frappe._dict`: a document's child table is usually
	called `items`, and on a dict subclass `doc.items` resolves to `dict.items`
	— the bound method, not the rows. Production code that reads `doc.items`
	would then silently iterate nothing.
	"""

	def __init__(self, **fields):
		self.__dict__.update(fields)

	def get(self, key, default=None):
		value = self.__dict__.get(key)
		return default if value is None else value

	def __getattr__(self, name):
		if name.startswith("__"):
			raise AttributeError(name)
		return None

	def __repr__(self):
		return f"<DocStub {self.__dict__}>"


def stub(cls, methods=(), **fields):
	"""A `DocStub` posing as `cls`, with `methods` bound onto it.

	Lets a controller method be exercised without the Document machinery (and
	therefore without the DocType existing on the site). Only the methods a test
	actually needs are bound, so an unexpected call fails loudly rather than
	silently doing something real.
	"""
	doc = DocStub(**fields)
	for name in methods:
		setattr(doc, name, MethodType(getattr(cls, name), doc))
	return doc


class Recorder:
	"""Callable that remembers how it was called."""

	def __init__(self, result=None):
		self.result = result
		self.calls: list[tuple[tuple, dict]] = []

	def __call__(self, *args, **kwargs):
		self.calls.append((args, kwargs))
		return self.result

	@property
	def called(self) -> bool:
		return bool(self.calls)

	@property
	def count(self) -> int:
		return len(self.calls)


class ThrowingRecorder(Recorder):
	"""Callable that records, then raises — for standing in for a `throw`ing dep."""

	def __init__(self, exc):
		super().__init__()
		self.exc = exc

	def __call__(self, *args, **kwargs):
		super().__call__(*args, **kwargs)
		raise self.exc


class MessageAssertions:
	"""Mixin: assert on the message a `frappe.throw` actually produced."""

	def assert_throws(self, func, *args, **kwargs):
		"""Run `func`, require a ValidationError, return the message that went with it."""
		frappe.clear_messages()
		with self.assertRaises(frappe.ValidationError):
			func(*args, **kwargs)
		log = frappe.get_message_log()
		self.assertTrue(log, "frappe.throw was expected to leave a message behind")
		last = log[-1]
		frappe.clear_messages()
		return frappe._dict(
			title=last.get("title"),
			message=last.get("message") or "",
			plain=plain(last.get("message") or ""),
		)

	def assert_mentions(self, message, *needles):
		for needle in needles:
			self.assertIn(
				needle,
				message.plain,
				f"message should name {needle!r}; got: {message.plain}",
			)
