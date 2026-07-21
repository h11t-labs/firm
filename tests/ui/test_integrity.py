"""Specs for the dashboard tamper-evidence verify-status panel (design D22-D25).

Three layers, mirroring the split in :mod:`firm.ui.audit_queries` / :mod:`firm.ui.render`:

* :func:`integrity_state` is pure, so the whole six-row state table (plus ``verify_max_age``
  forcing and anchor-absent-by-design) is asserted directly without a database;
* the two query helpers (:func:`verify_status_row`, :func:`integrity_config`) are checked against
  a real (seeded) audit schema;
* rendering per state and the overview escalation go through :mod:`firm.ui.render`.
"""

from __future__ import annotations

import importlib.resources
from datetime import datetime, timedelta

from firm.ui import audit_queries, render
from firm.ui.audit_queries import IntegrityConfig, integrity_state

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
        "cycle_position": 3,
        "cycle_length": 7,
        "newest_anchor_at": NOW - timedelta(seconds=30),
        "anchor_configured": True,
        "unsealed_tail_count": 2,
        "unsealed_tail_oldest_at": NOW - timedelta(seconds=30),
        "affected_identifiers": None,
        "duration_seconds": 1.2,
    }
    base.update(over)
    return base


# -- state derivation: the six state-table rows ------------------------------------------------


def test_state_not_configured_when_no_key_and_no_status() -> None:
    st = integrity_state(None, _cfg(key=False, sealing=False), now=NOW)
    assert st.state == "not_configured"
    assert st.tone == "neutral"
    assert st.escalate is False


def test_state_never_ran_when_configured_but_no_status() -> None:
    st = integrity_state(None, _cfg(key=True, sealing=True), now=NOW)
    assert st.state == "never_ran"
    assert st.tone == "warn"
    assert st.escalate is True  # amber liveness -> shows on overview too


def test_state_ok_when_fresh_and_clean() -> None:
    st = integrity_state(_status(), _cfg(), now=NOW)
    assert st.state == "ok"
    assert st.tone == "ok"
    assert st.escalate is False
    assert st.causes == ()


def test_state_warning_on_late_commit_does_not_escalate() -> None:
    st = integrity_state(_status(outcome="warning", warning_count=2), _cfg(), now=NOW)
    assert st.state == "warning"
    assert "late_commits" in st.causes
    assert st.escalate is False  # benign late commit stays on the audit tab


def test_state_error_is_amber_and_escalates() -> None:
    st = integrity_state(
        _status(outcome="error", error_message="unknown key_id ab12"), _cfg(), now=NOW
    )
    assert st.state == "error"
    assert st.tone == "warn"  # red stays reserved for proven tampering (D24)
    assert st.escalate is True


def test_state_tampered_dominates_everything() -> None:
    st = integrity_state(_status(outcome="tampered", tampered_count=3), _cfg(), now=NOW)
    assert st.state == "tampered"
    assert st.tone == "danger"
    assert st.escalate is True


def test_tampered_count_alone_forces_tampered() -> None:
    # A stored outcome that lags the counts must not soften a real finding.
    st = integrity_state(_status(outcome="ok", tampered_count=1), _cfg(), now=NOW)
    assert st.state == "tampered"


# -- staleness forcing + anchor-absent-by-design -----------------------------------------------


def test_verify_max_age_forces_amber_over_a_stored_ok() -> None:
    old = _status(ran_at=NOW - timedelta(hours=30))  # a nightly cron that skipped a day
    st = integrity_state(old, _cfg(), now=NOW, verify_max_age=24 * 3600.0)
    assert st.state == "warning"
    assert "stale" in st.causes
    assert st.escalate is True  # a dead verify cron is a liveness alarm


def test_fresh_run_within_max_age_stays_ok() -> None:
    st = integrity_state(_status(ran_at=NOW - timedelta(hours=1)), _cfg(), now=NOW)
    assert st.state == "ok"


def test_anchor_absent_by_design_never_reads_as_stale() -> None:
    # No anchor configured: an aggressive threshold must not manufacture an "anchor stale" amber.
    st = integrity_state(
        _status(anchor_configured=False, newest_anchor_at=None),
        _cfg(),
        now=NOW,
        anchor_max_age=1.0,
    )
    assert st.state == "ok"
    assert "anchor_stale" not in st.causes


def test_configured_anchor_past_threshold_warns() -> None:
    st = integrity_state(
        _status(anchor_configured=True, newest_anchor_at=NOW - timedelta(hours=1)),
        _cfg(),
        now=NOW,
        anchor_max_age=60.0,
    )
    assert st.state == "warning"
    assert "anchor_stale" in st.causes
    assert st.escalate is False  # a lagging anchor sink is not a pipeline-liveness alarm


def test_stalled_sealer_escalates() -> None:
    st = integrity_state(
        _status(unsealed_tail_oldest_at=NOW - timedelta(hours=30), unsealed_tail_count=500),
        _cfg(),
        now=NOW,
        verify_max_age=24 * 3600.0,
    )
    assert st.state == "warning"
    assert "sealer_stalled" in st.causes
    assert st.escalate is True


# -- query helpers -----------------------------------------------------------------------------


def test_verify_status_row_none_when_never_run(runtime) -> None:
    with runtime.engine.connect() as conn:
        assert audit_queries.verify_status_row(conn) is None


def test_verify_status_row_reads_upserted_row(runtime, seed) -> None:
    seed.verify_status(outcome="warning", warning_count=4, unsealed_tail_count=7)
    with runtime.engine.connect() as conn:
        row = audit_queries.verify_status_row(conn)
    assert row is not None
    assert row["outcome"] == "warning"
    assert row["warning_count"] == 4
    assert row["unsealed_tail_count"] == 7


def test_verify_status_row_takes_newest_when_several_exist(runtime, seed) -> None:
    seed.verify_status(outcome="ok", ran_at=NOW - timedelta(days=1))
    seed.verify_status(outcome="tampered", tampered_count=1, ran_at=NOW)
    with runtime.engine.connect() as conn:
        row = audit_queries.verify_status_row(conn)
    assert row is not None
    assert row["outcome"] == "tampered"


def test_integrity_config_without_seals_is_inactive(runtime) -> None:
    with runtime.engine.connect() as conn:
        cfg = audit_queries.integrity_config(conn, key_configured=False)
    assert cfg.key_configured is False
    assert cfg.sealing_active is False
    assert cfg.sealing_since is None


def test_integrity_config_reads_activation_from_oldest_seal(runtime, seed) -> None:
    first = NOW - timedelta(days=2)
    seed.seal(seq=1, sealed_at=first)
    seed.seal(seq=2, sealed_at=NOW)
    with runtime.engine.connect() as conn:
        cfg = audit_queries.integrity_config(conn, key_configured=True)
    assert cfg.key_configured is True
    assert cfg.sealing_active is True
    assert cfg.sealing_since == first


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
    assert "cycle 3/7" in body
    assert 'role="alert"' not in body


def test_render_warning_itemizes_the_cause(runtime) -> None:
    body = _audit_html(_state(_status(outcome="warning", warning_count=2)))
    assert 'class="integrity warn"' in body
    assert 'class="integrity-icon"' in body
    assert ">Warning</span>" in body
    assert "2 late commits in a sealed range" in body


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
        '{"kind": "row", "label": "row 42", "id": 42, '
        '"message": "row 42 row_mac does not recompute (modified)", "verdict": "tampered"},'
        '{"kind": "seal", "label": "seal 12", '
        '"message": "seal seq 12 range no longer matches its rows_mac/row_count", '
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
    # The real per-finding "what/why" is surfaced, not just the generic sentence.
    assert "row 42 row_mac does not recompute (modified)" in body
    assert "seal seq 12 range no longer matches its rows_mac/row_count" in body
    assert 'class="integrity-items"' in body
    assert "Affected" in body
    assert 'href="/audit/42"' in body  # the row-level finding links into the audit table
    assert 'class="integrity-chip"' in body
    assert "firm-audit verify --full" in body  # the verify command
    assert render._TAMPER_DOCS_URL in body  # runbook link


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
    assert 'class="integrity-affected"' in tampered_body
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


def test_overview_hides_benign_late_commit_warning(runtime) -> None:
    body = _overview_html(_state(_status(outcome="warning", warning_count=2)))
    assert 'class="integrity' not in body


def test_overview_no_audit_part_renders_nothing_integrity(runtime) -> None:
    body = render.overview_page(["queue"], {}, [], [], [], integrity=None)
    assert 'class="integrity' not in body
