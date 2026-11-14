#!/usr/bin/env python3
"""Benchmark: heap_hunter (hunt) vs Eclipse MAT on complicated HPROF dumps
under limited memory availability.

Phases per scenario:
  index      hunt: `hunt index` + `hunt overview`      MAT: `ParseHeapDump.sh <dump>`
  queries    hunt: histogram, dominator-tree, roots   MAT: suspects + top_components + bench_extract

Memory limits are tool-level (hunt --sorting/--dominator-budget-mb, MAT -Xmx) and
peak RSS is measured for every run. Hard cgroup caps are not available in this
WSL2 hybrid-cgroup environment (memory controller not delegated), which is
documented in the README; tool-level caps are enforced by each tool itself.

Usage:
  ./bench.py --quick          small matrix (default): mixed-512m + mixed-2g, 2 caps each
  ./bench.py --full           full matrix incl. mixed-6g and more caps
  ./bench.py --gen-only       only generate missing dumps
  ./bench.py --all-caps       run every cap in the matrix instead of stopping
                              at the first successful index
  ./bench.py --dump mixed-512m --hunt-budget 256 --mat-xmx 512m   single scenario

Caps run from highest to lowest and the ladder stops at the first successful
index per tool (lower caps fail the same way but only after a full re-scan).
"""

import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HUNT = ROOT / ".." / "heap_hunter" / "zig-out" / "bin" / "hunt"
JAR = ROOT / ".." / "heap_forge" / "target" / "heapforge-1.0-SNAPSHOT.jar"
MAT_SH = ROOT / "mat" / "ParseHeapDump.sh"
BENCH_REPORT = ROOT / "mat-report" / "bench_extract.xml"
DUMPS = ROOT / "dumps"
RUNS = ROOT / "runs"
RESULTS = ROOT / "results"

MB = 1024 * 1024

DUMP_TIERS = {
    # stem: (profile, target_mb, gen_xmx)
    "mixed-512m": ("mixed", 512, "2048m"),
    "mixed-2g": ("mixed", 2048, "4608m"),
    "mixed-6g": ("mixed", 6144, "10240m"),
}

QUICK = {
    "mixed-512m": {"hunt": [256, 512], "mat": [512, 1024]},
    "mixed-2g": {"hunt": [512, 1024], "mat": [1024, 2048]},
}

FULL = {
    "mixed-512m": {"hunt": [256, 512, 1024], "mat": [512, 1024, 2048]},
    "mixed-2g": {"hunt": [256, 512, 1024, 2048], "mat": [1024, 2048, 4096]},
    "mixed-6g": {"hunt": [512, 1024, 2048, 4096], "mat": [2048, 4096, 8192]},
}


def ensure_dirs():
    for d in (DUMPS, RUNS, RESULTS):
        d.mkdir(parents=True, exist_ok=True)


def check_setup():
    missing = [p for p in (HUNT, JAR, MAT_SH, BENCH_REPORT) if not Path(p).exists()]
    if missing:
        sys.exit("missing {}, run ./setup.sh first: {}".format(missing, " ".join(map(str, missing))))


# --------------------------------------------------------------------------- run


def run_cmd(argv, timeout_s, cwd=None, out_path=None):
    """Run argv in a new session; kill the whole group on timeout.

    stdout/stderr go to temp files (not pipes): some commands (e.g. `hunt
    roots` on deep chains) emit hundreds of MB and would deadlock on a full
    pipe buffer since nothing is read until the process exits.

    If out_path is given, the child's full stdout is copied there.

    Returns dict with rc, status ('ok'|'fail'|'timeout'), wall_s, max_rss_kb,
    stdout, stderr (last 4000 chars each).
    """
    start = time.monotonic()
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(
            [str(a) for a in argv],
            cwd=str(cwd) if cwd else None,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
        )
        deadline = start + timeout_s
        max_rss_kb = 0
        rc = None
        while True:
            try:
                pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == proc.pid:
                if hasattr(usage, "ru_maxrss"):
                    max_rss_kb = usage.ru_maxrss
                rc = os.waitstatus_to_exitcode(status)
                break
            if time.monotonic() > deadline:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    pid, status, usage = os.wait4(proc.pid, 0)
                    rc = os.waitstatus_to_exitcode(status)
                    if hasattr(usage, "ru_maxrss"):
                        max_rss_kb = usage.ru_maxrss
                except ChildProcessError:
                    pass
                proc.returncode = rc if rc is not None else -9
                _copy_out_path(out_f, out_path)
                return _read_cmd_output(out_f, err_f, rc, "timeout",
                                        round(time.monotonic() - start, 2),
                                        0,  # rusage unreliable: group-kill reaps children without folding usage
                                        )
            time.sleep(0.2)
        if rc is None:
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = -1
        proc.returncode = rc
        _copy_out_path(out_f, out_path)
        res = _read_cmd_output(out_f, err_f, rc, None,
                               round(time.monotonic() - start, 2), max_rss_kb)
        is_oom = rc not in (0,) and re.search(
            r"OutOfMemoryError|OutOfMemory|GC overhead limit", res["stdout"] + res["stderr"])
        res["status"] = "ok" if rc == 0 else ("oom" if is_oom else "fail")
        return res


def _copy_out_path(out_f, out_path):
    if out_path is None:
        return
    out_f.seek(0)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as dst:
        shutil.copyfileobj(out_f, dst)


def _read_cmd_output(out_f, err_f, rc, status, wall_s, max_rss_kb):
    out_f.seek(0)
    err_f.seek(0)
    out_d = out_f.read().decode(errors="replace")
    err_d = err_f.read().decode(errors="replace")
    return {
        "rc": rc, "status": status or "",
        "wall_s": wall_s, "max_rss_kb": max_rss_kb,
        "stdout": out_d[-4000:],
        "stderr": err_d[-4000:],
    }


# ------------------------------------------------------------------- dump gen


def generate_dump(stem):
    profile, target_mb, xmx = DUMP_TIERS[stem]
    path = DUMPS / f"{stem}.hprof"
    if path.exists():
        print(f"  dump {stem} exists ({path.stat().st_size // MB} MB), skipping")
        return path
    print(f"  generating {stem} (profile={profile}, target={target_mb} MB, -Xmx{xmx}) ...")
    res = run_cmd(
        ["java", f"-Xmx{xmx}", "-jar", JAR, "stress", profile, str(target_mb), str(path), "true"],
        timeout_s=3600,
    )
    if res["status"] != "ok" or not path.exists():
        sys.exit(f"dump generation failed for {stem}: {res['status']}\n{res['stderr'][-1500:]}")
    print(f"    built in {res['wall_s']}s, size {path.stat().st_size // MB} MB, peak RSS {res['max_rss_kb'] // 1024} MB")
    return path


# ------------------------------------------------------------------------- hunt


def parse_last_json(stdout):
    """First line (scanned in reverse) that parses as a JSON object.

    hunt prints progress lines and trailing status lines ('index loaded'),
    so the JSON payload is not reliably the last line.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def hunt_run(scenario_dir, dump, budget_mb, timeout_s):
    """Phase 'index': hunt index + hunt overview."""
    idx = scenario_dir / "idx"
    if idx.exists():
        shutil.rmtree(idx)
    r1 = run_cmd([HUNT, "index", dump, "--index-dir", idx,
                  "--sorting-budget-mb", budget_mb, "--dominator-budget-mb", budget_mb, "--force"],
                 timeout_s)
    r2 = None
    if r1["status"] == "ok":
        r2 = run_cmd([HUNT, "overview", dump, "--index-dir", idx], timeout_s)
    wall = r1["wall_s"] + (r2["wall_s"] if r2 else 0)
    rss = max(r1["max_rss_kb"], r2["max_rss_kb"] if r2 else 0)
    status = r1["status"] if r1["status"] != "ok" else (r2["status"] if r2 else "fail")
    index_bytes = sum(f.stat().st_size for f in idx.rglob("*") if f.is_file()) if idx.exists() else 0
    meta = parse_last_json(r1["stdout"])
    return {
        "phase": "index", "status": status, "wall_s": round(wall, 2),
        "peak_rss_mb": round(rss / 1024, 1), "index_mb": round(index_bytes / MB, 1),
        "n_objects": meta.get("n_objects"), "n_edges": meta.get("n_edges"),
        "notes": r1["stderr"][-300:] if status != "ok" else "",
    }


def hunt_query_id(idx, class_name):
    """First object id of a class, used for dominator-tree / roots queries."""
    esc = re.escape(class_name)
    res = run_cmd([HUNT, "objects", esc, "--index-dir", idx, "--limit", "1"],
                  timeout_s=300)
    try:
        data = parse_last_json(res["stdout"])
        return data["items"][0]["id"]
    except (ValueError, IndexError, KeyError):
        return None


def hunt_queries(scenario_dir, idx, timeout_s):
    """Phases: histogram, dominator-tree, roots (re-use the already built idx)."""
    rows = []
    hist_csv = scenario_dir / "histogram.csv"
    hist = run_cmd([HUNT, "histogram", "--index-dir", idx, "--limit", "1000000"], timeout_s,
                   out_path=hist_csv)
    rows.append({"phase": "histogram", "status": hist["status"], "wall_s": hist["wall_s"],
                 "peak_rss_mb": round(hist["max_rss_kb"] / 1024, 1), "index_mb": "",
                 "n_objects": "", "n_edges": "", "notes": ""})

    obj_id = hunt_query_id(idx, "com.example.heapforge.stress.DeepNode") \
        or hunt_query_id(idx, "com.example.heapforge.User")
    if obj_id is None:
        first = parse_hunt_histogram(scenario_dir / "histogram.csv")
        if first:
            obj_id = hunt_query_id(idx, first)
    for name, argv in (
        ("dominator", [HUNT, "dominator", obj_id, "--index-dir", idx]),
        ("roots", [HUNT, "roots", obj_id, "--index-dir", idx]),
    ):
        if obj_id is None:
            rows.append({"phase": name, "status": "skip", "wall_s": "", "peak_rss_mb": "",
                         "index_mb": "", "n_objects": "", "n_edges": "", "notes": "no object id"})
            continue
        r = run_cmd(argv, timeout_s, out_path=scenario_dir / f"{name}.json")
        rows.append({"phase": name, "status": r["status"], "wall_s": r["wall_s"],
                     "peak_rss_mb": round(r["max_rss_kb"] / 1024, 1), "index_mb": "",
                     "n_objects": "", "n_edges": "", "notes": r["stderr"][-200:] if r["status"] != "ok" else ""})
    return rows


def parse_hunt_histogram(path):
    if not Path(path).exists():
        return None
    with open(path) as fh:
        for line in fh:
            if line.startswith("Class,"):
                continue
            parts = line.rstrip("\n").split(",")
            if len(parts) >= 3 and parts[0] and parts[0] != "Class":
                return parts[0]
    return None


# -------------------------------------------------------------------------- MAT


MAT_INDEX_SUFFIXES = (".index", ".threads", ".log", ".a2s", ".idx", ".inbound", ".outbound",
                      ".o2c", ".o2hprof", ".o2ret", ".domIn", ".domOut", ".i2sv2")


def mat_clean(dump):
    parent = Path(dump).parent
    stem = Path(dump).stem
    for f in parent.glob(stem + ".*"):
        if f.is_file() and f.name.endswith(MAT_INDEX_SUFFIXES):
            f.unlink()
    for f in parent.glob(stem + "_*"):
        if f.is_file():
            f.unlink()
        elif f.is_dir():
            shutil.rmtree(f, ignore_errors=True)


def mat_index_bytes(dump):
    total = 0
    stem = Path(dump).stem
    for f in Path(dump).parent.glob(stem + ".*"):
        if f.is_file() and f.name.endswith(MAT_INDEX_SUFFIXES):
            total += f.stat().st_size
    return total


def mat_clean_reports_only(dump):
    """Delete previous report outputs (keep index files) before a reports re-run."""
    parent = Path(dump).parent
    stem = Path(dump).stem
    for f in parent.glob(stem + "_*"):
        if f.is_file():
            f.unlink()
        elif f.is_dir():
            shutil.rmtree(f, ignore_errors=True)


def mat_run(scenario_dir, dump, xmx_mb, timeout_s, reports=False):
    """Phase 'index' (or 'reports' when reports=True) via ParseHeapDump.sh."""
    if not reports:
        mat_clean(dump)
    else:
        mat_clean_reports_only(dump)
    argv = [MAT_SH, dump]
    if reports:
        argv += ["org.eclipse.mat.api:suspects", "org.eclipse.mat.api:top_components",
                 str(BENCH_REPORT)]
    argv += ["-vmargs", f"-Xmx{xmx_mb}m"]
    r = run_cmd(argv, timeout_s)
    index_bytes = mat_index_bytes(dump)
    return {
        "phase": "reports" if reports else "index",
        "status": r["status"], "wall_s": r["wall_s"],
        "peak_rss_mb": round(r["max_rss_kb"] / 1024, 1),
        "index_mb": round(index_bytes / MB, 1),
        "n_objects": "", "n_edges": "",
        "notes": (r["stderr"][-300:] if r["status"] != "ok" else "").replace("\n", " ")[:300],
    }


# ------------------------------------------------------------------ correctness


def load_hunt_hist_csv(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            if line.startswith("Class,"):
                continue
            parts = line.rstrip("\n").split(",")
            if len(parts) >= 3 and parts[0]:
                try:
                    out[parts[0]] = (int(parts[1]), int(parts[2]))
                except ValueError:
                    pass
    return out


def load_mat_hist_csv(path):
    # MAT CSVs can contain duplicate class rows (zero-value repeats); sum them
    # so real counts are not overwritten by later zero rows.
    out = {}
    with open(path) as fh:
        for line in fh:
            if line.startswith("Class Name,"):
                continue
            parts = [p.strip().strip('"') for p in line.rstrip("\n").split(",")]
            if len(parts) >= 3 and parts[0]:
                try:
                    v = (int(parts[1].replace(",", "")),
                         int(parts[2].replace(",", "")))
                except ValueError:
                    continue
                prev = out.get(parts[0], (0, 0))
                out[parts[0]] = (prev[0] + v[0], prev[1] + v[1])
    return out


def find_report_csv(dump, marker):
    for f in Path(dump).parent.glob(Path(dump).stem + "_Bench/pages/*.csv"):
        if marker in f.name:
            return f
    return None


def correctness(stem, dump, results_rows):
    """Compare hunt histogram against MAT histogram."""
    cand = sorted(RUNS.glob(f"hunt-{stem}-*/histogram.csv"))
    hunt_csv = cand[-1] if cand else None
    mat_csv = find_report_csv(dump, "Histogram")
    if not hunt_csv or not mat_csv:
        print(f"  correctness {stem}: missing inputs (hunt={hunt_csv} mat={mat_csv})")
        return None
    h = load_hunt_hist_csv(hunt_csv)
    m = load_mat_hist_csv(mat_csv)
    common = set(h) & set(m)
    only_h = set(h) - set(m)
    only_m = set(m) - set(h)
    h_obj = sum(h[c][0] for c in h)
    m_obj = sum(m[c][0] for c in m)
    h_bytes = sum(h[c][1] for c in h)
    m_bytes = sum(m[c][1] for c in m)
    max_inst_delta = 0
    max_bytes_delta = 0
    diffs = []
    for c in common:
        di = abs(h[c][0] - m[c][0])
        db = abs(h[c][1] - m[c][1])
        if di > max_inst_delta:
            max_inst_delta = di
        if db > max_bytes_delta:
            max_bytes_delta = db
        if di > 0 or db > 0:
            diffs.append((c, h[c], m[c]))
    report = {
        "dump": stem,
        "classes_hunt": len(h), "classes_mat": len(m), "classes_common": len(common),
        "classes_only_hunt": len(only_h), "classes_only_mat": len(only_m),
        "objects_hunt": h_obj, "objects_mat": m_obj,
        "objects_delta_pct": round(100.0 * (h_obj - m_obj) / m_obj, 3) if m_obj else None,
        "shallow_bytes_hunt": h_bytes, "shallow_bytes_mat": m_bytes,
        "shallow_delta_pct": round(100.0 * (h_bytes - m_bytes) / m_bytes, 3) if m_bytes else None,
        "max_instance_delta": max_inst_delta, "max_shallow_delta_bytes": max_bytes_delta,
        "classes_with_deltas": len(diffs),
        "top_diffs": [{"class": c, "hunt": h[c], "mat": m[c]} for c, _, _ in
                      sorted(diffs, key=lambda x: abs(x[1][1] - x[2][1]), reverse=True)[:10]],
    }
    (RESULTS / f"correctness-{stem}.json").write_text(json.dumps(report, indent=2))
    print(f"  correctness {stem}: objects hunt={h_obj:,} mat={m_obj:,} "
          f"({report['objects_delta_pct']}%), shallow bytes delta {report['shallow_delta_pct']}%, "
          f"classes with diffs {len(diffs)}/{len(common)}")
    return report


# ----------------------------------------------------------------------- main


def emit_results(rows, reports):
    RESULTS.mkdir(parents=True, exist_ok=True)
    # Caps run highest-first; present results low-cap-first for readability.
    rows = sorted(rows, key=lambda r: (r["dump"], r["tool"], int(r["cap_mb"] or 0)))
    fields = ["tool", "dump", "cap_mb", "phase", "status", "wall_s", "peak_rss_mb",
              "index_mb", "n_objects", "n_edges", "notes"]
    with open(RESULTS / "results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    payload = {
        "environment": {
            "hostname": os.uname().nodename, "kernel": os.uname().release,
            "cpus": os.cpu_count(),
            "mem_total_kb": read_meminfo("MemTotal"), "swap_total_kb": read_meminfo("SwapTotal"),
            "hunt_version": hunt_version(), "mat_version": "1.17.0.20260601",
            "java_version": java_version(),
        },
        "runs": rows,
        "correctness": reports,
    }
    (RESULTS / "results.json").write_text(json.dumps(payload, indent=2))
    write_summary(rows, payload)


def read_meminfo(key):
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def hunt_version():
    try:
        return subprocess.run([str(HUNT), "--version"], capture_output=True, text=True,
                               timeout=30).stdout.strip()
    except Exception:
        return None


def java_version():
    try:
        return subprocess.run(["java", "-version"], capture_output=True, text=True,
                               timeout=30).stderr.splitlines()[0]
    except Exception:
        return None


def fmt(v):
    if v in ("", None):
        return "-"
    return v


def write_summary(rows, payload):
    lines = []
    env = payload["environment"]
    lines.append("# heap_hunter vs Eclipse MAT benchmark\n")
    lines.append("## Environment\n")
    lines.append(f"- host: {env['hostname']} ({env['kernel']}), {env['cpus']} CPUs")
    lines.append(f"- RAM: {env['mem_total_kb'] // 1024} MB, swap: {env['swap_total_kb'] // 1024} MB")
    lines.append(f"- hunt: {env['hunt_version']}")
    lines.append(f"- MAT: {env['mat_version']} ({env['java_version']})")
    lines.append("- memory limits: tool-level caps (hunt --sorting/--dominator-budget-mb,"
                  " MAT -Xmx); hard cgroup caps unavailable in this WSL2 hybrid-cgroup setup")
    lines.append("")
    lines.append("## Index phase (per dump x cap)\n")
    for dump in sorted({r["dump"] for r in rows}):
        lines.append(f"### {dump}\n")
        lines.append("| tool | cap MB | status | wall s | peak RSS MB | index MB |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            if r["dump"] == dump and r["phase"] == "index":
                lines.append(f"| {r['tool']} | {r['cap_mb']} | {r['status']} | "
                             f"{fmt(r['wall_s'])} | {fmt(r['peak_rss_mb'])} | {fmt(r['index_mb'])} |")
        lines.append("")
    lines.append("## Query phases (successful index reused)\n")
    for dump in sorted({r["dump"] for r in rows}):
        lines.append(f"### {dump}\n")
        lines.append("| tool | phase | status | wall s | peak RSS MB |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            if r["dump"] == dump and r["phase"] != "index":
                lines.append(f"| {r['tool']} | {r['phase']} | {r['status']} | "
                             f"{fmt(r['wall_s'])} | {fmt(r['peak_rss_mb'])} |")
        lines.append("")
    if payload["correctness"]:
        lines.append("## Correctness cross-check (hunt vs MAT histograms)\n")
        lines.append("| dump | objects hunt | objects MAT | obj delta % | shallow delta % | classes w/ diffs |")
        lines.append("|---|---|---|---|---|---|")
        for rep in payload["correctness"]:
            if rep:
                lines.append(f"| {rep['dump']} | {rep['objects_hunt']:,} | {rep['objects_mat']:,} "
                             f"| {rep['objects_delta_pct']} | {rep['shallow_delta_pct']} "
                             f"| {rep['classes_with_deltas']}/{rep['classes_common']} |")
        lines.append("")
    (RESULTS / "summary.md").write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--gen-only", action="store_true")
    ap.add_argument("--all-caps", action="store_true",
                    help="run every cap in the matrix instead of stopping at the first successful index")
    ap.add_argument("--dump")
    ap.add_argument("--hunt-budget", type=int)
    ap.add_argument("--mat-xmx", type=int)
    ap.add_argument("--timeout", type=int, default=1800, help="per-run timeout seconds")
    ap.add_argument("--report-timeout", type=int, default=2700,
                    help="timeout seconds for MAT report runs")
    args = ap.parse_args()

    ensure_dirs()
    check_setup()

    if args.dump:
        stems = [args.dump]
        matrix = {args.dump: {"hunt": [args.hunt_budget or 1024], "mat": [args.mat_xmx or 2048]}}
    else:
        matrix = FULL if args.full else QUICK
        stems = list(matrix)

    print("== generating dumps")
    dump_paths = {}
    for stem in stems:
        if stem not in DUMP_TIERS:
            sys.exit(f"unknown dump {stem}; known: {list(DUMP_TIERS)}")
        dump_paths[stem] = generate_dump(stem)
    if args.gen_only:
        print("done (gen-only)")
        return

    timeout_s = args.timeout
    rows = []
    correctness_reports = []

    print("== index phase")
    for stem in stems:
        dump = dump_paths[stem]
        dump_mb = dump.stat().st_size // MB
        # Run caps from highest to lowest and stop at the first successful
        # index: lower caps fail the same way but only after a full re-scan
        # of the dump. --all-caps restores the exhaustive ladder.
        hunt_caps = sorted(matrix[stem]["hunt"], reverse=True)
        for budget in hunt_caps:
            sc = RUNS / f"hunt-{stem}-{budget}"
            sc.mkdir(parents=True, exist_ok=True)
            print(f"  hunt {stem} budget={budget}MB (dump {dump_mb} MB)")
            row = hunt_run(sc, dump, budget, timeout_s)
            rows.append({"tool": "hunt", "dump": stem, "cap_mb": budget, **row})
            print(f"    -> {row['status']} {row['wall_s']}s rss={row['peak_rss_mb']}MB "
                  f"idx={row['index_mb']}MB")
            if not args.all_caps and row["status"] == "ok":
                lower = hunt_caps[hunt_caps.index(budget) + 1:]
                if lower:
                    print(f"    success at cap={budget}; skipping lower caps {lower} "
                          "(use --all-caps to run all)")
                break
        mat_caps = sorted(matrix[stem]["mat"], reverse=True)
        for xmx in mat_caps:
            sc = RUNS / f"mat-{stem}-{xmx}"
            sc.mkdir(parents=True, exist_ok=True)
            print(f"  MAT  {stem} -Xmx={xmx}m (dump {dump_mb} MB)")
            row = mat_run(sc, dump, xmx, timeout_s)
            rows.append({"tool": "mat", "dump": stem, "cap_mb": xmx, **row})
            print(f"    -> {row['status']} {row['wall_s']}s rss={row['peak_rss_mb']}MB "
                  f"idx={row['index_mb']}MB")
            if not args.all_caps and row["status"] == "ok":
                lower = mat_caps[mat_caps.index(xmx) + 1:]
                if lower:
                    print(f"    success at -Xmx={xmx}m; skipping lower caps {lower} "
                          "(use --all-caps to run all)")
                break

    print("== query phases (on successful index)")
    for stem in stems:
        dump = dump_paths[stem]
        ok_hunt = [r for r in rows if r["tool"] == "hunt" and r["dump"] == stem
                   and r["phase"] == "index" and r["status"] == "ok"]
        if ok_hunt:
            budget = max(int(r["cap_mb"]) for r in ok_hunt)
            sc = RUNS / f"hunt-{stem}-{budget}"
            print(f"  hunt queries {stem} (reusing budget={budget} index)")
            qrows = hunt_queries(sc, sc / "idx", timeout_s)
            for q in qrows:
                rows.append({"tool": "hunt", "dump": stem, "cap_mb": budget, **q})
                print(f"    {q['phase']} -> {q['status']} {q['wall_s']}s rss={q['peak_rss_mb']}MB")
        ok_mat = [r for r in rows if r["tool"] == "mat" and r["dump"] == stem
                  and r["phase"] == "index" and r["status"] == "ok"]
        if ok_mat:
            xmx = max(int(r["cap_mb"]) for r in ok_mat)
            sc = RUNS / f"mat-{stem}-{xmx}"
            print(f"  MAT reports {stem} (reusing -Xmx={xmx} index)")
            row = mat_run(sc, dump, xmx, args.report_timeout, reports=True)
            rows.append({"tool": "mat", "dump": stem, "cap_mb": xmx, **row})
            print(f"    reports -> {row['status']} {row['wall_s']}s rss={row['peak_rss_mb']}MB")

    print("== correctness cross-check")
    for stem in stems:
        correctness_reports.append(correctness(stem, dump_paths[stem], rows))

    emit_results(rows, correctness_reports)
    print(f"results written to {RESULTS}/results.csv, results.json, summary.md")


if __name__ == "__main__":
    main()
