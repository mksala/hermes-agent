"""Tests for resource profile loading + bottleneck classification in timings_report.py."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "timings_report.py"
_spec = importlib.util.spec_from_file_location("timings_report", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load timings_report.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_T0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _ts(seconds: float) -> str:
    dt = _T0.timestamp() + seconds
    return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _job(name: str, dur_s: float, start_s: float = 0.0, conclusion: str = "success") -> dict:
    return {
        "name": name,
        "duration_s": dur_s,
        "conclusion": conclusion,
        "started_at": _ts(start_s),
        "completed_at": _ts(start_s + dur_s),
        "wait_s": 0.0,
    }


def _timings(jobs: list[dict]) -> dict:
    return {"run_id": "123", "head_sha": "abc", "created_at": "", "jobs": jobs}


def _profile(label: str, cpu_avg: float = 50.0, cpu_peak: float = 80.0,
             mem_peak: float = 500.0, mem_total: float = 16000.0,
             disk_ops: float = 10.0, disk_mb: float = 5.0,
             duration_s: float = 60.0) -> dict:
    return {
        "label": label,
        "duration_s": duration_s,
        "cpu": {"avg_usage_pct": cpu_avg, "peak_usage_pct": cpu_peak, "samples": 60},
        "memory": {"avg_mb": mem_peak * 0.8, "peak_mb": mem_peak,
                   "total_mb": mem_total, "samples": 60},
        "disk": {"total_mb": disk_mb, "avg_ops_per_s": disk_ops,
                 "peak_ops_per_s": disk_ops * 2, "samples": 60},
    }


# ── load_resource_profiles ──────────────────────────────────────────────

def test_load_profiles_from_directory():
    with tempfile.TemporaryDirectory() as d:
        # Simulate artifact download structure: resource-profiles/<name>/resource-profile.json
        p1 = os.path.join(d, "resource-profile-tests-slice-1")
        os.makedirs(p1)
        with open(os.path.join(p1, "resource-profile.json"), "w") as f:
            json.dump(_profile("tests-slice-1"), f)

        p2 = os.path.join(d, "resource-profile-ruff-blocking")
        os.makedirs(p2)
        with open(os.path.join(p2, "resource-profile.json"), "w") as f:
            json.dump(_profile("ruff-blocking"), f)

        profiles = _mod.load_resource_profiles(d)
        assert len(profiles) == 2
        assert "tests-slice-1" in profiles
        assert "ruff-blocking" in profiles
        assert profiles["tests-slice-1"]["cpu"]["avg_usage_pct"] == 50.0


def test_load_profiles_empty_dir():
    with tempfile.TemporaryDirectory() as d:
        profiles = _mod.load_resource_profiles(d)
        assert profiles == {}


def test_load_profiles_nonexistent_dir():
    profiles = _mod.load_resource_profiles("/nonexistent/path/xyz")
    assert profiles == {}


def test_load_profiles_malformed_json_skipped():
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "resource-profile-bad")
        os.makedirs(p1)
        with open(os.path.join(p1, "resource-profile.json"), "w") as f:
            f.write("not json {{{")
        profiles = _mod.load_resource_profiles(d)
        assert profiles == {}


# ── classify_bottleneck ─────────────────────────────────────────────────

def test_bottleneck_no_profiles_evenly_distributed():
    """Multiple short jobs, no profiles → evenly distributed."""
    t = _timings([
        _job("a", 30.0),
        _job("b", 30.0, start_s=30.0),
        _job("c", 30.0, start_s=60.0),
    ])
    verdict = _mod.classify_bottleneck(t, {})
    assert "evenly distributed" in verdict.lower()


def test_bottleneck_cpu_bound():
    """A job with >80% avg CPU → CPU-bound."""
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, cpu_peak=99.0, duration_s=120.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "cpu" in verdict.lower()
    assert "compile" in verdict
    assert "95" in verdict


def test_bottleneck_disk_io_bound():
    """A job with >500 completed disk IOs/s → disk IO-bound."""
    t = _timings([_job("io-heavy", 60.0)])
    profiles = {"io-heavy": _profile("io-heavy", cpu_avg=30.0, disk_ops=900.0, disk_mb=500.0, duration_s=60.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "disk" in verdict.lower()
    assert "io-heavy" in verdict


def test_bottleneck_memory_bound():
    """A job using >85% of total memory → memory-bound."""
    t = _timings([_job("big-mem", 60.0)])
    profiles = {"big-mem": _profile("big-mem", cpu_avg=30.0, mem_peak=14500.0,
                                    mem_total=16000.0, duration_s=60.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "memory" in verdict.lower()
    assert "big-mem" in verdict
    assert "14500" in verdict


def test_bottleneck_memory_fallback_floor_without_total():
    """Profiles without total_mb fall back to a 16 GB floor."""
    t = _timings([_job("big-mem", 60.0)])
    p = _profile("big-mem", cpu_avg=30.0, mem_peak=15000.0, duration_s=60.0)
    del p["memory"]["total_mb"]
    verdict = _mod.classify_bottleneck(t, {"big-mem": p})
    assert "memory" in verdict.lower()


def test_bottleneck_moderate_used_memory_not_memory_bound():
    """Regression: moderate absolute usage on a big node must NOT flag as
    memory-bound (the old absolute >6000 MB threshold misfired on any
    box with lots of RAM)."""
    t = _timings([_job("normal", 60.0)])
    profiles = {"normal": _profile("normal", cpu_avg=95.0, cpu_peak=99.0,
                                   mem_peak=8000.0, mem_total=32000.0,
                                   duration_s=60.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "memory" not in verdict.lower()
    assert "cpu" in verdict.lower()


def test_bottleneck_wait_bound():
    """Total wait > 50% of wall time → wait-bound."""
    # Job A runs 0-10s, Job B waits 100s then runs 10s (wait=100s, wall=110s)
    t = _timings([
        _job("fast", 10.0, start_s=0.0),
        _job("delayed", 10.0, start_s=100.0),
    ])
    # Set wait_s on the delayed job
    t["jobs"][1]["wait_s"] = 100.0
    verdict = _mod.classify_bottleneck(t, {})
    assert "wait" in verdict.lower()


def test_bottleneck_serial_no_profiles():
    """No profiles, low parallelism, one job dominates → serial bottleneck."""
    t = _timings([
        _job("slow", 300.0, start_s=0.0),
        _job("fast1", 10.0, start_s=0.0),
        _job("fast2", 10.0, start_s=0.0),
    ])
    verdict = _mod.classify_bottleneck(t, {})
    assert "slow" in verdict.lower()


def test_bottleneck_no_jobs():
    """Empty timings → insufficient data."""
    t = _timings([])
    verdict = _mod.classify_bottleneck(t, {})
    assert "insufficient" in verdict.lower()


def test_bottleneck_cpu_wins_over_disk():
    """When both CPU and disk are high, CPU should win (higher sort key)."""
    t = _timings([_job("heavy", 120.0)])
    profiles = {"heavy": _profile("heavy", cpu_avg=95.0, disk_ops=600.0, duration_s=120.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "cpu" in verdict.lower()


# ── generate_summary includes bottleneck ─────────────────────────────────

def test_summary_includes_bottleneck():
    t = _timings([_job("tests", 60.0)])
    summary = _mod.generate_summary(t, None)
    assert "Bottleneck:" in summary


def test_summary_includes_bottleneck_with_profiles():
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, duration_s=120.0)}
    summary = _mod.generate_summary(t, None, profiles)
    assert "Bottleneck:" in summary
    assert "CPU" in summary


# ── generate_review_status includes bottleneck ───────────────────────────

def test_review_status_includes_bottleneck():
    t = _timings([_job("tests", 60.0)])
    statuses = _mod.generate_review_status(t, None)
    result = statuses[0]["results"][0]
    assert "Bottleneck:" in result["summary"]


def test_review_status_includes_bottleneck_with_profiles():
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, duration_s=120.0)}
    statuses = _mod.generate_review_status(t, None, profiles=profiles)
    result = statuses[0]["results"][0]
    assert "Bottleneck:" in result["summary"]
    assert "CPU" in result["summary"]


# ── generate_html includes resource sections ─────────────────────────────

def test_html_includes_bottleneck_box():
    t = _timings([_job("tests", 60.0)])
    html = _mod.generate_html(t, None)
    assert "Bottleneck Analysis" in html


def test_html_includes_resource_table_with_profiles():
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, duration_s=120.0)}
    html = _mod.generate_html(t, None, profiles)
    assert "Resource Usage" in html
    assert "compile" in html
    assert "95%" in html


def test_html_no_resource_table_without_profiles():
    t = _timings([_job("tests", 60.0)])
    html = _mod.generate_html(t, None)
    assert "Resource Usage" not in html


# ── profile → job matching ───────────────────────────────────────────────

def test_match_pairs_profile_with_its_job():
    t = _timings([_job("Python tests / Run tests slice 3/8", 60.0)])
    matched = _mod.match_profiles_to_jobs(t, {"tests-slice-3": _profile("tests-slice-3")})
    assert matched["Python tests / Run tests slice 3/8"]["label"] == "tests-slice-3"


def test_match_is_one_to_one_across_lookalike_slices():
    """Each slice-N profile lands on slice-N, not on a same-shaped sibling.

    The eight slice labels differ in exactly one token, so a per-profile
    argmax can collapse several onto one job. Assert the pairing is a
    bijection and that every pair agrees on its slice number.
    """
    names = [f"Python tests / Run tests slice {i}/8" for i in range(1, 9)]
    t = _timings([_job(n, 60.0) for n in names])
    profiles = {f"tests-slice-{i}": _profile(f"tests-slice-{i}") for i in range(1, 9)}

    matched = _mod.match_profiles_to_jobs(t, profiles)

    assert len(matched) == len(names)
    assert set(matched) == set(names)
    assert len({p["label"] for p in matched.values()}) == len(profiles)
    for i in range(1, 9):
        assert matched[f"Python tests / Run tests slice {i}/8"]["label"] == f"tests-slice-{i}"


def test_match_rejects_unrelated_job():
    t = _timings([_job("Docs Site / docs-site-checks", 60.0)])
    assert _mod.match_profiles_to_jobs(t, {"tests-slice-1": _profile("tests-slice-1")}) == {}


def test_match_digit_mismatch_never_pairs():
    """A digit in the label must appear in the job name — no near-miss pairing."""
    t = _timings([_job("Python tests / Run tests slice 7/8", 60.0)])
    assert _mod.match_profiles_to_jobs(t, {"tests-slice-3": _profile("tests-slice-3")}) == {}


def test_match_is_order_independent():
    """Same input in a different dict order yields the same pairing."""
    names = [f"Python tests / Run tests slice {i}/8" for i in range(1, 5)]
    t = _timings([_job(n, 60.0) for n in names])
    fwd = {f"tests-slice-{i}": _profile(f"tests-slice-{i}") for i in range(1, 5)}
    rev = dict(reversed(list(fwd.items())))

    a = {k: v["label"] for k, v in _mod.match_profiles_to_jobs(t, fwd).items()}
    b = {k: v["label"] for k, v in _mod.match_profiles_to_jobs(t, rev).items()}
    assert a == b


def test_match_skips_skipped_jobs():
    t = _timings([_job("Python tests / Run tests slice 1/8", 0.0, conclusion="skipped")])
    assert _mod.match_profiles_to_jobs(t, {"tests-slice-1": _profile("tests-slice-1")}) == {}


# ── sparkline / overlay rendering ────────────────────────────────────────

def _with_series(label: str, cpu, mem, disk) -> dict:
    p = _profile(label)
    p["series"] = {"interval_s": 1.0, "points": len(cpu),
                   "cpu_pct": cpu, "mem_pct": mem, "disk_pct": disk}
    return p


def test_sparkline_inverts_y_so_100_pct_is_at_the_top():
    """A 100 sample must draw at y=0 and a 0 sample at y=100."""
    svg = _mod._sparkline_svg([100, 0], "#fff", "cpu")
    assert "0,0" in svg
    assert "1,100" in svg


def test_sparkline_closes_the_area_along_the_bottom():
    svg = _mod._sparkline_svg([50, 50, 50], "#fff", "cpu")
    pts = svg.split('points="')[1].split('"')[0]
    assert pts.startswith("0,100")
    assert pts.endswith("2,100")


def test_sparkline_viewbox_spans_the_series_indices():
    svg = _mod._sparkline_svg([10] * 7, "#fff", "cpu")
    assert 'viewBox="0 0 6 100"' in svg
    # Non-uniform scaling is what lets one SVG fit any bar width.
    assert 'preserveAspectRatio="none"' in svg


def test_sparkline_stroke_does_not_scale_with_the_viewbox():
    """Without this the stroke smears under non-uniform scaling."""
    assert 'vector-effect="non-scaling-stroke"' in _mod._sparkline_svg([1, 2], "#fff", "cpu")


def test_sparkline_empty_series_renders_nothing():
    assert _mod._sparkline_svg([], "#fff", "cpu") == ""


def test_overlay_renders_a_layer_per_populated_series():
    p = _with_series("j", [1, 2], [3, 4], [5, 6])
    assert _mod._resource_overlay(p, expanded=False).count("<svg") == 3


def test_overlay_skips_series_with_no_samples():
    p = _with_series("j", [1, 2], [], [])
    assert _mod._resource_overlay(p, expanded=False).count("<svg") == 1


def test_overlay_absent_without_series_data():
    assert _mod._resource_overlay(_profile("j"), expanded=False) == ""
    assert _mod._resource_overlay(None, expanded=False) == ""


def test_overlay_collapsed_and_expanded_are_distinguishable():
    p = _with_series("j", [1, 2], [3, 4], [5, 6])
    assert "collapsed" in _mod._resource_overlay(p, expanded=False)
    assert "expanded" in _mod._resource_overlay(p, expanded=True)


def test_gantt_emits_both_overlay_variants_for_a_matched_job():
    """A profiled job gets the in-bar strip AND the full-height expanded layer."""
    t = _timings([_job("Python tests / Run tests slice 1/8", 60.0)])
    profiles = {"tests-slice-1": _with_series("tests-slice-1", [1, 2], [3, 4], [5, 6])}

    html = _mod.generate_html(t, None, profiles)

    assert "res-overlay collapsed" in html
    assert "res-overlay expanded" in html
    # The expanded layer is positioned by .res-holder at the job bar's extent.
    assert "res-holder" in html


def test_gantt_has_no_overlay_when_nothing_matches():
    t = _timings([_job("Docs Site / docs-site-checks", 60.0)])
    profiles = {"tests-slice-1": _with_series("tests-slice-1", [1, 2], [3, 4], [5, 6])}
    # Match on the rendered element, not the stylesheet — the CSS block
    # always mentions the class names.
    assert 'class="res-overlay' not in _mod.generate_html(t, None, profiles)


def test_gantt_overlay_survives_profiles_without_series():
    """Old artifacts (pre-series) must not break the chart — table only."""
    t = _timings([_job("Python tests / Run tests slice 1/8", 60.0)])
    html = _mod.generate_html(t, None, {"tests-slice-1": _profile("tests-slice-1")})
    assert 'class="res-overlay' not in html
    assert "Resource Usage" in html


# ── overlay placement: profiled window, not the whole job ───────────────
#
# The profiler wraps ONE step, so on a job dominated by checkout/setup/post
# the samples cover a slice in the middle of the bar. Stretching them to the
# full bar puts a CPU spike under a step that never ran.

_FULL_BAR = (0.0, 1.0)


def _window(profile, job_start_s=0.0, job_dur_s=100.0):
    """(start, width) of the overlay as fractions of the job bar."""
    return _mod._profile_window_frac(
        profile,
        _mod.parse_ts(_ts(job_start_s)),
        _mod.parse_ts(_ts(job_start_s + job_dur_s)),
    )


def _profiled(start_s: float | None = None, end_s: float = 0.0):
    p = _with_series("tests", [1, 2], [3, 4], [5, 6])
    if start_s is not None:
        p["started_at"], p["completed_at"] = _ts(start_s), _ts(end_s)
    return p


def test_overlay_spans_only_the_profiled_slice_of_the_job():
    """A 30s profile inside a 100s job covers 60%..90%, not the whole bar."""
    assert _window(_profiled(60, 90)) == pytest.approx((0.6, 0.3))


def test_overlay_placement_is_relative_to_the_jobs_own_start():
    """Offsets are measured from the job's start, not the run's."""
    assert _window(_profiled(250, 275), job_start_s=200.0) == pytest.approx((0.5, 0.25))


def test_overlay_falls_back_to_full_bar_without_timestamps():
    """Profiles predating started_at/completed_at keep the old behaviour."""
    p = _profiled()
    assert "started_at" not in p
    assert _window(p) == _FULL_BAR


def test_overlay_falls_back_when_window_misses_the_job():
    """Clock skew that puts the window outside the job must not vanish it."""
    assert _window(_profiled(9000, 9030)) == _FULL_BAR


def test_overlay_clamps_a_profiler_that_outran_the_job():
    """A profile ending after the job's completed_at is clipped to the bar."""
    start, width = _window(_profiled(60, 150))
    assert (start, start + width) == pytest.approx((0.6, 1.0))


def test_overlay_keeps_a_hairline_for_a_very_short_profile():
    """A sub-percent window stays visible rather than collapsing to nothing."""
    assert _window(_profiled(50, 50.01))[1] >= 0.005


def test_gantt_positions_both_overlay_states_over_the_same_window():
    """The in-bar strip and the expanded layer must agree on the x-axis."""
    t = _timings([_job("Python tests / Run tests slice 1/8", 100.0)])
    p = _profiled(60, 90)
    p["label"] = "tests-slice-1"

    html = _mod.generate_html(t, None, {"tests-slice-1": p})

    geom = {
        kind: (round(float(l)), round(float(w)))
        for kind, l, w in re.findall(
            r'res-(holder|clip)[^>]*style="left:([\d.]+)%;width:([\d.]+)%"', html
        )
    }
    # The job spans the whole run here, so bar- and track-relative agree.
    assert geom == {"holder": (60, 30), "clip": (60, 30)}, html[:400]
