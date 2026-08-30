"""Local dashboard over the Exception Ledger pipeline.

    python3 -m src.dashboard          # http://127.0.0.1:8000

A PRESENTATION LAYER AND NOTHING ELSE. It runs the existing pipeline and
renders what comes back. It computes no reconciliation logic of its own: every
number on screen is read from a PipelineResult, an EvaluationReport, the audit
log or the frozen calibration artifact, and this module is the last thing in
the repository that could change a decision — it does not.

Two rules held throughout:

  * NOTHING IS INVENTED. Where a figure the UI wants does not exist upstream,
    the panel says so rather than deriving a plausible-looking substitute. The
    two places this bites are labelled in the UI itself: batch upload (the
    pipeline reads fixed source files) and precision (which comes from the
    stored frozen-split confusion matrix, not from a live recomputation).
  * NOTHING IS HARDCODED. There are no literal counts, rates or amounts in this
    file. Every value is rendered from the result object; a test asserts the
    page changes when the underlying run changes.

Colour encodes STATE, not series, so it uses the fixed status palette. Status
colour never carries meaning alone — every badge ships a glyph and a text
label, which is also what keeps the sub-3:1 light-surface steps legible.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import confidence as C
from . import gate as G
from . import pipeline as P
from . import report as R
from .rule_engine import (
    RuleEngine,
    STATUS_BREACHED,
    STATUS_NOT_APPLICABLE,
    STATUS_OUTSIDE_WINDOW,
    STATUS_WITHIN_WINDOW,
)
from .source_records import PIPELINE_SOURCES, REPO_ROOT

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# The three gate outcomes, in the UI's wording. INDETERMINATE_REVIEW is the
# gate's `indeterminate` — renamed here only for the screen, never in the data.
OUTCOME_LABELS = {
    G.AUTO_RECONCILE: "AUTO_RECONCILED",
    G.CLASSIFIED_EXCEPTION: "CLASSIFIED_EXCEPTION",
    G.INDETERMINATE: "INDETERMINATE_REVIEW",
}

# Status palette — fixed, never themed. Each pairs with a glyph and a label so
# hue is never the only channel.
STATUS = {
    G.AUTO_RECONCILE: ("good", "#0ca30c", "●"),
    G.CLASSIFIED_EXCEPTION: ("serious", "#ec835a", "◆"),
    G.INDETERMINATE: ("critical", "#d03b3b", "▲"),
    "quarantined": ("warning", "#fab219", "■"),
}

DISCLAIMER = (
    "Synthetic GSTR-2B-style data. No live GSTN connectivity. Not tax advice."
)
POSITIONING = R.POSITIONING
SUPPORTING_LINE = R.SUPPORTING_LINE


# ---------------------------------------------------------------------------
# Data assembly — everything below reads, nothing decides
# ---------------------------------------------------------------------------


@dataclass
class QueueRow:
    """One row of the exception / review queue.

    `itc_at_risk` is the rule engine's own per-record ITC variance, obtained by
    calling RuleEngine.itc_variance so the dashboard cannot drift from the
    DRC-01C arithmetic. `days_left` comes from the Rule 88D flag's detail.
    Neither is a new metric.
    """

    record_id: str
    invoice_id: str
    outcome: str
    rule_id: Optional[str]
    category: Optional[str]
    confidence: float
    itc_at_risk: float
    rule88d_status: str
    drc01c_status: str
    reason: str


@dataclass
class DashboardState:
    """Everything one dashboard view needs. Built once per batch run."""

    result: P.PipelineResult
    report: R.EvaluationReport
    queue: List[QueueRow] = field(default_factory=list)
    precision: Optional[Dict[str, Any]] = None
    run_number: int = 0

    def outcome_counts(self) -> Dict[str, int]:
        return self.result.outcome_counts()

    def row(self, record_id: str) -> Optional[Dict[str, Any]]:
        return self.result.by_record().get(record_id)


def _rule88d_detail(evaluation) -> Tuple[str, str]:
    flag = evaluation.flag("OPS-88D")
    return (flag.status, flag.detail) if flag else (STATUS_NOT_APPLICABLE, "")


def build_queue(result: P.PipelineResult, engine: RuleEngine) -> List[QueueRow]:
    """The exception + review queue, ordered by review risk.

    'Review risk' is an ORDERING over existing numbers, not a score: records
    whose Rule 88D window has closed come first, then those still inside a
    window, then the rest — each group sorted by ITC at risk, descending.
    Nothing here invents a severity number.
    """
    rows: List[QueueRow] = []
    for record_id, row in result.by_record().items():
        decision = row["decision"]
        if decision.outcome == G.AUTO_RECONCILE:
            continue
        evaluation = row["evaluation"]
        status88d, _ = _rule88d_detail(evaluation)
        drc = evaluation.flag("OPS-DRC01C")
        rows.append(QueueRow(
            record_id=record_id,
            invoice_id=decision.invoice_id,
            outcome=decision.outcome,
            rule_id=decision.rule_id,
            category=decision.category,
            confidence=decision.confidence.value,
            itc_at_risk=engine.itc_variance(row["match"], row["evidence"]),
            rule88d_status=status88d,
            drc01c_status=drc.status if drc else "",
            reason=decision.reason,
        ))

    priority = {STATUS_OUTSIDE_WINDOW: 0, STATUS_WITHIN_WINDOW: 1,
                STATUS_NOT_APPLICABLE: 2}
    rows.sort(key=lambda r: (priority.get(r.rule88d_status, 3), -r.itc_at_risk))
    return rows


def load_precision(split: str = R.FROZEN_TEST) -> Optional[Dict[str, Any]]:
    """Precision/recall from the STORED confusion matrix (§2.6 calibration).

    Not recomputed here: the calibration artifact already records TP/FP/TN/FN
    for both splits, and §2.6 forbids re-deriving those numbers outside the
    calibration run. Returns None if the artifact lacks the split, so the UI
    can say 'not available' instead of showing a guess.
    """
    try:
        calibration = C.load_calibration()
    except (OSError, ValueError):
        return None
    key = "frozen_test_split" if split == R.FROZEN_TEST else "calibration_split"
    block = calibration.get(key)
    if not block:
        return None
    tp, fp = block.get("true_positives"), block.get("false_positives")
    fn = block.get("false_negatives")
    if tp is None or fp is None or fn is None:
        return None
    return {
        "split": split,
        "source": f"src/rules/calibration_v2026_04.yaml -> {key}",
        "true_positives": tp, "false_positives": fp,
        "true_negatives": block.get("true_negatives"), "false_negatives": fn,
        "precision": (tp / (tp + fp)) if (tp + fp) else None,
        "recall": (tp / (tp + fn)) if (tp + fn) else None,
        "n_records": block.get("n_records"),
    }


def run_batch(db_path: Optional[str] = None, ai: bool = False,
              now: Optional[str] = None, run_number: int = 1) -> DashboardState:
    """Execute the pipeline and assemble a view over it."""
    from . import normalization as N

    client = N.build_client() if ai else None
    result = P.run(db_path=db_path or P.DEFAULT_DB_PATH, ai_client=client, now=now)
    report = R.build_report(result, R.load_splits(), R.FROZEN_TEST,
                            expected_outcomes=R.load_expected_outcomes())
    return DashboardState(
        result=result,
        report=report,
        queue=build_queue(result, RuleEngine()),
        precision=load_precision(),
        run_number=run_number,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --panel: #ffffff; --line: #e4e3df;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #7a7975;
  --good: #0ca30c; --serious: #ec835a; --critical: #d03b3b; --warning: #fab219;
  --accent: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19; --panel: #232322; --line: #3a3a37;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #96958c; --accent: #3987e5;
  }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--surface); color:var(--ink);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
a { color: var(--accent); }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 64px; }
header.masthead { border-bottom:1px solid var(--line); padding-bottom:18px; margin-bottom:22px; }
h1 { font-size:20px; margin:0 0 6px; letter-spacing:-0.01em; }
.tagline { font-size:15px; color:var(--ink); margin:0 0 4px; max-width:62ch; }
.support { font-size:13px; color:var(--ink-2); margin:0 0 10px; }
.disclaimer { display:inline-block; font-size:12px; color:var(--ink-2);
  border:1px solid var(--line); border-radius:6px; padding:5px 9px; background:var(--panel); }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--ink-2); margin:30px 0 10px; font-weight:600; }
.panel { background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:16px; }
.tiles { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); }
.tile { background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:13px 14px; }
.tile .k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-3); }
.tile .v { font-size:25px; font-variant-numeric:tabular-nums; margin-top:3px; letter-spacing:-0.02em; }
.tile .s { font-size:12px; color:var(--ink-2); margin-top:2px; }
.bar { display:flex; height:26px; border-radius:5px; overflow:hidden; gap:2px; margin:8px 0 6px; }
.seg { display:flex; align-items:center; justify-content:center; color:#fff;
  font-size:11px; font-weight:600; min-width:2px; }
.legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:var(--ink-2); }
.badge { display:inline-flex; align-items:center; gap:5px; font-size:12px;
  font-weight:600; white-space:nowrap; }
.dot { font-size:10px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--ink-3); font-weight:600; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
tbody tr:hover { background:color-mix(in srgb, var(--accent) 6%, transparent); }
.scroll { overflow-x:auto; }
code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.btn { display:inline-block; background:var(--accent); color:#fff; border:0;
  border-radius:7px; padding:9px 15px; font-size:13px; font-weight:600;
  cursor:pointer; text-decoration:none; }
.btn.sec { background:transparent; color:var(--ink); border:1px solid var(--line); }
.row { display:flex; gap:9px; flex-wrap:wrap; align-items:center; }
.note { font-size:12px; color:var(--ink-2); margin-top:9px; }
.warnbox { border-left:3px solid var(--critical); padding:9px 12px;
  background:color-mix(in srgb, var(--critical) 7%, transparent);
  border-radius:0 6px 6px 0; font-size:13px; margin-top:10px; }
.okbox { border-left:3px solid var(--good); padding:9px 12px;
  background:color-mix(in srgb, var(--good) 7%, transparent);
  border-radius:0 6px 6px 0; font-size:13px; margin-top:10px; }
.kv { display:grid; grid-template-columns:190px 1fr; gap:5px 14px; font-size:13px; }
.kv dt { color:var(--ink-3); }
.kv dd { margin:0; }
.diff-y { color:var(--good); font-weight:600; }
.diff-n { color:var(--critical); font-weight:600; }
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: Optional[float]) -> str:
    return "—" if value is None else f"₹{value:,.2f}"


def badge(outcome: str) -> str:
    """Status badge: glyph + colour + text label. Never colour alone."""
    role, colour, glyph = STATUS.get(outcome, ("", "var(--ink-2)", "•"))
    label = OUTCOME_LABELS.get(outcome, outcome.upper())
    return (f'<span class="badge" style="color:{colour}">'
            f'<span class="dot" aria-hidden="true">{glyph}</span>'
            f'<span>{esc(label)}</span></span>')


def tile(key: str, value: str, sub: str = "") -> str:
    return (f'<div class="tile"><div class="k">{esc(key)}</div>'
            f'<div class="v">{esc(value)}</div>'
            + (f'<div class="s">{esc(sub)}</div>' if sub else "") + "</div>")


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Exception Ledger</title><style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


def masthead(state: Optional[DashboardState] = None) -> str:
    """The one place the positioning appears — stated once, not repeated."""
    run = ""
    if state:
        t = state.result.throughput()
        run = (f'<div class="note">Batch run #{state.run_number} · '
               f'mode <code>{esc(t["mode"])}</code> · rules v'
               f'{esc(state.result.rules_version)} · '
               f'fingerprint <code>{esc(P.fingerprint(state.result)[:16])}</code></div>')
    return f"""<header class="masthead">
<h1><a href="/" style="text-decoration:none;color:inherit">Exception Ledger</a></h1>
<p class="tagline">{esc(POSITIONING)}</p>
<p class="support">{esc(SUPPORTING_LINE)}</p>
<span class="disclaimer">⚠ {esc(DISCLAIMER)}</span>
{run}</header>"""


def render_home(state: DashboardState) -> str:
    result, report = state.result, state.report
    counts = state.outcome_counts()
    t = result.throughput()
    scored = result.scored
    total = result.total_read
    quarantined = result.quarantined_count

    # --- batch source + run control -------------------------------------
    sources = "".join(
        f"<tr><td><code>{esc(os.path.relpath(path, REPO_ROOT))}</code></td>"
        f'<td class="num">{result.records_read.get(name, "—")}</td></tr>'
        for name, path in PIPELINE_SOURCES.items()
    )
    batch = f"""<h2>Batch</h2><div class="panel">
<div class="scroll"><table><thead><tr><th>Source file</th>
<th class="num">Rows read</th></tr></thead><tbody>{sources}</tbody></table></div>
<div class="row" style="margin-top:12px">
  <form method="post" action="/run"><button class="btn" type="submit">Run batch</button></form>
  <form method="post" action="/run"><input type="hidden" name="ai" value="1">
    <button class="btn sec" type="submit">Run with AI-assisted normalisation</button></form>
</div>
<p class="note"><strong>Upload is not offered.</strong> The pipeline reads the
fixed source files above; accepting an arbitrary batch would require changing
<code>pipeline.run()</code>, which is out of scope for this layer. Nothing here
is stubbed to look like an upload that does not work.</p></div>"""

    # --- record counts ---------------------------------------------------
    q_role, q_colour, q_glyph = STATUS["quarantined"]
    tiles = (
        tile("Total records read", f"{total:,}", "purchase register + GSTR-2B")
        + tile("Valid (scored)", f"{scored:,}",
               f"{100 * scored / total:.1f}% of rows read" if total else "")
        + tile("Quarantined", f"{quarantined:,}",
               "failed input validation — never scored")
    )

    # --- outcome distribution -------------------------------------------
    segs, legend, otiles = "", "", ""
    for outcome in G.OUTCOMES:
        n = counts.get(outcome, 0)
        pct = 100 * n / scored if scored else 0
        role, colour, glyph = STATUS[outcome]
        segs += (f'<div class="seg" style="background:{colour};flex:{max(n, 1)}"'
                 f' title="{esc(OUTCOME_LABELS[outcome])}: {n}">'
                 f'{n if pct > 6 else ""}</div>')
        legend += (f'<span class="badge" style="color:{colour}">'
                   f'<span class="dot" aria-hidden="true">{glyph}</span>'
                   f'{esc(OUTCOME_LABELS[outcome])} — {n} ({pct:.1f}%)</span>')
        otiles += tile(OUTCOME_LABELS[outcome], f"{n:,}", f"{pct:.1f}% of scored")

    # --- metrics ---------------------------------------------------------
    if state.precision and state.precision["precision"] is not None:
        p = state.precision
        precision_tile = tile(
            "Precision (auto-reconcile)", f"{100 * p['precision']:.1f}%",
            f"TP {p['true_positives']} / FP {p['false_positives']} · {p['split']}")
        precision_note = (f'<p class="note">Precision and recall are read from the '
                          f'stored confusion matrix in <code>{esc(p["source"])}</code>, '
                          f'not recomputed here — §2.6 fixes those numbers at '
                          f'calibration time. Recall '
                          f'{100 * p["recall"]:.1f}% (FN {p["false_negatives"]}).</p>')
    else:
        precision_tile = tile("Precision (auto-reconcile)", "not available",
                              "no stored confusion matrix")
        precision_note = ('<p class="note">Precision is not shown because the '
                          'calibration artifact carries no confusion matrix. '
                          'No substitute has been derived.</p>')

    mr = report.match_rate
    metrics = (
        precision_tile
        + tile("Coverage (resolved)", f"{100 * mr.rate:.1f}%",
               f"{mr.resolved}/{mr.total} on {report.split}")
        + tile("Throughput", f"{t['records_per_second']:.1f}/s",
               f"{t['elapsed_seconds']:.2f}s · {t['valid_records']} valid records")
        + tile("Estimated ITC exposure", money(report.itc_exposure_total),
               f"{money(report.itc_exposure_breaching)} with "
               f"{report.suppliers_breaching_drc01c} supplier(s) over DRC-01C")
    )

    # --- AI status -------------------------------------------------------
    if not result.ai_assisted:
        ai_box = ('<div class="okbox"><strong>Deterministic.</strong> '
                  'AI-assisted normalisation was not requested; the deterministic '
                  'half ran alone.</div>')
    elif result.ai_fell_back_entirely:
        ai_box = (f'<div class="warnbox"><strong>FALLBACK — every AI call failed.</strong> '
                  f'{result.ai_attempted} field(s) sent, {result.ai_failed} failed, '
                  f'{result.ai_applied} applied. The deterministic result stands for '
                  f'all of them. <strong>This batch is NOT AI-normalised.</strong></div>')
    else:
        ai_box = (f'<div class="okbox"><strong>AI-assisted.</strong> '
                  f'{result.ai_attempted} field(s) sent, {result.ai_applied} repaired, '
                  f'{result.ai_failed} failed or contract-rejected. AI output may only '
                  f'rewrite free text; it cannot produce a match, confidence, rule or '
                  f'outcome.</div>')

    # --- queue -----------------------------------------------------------
    rows = ""
    for q in state.queue[:60]:
        flag = ""
        if q.rule88d_status == STATUS_OUTSIDE_WINDOW:
            flag = ('<span class="badge" style="color:var(--critical)">'
                    '<span class="dot" aria-hidden="true">▲</span>88D closed</span>')
        elif q.rule88d_status == STATUS_WITHIN_WINDOW:
            flag = ('<span class="badge" style="color:var(--warning)">'
                    '<span class="dot" aria-hidden="true">■</span>88D open</span>')
        drc = ('<span class="badge" style="color:var(--critical)">'
               '<span class="dot" aria-hidden="true">▲</span>DRC-01C</span>'
               if q.drc01c_status == STATUS_BREACHED else "")
        rows += (f'<tr><td><a href="/record?id={urllib.parse.quote(q.record_id)}">'
                 f'<code>{esc(q.record_id.split(":")[-1])}</code></a></td>'
                 f'<td><code>{esc(q.invoice_id)}</code></td>'
                 f'<td>{badge(q.outcome)}</td>'
                 f'<td><code>{esc(q.rule_id or "—")}</code> {esc(q.category or "")}</td>'
                 f'<td class="num">{q.confidence:.1f}</td>'
                 f'<td class="num">{money(q.itc_at_risk)}</td>'
                 f'<td>{flag} {drc}</td></tr>')

    queue_panel = f"""<h2>Exception &amp; review queue</h2><div class="panel">
<p class="note" style="margin-top:0">{len(state.queue)} record(s) not
auto-reconciled. Ordered by <strong>review risk</strong>: Rule 88D window
closed first, then still open, then not applicable — each group by ITC at risk,
descending. That ordering uses existing figures only; no severity score is
invented. Showing the first {min(60, len(state.queue))}.</p>
<div class="scroll"><table><thead><tr><th>Record</th><th>Invoice</th>
<th>Outcome</th><th>Rule</th><th class="num">Confidence</th>
<th class="num">ITC at risk</th><th>Operational</th></tr></thead>
<tbody>{rows}</tbody></table></div></div>"""

    # --- quarantine ------------------------------------------------------
    qcounts = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td class='num'>{v}</td></tr>"
        for k, v in report.quarantined_by_error.items())
    quarantine_panel = f"""<h2>Quarantine</h2><div class="panel">
<p class="note" style="margin-top:0">
<span class="badge" style="color:{q_colour}">
<span class="dot" aria-hidden="true">{q_glyph}</span>{quarantined} quarantined</span>
— rejected at input validation. A data-quality problem, not a reconciliation
one: never counted in the match rate, the exception count or the indeterminate
count. <a href="/quarantine">View all</a></p>
<div class="scroll"><table><thead><tr><th>Validation error</th>
<th class="num">Records</th></tr></thead><tbody>{qcounts}</tbody></table></div></div>"""

    exports = """<h2>Export</h2><div class="panel"><div class="row">
<a class="btn sec" href="/export/decisions.csv">Decisions (CSV)</a>
<a class="btn sec" href="/export/queue.csv">Queue (CSV)</a>
<a class="btn sec" href="/export/quarantine.csv">Quarantine (CSV)</a>
<a class="btn sec" href="/export/report.json">Evaluation report (JSON)</a>
<a class="btn sec" href="/export/report.txt">Evaluation report (TXT)</a>
<a class="btn sec" href="/export/audit.csv">Audit log (CSV)</a>
</div><p class="note">Generated from this run. CSV and JSON are the pipeline's
own values; TXT is the §2.7 evaluation report verbatim.</p></div>"""

    return page("Dashboard", f"""{masthead(state)}
<h2>Records</h2><div class="tiles">{tiles}</div>
<h2>Gate outcomes</h2><div class="panel">
<div class="bar">{segs}</div><div class="legend">{legend}</div></div>
<div class="tiles" style="margin-top:10px">{otiles}</div>
<h2>Metrics</h2><div class="tiles">{metrics}</div>{precision_note}
<h2>Normalisation mode</h2><div class="panel">{ai_box}</div>
{queue_panel}{quarantine_panel}{batch}{exports}""")


def render_record(state: DashboardState, record_id: str) -> Optional[str]:
    """Raw vs normalised evidence, rule, confidence, threshold and audit."""
    row = state.row(record_id)
    if row is None:
        return None
    match, evidence = row["match"], row["evidence"]
    evaluation, decision = row["evaluation"], row["decision"]
    audit = next((a for a in state.result.audit_entries
                  if a.record_id == record_id), None)

    # --- evidence: raw vs normalised, field by field ---------------------
    ev_rows = ""
    for name, entry in evidence.field_map().items():
        ok = entry.get("match")
        mark = ('<span class="diff-y">match</span>' if ok is True
                else '<span class="diff-n">differs</span>')
        delta = entry.get("delta")
        ev_rows += (f"<tr><td><code>{esc(name)}</code></td>"
                    f"<td>{esc(entry.get('pr_value'))}</td>"
                    f"<td>{esc(entry.get('2b_value'))}</td>"
                    f"<td class='num'>{esc(delta) if delta is not None else '—'}</td>"
                    f"<td>{mark}</td></tr>")

    # --- raw vs normalised source values ---------------------------------
    def norm_table(record, title):
        if record is None:
            return f"<p class='note'>{esc(title)}: no counterpart (no_candidate_found).</p>"
        changed = {c.field: c for c in record.changes}
        body = ""
        for key in record.raw:
            change = changed.get(key)
            style = ' style="background:color-mix(in srgb,var(--warning) 12%,transparent)"' \
                if change else ""
            method = f"<code>{esc(change.method)}</code>" if change else "—"
            body += (f"<tr{style}><td><code>{esc(key)}</code></td>"
                     f"<td>{esc(record.raw.get(key))}</td>"
                     f"<td>{esc(record.normalized.get(key))}</td>"
                     f"<td>{method}</td></tr>")
        return (f"<h2>{esc(title)} — raw vs normalised</h2><div class='panel scroll'>"
                f"<table><thead><tr><th>Field</th><th>Raw</th><th>Normalised</th>"
                f"<th>Changed by</th></tr></thead><tbody>{body}</tbody></table>"
                f"<p class='note'>Highlighted rows were altered by §2.2 "
                f"normalisation. Raw values are preserved verbatim.</p></div>")

    flags = "".join(
        f"<tr><td><code>{esc(f.check_id)}</code></td><td>{esc(f.name)}</td>"
        f"<td>{esc(f.status)}</td><td>{esc(f.detail)}</td></tr>"
        for f in evaluation.operational_flags)

    contrib = ", ".join(f"{k}={v:g}" for k, v in
                        sorted(decision.confidence.contributions.items()) if v)
    components = ", ".join(f"{k}={v:g}" for k, v in match.components.items()) \
        if match.components else "—"

    audit_block = "<p class='note'>No audit row.</p>"
    if audit:
        audit_block = f"""<dl class="kv">
<dt>record_id</dt><dd><code>{esc(audit.record_id)}</code></dd>
<dt>action</dt><dd>{badge(audit.action)}</dd>
<dt>rule_id_fired</dt><dd><code>{esc(audit.rule_id_fired or 'null')}</code></dd>
<dt>confidence_score</dt><dd>{esc(audit.confidence_score)}</dd>
<dt>reviewer_decision</dt><dd><code>{esc(audit.reviewer_decision or 'null')}</code>
 — nullable; the pipeline never writes it</dd>
<dt>timestamp</dt><dd><code>{esc(audit.timestamp)}</code></dd>
<dt>evidence_snapshot</dt><dd>{len(audit.evidence_snapshot)} bytes of JSON,
 logged verbatim</dd></dl>"""

    return page(f"Record {record_id}", f"""{masthead(state)}
<p><a href="/">← Back to dashboard</a></p>
<h2>Decision</h2><div class="panel"><dl class="kv">
<dt>Record</dt><dd><code>{esc(record_id)}</code></dd>
<dt>Invoice</dt><dd><code>{esc(decision.invoice_id)}</code></dd>
<dt>Outcome</dt><dd>{badge(decision.outcome)}</dd>
<dt>Rule ID / version</dt><dd><code>{esc(decision.rule_id or 'none fired')}</code>
 {esc(decision.category or '')} · rules v{esc(evaluation.classification.rules_version)}</dd>
<dt>Confidence</dt><dd><strong>{decision.confidence.value:.1f}</strong> / 100
 &nbsp; threshold <strong>{decision.threshold:g}</strong> &nbsp;
 clean match: {esc(decision.confidence.clean_match)}</dd>
<dt>Confidence from</dt><dd class="mono">{esc(contrib) or 'no field matched'}</dd>
<dt>Match</dt><dd><code>{esc(match.b2_id or 'no_candidate_found')}</code>
 &nbsp; score {match.score:.1f}</dd>
<dt>Score components</dt><dd class="mono">{esc(components)}</dd>
<dt>Reason</dt><dd>{esc(decision.reason)}</dd></dl></div>
<h2>Evidence comparison (§2.4)</h2><div class="panel scroll">
<table><thead><tr><th>Field</th><th>Purchase register</th><th>GSTR-2B</th>
<th class="num">Delta</th><th>Result</th></tr></thead><tbody>{ev_rows}</tbody></table></div>
<h2>Operational checks (§2.5)</h2><div class="panel scroll"><table><thead><tr>
<th>Check</th><th>Name</th><th>Status</th><th>Detail</th></tr></thead>
<tbody>{flags}</tbody></table></div>
{norm_table(match.pr_record, "Purchase register")}
{norm_table(match.b2_record, "GSTR-2B")}
<h2>Audit log (§2.7)</h2><div class="panel">{audit_block}</div>""")


def render_quarantine(state: DashboardState) -> str:
    rows = "".join(
        f"<tr><td><code>{esc(e.source_record_id)}</code></td>"
        f"<td>{esc(e.source)}</td><td class='num'>{e.source_row_number}</td>"
        f"<td><code>{esc(e.validation_error)}</code></td>"
        f"<td><code>{esc(e.error_field)}</code></td>"
        f"<td>{esc(e.validation_message)}</td></tr>"
        for e in state.result.quarantined)
    return page("Quarantine", f"""{masthead(state)}
<p><a href="/">← Back to dashboard</a></p>
<h2>Quarantined records</h2><div class="panel">
<p class="note" style="margin-top:0">{state.result.quarantined_count} record(s)
failed §2.1 input validation and exited the pipeline. They were never
normalised, matched, scored or audited, and are counted separately from every
figure on the dashboard.</p>
<div class="scroll"><table><thead><tr><th>Record</th><th>Source</th>
<th class="num">Row</th><th>Error type</th><th>Field</th><th>Message</th>
</tr></thead><tbody>{rows}</tbody></table></div></div>""")


# ---------------------------------------------------------------------------
# Exports — CSV, JSON, TXT, all from this run
# ---------------------------------------------------------------------------


def _csv(header: List[str], rows) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def export_decisions_csv(state: DashboardState) -> str:
    joined = state.result.by_record()
    return _csv(
        ["record_id", "invoice_id", "outcome", "rule_id", "category",
         "confidence", "threshold", "clean_match", "matched_2b_record",
         "match_score", "matched_fields", "mismatched_fields", "reason"],
        [[d.record_id, d.invoice_id, d.outcome, d.rule_id or "", d.category or "",
          f"{d.confidence.value:.2f}", f"{d.threshold:g}",
          d.confidence.clean_match,
          joined[d.record_id]["match"].b2_id or "",
          f"{joined[d.record_id]['match'].score:.2f}",
          "|".join(joined[d.record_id]["evidence"].matched_fields()),
          "|".join(joined[d.record_id]["evidence"].mismatched_fields()),
          d.reason]
         for d in state.result.decisions])


def export_queue_csv(state: DashboardState) -> str:
    return _csv(
        ["rank", "record_id", "invoice_id", "outcome", "rule_id", "category",
         "confidence", "itc_at_risk", "rule88d_status", "drc01c_status", "reason"],
        [[i, q.record_id, q.invoice_id, q.outcome, q.rule_id or "",
          q.category or "", f"{q.confidence:.2f}", f"{q.itc_at_risk:.2f}",
          q.rule88d_status, q.drc01c_status, q.reason]
         for i, q in enumerate(state.queue, start=1)])


def export_quarantine_csv(state: DashboardState) -> str:
    return _csv(
        ["record_id", "source", "source_record_id", "source_row_number",
         "validation_error", "error_field", "validation_message",
         "raw_record_snapshot", "timestamp"],
        [[e.record_id, e.source, e.source_record_id, e.source_row_number,
          e.validation_error, e.error_field, e.validation_message,
          e.raw_record_snapshot, e.timestamp]
         for e in state.result.quarantined])


def export_audit_csv(state: DashboardState) -> str:
    return _csv(
        ["record_id", "invoice_id", "action", "rule_id_fired", "category",
         "confidence_score", "reviewer_decision", "timestamp",
         "evidence_snapshot"],
        [[a.record_id, a.invoice_id, a.action, a.rule_id_fired or "",
          a.category or "", a.confidence_score, a.reviewer_decision or "",
          a.timestamp, a.evidence_snapshot]
         for a in state.result.audit_entries])


def export_report_json(state: DashboardState) -> str:
    payload = R.to_dict(state.report)
    payload["disclaimer"] = DISCLAIMER
    payload["run"] = {
        "mode": state.result.mode,
        "fingerprint": P.fingerprint(state.result),
        "rules_version": state.result.rules_version,
        "throughput": state.result.throughput(),
        "ai_stats": state.result.ai_stats,
    }
    payload["queue"] = [
        {"record_id": q.record_id, "invoice_id": q.invoice_id,
         "outcome": q.outcome, "rule_id": q.rule_id, "category": q.category,
         "confidence": q.confidence, "itc_at_risk": q.itc_at_risk,
         "rule88d_status": q.rule88d_status, "drc01c_status": q.drc01c_status}
        for q in state.queue
    ]
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def export_report_txt(state: DashboardState) -> str:
    return R.render(state.report) + "\n"


EXPORTS = {
    "/export/decisions.csv": ("text/csv", export_decisions_csv),
    "/export/queue.csv": ("text/csv", export_queue_csv),
    "/export/quarantine.csv": ("text/csv", export_quarantine_csv),
    "/export/audit.csv": ("text/csv", export_audit_csv),
    "/export/report.json": ("application/json", export_report_json),
    "/export/report.txt": ("text/plain", export_report_txt),
}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class DashboardApp:
    """Holds the current batch state. One run at a time, guarded by a lock."""

    def __init__(self, now: Optional[str] = None, db_path: Optional[str] = None):
        self._lock = threading.Lock()
        self._now = now
        self._db_path = db_path
        self._runs = 0
        self.state: Optional[DashboardState] = None

    def run(self, ai: bool = False) -> DashboardState:
        with self._lock:
            self._runs += 1
            self.state = run_batch(db_path=self._db_path, ai=ai, now=self._now,
                                   run_number=self._runs)
            return self.state

    def ensure(self) -> DashboardState:
        return self.state if self.state is not None else self.run()


class Handler(BaseHTTPRequestHandler):
    app: DashboardApp = None            # injected by serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):       # keep the console quiet
        pass

    def _send(self, body: str, content_type: str = "text/html",
              status: int = 200, filename: Optional[str] = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if filename:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        state = self.app.ensure()

        if path == "/":
            self._send(render_home(state))
        elif path == "/quarantine":
            self._send(render_quarantine(state))
        elif path == "/record":
            record_id = (query.get("id") or [""])[0]
            body = render_record(state, record_id)
            if body is None:
                self._send(page("Not found", masthead(state) +
                                f"<h2>No such record</h2><div class='panel'>"
                                f"<code>{esc(record_id)}</code> is not in this "
                                f"batch. It may have been quarantined — see "
                                f"<a href='/quarantine'>quarantine</a>.</div>"),
                           status=404)
            else:
                self._send(body)
        elif path in EXPORTS:
            content_type, builder = EXPORTS[path]
            self._send(builder(state), content_type,
                       filename=os.path.basename(path))
        else:
            self._send(page("Not found", masthead(state) +
                            "<h2>Not found</h2><div class='panel'>"
                            "<a href='/'>Back to dashboard</a></div>"), status=404)

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/run":
            self._send("not found", "text/plain", status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        self.app.run(ai=bool(form.get("ai")))
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                now: Optional[str] = None,
                db_path: Optional[str] = None) -> Tuple[ThreadingHTTPServer, DashboardApp]:
    app = DashboardApp(now=now, db_path=db_path)
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler), app


def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python3 -m src.dashboard",
        description="Local dashboard over the Exception Ledger pipeline.")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--now", default=None, help="pin log timestamps")
    args = ap.parse_args(argv)

    server, app = make_server(args.host, args.port, now=args.now)
    print("Exception Ledger — dashboard")
    print(f"  {DISCLAIMER}\n")
    print(f"  running the first batch...")
    state = app.ensure()
    counts = state.outcome_counts()
    print(f"  {state.result.total_read} rows read, {state.result.scored} scored, "
          f"{state.result.quarantined_count} quarantined")
    print(f"  {counts[G.AUTO_RECONCILE]} auto-reconciled, "
          f"{counts[G.CLASSIFIED_EXCEPTION]} classified, "
          f"{counts[G.INDETERMINATE]} indeterminate\n")
    print(f"  http://{args.host}:{args.port}/    (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
