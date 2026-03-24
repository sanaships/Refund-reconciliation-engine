# Refund Reconciliation Engine

> A production-grade simulation of refund matching logic for transaction-linked credit products.

## The Problem

In any product where a **financial obligation is created at the transaction level** — BNPL, card-linked installments, debit flex, expense management, merchant cash advances, or subscription financing — refunds present a structural reconciliation gap.

When a refund routes back through the card network or a separate payment channel, it often arrives **without a clean reference to the original transaction**. The money hits the account. The obligation stays open.

This is not a BNPL-specific problem. It is a structural gap in **any system where the origination channel and the refund channel don't share a common identifier** — and it compounds at scale.

Affected product types include:
- **Card-linked / transaction-linked BNPL** (Cash App Pay, debit flex, virtual card BNPL)
- **Co-brand & store credit cards** where rewards or limits are tied to specific purchases
- **Corporate expense cards** (Brex, Ramp) where approved expenses must reconcile to transactions
- **Merchant cash advances** where repayment is calculated against original sales
- **Earned Wage Access** where advances are tied to specific shifts or payroll transactions
- **Subscription installment plans** where cancellation credits must offset future obligations

This creates:
- ❌ Incorrect outstanding balances
- ❌ Erroneous dunning / collections activity
- ❌ Customer support volume and disputes
- ❌ Reconciliation failures and audit gaps at scale

## The Solution: 3-Layer Cascade Architecture

Rather than treating this as a single fuzzy-match problem, this engine uses a **cascade of progressively weaker — but explicitly gated — signals**.
```
Inbound Refund
     │
     ▼
LAYER 1: Hard Link (deterministic)
  → Visa Transaction Identifier (TID)
  → Retrieval Reference Number (RRN)
  → Mastercard Trace ID / TLID
  → If found: apply immediately. Never override with fuzzy logic.
  → If ABSENT: this absence is the routing signal for Layer 3.
     │
     ▼
LAYER 2: Merchant Entity Resolution
  → Normalize raw descriptors to canonical identity BEFORE scoring
  → "AMZN MKTP US" → "amazon" | "STARBUCKS #12043" → "starbucks"
  → Runs as a prerequisite, not a score component
     │
     ▼
LAYER 3: Gated Fuzzy Cascade
  → Gates: same customer, txn precedes refund, balance > 0, time window
  → Score: amount (35%) + merchant (30%) + recency (20%) + MCC (5%) + POS (5%)
  → Competition band: if top-2 within 0.08 → always route to review
  → Routing: ≥0.82 auto-match | ≥0.50 ops review | <0.50 unmatched
```

## The Key Non-Traditional Variable

Most matching systems treat missing network reference fields as a simple failure state. This engine treats the **absence of those fields as an explicit routing signal**.

- Fields **present + valid** → deterministic hard link
- Fields **present but malformed** → data quality flag, fall to fuzzy with warning
- Fields **absent** → activates fuzzy cascade, informs downstream signal weighting

This makes the system **regulatorily defensible**: every decision has an audit trail, and probabilistic guesses never silently override lifecycle identifiers.

## Scenarios Covered

| # | Scenario | Expected |
|---|----------|----------|
| S1 | Clean Visa TID hard link | HARD_LINKED |
| S2 | No TID, normalized merchant name match | AUTO_MATCH |
| S3 | Partial refund ($45 of $120) | AUTO_MATCH |
| S4 | 3 identical transactions, same merchant (ambiguous) | REVIEW_REQUIRED |
| S5 | Missing merchant_id, descriptor alias resolved | AUTO_MATCH |
| S6 | Hard link found but transaction already refunded | REVIEW_REQUIRED |
| S7 | Unknown merchant + excessive time gap | UNMATCHED |
| S8 | Two sequential partial refunds on same transaction | AUTO_MATCH × 2 |
| S9 | RRN hard link | HARD_LINKED |
| S10 | All reference fields null | REVIEW_REQUIRED |

## Running It
```bash
pip install streamlit pandas
streamlit run app.py
python tests/test_engine.py
```

## Project Structure
```
refund-reconciliation-engine/
├── engine/
│   └── reconciliation_engine.py   # Core 3-layer cascade engine
├── data/
│   └── generate_data.py           # Synthetic scenario generator
├── tests/
│   └── test_engine.py             # 10-scenario validation suite
├── app.py                         # Streamlit demo UI
└── README.md
```

## Why This Matters at Scale

At high transaction volume, a 1% false-positive match rate means:
- Incorrect obligation closures → balance sheet errors
- Misapplied partial refunds → downstream installment miscalculation
- Regulatory exposure if reconciliation can't be audited

The confidence-tiered routing (auto / review / unmatched) lets you **tune the ops cost vs. accuracy tradeoff** explicitly rather than hiding it inside a single threshold.

---

*Inspired by real reconciliation challenges across transaction-linked credit products including BNPL, debit flex, and card-linked installments. Built to demonstrate systems thinking at the intersection of fintech, credit operations, and backend product design.*
