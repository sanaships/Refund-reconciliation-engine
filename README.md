# BNPL Refund-to-Loan Reconciliation Engine

> A production-grade simulation of refund matching logic for transaction-linked lending products.

## The Problem

In Buy Now Pay Later (BNPL) and card-linked installment products, a loan is created for each financed purchase. When a customer is refunded, the refund often arrives through the card network **without a clean reference to the original transaction** — the money hits the account, but the loan stays open.

This creates:
- ❌ Incorrect outstanding balances
- ❌ Erroneous dunning / collections activity
- ❌ Customer support volume
- ❌ Reconciliation failures at scale

This is an **industry-wide problem** affecting any issuer running transaction-linked credit.

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
| S4 | 3 identical Amazon transactions (ambiguous) | REVIEW_REQUIRED |
| S5 | Missing merchant_id, descriptor alias resolved | AUTO_MATCH |
| S6 | Hard link found but transaction already refunded | REVIEW_REQUIRED |
| S7 | Unknown merchant + excessive time gap | UNMATCHED |
| S8 | Two sequential partial refunds on same transaction | AUTO_MATCH × 2 |
| S9 | RRN hard link | HARD_LINKED |
| S10 | All reference fields null | REVIEW_REQUIRED |

## Running It

```bash
# Install dependencies
pip install streamlit pandas

# Run the demo UI
streamlit run app.py

# Run tests
python tests/test_engine.py
```

## Project Structure

```
bnpl-refund-reconciliation/
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
- Incorrect loan closures → balance sheet errors
- Misapplied partial refunds → downstream installment miscalculation
- Regulatory exposure if reconciliation can't be audited

The confidence-tiered routing (auto / review / unmatched) lets you **tune the ops cost vs. accuracy tradeoff** explicitly rather than hiding it inside a single threshold.

---

*Inspired by real reconciliation challenges in transaction-linked lending. Built to demonstrate systems thinking at the intersection of fintech, credit, and backend product design.*
