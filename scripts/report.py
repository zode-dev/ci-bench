#!/usr/bin/env python3
"""Summarise a ci-bench results directory.

  scripts/report.py results/<date>     writes summary.json and report.md there

Per repository, provider and mode: runs, successes, the days they span,
and P50 and P95 of end to end (dispatch to the last job done), queued
(dispatch to the first job starting) and run (first job starting to the
last job done). Runner minutes (the jobs' run time added up) are the bill
and are reported apart, never added to a wait. A cell under 20 runs is
marked a small sample. For Zode jobs, the median of every REQ-050 phase,
and the check that the stored phases add up to GitHub's own queue plus run
time.
"""
import importlib.util
import json
import os
import statistics
import sys
from collections import defaultdict

_spec = importlib.util.spec_from_file_location("site_data", os.path.join(os.path.dirname(os.path.abspath(__file__)), "site-data.py"))
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

PHASES = ["webhook", "dispatch", "sandbox", "runner_boot", "assign", "setup", "steps"]


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def p95(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))], 1)


def fmt(x, unit="s"):
    return "n/a" if x is None else ("%g%s" % (x, unit))


def secs(a, b):
    from datetime import datetime
    if not a or not b:
        return None
    pa = datetime.fromisoformat(a.replace("Z", "+00:00"))
    pb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return (pb - pa).total_seconds()


def main(d):
    recs = [json.loads(l) for l in open(os.path.join(d, "runs.jsonl")) if l.strip()]
    groups = defaultdict(list)
    for r in recs:
        groups[(r["repo"], r["provider"], r["mode"])].append(r)
    rows = []
    for (repo, prov, mode), all_rs in sorted(groups.items()):
        c = sd.cell(all_rs, mode)
        rows.append({
            "repo": repo, "provider": prov, "runner": c["runner"], "machine": c["machine"], "mode": mode,
            "runs": c["runs"], "success": c["passed"], "used": c["used"], "days": c["days"],
            "small_sample": c["small_sample"],
            "end_to_end_s": c["end_to_end_s"], "queued_s": c["queued_s"], "run_s": c["run_s"],
            "runner_minutes": c["runner_minutes"],
        })
    # Zode phases, per mode, over every job that ran
    zjobs = defaultdict(list)
    gq = defaultdict(list)
    for r in recs:
        for j in r["jobs"]:
            if j["conclusion"] == "skipped" or not j["started_at"]:
                continue
            if r["provider"] == "zode" and j.get("zode"):
                zjobs[r["mode"]].append(j)
            if r["provider"] == "github":
                gq[r["mode"]].append(j["queue_s"])
    phases = {}
    for mode, js in sorted(zjobs.items()):
        ph = {p: med([(j["zode"]["phase_ms"] or {}).get(p) for j in js]) for p in PHASES + ["queue", "total"]}
        ph["queue_p95"] = p95([(j["zode"]["phase_ms"] or {}).get("queue") for j in js])
        ph["jobs"] = len(js)
        phases[mode] = ph
    gh_queue = {m: {"median_s": med(q), "p95_s": p95(q), "jobs": len(q)} for m, q in gq.items()}
    # REQ-050: stored phases add up to GitHub's queue plus run time within 1 s
    checked = ok = incomplete = 0
    worst = 0.0
    negatives = defaultdict(int)
    for js in zjobs.values():
        for j in js:
            pm = j["zode"]["phase_ms"] or {}
            if any(pm.get(p) is None for p in PHASES):
                incomplete += 1
                continue
            for p in PHASES:
                if pm[p] < 0:
                    negatives[p] += 1
            checked += 1
            github_total = secs(j["created_at"], j["completed_at"])
            diff = abs(sum(pm[p] for p in PHASES) / 1000.0 - github_total)
            worst = max(worst, diff)
            ok += diff <= 1.0
    check = {"jobs_checked": checked, "within_1s": ok, "worst_diff_s": round(worst, 3),
             "jobs_missing_a_phase": incomplete, "negative_phases": dict(negatives)}
    summary = {"results": d, "records": len(recs), "rows": rows, "zode_phases_ms_median": phases,
               "github_queue": gh_queue, "req050_check": check}
    json.dump(summary, open(os.path.join(d, "summary.json"), "w"), indent=1)

    days = len({r["dispatched_at"][:10] for r in recs if r.get("dispatched_at")})
    small = any(r["small_sample"] for r in rows)
    pp = lambda x: "n/a" if x is None or x["p50"] is None else "%gs / %gs" % (x["p50"], x["p95"])
    out = ["# ci-bench results: %s" % os.path.basename(os.path.normpath(d)), "",
           "%d runs over %d days. Each cell is P50 / P95 over the runs that passed (over all runs when none passed). "
           "End to end is dispatch to the last job done; it is queued (dispatch to the first job starting) plus run "
           "(first job starting to the last job done). Runner minutes are the bill, every job's run time added up, "
           "and are never added to a wait. Percentiles do not add: the P50s of queued and run need not sum to the P50 of end to end. Machines: %s. Cache state is the Mode column, the same on both sides."
           % (len(recs), days, "; ".join(sorted({"%s = %s" % (r["runner"], r["machine"]) for r in rows}))), ""]
    if small:
        out += ["Small sample: every cell here has fewer than %d runs, so P95 is close to the slowest run." % sd.MIN_SAMPLE, ""]
    out += ["### Developer wait (P50 / P95)", "",
            "| Repo | Mode | Runs (GitHub / Zode) | GitHub end to end | Zode end to end | GitHub queued | Zode queued | GitHub run | Zode run |",
            "|---|---|---|---|---|---|---|---|---|"]
    by = {(r["repo"], r["provider"], r["mode"]): r for r in rows}
    pairs = []
    for repo in sorted({r["repo"] for r in rows}):
        for mode in ("cold", "warm"):
            g, z = by.get((repo, "github", mode)), by.get((repo, "zode", mode))
            if g or z:
                pairs.append((repo, mode, g, z))
    cnt = lambda r: "%d/%d" % (r["success"], r["runs"]) if r else "n/a"
    gv = lambda r, k: pp(r[k]) if r else "n/a"
    for repo, mode, g, z in pairs:
        out.append("| %s | %s | %s / %s | %s | %s | %s | %s | %s | %s |" % (
            repo, mode, cnt(g), cnt(z), gv(g, "end_to_end_s"), gv(z, "end_to_end_s"), gv(g, "queued_s"),
            gv(z, "queued_s"), gv(g, "run_s"), gv(z, "run_s")))
    mm = lambda x: "n/a" if x is None or x["p50"] is None else "%g / %g" % (x["p50"], x["p95"])
    out += ["", "### Runner minutes, the bill (P50 / P95, a separate axis)", "",
            "| Repo | Mode | GitHub | Zode |", "|---|---|---|---|"]
    for repo, mode, g, z in pairs:
        out.append("| %s | %s | %s | %s |" % (repo, mode, mm(g["runner_minutes"]) if g else "n/a",
                                             mm(z["runner_minutes"]) if z else "n/a"))
    out += ["", "## Zode phases (median ms over every job that ran)", "",
            "| Mode | Jobs | webhook | dispatch | sandbox | runner_boot | assign | setup | steps | queue (p50 / p95) |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for mode, ph in phases.items():
        out.append("| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s / %s |" % (
            mode, ph["jobs"], *[fmt(ph[p], "") for p in PHASES], fmt(ph["queue"], ""), fmt(ph["queue_p95"], "")))
    out += ["", "GitHub-hosted queue (created to started): " + ", ".join(
        "%s median %s, p95 %s over %d jobs" % (m, fmt(v["median_s"]), fmt(v["p95_s"]), v["jobs"])
        for m, v in sorted(gh_queue.items())), "",
            "REQ-050 check: %d Zode jobs with every phase; %d add up to GitHub's created-to-completed within 1 s "
            "(worst %.3f s); %d missing a phase; negative phases %s." % (
                checked, ok, worst, incomplete, dict(negatives) or "none")]
    open(os.path.join(d, "report.md"), "w").write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
