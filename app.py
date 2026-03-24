"""
BNPL Refund Reconciliation Engine — Streamlit Demo
====================================================
Lightweight UI to demonstrate the engine across all 10 scenarios.
Run with: streamlit run app.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
import pandas as pd

from engine.reconciliation_engine import ReconciliationEngine, MatchDecision
from data.generate_data import generate_scenarios

# ── Page config ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="BNPL Refund Reconciliation Engine",
    page_icon="🏦",
    layout="wide",
)

# ── Decision color map ───────────────────────────────────────────────────
DECISION_COLORS = {
    MatchDecision.HARD_LINKED:      "#1a9641",
    MatchDecision.AUTO_MATCH:       "#4dac26",
    MatchDecision.REVIEW_REQUIRED:  "#f4a736",
    MatchDecision.UNMATCHED:        "#d7191c",
}

DECISION_LABELS = {
    MatchDecision.HARD_LINKED:      "🔗 Hard Linked",
    MatchDecision.AUTO_MATCH:       "✅ Auto Matched",
    MatchDecision.REVIEW_REQUIRED:  "⚠️ Review Required",
    MatchDecision.UNMATCHED:        "❌ Unmatched",
}

# ── Load data ────────────────────────────────────────────────────────────
@st.cache_data
def load_data():
    transactions, refunds, scenarios = generate_scenarios()
    return transactions, refunds, scenarios


def run_engine(transactions, refunds):
    engine = ReconciliationEngine(transactions)
    refund_map = {r.refund_id: r for r in refunds}
    return engine, refund_map


# ── Main UI ──────────────────────────────────────────────────────────────
st.title("🏦 BNPL Refund-to-Loan Reconciliation Engine")
st.markdown("""
> **Problem:** In transaction-linked BNPL, refunds arrive through the card network without
> a clean reference to the original financed purchase — leaving loans open after customers
> have been refunded. This engine uses a 3-layer cascade to match them correctly.
""")

transactions, refunds, scenarios = load_data()
engine, refund_map = run_engine(transactions, refunds)

# Process all refunds
all_results = []
for scenario in scenarios:
    refund = refund_map[scenario["refund"]]
    result = engine.process_refund(refund)
    all_results.append({
        "scenario": scenario["id"],
        "description": scenario["desc"],
        "refund_id": result.refund_id,
        "decision": result.decision,
        "confidence": result.confidence_score,
        "method": result.match_method,
        "matched_txn": result.matched_transaction_id or "—",
        "allocated_usd": result.allocation_amount,
        "reasoning": result.reasoning,
        "expected": scenario["expected"],
        "pass": result.decision.value == scenario["expected"],
    })

metrics = engine.get_metrics()

# ── Metrics row ──────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📊 Engine Performance Metrics")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Hard Link Rate",  f"{metrics['hard_link_rate']*100:.0f}%")
col2.metric("Auto Match Rate", f"{metrics['auto_match_rate']*100:.0f}%")
col3.metric("Review Queue",    f"{metrics['review_rate']*100:.0f}%",
            help="Routed to ops for manual review")
col4.metric("Unmatched",       f"{metrics['unmatched_rate']*100:.0f}%")
col5.metric("Avg Confidence",  f"{metrics['avg_confidence_score']:.2f}")

col6, col7 = st.columns(2)
col6.metric("Total Auto-Resolved",  metrics['total_auto_resolved'])
col7.metric("Total Allocated",      f"${metrics['total_allocated_usd']:,.2f}")

# ── Test pass/fail summary ───────────────────────────────────────────────
st.markdown("---")
passed = sum(1 for r in all_results if r["pass"])
total  = len(all_results)
st.subheader(f"🧪 Test Results: {passed}/{total} scenarios passed")

# ── Results table ────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📋 Scenario-by-Scenario Results")

for row in all_results:
    decision_label = DECISION_LABELS[row["decision"]]
    pass_icon = "✅" if row["pass"] else "❌"
    confidence_bar = "█" * int(row["confidence"] * 10) + "░" * (10 - int(row["confidence"] * 10))

    with st.expander(
        f"{pass_icon} [{row['scenario']}] {row['description']} — {decision_label}",
        expanded=False,
    ):
        c1, c2, c3 = st.columns(3)
        c1.metric("Decision",    decision_label)
        c2.metric("Confidence",  f"{row['confidence']:.3f}  {confidence_bar}")
        c3.metric("Allocated",   f"${row['allocated_usd']:.2f}")

        c4, c5 = st.columns(2)
        c4.markdown(f"**Method:** `{row['method']}`")
        c5.markdown(f"**Matched Txn:** `{row['matched_txn']}`")

        st.markdown(f"**Reasoning:** {row['reasoning']}")

        if row["decision"] in (MatchDecision.REVIEW_REQUIRED, MatchDecision.UNMATCHED):
            st.warning("⚠️ This refund has been routed to the manual ops review queue.")

# ── Algorithm Architecture ────────────────────────────────────────────────
st.markdown("---")
st.subheader("🏗️ Algorithm Architecture")
st.markdown("""
```
Inbound Refund
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  LAYER 1: Hard Link Check                           │
│  → Visa TID / RRN / Mastercard Trace ID present?   │
│  → If YES and valid: HARD_LINKED → apply refund    │
│  → If present but not found: fall through + warn   │
│  → If absent: this ABSENCE is the routing signal   │
└────────────────────────┬────────────────────────────┘
                         │ (no hard link)
                         ▼
┌─────────────────────────────────────────────────────┐
│  LAYER 2: Merchant Entity Resolution                │
│  → Normalize raw descriptor to canonical name      │
│  → "AMZN MKTP" → "amazon"                          │
│  → "STARBUCKS #12043" → "starbucks"                │
│  → Runs BEFORE scoring — not part of score         │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  LAYER 3: Gated Fuzzy Cascade                       │
│                                                     │
│  GATES (must pass ALL):                             │
│   ✓ Same customer/account                           │
│   ✓ Transaction precedes refund                     │
│   ✓ Remaining balance > 0                           │
│   ✓ Within refund window (or strong alt signal)     │
│                                                     │
│  SCORING (weighted signals):                        │
│   • Amount match         35%                        │
│   • Merchant identity    30%                        │
│   • Recency (decay)      20%                        │
│   • MCC consistency       5%                        │
│   • POS environment       5%                        │
│   • Competition band      → always review if <0.08  │
│                                                     │
│  ROUTING:                                           │
│   ≥ 0.82  → AUTO_MATCH                             │
│   ≥ 0.50  → REVIEW_REQUIRED                        │
│   < 0.50  → UNMATCHED                              │
└─────────────────────────────────────────────────────┘
```
""")

st.markdown("""
### 🔑 The Non-Traditional Variable

The key insight that makes this system **reliable and auditable** — unlike most fuzzy matchers —
is treating the **absence of network reference fields** as an explicit routing signal, not just a failure state.

- If `Visa TID` / `RRN` / `Trace ID` are **present → valid**: deterministic hard link, no fuzzy logic runs
- If those fields are **present but malformed**: flag as data quality issue, fall to fuzzy with a warning
- If those fields are **absent**: this absence activates the fuzzy cascade — and it tells you *how much*
  to trust the downstream signals (e.g., descriptor-only match gets lower weight when no MID is present either)

This makes the algorithm **regulatorily defensible**: every decision has an explicit reason, and the system
never silently overrides a lifecycle identifier with a probabilistic guess.
""")

st.markdown("---")
st.caption("Built with Python · Designed for senior PM portfolio · Inspired by Cash App BNPL reconciliation challenges")
