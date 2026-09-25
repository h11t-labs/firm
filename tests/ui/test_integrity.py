"""Specs for how the dashboard renders the tamper-evidence integrity state (design D22-D25).

The state itself is derived by :func:`firm.audit.queries.integrity_state` (specced in
``tests/audit/test_integrity_status.py``); this file covers the panel :mod:`firm.ui.render` builds
from it, per state, plus the overview escalation.
"""

from __future__ import annotations

import importlib.resources
from datetime import datetime, timedelta

from firm.audit.queries import IntegrityConfig, integrity_state
from firm.ui import render

NOW = datetime(2026, 7, 20, 12, 0, 0)


def _cfg(
    *, key: bool = True, sealing: bool = True, since: datetime | None = None
) -> IntegrityConfig:
    return IntegrityConfig(
        key_configured=key,
        sealing_active=sealing,
        sealing_since=since or datetime(2026, 7, 1, 0, 0, 0),
    )


def _status(**over: object) -> dict[str, object]:
    """A healthy, freshly-verified status row; override individual fields per test."""
    base: dict[str, object] = {
        "ran_at": NOW - timedelta(minutes=5),
        "outcome": "ok",
        "ok_count": 10,
        "warning_count": 0,
        "unprotected_count": 0,
        "tampered_count": 0,
        "error_message": None,
        "last_full_coverage_at": NOW - timedelta(hours=6),
        "newest_anchor_at": NOW - timedelta(seconds=30),
        "anchor_configured": True,
        "unsealed_tail_count": 2,
        "unsealed_tail_oldest_at": NOW - timedelta(seconds=30),
        "affected_identifiers": None,
        "duration_seconds": 1.2,
    }
    base.update(over)
    return base


# -- rendering per state -----------------------------------------------------------------------

_EMPTY_STATS = {"events": 0, "actions": 0, "last_event_at": None}
_EMPTY_FILTERS = {"action": "", "subject": "", "actor": "", "correlation_id": ""}


def _audit_html(state) -> str:
    return render.audit_page(["audit"], _EMPTY_STATS, [], _EMPTY_FILTERS, integrity=state)


def _state(status, config=None, **kw):
    return integrity_state(status, config or _cfg(), now=NOW, **kw)


def test_render_ok_is_a_calm_strip(runtime) -> None:
    body = _audit_html(_state(_status()))
    assert 'class="integrity ok"' in body
    assert 'class="integrity-icon"' in body  # shield medallion anchors the verdict
    assert 'class="integrity-verdict"' in body  # "Integrity  OK", label + status word
    assert ">Integrity</span>" in body
    assert ">OK</span>" in body
    # The strip's facts are labelled units, not one grey run-on.
    assert 'class="integrity-facts"' in body
    assert "verified" in body  # freshness — the primary fact
    assert "unsealed tail" in body  # label
    assert "2 rows" in body  # value
    assert "cycle" not in body
    assert 'role="alert"' not in body


def test_render_warning_itemizes_the_cause(runtime) -> None:
    body = _audit_html(_state(_status(outcome="warning", warning_count=2)))
    assert 'class="integrity warn"' in body
    assert 'class="integrity-icon"' in body
    assert ">Warning</span>" in body
    assert "verify reported 2 warnings" in body


def test_render_error_carries_the_failure_message(runtime) -> None:
    body = _audit_html(_state(_status(outcome="error", error_message="unknown key_id ab12")))
    assert 'class="integrity warn"' in body
    assert ">Error</span>" in body
    assert "verify failed: unknown key_id ab12" in body


def test_render_tampered_is_a_banner_with_links_and_next_step(runtime) -> None:
    # The structured findings the verifier now persists: a row-level finding (linkable id + its own
    # message) and a seal-level finding (a message, no link).
    affected = (
        "["
        '{"kind": "row", "label": "#42 invoice.paid", "id": 42, '
        '"message": "modified after it was sealed (signature no longer matches)", '
        '"verdict": "tampered"},'
        '{"kind": "seal", "label": "sealed range (11, 12]", '
        '"message": "records deleted, inserted, or swapped in this sealed range", '
        '"verdict": "tampered"}'
        "]"
    )
    status = _status(outcome="tampered", tampered_count=2, affected_identifiers=affected)
    body = _audit_html(_state(status))
    assert 'role="alert"' in body
    assert "integrity danger banner" in body
    assert 'class="integrity-icon"' in body
    assert "TAMPERED" in body
    assert "2 findings" in body
    assert "no longer matches its signatures" in body  # plain-language framing (the lead)
    # One findings list: each affected record's identity + its plain-language reason, no separate
    # pill row echoing the same labels.
    assert 'class="integrity-findings"' in body
    assert "#42 invoice.paid" in body  # the record's real identity, not "row 42"
    assert "modified after it was sealed (signature no longer matches)" in body
    assert "sealed range (11, 12]" in body
    assert 'href="/audit/42"' in body  # the row-level finding links into the audit table
    assert 'class="finding-ref"' in body
    assert "firm-audit verify --full" in body  # the verify command
    assert render._TAMPER_DOCS_URL in body  # runbook link


def test_render_tampered_survives_deeply_nested_affected_json(runtime) -> None:
    # Bug #3. A deeply-nested affected_identifiers blob must never crash the tampered banner — it is
    # rendered on every audit-page request, so an uncaught parse error (RecursionError on scanners
    # that recurse) is a persistent 500 DoS. Rendering must complete regardless of nesting depth.
    deep = "[" * 5000 + "]" * 5000
    status = _status(outcome="tampered", tampered_count=1, affected_identifiers=deep)
    body = _audit_html(_state(status))  # must not raise
    assert "TAMPERED" in body


def test_render_tampered_rejects_oversized_affected_json(runtime) -> None:
    huge = '[{"kind":"seal","label":"#1","verdict":"tampered"}]' + "x" * (render._MAX_AFFECTED_JSON)
    status = _status(outcome="tampered", tampered_count=1, affected_identifiers=huge)
    body = _audit_html(_state(status))  # must not raise, must not echo the blob
    assert "integrity findings unavailable" in body
    assert "xxxxxxxx" not in body


def test_render_tampered_without_messages_falls_back_to_generic_meaning(runtime) -> None:
    # Legacy / degraded data: chips but no per-finding messages — the generic sentence stands alone.
    affected = '[{"kind": "seal", "label": "#12", "id": 4041}]'
    status = _status(outcome="tampered", tampered_count=1, affected_identifiers=affected)
    body = _audit_html(_state(status))
    assert "no longer matches its signatures" in body
    assert 'class="integrity-items"' not in body  # nothing to itemize
    assert 'href="/audit/4041"' in body


def test_mobile_wrap_contract_is_present(runtime) -> None:
    css = importlib.resources.files("firm.ui").joinpath("static", "style.css").read_text()
    media_query = "@media (max-width: 560px)"
    assert media_query in css
    mobile_css = css.split(media_query, maxsplit=1)[1]
    compact_mobile_css = mobile_css.replace(" ", "")
    # The banner drops its timestamp below the title and gives links a >=44px touch target.
    assert ".integrity.banner .integrity-when" in mobile_css
    assert "flex-basis:100%" in compact_mobile_css
    assert ".integrity.banner a" in mobile_css
    assert ".integrity-next a" in mobile_css
    assert "min-height:44px" in compact_mobile_css

    ok_body = _audit_html(_state(_status()))
    assert 'class="integrity-facts"' in ok_body

    affected = '[{"kind": "seal", "label": "#12", "id": 4041}]'
    tampered = _status(outcome="tampered", tampered_count=1, affected_identifiers=affected)
    tampered_body = _audit_html(_state(tampered))
    assert 'class="integrity-findings"' in tampered_body
    assert 'class="integrity-next' in tampered_body


def test_render_never_ran_points_at_the_cron(runtime) -> None:
    body = _audit_html(integrity_state(None, _cfg(key=True), now=NOW))
    assert "Never verified" in body
    assert "schedule a firm-audit verify cron" in body


def test_render_not_configured_is_neutral(runtime) -> None:
    body = _audit_html(integrity_state(None, _cfg(key=False, sealing=False), now=NOW))
    assert 'class="integrity neutral"' in body
    assert "Not configured" in body
    assert "set FIRM_AUDIT_KEY" in body
    assert 'role="alert"' not in body


def test_render_tampered_escapes_affected_label(runtime) -> None:
    affected = '[{"kind": "seal", "label": "<script>x</script>", "id": 5}]'
    status = _status(outcome="tampered", tampered_count=1, affected_identifiers=affected)
    body = _audit_html(_state(status))
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


# -- overview escalation (D23) -----------------------------------------------------------------


def _overview_html(state) -> str:
    return render.overview_page(["queue", "audit"], {}, [], [], [], integrity=state)


def test_overview_shows_tampered_banner(runtime) -> None:
    body = _overview_html(_state(_status(outcome="tampered", tampered_count=1)))
    assert "TAMPERED" in body
    assert "integrity danger" in body


def test_overview_shows_amber_liveness(runtime) -> None:
    state = _state(_status(ran_at=NOW - timedelta(hours=30)), verify_max_age=24 * 3600.0)
    body = _overview_html(state)
    assert 'class="integrity warn"' in body


def test_overview_hides_ok_strip(runtime) -> None:
    body = _overview_html(_state(_status()))
    assert "integrity" not in body  # the calm OK strip stays audit-only


def test_overview_hides_non_liveness_verifier_warning(runtime) -> None:
    body = _overview_html(_state(_status(outcome="warning", warning_count=2)))
    assert 'class="integrity' not in body


def test_overview_no_audit_part_renders_nothing_integrity(runtime) -> None:
    body = render.overview_page(["queue"], {}, [], [], [], integrity=None)
    assert 'class="integrity' not in body
