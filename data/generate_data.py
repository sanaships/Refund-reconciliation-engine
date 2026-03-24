"""
Synthetic Data Generator
========================
Generates realistic BNPL transaction + refund scenarios that mimic
real processor data patterns — including the messy edge cases.
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.reconciliation_engine import Transaction, Refund, RefundType

random.seed(42)

BASE_DATE = datetime(2024, 10, 1, 9, 0, 0)

MERCHANTS = [
    {"merchant_id": "M001", "name": "Amazon",     "descriptor": "AMZN MKTP US",      "mcc": "5999"},
    {"merchant_id": "M002", "name": "Starbucks",  "descriptor": "STARBUCKS #12043",   "mcc": "5812"},
    {"merchant_id": "M003", "name": "Uber Eats",  "descriptor": "UBER* EATS",         "mcc": "5812"},
    {"merchant_id": "M004", "name": "Target",     "descriptor": "TARGET 00012345",    "mcc": "5311"},
    {"merchant_id": "M005", "name": "Apple",      "descriptor": "APPLE.COM/BILL",     "mcc": "5734"},
    {"merchant_id": "M006", "name": "Walmart",    "descriptor": "WALMART SUPERCENTER","mcc": "5411"},
]

# Malformed / alias descriptors that processors sometimes send back on refunds
DESCRIPTOR_MUTATIONS = {
    "M001": ["Amazon.com", "AMZN*MKTP", "Amazon Marketplace", None],
    "M002": ["Starbucks Coffee", "SBUX", "STARBUCKS"],
    "M003": ["Uber Eats", "UBEREATS", "UBER EATS"],
    "M004": ["Target Store", "TARGET.COM", "TARGET"],
    "M005": ["Apple Store", "iTunes", "APPLE"],
    "M006": ["Wal-Mart", "WALMART", "Walmart.com"],
}


def make_transaction(txn_id, customer_id, merchant, days_ago, amount,
                     visa_tid=None, rrn=None) -> Transaction:
    auth_time = BASE_DATE - timedelta(days=days_ago)
    return Transaction(
        transaction_id=txn_id,
        customer_id=customer_id,
        merchant_id=merchant["merchant_id"],
        merchant_descriptor=merchant["descriptor"],
        merchant_name=merchant["name"],
        mcc=merchant["mcc"],
        amount=amount,
        auth_timestamp=auth_time,
        cleared_timestamp=auth_time + timedelta(hours=2),
        remaining_balance=amount,
        visa_tid=visa_tid,
        rrn=rrn,
        pos_entry_mode="CHIP",
    )


def make_refund(refund_id, customer_id, amount, days_ago, merchant_id=None,
                descriptor=None, visa_tid=None, rrn=None,
                refund_type=RefundType.FULL) -> Refund:
    return Refund(
        refund_id=refund_id,
        customer_id=customer_id,
        amount=amount,
        refund_timestamp=BASE_DATE - timedelta(days=days_ago),
        merchant_id=merchant_id,
        merchant_descriptor=descriptor,
        original_visa_tid=visa_tid,
        original_rrn=rrn,
        mastercard_trace_id=None,
        refund_type=refund_type,
    )


def generate_scenarios() -> tuple[list[Transaction], list[Refund], list[dict]]:
    """
    Generate 10 realistic scenarios covering the full edge-case spectrum.
    Returns (transactions, refunds, scenario_metadata)
    """
    transactions = []
    refunds = []
    scenarios = []

    amz = MERCHANTS[0]
    sbux = MERCHANTS[1]
    uber = MERCHANTS[2]
    tgt = MERCHANTS[3]
    apl = MERCHANTS[4]
    wmt = MERCHANTS[5]

    # ── Scenario 1: Clean hard link via Visa TID ──────────────────────────
    t1 = make_transaction("TXN-001", "CUST-A", amz, days_ago=14, amount=89.99,
                          visa_tid="VTD-111AAA")
    r1 = make_refund("REF-001", "CUST-A", 89.99, days_ago=7,
                     visa_tid="VTD-111AAA")
    transactions.append(t1); refunds.append(r1)
    scenarios.append({"id": "S1", "refund": "REF-001", "desc": "Clean Visa TID hard link",
                       "expected": "HARD_LINKED"})

    # ── Scenario 2: No network ID, clean fuzzy match ──────────────────────
    t2 = make_transaction("TXN-002", "CUST-B", sbux, days_ago=10, amount=6.75)
    r2 = make_refund("REF-002", "CUST-B", 6.75, days_ago=3,
                     merchant_id="M002", descriptor="Starbucks Coffee")
    transactions.append(t2); refunds.append(r2)
    scenarios.append({"id": "S2", "refund": "REF-002", "desc": "No TID, fuzzy match via normalized merchant name",
                       "expected": "AUTO_MATCH"})

    # ── Scenario 3: Partial refund ────────────────────────────────────────
    t3 = make_transaction("TXN-003", "CUST-C", tgt, days_ago=20, amount=120.00)
    r3 = make_refund("REF-003", "CUST-C", 45.00, days_ago=5,
                     merchant_id="M004", descriptor="TARGET",
                     refund_type=RefundType.PARTIAL)
    transactions.append(t3); refunds.append(r3)
    scenarios.append({"id": "S3", "refund": "REF-003", "desc": "Partial refund — $45 of $120",
                       "expected": "AUTO_MATCH"})

    # ── Scenario 4: Multiple same-merchant transactions (ambiguous) ───────
    t4a = make_transaction("TXN-004A", "CUST-D", amz, days_ago=30, amount=54.99)
    t4b = make_transaction("TXN-004B", "CUST-D", amz, days_ago=15, amount=54.99)
    t4c = make_transaction("TXN-004C", "CUST-D", amz, days_ago=5,  amount=54.99)
    r4  = make_refund("REF-004", "CUST-D", 54.99, days_ago=2,
                      merchant_id="M001", descriptor="AMZN*MKTP")
    transactions.extend([t4a, t4b, t4c]); refunds.append(r4)
    scenarios.append({"id": "S4", "refund": "REF-004",
                       "desc": "3 identical Amazon transactions — ambiguous, should route to review",
                       "expected": "REVIEW_REQUIRED"})

    # ── Scenario 5: Malformed descriptor, no merchant_id ─────────────────
    t5 = make_transaction("TXN-005", "CUST-E", uber, days_ago=8, amount=22.50)
    r5 = make_refund("REF-005", "CUST-E", 22.50, days_ago=1,
                     descriptor="UBER EATS")   # no merchant_id
    transactions.append(t5); refunds.append(r5)
    scenarios.append({"id": "S5", "refund": "REF-005",
                       "desc": "No merchant_id, descriptor alias resolved via normalization",
                       "expected": "AUTO_MATCH"})

    # ── Scenario 6: Fully refunded transaction (duplicate refund attempt) ─
    t6 = make_transaction("TXN-006", "CUST-F", apl, days_ago=60, amount=9.99,
                          visa_tid="VTD-666FFF")
    t6.remaining_balance = 0.0  # Already refunded
    r6 = make_refund("REF-006", "CUST-F", 9.99, days_ago=1,
                     visa_tid="VTD-666FFF")
    transactions.append(t6); refunds.append(r6)
    scenarios.append({"id": "S6", "refund": "REF-006",
                       "desc": "Hard link found but transaction already fully refunded — ops review",
                       "expected": "REVIEW_REQUIRED"})

    # ── Scenario 7: No viable match (wrong customer, old transaction) ─────
    t7 = make_transaction("TXN-007", "CUST-G", wmt, days_ago=200, amount=200.00)
    r7 = make_refund("REF-007", "CUST-G", 200.00, days_ago=1,
                     merchant_id="M999", descriptor="UNKNOWN MERCHANT")
    transactions.append(t7); refunds.append(r7)
    scenarios.append({"id": "S7", "refund": "REF-007",
                       "desc": "No merchant match + excessive time gap → unmatched",
                       "expected": "UNMATCHED"})

    # ── Scenario 8: Two partial refunds on same transaction ───────────────
    t8 = make_transaction("TXN-008", "CUST-H", tgt, days_ago=25, amount=150.00)
    r8a = make_refund("REF-008A", "CUST-H", 50.00, days_ago=15,
                      merchant_id="M004", descriptor="TARGET",
                      refund_type=RefundType.PARTIAL)
    r8b = make_refund("REF-008B", "CUST-H", 75.00, days_ago=5,
                      merchant_id="M004", descriptor="TARGET",
                      refund_type=RefundType.PARTIAL)
    transactions.append(t8); refunds.extend([r8a, r8b])
    scenarios.append({"id": "S8A", "refund": "REF-008A",
                       "desc": "First of two partial refunds — $50 of $150",
                       "expected": "AUTO_MATCH"})
    scenarios.append({"id": "S8B", "refund": "REF-008B",
                       "desc": "Second partial refund — $75 of remaining $100",
                       "expected": "AUTO_MATCH"})

    # ── Scenario 9: RRN hard link ─────────────────────────────────────────
    t9 = make_transaction("TXN-009", "CUST-I", sbux, days_ago=3, amount=12.00,
                          rrn="RRN-999XYZ")
    r9 = make_refund("REF-009", "CUST-I", 12.00, days_ago=0,
                     rrn="RRN-999XYZ")
    transactions.append(t9); refunds.append(r9)
    scenarios.append({"id": "S9", "refund": "REF-009",
                       "desc": "Hard link via RRN (Retrieval Reference Number)",
                       "expected": "HARD_LINKED"})

    # ── Scenario 10: Complete data missing (null refund fields) ──────────
    t10 = make_transaction("TXN-010", "CUST-J", amz, days_ago=45, amount=299.00)
    r10 = make_refund("REF-010", "CUST-J", 299.00, days_ago=10)   # no IDs, no descriptor
    transactions.append(t10); refunds.append(r10)
    scenarios.append({"id": "S10", "refund": "REF-010",
                       "desc": "All reference fields null — amount + recency only signal",
                       "expected": "REVIEW_REQUIRED"})

    return transactions, refunds, scenarios


if __name__ == "__main__":
    txns, refs, scens = generate_scenarios()
    out_dir = Path(__file__).parent

    with open(out_dir / "transactions.json", "w") as f:
        json.dump([t.to_dict() for t in txns], f, indent=2)

    with open(out_dir / "refunds.json", "w") as f:
        json.dump([r.to_dict() for r in refs], f, indent=2)

    with open(out_dir / "scenarios.json", "w") as f:
        json.dump(scens, f, indent=2)

    print(f"Generated {len(txns)} transactions, {len(refs)} refunds, {len(scens)} scenarios")
    print(f"Files written to: {out_dir}")
