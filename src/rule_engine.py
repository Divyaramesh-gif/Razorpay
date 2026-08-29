"""§2.5 Versioned GST rules + operational checks — build-order step 6.

Two distinct categories, **reported separately in the final output — do not
merge them**:

    Classification rules   what happened
    Operational checks     what to do about it, and by when

Both are versioned in one YAML (`rules/rules_v2026_04.yaml`), which holds the
version-sensitive parameters. The logic here is deterministic Python:
if/else over the §2.4 evidence object plus the prior-period snapshot. **No LLM
participates in a GST classification.** The same evidence always yields the
same rule, and the reason string names the values that drove it, so any
classification can be re-derived by hand in a review.

The rule engine CONSUMES the evidence object but does not modify it (§2.4) —
it only ever reads through `Evidence`'s accessors.

Scope note: this stage says which named category applies, not what to do with
the record. Whether a classified exception is auto-reconciled, reported, or
sent to human review is the confidence gate's call (§2.6, build step 7). A
record where no rule fires simply reports `category=None`; that is an input to
the gate, not a verdict of "fine".
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Set

import yaml

from .evidence import Evidence
from .matcher import Match
from .source_records import SOURCE_PRIOR_PERIOD, load_source
from .validation import parse_amount, parse_date

RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
DEFAULT_RULES_PATH = os.path.join(RULES_DIR, "rules_v2026_04.yaml")

APPLIES_MATCHED_PAIR = "matched_pair"
APPLIES_NO_CANDIDATE = "no_candidate_found"

STATUS_WITHIN_WINDOW = "within_window"
STATUS_OUTSIDE_WINDOW = "outside_window"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_BREACHED = "breached"
STATUS_WITHIN_THRESHOLD = "within_threshold"


# ---------------------------------------------------------------------------
# Result types — the two categories stay separate all the way out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """WHAT HAPPENED. `rule_id`/`category` are None when no rule fires."""

    rule_id: Optional[str]
    category: Optional[str]
    reason: str
    rules_version: str

    @property
    def fired(self) -> bool:
        return self.rule_id is not None


@dataclass(frozen=True)
class OperationalFlag:
    """WHAT TO DO ABOUT IT, AND BY WHEN."""

    check_id: str
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class RuleEvaluation:
    record_id: str
    invoice_id: str
    candidate_found: bool
    classification: Classification
    operational_flags: List[OperationalFlag] = field(default_factory=list)

    def flag(self, check_id: str) -> Optional[OperationalFlag]:
        return next((f for f in self.operational_flags if f.check_id == check_id), None)


@dataclass(frozen=True)
class BatchEvaluation:
    """§2.7 wants two tables, so the batch keeps two lists, not one blended one."""

    evaluations: List[RuleEvaluation]
    rules_version: str
    itc_variance_by_gstin: Dict[str, float] = field(default_factory=dict)

    def classification_table(self) -> List[RuleEvaluation]:
        return [e for e in self.evaluations if e.classification.fired]

    def unclassified(self) -> List[RuleEvaluation]:
        return [e for e in self.evaluations if not e.classification.fired]

    def operational_table(self, check_id: str,
                          statuses: Sequence[str] = ()) -> List[RuleEvaluation]:
        out = []
        for e in self.evaluations:
            flag = e.flag(check_id)
            if flag is None or flag.status == STATUS_NOT_APPLICABLE:
                continue
            if statuses and flag.status not in statuses:
                continue
            out.append(e)
        return out


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


class RuleEngine:
    """Deterministic evaluation of the versioned rules against §2.4 evidence."""

    def __init__(self, rules_path: str = DEFAULT_RULES_PATH,
                 prior_period_invoice_ids: Optional[Set[str]] = None):
        with open(rules_path, encoding="utf-8") as fh:
            self.rules = yaml.safe_load(fh)
        self.version = self.rules["version"]
        self.classification_rules = self.rules["classification_rules"]
        self.operational_checks = {c["id"]: c for c in self.rules["operational_checks"]}
        self._by_name = {r["name"]: r for r in self.classification_rules}
        self.prior_period_invoice_ids = (
            prior_period_invoice_ids
            if prior_period_invoice_ids is not None
            else load_prior_period_invoice_ids()
        )

    # -- classification rules (WHAT HAPPENED) -------------------------------

    def _no_rule(self, reason: str) -> Classification:
        return Classification(None, None, reason, self.version)

    def _fire(self, name: str, reason: str) -> Classification:
        rule = self._by_name[name]
        return Classification(rule["id"], rule["name"], reason, self.version)

    def _check_gstin_header_mismatch(self, ev: Evidence) -> Optional[Classification]:
        rule = self._by_name["gstin_header_mismatch"]
        cond = rule["conditions"]
        pr_gstin, b2_gstin = ev.pr_value("gstin"), ev.b2_value("gstin")
        if not pr_gstin or not b2_gstin or pr_gstin == b2_gstin:
            return None
        if len(pr_gstin) != 15 or len(b2_gstin) != 15:
            return None
        if cond["gstin_state_prefix_differs"] and pr_gstin[:2] == b2_gstin[:2]:
            return None
        if cond["pan_identical"] and pr_gstin[2:12] != b2_gstin[2:12]:
            return None

        heads = ev.fields.get("tax_heads", {})
        if cond["tax_head_profile_differs"] and heads.get("match") is not False:
            return None
        delta = heads.get("delta")
        if delta is None or abs(delta) > cond["total_tax_delta_tolerance"]:
            return None

        return self._fire(
            "gstin_header_mismatch",
            f"state prefix {pr_gstin[:2]} -> {b2_gstin[:2]} with identical PAN "
            f"{pr_gstin[2:12]}; tax heads {heads.get('pr_value')} -> "
            f"{heads.get('2b_value')} with total tax equal "
            f"(delta Rs.{abs(delta):.2f})",
        )

    def _matches_credit_note_pattern(self, delta: float, pr_amount: Optional[float],
                                     patterns) -> Optional[str]:
        for pattern in patterns:
            tolerance = pattern.get("tolerance", 1.00)
            if pattern["type"] == "fixed_amount":
                for value in pattern["values"]:
                    if abs(delta - value) <= tolerance:
                        return f"fixed credit note of Rs.{value:,.2f}"
            elif pattern["type"] == "percentage_of_taxable":
                if pr_amount:
                    for pct in pattern["values"]:
                        expected = pr_amount * pct / 100.0
                        if abs(delta - expected) <= max(tolerance, 1.00):
                            return f"{pct:g}% of the register taxable value"
        return None

    def _check_credit_note_netting(self, ev: Evidence) -> Optional[Classification]:
        rule = self._by_name["credit_note_netting"]
        cond = rule["conditions"]
        if cond.get("gstin_must_match") and ev.is_match("gstin") is not True:
            return None
        delta = ev.delta("amount")
        if delta is None or delta <= cond["minimum_delta"]:
            return None            # 2B must be LOWER than the register
        matched = self._matches_credit_note_pattern(
            delta, ev.pr_value("amount"), cond["credit_note_patterns"]
        )
        if matched is None:
            return None
        return self._fire(
            "credit_note_netting",
            f"2B is lower by Rs.{delta:,.2f}, matching {matched}",
        )

    def _check_absence(self, ev: Evidence) -> Classification:
        """§2.5: no_candidate_found -> late-filed-supplier or
        invoice-removed-post-claim, disambiguated by the prior-period snapshot."""
        invoice_id = ev.invoice_id
        in_snapshot = invoice_id in self.prior_period_invoice_ids
        if in_snapshot:
            return self._fire(
                "invoice_removed_post_claim",
                f"absent from the current GSTR-2B but present in the "
                f"prior-period snapshot as {invoice_id}",
            )
        return self._fire(
            "late_filed_supplier",
            f"absent from the current GSTR-2B and from the prior-period "
            f"snapshot; supplier has not filed {invoice_id}",
        )

    def classify(self, ev: Evidence) -> Classification:
        """Evaluate the classification rules in file order; first fired wins."""
        if not ev.candidate_found:
            return self._check_absence(ev)

        for rule in self.classification_rules:
            if rule["applies_to"] != APPLIES_MATCHED_PAIR:
                continue
            checker = getattr(self, f"_check_{rule['name']}", None)
            if checker is None:
                continue
            result = checker(ev)
            if result is not None:
                return result

        mismatched = ev.mismatched_fields()
        if not mismatched:
            return self._no_rule("all compared fields agree")
        return self._no_rule(
            "no classification rule matched; fields differing: "
            + ", ".join(mismatched)
        )

    # -- operational checks (WHAT TO DO, AND BY WHEN) -----------------------

    def check_rule_88d(self, match: Match) -> OperationalFlag:
        check = self.operational_checks["OPS-88D"]
        params = check["parameters"]
        record = match.pr_record
        raw_intimation = record.value(params["intimation_date_field"])
        current = parse_date(record.value(params["current_date_field"]))

        if not raw_intimation or current is None:
            return OperationalFlag(
                check["id"], check["name"], STATUS_NOT_APPLICABLE,
                "no Rule 88D intimation issued for this record",
            )
        intimation = parse_date(raw_intimation)
        if intimation is None:
            return OperationalFlag(
                check["id"], check["name"], STATUS_NOT_APPLICABLE,
                f"intimation date {raw_intimation!r} is unusable",
            )
        elapsed = (current - intimation).days
        window = params["window_days"]
        if elapsed < 0:
            # Intimation dated after the batch's current date: a data problem,
            # not a deadline. Reporting it as "outside the window" would be
            # actively wrong — the window has not opened yet.
            return OperationalFlag(
                check["id"], check["name"], STATUS_NOT_APPLICABLE,
                f"intimation date {intimation.isoformat()} is after the "
                f"current date {current.isoformat()}; window not yet open",
            )
        if elapsed <= window:
            return OperationalFlag(
                check["id"], check["name"], STATUS_WITHIN_WINDOW,
                f"{elapsed} of {window} days elapsed since "
                f"{intimation.isoformat()}; {window - elapsed} day(s) left to respond",
            )
        return OperationalFlag(
            check["id"], check["name"], STATUS_OUTSIDE_WINDOW,
            f"{elapsed} days elapsed since {intimation.isoformat()}; "
            f"the {window}-day response window closed "
            f"{elapsed - window} day(s) ago",
        )

    def itc_variance(self, match: Match, ev: Evidence) -> float:
        """ITC at risk for one record, in rupees.

        A no_candidate_found record puts the WHOLE claimed credit at risk; a
        matched pair risks only the difference between the two tax figures.
        """
        heads = ev.fields.get("tax_heads", {})
        pr_total = heads.get("pr_total_tax")
        if pr_total is None:
            pr_total = parse_amount(match.pr_record.value("total_tax")) or 0.0
        if not ev.candidate_found:
            return round(abs(pr_total), 2)
        b2_total = heads.get("2b_total_tax")
        if b2_total is None:
            return round(abs(pr_total), 2)
        return round(abs(pr_total - b2_total), 2)

    def check_drc_01c(self, gstin: str,
                      variance_by_gstin: Dict[str, float]) -> OperationalFlag:
        check = self.operational_checks["OPS-DRC01C"]
        threshold = check["parameters"]["threshold_rupees"]
        total = variance_by_gstin.get(gstin, 0.0)
        if total > threshold:
            return OperationalFlag(
                check["id"], check["name"], STATUS_BREACHED,
                f"cumulative ITC variance Rs.{total:,.2f} for supplier {gstin} "
                f"exceeds the Rs.{threshold:,.2f} auto-notice trigger",
            )
        return OperationalFlag(
            check["id"], check["name"], STATUS_WITHIN_THRESHOLD,
            f"cumulative ITC variance Rs.{total:,.2f} for supplier {gstin} "
            f"is within the Rs.{threshold:,.2f} trigger",
        )

    # -- batch evaluation ---------------------------------------------------

    def evaluate_batch(self, matches: Sequence[Match],
                       evidences: Sequence[Evidence]) -> BatchEvaluation:
        """Two passes: per-record rules, then the batch-level DRC-01C aggregate.

        DRC-01C is cumulative *per vendor*, so it cannot be decided from one
        record alone — hence the second pass.
        """
        if len(matches) != len(evidences):
            raise ValueError("matches and evidences must correspond one to one")

        aggregate_by = self.operational_checks["OPS-DRC01C"]["parameters"]["aggregate_by"]
        if aggregate_by != "pr_vendor_gstin":
            raise ValueError(f"unsupported DRC-01C aggregation {aggregate_by!r}")

        variance_by_gstin: Dict[str, float] = defaultdict(float)
        staged = []
        for match, ev in zip(matches, evidences):
            gstin = match.pr_record.value("vendor_gstin")
            variance_by_gstin[gstin] += self.itc_variance(match, ev)
            staged.append((match, ev, gstin))

        variance_by_gstin = {k: round(v, 2) for k, v in variance_by_gstin.items()}

        evaluations = [
            RuleEvaluation(
                record_id=match.pr_record.source_id,
                invoice_id=ev.invoice_id,
                candidate_found=ev.candidate_found,
                classification=self.classify(ev),
                operational_flags=[
                    self.check_rule_88d(match),
                    self.check_drc_01c(gstin, variance_by_gstin),
                ],
            )
            for match, ev, gstin in staged
        ]

        return BatchEvaluation(
            evaluations=evaluations,
            rules_version=self.version,
            itc_variance_by_gstin=variance_by_gstin,
        )


def load_prior_period_invoice_ids(source: str = SOURCE_PRIOR_PERIOD) -> Set[str]:
    """§2.5's disambiguation signal, read from the snapshot CSV."""
    return {r.get("invoice_id") for r in load_source(source) if r.get("invoice_id")}
