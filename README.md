# Inter Branch

Inter-branch receivables, payables, stock transfers and service settlements for
ERPNext v15+. A standalone Frappe app: install it on any site that runs a
single company with several branches and needs each branch's books to stand up
on their own.

Built by Enfono Technologies. The accounting model is the one proven in
production on RMAX; this app is the generic, configurable rewrite — **no client
warehouse, price list or account name is hardcoded anywhere in it**.

## The problem

One company, several branches. Branch A ships stock to Branch B, or does work
for it. The company total is always right, but each branch's own P&L and
balance sheet are wrong: A carries a cost B benefited from, and nothing in
standard ERPNext records the obligation between them.

## What it posts

**Stock crossing a branch boundary** is an asset moving. Two legs settle it:

| Leg | Account | Root type | Branch |
|---|---|---|---|
| Dr | Due from *receiver* | Asset | sender |
| Cr | Due to *sender* | Liability | receiver |

**A service crossing a branch boundary** has no asset — the cost is already
sunk in the performer's P&L. Four legs settle it:

| Leg | Account | Root type | Branch |
|---|---|---|---|
| Cr | Service Recovered | Income | performer |
| Dr | Due from *receiver* | Asset | performer |
| Dr | Service Charged | Expense | receiver |
| Cr | Due to *performer* | Liability | receiver |

The recharge pair is deliberately **not** an internal Sales Invoice. An
internal sale inflates consolidated turnover by the recharge amount every time
and has to be eliminated by hand at every close. Recovered and Charged are
equal and opposite by construction, so the elimination is structural.

> `Inter-Branch Service Recovered` is Income root-type but is **not turnover**.
> The app stamps `custom_exclude_from_turnover` on it. Any revenue KPI,
> dashboard or statutory report that sums Income must filter on that flag.

### Stock accounts

A stock transfer's two legs only balance if ERPNext actually writes stock GL for
them, and it writes a stock leg **per warehouse** — only for warehouses that
resolve to a stock account, and only when the two warehouses resolve to
*different* accounts. Two warehouses on one account produce a debit and a credit
that merge to zero, so nothing is written at all.

Perpetual inventory alone is therefore not enough. Each branch's warehouse needs
its **own** stock account: set `Warehouse → Account` per branch warehouse, or
give each one a parent warehouse that carries its own account. A warehouse with
no account of its own falls back to the parent, then to the company's **Default
Inventory Account**, then to any Stock account on the company — and that last
fallback is what silently lands two branches on one shared account.

A transfer whose warehouses fail either test is **refused at submit**, because
posting it would create a companion Journal Entry with nothing to offset it and
leave both branches' trial balances out by the value of the transfer.

## What you get

| Thing | Type | What it does |
|---|---|---|
| **Inter Branch Settings** | Single | Per-company cut-over date, bridge branch, parent groups, recharge accounts, stock-transfer defaults, behaviour switches |
| **Inter Branch Configuration** | Master, one per branch | Asset / liability account heads, recharge account overrides, warehouse and cost-centre mappings |
| **Inter Branch Stock Transfer** | Submittable | Stock Entry + companion JE (+ optional Delivery Note), returns, cancel cascade |
| **Inter Branch Service Settlement** | Submittable | Four-leg recharge JE; flat, hours × rate, or cost-plus-% |
| **Inter Branch Reconciliation** | Report | Branch × branch matrix with an Unmatched column |
| Journal Entry auto-injection | Hook | Balances any manual JE that touches two branches; three or more use the configured bridge branch |
| Branch tagging | Hook | Fills the Branch dimension on stock documents from the warehouse mapping |

## Install

```bash
bench get-app https://github.com/EnfonoTech/inter-branch --branch main
```

```bash
bench --site <site> install-app inter_branch
```

## Set up

1. **Inter Branch Settings** → add a row per company. Set the **cut-over
   date** — a blank date means the automatic posting layer stays off for that
   company, which is the safe default while you configure the rest.
2. **Inter Branch Configuration** → one per branch. Map its warehouses and
   cost centres. Point the **Due-from** and **Due-to** account heads at your
   own chart, or leave them blank and the app creates them under the
   configured parent groups on first use and writes them back to the record.
3. Enable **Enforce Branch Dimension** on the company row once every warehouse
   is mapped. It marks Branch mandatory for Balance Sheet and P&L accounts at
   the GL-posting layer, which is what makes branch statements trustworthy —
   and what blocks postings that would be untraceable.

`bench migrate` re-applies custom fields, the accounting dimension and the
parent account groups every time. All of it is idempotent.

### One Branch, one Company

There is **one Inter Branch Configuration per Branch** — the record is autonamed
from the branch — and that configuration carries a company. So a Branch belongs
to exactly one Company, and every document is checked against it: a transfer or
settlement on company Y whose branch is configured for company X is refused up
front, rather than failing later inside Journal Entry validation.

If the same physical branch trades under two companies, create **a Branch per
company** (`Riyadh — Acme`, `Riyadh — Globex`) and configure each one
separately. A configuration's company also cannot be changed once it has account
heads, warehouses or cost centres on it, since all of those belong to the
company it was set up under; clear them first, or make a new Branch.

## Multi-branch Journal Entries

A manual JE touching two branches is balanced automatically from the
imbalance. Three or more is ambiguous — who owes whom cannot be inferred — so
the company's **Bridge Branch** is used as the counterparty for all the
others, and the entry is rejected if the bridge is not configured or not
present in the entry.

## Tests

```bash
bench --site <site> run-tests --app inter_branch
```

A single module, when you want just one:

```bash
bench --site <site> run-tests --module inter_branch.tests.test_posting_logic
```

Covers the imbalance arithmetic, the three pricing bases, the stock-account
preconditions and GL attribution, and the one-Branch-one-Company rule — all
without needing fixtures. End-to-end posting tests need a site with a company,
branches and a chart of accounts; they are not in this repo yet.

## Licence

Proprietary — © Enfono Technologies. See `license.txt`.
# Inter-branch
