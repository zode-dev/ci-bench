"""Tests for scripts/site-data.py. ci-bench's own CI runs these on Zode with
the sample workflow from the docs (.github/workflows/test.yml)."""
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("site_data", os.path.join(HERE, "scripts", "site-data.py"))
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


def run(provider, wall, conclusion="success", jobs=None, run_id=1, queued=2.0, day="15"):
    started = "2026-11-%sT02:00:%02d.000Z" % (day, int(queued))
    return {"repo": "x", "provider": provider, "runner": "zode-4x8" if provider == "zode" else "ubuntu-24.04",
            "mode": "cold", "iteration": run_id, "run_id": run_id, "run_url": "https://example/%d" % run_id,
            "conclusion": conclusion, "dispatched_at": "2026-11-%sT02:00:00.000Z" % day,
            "completed_at": "2026-11-%sT02:10:00.000Z" % day, "wall_s": wall, "queue_s_max": 2.0,
            "run_s_sum": wall - 2, "jobs": jobs or [{"run_s": wall - queued, "started_at": started,
                                                    "conclusion": conclusion, "zode": None}]}


def test_median_run_is_a_real_run_lower_middle_when_even():
    rs = [run("github", w, run_id=i) for i, w in enumerate([50, 10, 40, 20])]
    assert sd.median_run(rs)["wall_s"] == 20


def test_percentiles_are_nearest_rank():
    xs = list(range(1, 101))
    assert (sd.pct(xs, 0.5), sd.pct(xs, 0.95)) == (50, 95)
    assert (sd.pct([3, 1, 2], 0.5), sd.pct([3, 1, 2], 0.95)) == (2, 3)
    assert sd.pct([], 0.5) is None


def test_cell_reports_p50_p95_parts_and_never_a_single_best_run():
    rs = [run("github", w, run_id=i, queued=q) for i, (w, q) in enumerate([(100, 2), (60, 4), (300, 10), (80, 3)])]
    c = sd.cell(rs, "cold")
    assert c["end_to_end_s"] == {"p50": 80, "p95": 300}
    assert c["queued_s"] == {"p50": 3, "p95": 10}
    assert c["run_s"] == {"p50": 77, "p95": 290}
    assert c["small_sample"] is True and c["used"] == 4 and c["days"] == 1
    assert c["machine"].startswith("GitHub-hosted ubuntu-24.04, 4 vCPU") and c["cache"] == "cold"
    assert "wall_s" not in c, "no single run's number is a headline"


def test_runner_minutes_are_their_own_axis():
    r = run("github", 100, jobs=[{"run_s": 90, "started_at": "2026-11-15T02:00:05.000Z", "conclusion": "success"},
                                 {"run_s": 90, "started_at": "2026-11-15T02:00:05.000Z", "conclusion": "success"}])
    c = sd.cell([r], "warm")
    assert c["runner_minutes"]["p50"] == 3.0  # two parallel jobs bill 3 minutes
    assert c["end_to_end_s"]["p50"] == 100  # while the developer waited 100 s


def test_small_sample_label_clears_at_twenty_runs():
    assert sd.cell([run("zode", 50, run_id=i) for i in range(19)], "cold")["small_sample"] is True
    c = sd.cell([run("zode", 50, run_id=i, day="%02d" % (1 + i % 7)) for i in range(20)], "cold")
    assert c["small_sample"] is False and c["days"] == 7


def test_cell_prefers_passed_runs_and_says_when_none_passed():
    rs = [run("github", 10, "failure", run_id=1), run("github", 30, run_id=2), run("github", 50, run_id=3)]
    c = sd.cell(rs, "cold")
    assert (c["passed"], c["runs"], c["used"], c["failed_runs_only"]) == (2, 3, 2, False)
    assert c["end_to_end_s"]["p50"] == 30
    c = sd.cell([run("github", 10, "failure")], "cold")
    assert c["failed_runs_only"] is True


def test_github_list_price_rounds_each_job_up_to_a_minute():
    r = run("github", 100, jobs=[{"run_s": 61, "started_at": "t", "conclusion": "success"},
                                 {"run_s": 5, "started_at": "t", "conclusion": "success"},
                                 {"run_s": None, "started_at": None, "conclusion": "skipped"}])
    assert sd.github_price(r) == round(3 * 0.012, 4)


def test_zode_price_is_billed_or_marked_estimate():
    billed = run("zode", 100, jobs=[{"run_s": 90, "started_at": "t", "conclusion": "success",
                                     "zode": {"cost_micros": 1500, "cost_final": True}}])
    assert sd.zode_price(billed) == (0.0015, "billed")
    sibling = run("zode", 100, jobs=[{"run_s": 3600, "started_at": "t", "conclusion": "success",
                                      "zode": {"cost_micros": 0, "cost_final": True}}])
    assert sd.zode_price(sibling) == (0.045, "estimate")


def test_verdict_names_losses_at_p50():
    cellv = lambda p50, p95, passed=1, small=True: {"end_to_end_s": {"p50": p50, "p95": p95}, "passed": passed,
                                                    "small_sample": small}
    v = sd.verdict(cellv(100, 120), cellv(250, 300))
    assert v["result"] == "loss" and v["words"] == "2.5x slower at P50" and v["p95_ratio"] == 0.4
    v = sd.verdict(cellv(300, 300), cellv(100, 100, passed=0))
    assert v["result"] == "win" and v["both_passed"] is False and v["small_sample"] is True
    assert sd.verdict(cellv(100, 100), cellv(102, 102))["result"] == "even"


def test_published_data_matches_its_runs():
    """site/ci.json regenerates from the results it names."""
    path = os.path.join(HERE, "site", "ci.json")
    if not os.path.exists(path):
        return
    pub = json.load(open(path))
    meta = json.load(open(os.path.join(HERE, "repos.json")))["repos"]
    fresh = sd.build([os.path.join(HERE, d) for d in pub["results"]], meta)
    for a, b in zip(pub["repos"], fresh["repos"]):
        assert a["full"] == b["full"], a["name"]
    assert pub["tally"] == fresh["tally"]
    assert pub["schema"] == 2 and pub["method"] and pub["machines"]["github"] and pub["machines"]["zode"]
