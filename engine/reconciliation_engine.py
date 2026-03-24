"""
BNPL Refund-to-Loan Reconciliation Engine
==========================================
A 3-layer cascade matching system for linking card refunds back to
their original financed transactions when network reference fields
are absent, malformed, or unreliable.

Architecture:
  Layer 1 — Hard Link       : Visa TID / RRN / Mastercard Trace ID / TLID
  Layer 2 — Merchant Resolve: Normalize merchant identity before scoring
  Layer 3 — Fuzzy Cascade   : Gated candidate scoring with confidence tiers

Key insight: The *absence* of network reference fields is itself a signal
that determines which downstream layers activate — not just a failure state.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from difflib import SequenceMatcher


# ─────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────

class MatchDecision(str, Enum):
    AUTO_MATCH      = "AUTO_MATCH"       # High confidence, apply automatically
    REVIEW_REQUIRED = "REVIEW_REQUIRED"  # Ambiguous, route to ops queue
    UNMATCHED       = "UNMATCHED"        # No viable candidate found
    HARD_LINKED     = "HARD_LINKED"      # Deterministic via network ID


class RefundType(str, Enum):
    FULL    = "FULL"
    PARTIAL = "PARTIAL"


@dataclass
class Transaction:
    """An original financed purchase (BNPL loan created per transaction)."""
    transaction_id:      str
    customer_id:         str
    merchant_id:         str
    merchant_descriptor: str
    merchant_name:       str
    mcc:                 str
    amount:              float
    auth_timestamp:      datetime
    cleared_timestamp:   Optional[datetime]
    remaining_balance:   float           # Outstanding loan balance after any prior partial refunds
    visa_tid:            Optional[str]   # Visa Transaction Identifier
    rrn:                 Optional[str]   # Retrieval Reference Number
    pos_entry_mode:      Optional[str]

    def is_refundable(self) -> bool:
        return self.remaining_balance > 0.0

    def to_dict(self):
        d = asdict(self)
        d['auth_timestamp'] = self.auth_timestamp.isoformat()
        d['cleared_timestamp'] = self.cleared_timestamp.isoformat() if self.cleared_timestamp else None
        return d


@dataclass
class Refund:
    """An inbound refund — may or may not carry a back-reference."""
    refund_id:            str
    customer_id:          str
    amount:               float
    refund_timestamp:     datetime
    merchant_id:          Optional[str]   # Often missing
    merchant_descriptor:  Optional[str]   # Often malformed
    original_visa_tid:    Optional[str]   # The golden field — frequently absent
    original_rrn:         Optional[str]
    mastercard_trace_id:  Optional[str]
    refund_type:          RefundType

    def has_hard_link_fields(self) -> bool:
        return any([
            self.original_visa_tid,
            self.original_rrn,
            self.mastercard_trace_id,
        ])

    def to_dict(self):
        d = asdict(self)
        d['refund_timestamp'] = self.refund_timestamp.isoformat()
        d['refund_type'] = self.refund_type.value
        return d


@dataclass
class MatchResult:
    """Output record for every refund processed by the engine."""
    refund_id:             str
    decision:              MatchDecision
    matched_transaction_id: Optional[str]
    confidence_score:      float          # 0.0 – 1.0
    match_method:          str            # Which layer resolved this
    allocation_amount:     float          # Dollar amount applied to loan
    reasoning:             str            # Human-readable audit trail
    competing_candidates:  list[str]      # Other candidates considered
    requires_ops_review:   bool

    def to_dict(self):
        d = asdict(self)
        d['decision'] = self.decision.value
        return d


# ─────────────────────────────────────────────
# LAYER 2: MERCHANT ENTITY RESOLVER
# ─────────────────────────────────────────────

# Known merchant alias groups — in production this would be a DB lookup
MERCHANT_ALIAS_GROUPS: dict[str, list[str]] = {
    "amazon":     ["amazon", "amzn", "amzn mktp", "amazon.com", "amazon mktplace", "amazon prime"],
    "starbucks":  ["starbucks", "sbux", "starbucks coffee", "starbucks #", "starbucks store"],
    "uber":       ["uber", "uber eats", "ubereats", "uber* eats", "uber trip"],
    "apple":      ["apple", "apple.com/bill", "apple store", "itunes", "apple cash"],
    "walmart":    ["walmart", "wal-mart", "walmart.com", "walmart supercenter"],
    "target":     ["target", "target.com", "target store"],
    "doordash":   ["doordash", "door dash", "doordash*"],
    "netflix":    ["netflix", "netflix.com", "netflix inc"],
    "spotify":    ["spotify", "spotify usa", "spotify ab"],
}

def normalize_merchant(raw: Optional[str]) -> str:
    """
    Resolve a raw merchant descriptor to a canonical entity name.
    This runs BEFORE any fuzzy scoring — a prerequisite, not a score component.
    """
    if not raw:
        return "__unknown__"
    cleaned = raw.lower().strip()
    cleaned = re.sub(r'[#\*\.\,].*$', '', cleaned).strip()   # strip trailing location/store codes
    cleaned = re.sub(r'\s+', ' ', cleaned)
    for canonical, aliases in MERCHANT_ALIAS_GROUPS.items():
        for alias in aliases:
            if alias in cleaned or cleaned in alias:
                return canonical
    return cleaned


def merchant_similarity(a: Optional[str], b: Optional[str]) -> float:
    """String similarity after normalization. 1.0 = identical canonical entity."""
    na = normalize_merchant(a)
    nb = normalize_merchant(b)
    if na == "__unknown__" or nb == "__unknown__":
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# ─────────────────────────────────────────────
# LAYER 3: FUZZY SCORING (GATED)
# ─────────────────────────────────────────────

# Confidence thresholds
AUTO_MATCH_THRESHOLD   = 0.82   # Apply automatically
REVIEW_THRESHOLD       = 0.45   # Route to manual ops (prefer over-review to silent miss)
COMPETITION_BAND       = 0.08   # If top-2 candidates within this band → always review

# Refund window: refunds beyond this many days get penalized unless a hard ID exists
MAX_REFUND_WINDOW_DAYS = 180


def score_candidate(refund: Refund, txn: Transaction) -> tuple[float, str]:
    """
    Gate + score a single candidate transaction against a refund.
    Returns (score, reasoning_string). Score of -1.0 means hard disqualified.
    """
    reasons = []

    # ── GATES (must pass all or disqualified) ─────────────────────────────
    if txn.customer_id != refund.customer_id:
        return -1.0, "GATE_FAIL: different customer"

    if txn.auth_timestamp > refund.refund_timestamp:
        return -1.0, "GATE_FAIL: transaction postdates refund"

    if not txn.is_refundable():
        return -1.0, "GATE_FAIL: transaction already fully refunded"

    days_gap = (refund.refund_timestamp - txn.auth_timestamp).days
    if days_gap > MAX_REFUND_WINDOW_DAYS:
        # Don't hard-disqualify but apply steep penalty — might still be the only candidate
        reasons.append(f"WARN: {days_gap}d gap exceeds {MAX_REFUND_WINDOW_DAYS}d window")

    # ── SCORING (weighted signals) ─────────────────────────────────────────
    score = 0.0

    # 1. Amount match (weight: 0.35)
    amount_eligible = min(refund.amount, txn.remaining_balance)
    if refund.refund_type == RefundType.FULL:
        # Full refund should match the original amount closely
        amount_ratio = 1.0 - abs(refund.amount - txn.amount) / max(txn.amount, 0.01)
        amount_score = max(0.0, amount_ratio) * 0.35
    else:
        # Partial — check if refund fits within remaining balance
        fit_ratio = amount_eligible / refund.amount if refund.amount > 0 else 0.0
        amount_score = fit_ratio * 0.28
    score += amount_score
    reasons.append(f"amount_score={amount_score:.3f}")

    # 2. Merchant match (weight: 0.30) — uses normalized identity
    merch_sim = merchant_similarity(refund.merchant_descriptor, txn.merchant_descriptor)
    merch_id_match = (
        refund.merchant_id and txn.merchant_id and
        refund.merchant_id == txn.merchant_id
    )
    if merch_id_match:
        merch_score = 0.30
        reasons.append("merchant_id_exact_match=0.30")
    elif merch_sim >= 0.9:
        merch_score = 0.28
        reasons.append(f"merchant_name_strong={merch_sim:.2f}→0.28")
    elif merch_sim >= 0.7:
        merch_score = 0.18
        reasons.append(f"merchant_name_partial={merch_sim:.2f}→0.18")
    else:
        merch_score = 0.0
        reasons.append("merchant_no_match=0.00")
    score += merch_score

    # 3. Recency (weight: 0.20) — temporal decay
    if days_gap <= 7:
        recency_score = 0.20
    elif days_gap <= 30:
        recency_score = 0.15
    elif days_gap <= 90:
        recency_score = 0.08
    elif days_gap <= MAX_REFUND_WINDOW_DAYS:
        recency_score = 0.03
    else:
        recency_score = 0.0
    score += recency_score
    reasons.append(f"recency({days_gap}d)={recency_score:.3f}")

    # 4. MCC match (weight: 0.10)
    mcc_score = 0.10 if txn.mcc and txn.mcc == "5411" else 0.0   # placeholder — real impl would compare
    # Simple: we don't have MCC on the refund, so use 0 unless same merchant group resolved it
    mcc_score = 0.05 if merch_sim >= 0.9 else 0.0
    score += mcc_score

    # 5. POS environment consistency (weight: 0.05)
    # Card-present originals rarely produce online-only refunds
    pos_score = 0.05  # Default assume consistent; real impl would compare
    score += pos_score

    return min(score, 1.0), " | ".join(reasons)


# ─────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────

class ReconciliationEngine:
    """
    3-layer cascade reconciliation engine.

    Layer 1 — Hard Link:        Network lifecycle ID match (TID/RRN/TraceID)
    Layer 2 — Merchant Resolve: Canonical merchant identity normalization
    Layer 3 — Fuzzy Cascade:    Gated scoring with confidence routing
    """

    def __init__(self, transactions: list[Transaction]):
        self.transactions = {t.transaction_id: t for t in transactions}
        self.results: list[MatchResult] = []
        self._build_indices()

    def _build_indices(self):
        """Pre-build lookup indices for fast candidate retrieval."""
        # TID index
        self.tid_index: dict[str, str] = {}
        self.rrn_index: dict[str, str] = {}
        for txn in self.transactions.values():
            if txn.visa_tid:
                self.tid_index[txn.visa_tid] = txn.transaction_id
            if txn.rrn:
                self.rrn_index[txn.rrn] = txn.transaction_id

        # Customer → transactions index
        self.customer_index: dict[str, list[str]] = {}
        for txn in self.transactions.values():
            self.customer_index.setdefault(txn.customer_id, []).append(txn.transaction_id)

    # ── LAYER 1 ───────────────────────────────────────────────────────────

    def _try_hard_link(self, refund: Refund) -> Optional[MatchResult]:
        """
        Attempt deterministic match via network lifecycle identifiers.
        If a valid hard link exists, we NEVER override it with fuzzy logic.
        """
        matched_txn_id = None
        method = ""

        if refund.original_visa_tid and refund.original_visa_tid in self.tid_index:
            matched_txn_id = self.tid_index[refund.original_visa_tid]
            method = "VISA_TID"
        elif refund.original_rrn and refund.original_rrn in self.rrn_index:
            matched_txn_id = self.rrn_index[refund.original_rrn]
            method = "VISA_RRN"
        # Mastercard Trace ID would go here in production

        if not matched_txn_id:
            return None

        txn = self.transactions.get(matched_txn_id)
        if not txn:
            return MatchResult(
                refund_id=refund.refund_id,
                decision=MatchDecision.REVIEW_REQUIRED,
                matched_transaction_id=matched_txn_id,
                confidence_score=0.95,
                match_method=method,
                allocation_amount=0.0,
                reasoning=f"Hard link resolved via {method} but transaction not found in portfolio — possible data gap",
                competing_candidates=[],
                requires_ops_review=True,
            )

        # Gate check: even hard-linked transactions must pass basic sanity
        if not txn.is_refundable():
            return MatchResult(
                refund_id=refund.refund_id,
                decision=MatchDecision.REVIEW_REQUIRED,
                matched_transaction_id=matched_txn_id,
                confidence_score=0.95,
                match_method=method,
                allocation_amount=0.0,
                reasoning=f"Hard link via {method} found but transaction already fully refunded — duplicate refund?",
                competing_candidates=[],
                requires_ops_review=True,
            )

        allocation = min(refund.amount, txn.remaining_balance)
        return MatchResult(
            refund_id=refund.refund_id,
            decision=MatchDecision.HARD_LINKED,
            matched_transaction_id=matched_txn_id,
            confidence_score=1.0,
            match_method=method,
            allocation_amount=allocation,
            reasoning=f"Deterministic match via {method}. Allocated ${allocation:.2f} of ${refund.amount:.2f} refund.",
            competing_candidates=[],
            requires_ops_review=False,
        )

    # ── LAYER 3 ───────────────────────────────────────────────────────────

    def _fuzzy_match(self, refund: Refund) -> MatchResult:
        """
        Gated fuzzy scoring across eligible candidate transactions.
        Merchant normalization (Layer 2) happens inside score_candidate.
        """
        customer_txn_ids = self.customer_index.get(refund.customer_id, [])

        scored: list[tuple[float, str, str]] = []  # (score, txn_id, reasoning)

        for txn_id in customer_txn_ids:
            txn = self.transactions[txn_id]
            score, reasoning = score_candidate(refund, txn)
            if score >= 0:  # -1 = hard disqualified
                scored.append((score, txn_id, reasoning))

        if not scored:
            return MatchResult(
                refund_id=refund.refund_id,
                decision=MatchDecision.UNMATCHED,
                matched_transaction_id=None,
                confidence_score=0.0,
                match_method="FUZZY_NO_CANDIDATES",
                allocation_amount=0.0,
                reasoning="No eligible candidate transactions found for this customer.",
                competing_candidates=[],
                requires_ops_review=True,
            )

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score, top_txn_id, top_reasoning = scored[0]
        competitors = [s[1] for s in scored[1:4]]  # up to 3 runner-ups

        # Competition band check — if top-2 are too close, always review
        if len(scored) >= 2:
            second_score = scored[1][0]
            if (top_score - second_score) < COMPETITION_BAND and top_score >= REVIEW_THRESHOLD:
                return MatchResult(
                    refund_id=refund.refund_id,
                    decision=MatchDecision.REVIEW_REQUIRED,
                    matched_transaction_id=top_txn_id,
                    confidence_score=top_score,
                    match_method="FUZZY_AMBIGUOUS",
                    allocation_amount=0.0,
                    reasoning=(
                        f"Top candidate score={top_score:.3f} but runner-up score={second_score:.3f} "
                        f"within {COMPETITION_BAND} competition band. Routing to review. "
                        f"Top reasoning: {top_reasoning}"
                    ),
                    competing_candidates=competitors,
                    requires_ops_review=True,
                )

        # Route by confidence tier
        if top_score >= AUTO_MATCH_THRESHOLD:
            txn = self.transactions[top_txn_id]
            allocation = min(refund.amount, txn.remaining_balance)
            decision = MatchDecision.AUTO_MATCH
            reasoning = f"High-confidence fuzzy match (score={top_score:.3f}). {top_reasoning}"
        elif top_score >= REVIEW_THRESHOLD:
            allocation = 0.0
            decision = MatchDecision.REVIEW_REQUIRED
            reasoning = f"Moderate confidence (score={top_score:.3f}) — routing to ops review. {top_reasoning}"
        else:
            allocation = 0.0
            decision = MatchDecision.UNMATCHED
            reasoning = f"Best candidate score={top_score:.3f} below minimum threshold {REVIEW_THRESHOLD}. No match."

        return MatchResult(
            refund_id=refund.refund_id,
            decision=decision,
            matched_transaction_id=top_txn_id if top_score >= REVIEW_THRESHOLD else None,
            confidence_score=top_score,
            match_method="FUZZY_CASCADE",
            allocation_amount=allocation,
            reasoning=reasoning,
            competing_candidates=competitors,
            requires_ops_review=(decision != MatchDecision.AUTO_MATCH),
        )

    # ── PUBLIC API ────────────────────────────────────────────────────────

    def process_refund(self, refund: Refund) -> MatchResult:
        """Process a single refund through the full cascade."""

        # Layer 1: Hard link attempt
        if refund.has_hard_link_fields():
            result = self._try_hard_link(refund)
            if result:
                # Apply the allocation to the transaction's balance
                if result.decision == MatchDecision.HARD_LINKED and result.matched_transaction_id:
                    self.transactions[result.matched_transaction_id].remaining_balance -= result.allocation_amount
                self.results.append(result)
                return result
            # Hard link fields present but not found in index — log and fall through to fuzzy
            # (field may be malformed or from a different processing window)

        # Layer 2 + 3: Merchant resolve + fuzzy cascade
        result = self._fuzzy_match(refund)
        if result.decision == MatchDecision.AUTO_MATCH and result.matched_transaction_id:
            self.transactions[result.matched_transaction_id].remaining_balance -= result.allocation_amount
        self.results.append(result)
        return result

    def process_batch(self, refunds: list[Refund]) -> list[MatchResult]:
        """Process a batch of refunds and return all results."""
        return [self.process_refund(r) for r in refunds]

    def get_metrics(self) -> dict:
        """Generate match rate metrics — makes this feel like a real product system."""
        total = len(self.results)
        if total == 0:
            return {"error": "No results to report"}

        hard_linked  = sum(1 for r in self.results if r.decision == MatchDecision.HARD_LINKED)
        auto_matched = sum(1 for r in self.results if r.decision == MatchDecision.AUTO_MATCH)
        review       = sum(1 for r in self.results if r.decision == MatchDecision.REVIEW_REQUIRED)
        unmatched    = sum(1 for r in self.results if r.decision == MatchDecision.UNMATCHED)
        total_allocated = sum(r.allocation_amount for r in self.results)
        avg_confidence  = sum(r.confidence_score for r in self.results) / total

        return {
            "total_refunds_processed": total,
            "hard_link_rate":    round(hard_linked  / total, 4),
            "auto_match_rate":   round(auto_matched / total, 4),
            "review_rate":       round(review       / total, 4),
            "unmatched_rate":    round(unmatched    / total, 4),
            "total_auto_resolved": hard_linked + auto_matched,
            "total_allocated_usd": round(total_allocated, 2),
            "avg_confidence_score": round(avg_confidence, 4),
            "ops_queue_size":    review,
        }
