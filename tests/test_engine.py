"""
Reconciliation Engine Test Suite
=================================
Validates all 10 edge-case scenarios against expected match decisions.
This is what makes the repo look credible — not just code, but a
testable system with ground-truth validation.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.reconciliation_engine import ReconciliationEngine, MatchDecision
from data.generate_data import generate_scenarios


def run_tests():
    transactions, refunds, scenarios = generate_scenarios()
    engine = ReconciliationEngine(transactions)

    refund_map = {r.refund_id: r for r in refunds}

    print("=" * 70)
    print("BNPL REFUND RECONCILIATION ENGINE — TEST SUITE")
    print("=" * 70)

    passed = 0
    failed = 0
    results_by_id = {}

    for scenario in scenarios:
        refund = refund_map[scenario["refund"]]
        result = engine.process_refund(refund)
        results_by_id[scenario["id"]] = result

        expected = MatchDecision(scenario["expected"])
        actual = result.decision
        ok = actual == expected

        status = "✅ PASS" if ok else "❌ FAIL"
        if ok:
            passed += 1
        else:
            failed += 1

        print(f"\n{status} [{scenario['id']}] {scenario['desc']}")
        print(f"         Expected : {expected.value}")
        print(f"         Got      : {actual.value}  (confidence={result.confidence_score:.3f})")
        print(f"         Method   : {result.match_method}")
        print(f"         Allocated: ${result.allocation_amount:.2f}")
        if result.competing_candidates:
            print(f"         Rivals   : {result.competing_candidates}")
        print(f"         Reasoning: {result.reasoning[:120]}...")

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed}/{passed+failed} passed")

    metrics = engine.get_metrics()
    print("\n📊 ENGINE METRICS:")
    for k, v in metrics.items():
        print(f"   {k:35s}: {v}")

    print("=" * 70)
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
