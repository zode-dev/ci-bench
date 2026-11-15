#!/usr/bin/env python3
"""Build the data behind the CI benchmark on zode.dev from one or more results directories.

  scripts/site-data.py results/<date> [results/<date> ...] [--out site/ci.json]

For every repository, provider and mode it reports P50 and P95 over every
run, never one best run:

  end to end   dispatch to the last job done (what a developer waits)
  queued       dispatch to the first job starting
  run          first job starting to the last job done (queued + run = end to end)

Runner minutes (every job's run time added up) and price are the bill. They
are a separate axis and are never added to a wait time. Each cell names the
machine (runner label) and cache state (cold or warm), counts its runs and the
days they span, and is marked a small sample under MIN_SAMPLE runs. Runs that
passed are used; when none passed, the failed runs are used and the cell says
so. Prices: Zode's is what the run was billed (the jobs' cost_micros from the
Zode API). GitHub-hosted jobs on a public repository are free, so GitHub's is
the list price of the same 4 vCPU, 16 GB machine for a private repository (the
4-core larger runner), each job rounded up to the minute as GitHub bills it.

Full-suite and selected runs are kept apart: every run here is a full suite.
Selected runs need Enforce (task 43); until then the selected column is
pending, and so are shards (task 41) on the benchmark set.
"""
import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# GitHub's list price, https://docs.github.com/en/billing/reference/actions-runner-pricing
# (read 13 Nov 2026): Linux x64 4-core larger runner, per minute.
GITHUB_4CORE_USD_PER_MIN = 0.012
# Zode's list price for zode-4x8, https://zode.dev/#pricing (src/lib/pricing.ts in zodeops/zode).
ZODE_4X8_USD_PER_HOUR = 0.045
# Fewer runs than this in a cell and its numbers are labelled a small sample.
MIN_SAMPLE = 20

# The machine behind each runner label, as the page names it.
MACHINES = {
    "ubuntu-24.04": "GitHub-hosted ubuntu-24.04, 4 vCPU, 16 GB RAM (public repository)",
    "ubuntu-latest": "GitHub-hosted ubuntu-latest, 4 vCPU, 16 GB RAM (public repository)",
    "zode-2x4": "Zode zode-2x4, 2 vCPU, 4 GB RAM",
    "zode-4x8": "Zode zode-4x8, 4 vCPU, 8 GB RAM",
    "zode-8x16": "Zode zode-8x16, 8 vCPU, 16 GB RAM",
}

METHOD = [
    "End to end is dispatch to the last job done: what a developer waits for. It is queued plus run.",
    "Queued is dispatch to the first job starting. Run is the first job starting to the last job done.",
    "Every number is a P50 or P95 over all runs of that repository, machine and cache state, with the run count and "
    "the days they span. No single best run is shown as a result.",
    "Runner minutes and price are the bill: every job's run time added up. They are never added to a wait time.",
    "Cold deletes the branch's Actions caches before the run. Warm runs right after with what cold saved. Both "
    "sides run the same mode at the same time.",
    "A cell with fewer than %d runs is marked small sample. P95 over a small sample is close to the slowest run." % 20,
]

PROVIDERS = {
    "github": {
        "label": "GitHub-hosted",
        "hardware": "4 vCPU, 16 GB RAM, 14 GB SSD (ubuntu-24.04 on a public repository)",
        "price": "List price of the same machine on a private repository: the Linux 4-core larger "
                 "runner at $0.012 a minute, each job rounded up to the minute. Public repositories "
                 "run free.",
        "price_source": "https://docs.github.com/en/billing/reference/actions-runner-pricing",
        "hardware_source": "https://docs.github.com/en/actions/reference/runners/github-hosted-runners",
    },
    "zode": {
        "label": "Zode",
        "hardware": "4 vCPU, 8 GB RAM (zode-4x8) in a microVM on a GCE n2-standard-16 node in "
                    "europe-west1, nested virtualisation, shared with real CI",
        "price": "What the run was billed: each job's runner compute from creation to deletion, "
                 "per second, from the Zode API, at $0.045 an hour for zode-4x8. When a job ran on a "
                 "runner started for another run, its bill sits on that run, so the price shown is "
                 "an estimate: the jobs' run time at the list price.",
        "price_source": "https://zode.dev/#pricing",
        "hardware_source": "https://docs.zode.dev/ci/benchmark",
    },
}

PENDING = {
    "selected": "Every run on this page runs the full suite. Test selection is in Shadow on Zode: it runs "
                "everything and records what it would have skipped. Selected runs need Enforce, which "
                "opens per repository after 200 clean Shadow runs. This column fills then.",
    "shards": "zode-ci test --shards splits a suite across forks of the job's machine. The benchmark "
              "workflows run unchanged, without the wrapper, so their numbers here are single-machine. "
              "Sharded numbers follow once the shard proof on saleor is published.",
}


def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def pct(xs, q, digits=1):
    """Nearest-rank percentile: the smallest value with at least q of the values at or below it."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[max(0, math.ceil(q * len(xs)) - 1)], digits)


def spread(xs, digits=1):
    return {"p50": pct(xs, 0.50, digits), "p95": pct(xs, 0.95, digits)}


def median_run(runs):
    """The run whose wall time is the median (lower middle when even)."""
    rs = sorted((r for r in runs if r.get("wall_s") is not None), key=lambda r: r["wall_s"])
    if not rs:
        return None
    return rs[(len(rs) - 1) // 2]


def ran(run):
    return [j for j in run["jobs"] if j.get("started_at") and j.get("conclusion") != "skipped"]


def parts(run):
    """End to end, queued and run seconds of one run (queued + run = end to end)."""
    e2e = run.get("wall_s")
    starts = [parse(j["started_at"]) for j in ran(run) if j.get("started_at") not in (None, "t")]
    d = parse(run.get("dispatched_at"))
    if e2e is None or not starts or d is None:
        return e2e, None, None
    queued = max(0.0, (min(starts) - d).total_seconds())
    return e2e, queued, max(0.0, e2e - queued)


def runner_minutes(run):
    return sum(j["run_s"] for j in ran(run) if j.get("run_s") is not None) / 60.0


def github_price(run):
    """List price of a run's jobs on the 4-core larger runner, per job rounded up to a minute."""
    total = 0.0
    for j in run["jobs"]:
        if j.get("run_s") is None or j.get("conclusion") == "skipped":
            continue
        total += math.ceil(j["run_s"] / 60.0) * GITHUB_4CORE_USD_PER_MIN
    return round(total, 4)


def zode_price(run):
    """What Zode billed the run's jobs. A job that ran on a runner Zode started
    for a job of another run carries no cost of its own (its runner's bill sits
    on that other job), so such a run gets the list price of its jobs' run
    time instead, marked as an estimate."""
    ran = [j for j in run["jobs"] if j.get("started_at") and j.get("conclusion") != "skipped"]
    micros = [(j.get("zode") or {}).get("cost_micros") for j in ran]
    if ran and all(m for m in micros):
        final = all((j.get("zode") or {}).get("cost_final") for j in ran)
        return round(sum(micros) / 1e6, 4), "billed" if final else "billed_so_far"
    secs_ = sum(j["run_s"] for j in ran if j.get("run_s") is not None)
    return round(secs_ / 3600.0 * ZODE_4X8_USD_PER_HOUR, 4), "estimate"


def cell(runs, mode):
    if not runs:
        return None
    passed = [r for r in runs if r["conclusion"] == "success"]
    pool = passed or runs
    p = [parts(r) for r in pool]
    prices, kinds = [], set()
    for r in pool:
        if r["provider"] == "zode":
            v, k = zode_price(r)
        else:
            v, k = github_price(r), "list"
        prices.append(v)
        kinds.add(k)
    days = sorted({parse(r["dispatched_at"]).date().isoformat() for r in pool if r.get("dispatched_at")})
    runners = sorted({r["runner"] for r in pool})
    e2e = spread([x[0] for x in p])
    near = min(pool, key=lambda r: abs((r.get("wall_s") or 0) - (e2e["p50"] or 0)))
    kind = "estimate" if "estimate" in kinds else "billed_so_far" if "billed_so_far" in kinds else kinds.pop()
    return {
        "runs": len(runs),
        "passed": len(passed),
        "used": len(pool),
        "failed_runs_only": not passed,
        "small_sample": len(pool) < MIN_SAMPLE,
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "runner": runners[0] if len(runners) == 1 else ", ".join(runners),
        "machine": MACHINES.get(runners[0], runners[0]) if len(runners) == 1 else ", ".join(runners),
        "cache": mode,
        "end_to_end_s": e2e,
        "queued_s": spread([x[1] for x in p]),
        "run_s": spread([x[2] for x in p]),
        "runner_minutes": spread([runner_minutes(r) for r in pool]),
        "price_usd": spread(prices, 4),
        "price_kind": kind,
        "near_p50_run_url": near["run_url"],
    }


def verdict(gh, zo):
    """How Zode did against GitHub on P50 end to end for one repository and mode."""
    if not gh or not zo:
        return None
    g, z = gh["end_to_end_s"]["p50"], zo["end_to_end_s"]["p50"]
    if not g or not z:
        return None
    ratio = g / z
    if ratio >= 1.05:
        word = "faster"
    elif ratio <= 1 / 1.05:
        word = "slower"
    else:
        word = "even"
    g95, z95 = gh["end_to_end_s"]["p95"], zo["end_to_end_s"]["p95"]
    return {
        "ratio": round(ratio, 2),
        "p95_ratio": round(g95 / z95, 2) if g95 and z95 else None,
        "result": {"faster": "win", "slower": "loss", "even": "even"}[word],
        "words": ("%.1fx faster at P50" % ratio) if word == "faster" else
                 ("%.1fx slower at P50" % (1 / ratio)) if word == "slower" else "within 5% at P50",
        "both_passed": gh["passed"] > 0 and zo["passed"] > 0,
        "small_sample": gh["small_sample"] or zo["small_sample"],
    }


def git_head():
    try:
        return subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def build(results_dirs, repos_meta):
    if isinstance(results_dirs, str):
        results_dirs = [results_dirs]
    recs = []
    for d in results_dirs:
        recs += [json.loads(l) for l in open(os.path.join(d, "runs.jsonl")) if l.strip()]
    by = {}
    for r in recs:
        by.setdefault((r["repo"], r["provider"], r["mode"]), []).append(r)
    modes = ["cold", "warm"]
    rows = []
    tally = {m: {"win": 0, "loss": 0, "even": 0, "compared": 0, "small_sample": 0} for m in modes}
    for meta in repos_meta:
        name = meta["name"]
        if not any(k[0] == name for k in by):
            continue
        row = {
            "name": name, "upstream": meta["upstream"], "commit": meta["commit"],
            "workflow": meta["workflow"], "lang": meta.get("lang"), "jobs": meta.get("jobs", 1),
            "mirror": meta.get("mirror") or "zodeops/bench-" + name,
            "full": {}, "verdict": {},
        }
        for mode in modes:
            gh = cell(by.get((name, "github", mode), []), mode)
            zo = cell(by.get((name, "zode", mode), []), mode)
            row["full"][mode] = {"github": gh, "zode": zo}
            v = verdict(gh, zo)
            row["verdict"][mode] = v
            if v:
                tally[mode]["compared"] += 1
                tally[mode][v["result"]] += 1
                tally[mode]["small_sample"] += v["small_sample"]
        rows.append(row)
    times = [parse(r["dispatched_at"]) for r in recs if r.get("dispatched_at")]
    ends = [parse(r["completed_at"]) for r in recs if r.get("completed_at")]
    machines = {p: sorted({r["runner"] for r in recs if r["provider"] == p}) for p in ("github", "zode")}
    return {
        "schema": 2,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results": [os.path.relpath(os.path.abspath(d), HERE) for d in results_dirs],
        "harness_commit": git_head(),
        "source": "https://github.com/zodeops/ci-bench",
        "window": {"from": min(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
                   "to": max(ends).strftime("%Y-%m-%dT%H:%M:%SZ") if ends else None,
                   "days": len({t.date() for t in times})},
        "runs": len(recs),
        "min_sample": MIN_SAMPLE,
        "method": METHOD,
        "machines": {p: [{"runner": m, "machine": MACHINES.get(m, m)} for m in ms] for p, ms in machines.items()},
        "providers": PROVIDERS,
        "tally": tally,
        "pending": PENDING,
        "repos": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="results directories; several are pooled into one sample")
    ap.add_argument("--out", default=None, help="also write here (site/ci.json)")
    ap.add_argument("--conditions", default=None,
                    help="how the run was measured, shown on the page (default: <results>/CONDITIONS if present)")
    a = ap.parse_args()
    meta = json.load(open(os.path.join(HERE, "repos.json")))["repos"]
    data = build(a.results, meta)
    cond = a.conditions
    cpath = os.path.join(a.results[0], "CONDITIONS")
    if cond is None and os.path.exists(cpath):
        cond = open(cpath).read().strip()
    if cond:
        data["conditions"] = cond
    body = json.dumps(data, indent=1) + "\n"
    with open(os.path.join(a.results[0], "site.json"), "w") as f:
        f.write(body)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            f.write(body)
    t = data["tally"]
    print("%d runs over %d days, %d repositories; at P50 end to end, cold: Zode %d wins %d losses; warm: %d wins "
          "%d losses; %d of %d comparisons rest on a small sample (under %d runs a cell)" % (
              data["runs"], data["window"]["days"], len(data["repos"]), t["cold"]["win"], t["cold"]["loss"],
              t["warm"]["win"], t["warm"]["loss"], t["cold"]["small_sample"] + t["warm"]["small_sample"],
              t["cold"]["compared"] + t["warm"]["compared"], MIN_SAMPLE), file=sys.stderr)


if __name__ == "__main__":
    main()
