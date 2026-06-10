"""crs-coverage: coverage report for a corpus.

Replays the corpus through the coverage-instrumented harness, merges the raw
llvm profile, and emits an llvm-cov summary + line report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .common import (corpus_dir, have, require_harness, resolve_build_dir, run,
                     seed_from_boot)

# The target's coverage build may be instrumented by a different LLVM than the
# runner's default tools, and the profraw format is version-locked. Probe these
# llvm-profdata/llvm-cov variants and use the first that can read the profile.
_LLVM_SUFFIXES = ["", "-18", "-19", "-17", "-20", "-16", "-15"]


def _pick_llvm(profraw: Path, scratch: Path):
    """Return (llvm-profdata, llvm-cov) able to read this profraw, or (None, None)."""
    for suf in _LLVM_SUFFIXES:
        profdata_tool, cov_tool = f"llvm-profdata{suf}", f"llvm-cov{suf}"
        if not have(profdata_tool) or not have(cov_tool):
            continue
        r = run([profdata_tool, "merge", "-sparse", str(profraw), "-o", str(scratch)], timeout=180)
        if r.returncode == 0:
            return profdata_tool, cov_tool
    return None, None


def _generate_native(harness: str, corpus: Path, *, build_dir, out_dir: Path, timeout: int) -> Path:
    bd = resolve_build_dir("coverage", build_dir)
    hb = require_harness(bd, harness)
    out_dir.mkdir(parents=True, exist_ok=True)

    profraw = out_dir / f"{harness}.profraw"
    profdata = out_dir / f"{harness}.profdata"

    # Replay the corpus (-runs=0 => no fuzzing, just execute provided inputs).
    run([hb, str(corpus), "-runs=0"], timeout=timeout,
        env={"LLVM_PROFILE_FILE": str(profraw)})

    if not profraw.exists():
        raise SystemExit(f"no profile produced at {profraw} (is this a coverage build?)")

    profdata_tool, cov_tool = _pick_llvm(profraw, profdata)
    if profdata_tool is None:
        raise SystemExit(
            "no compatible llvm-profdata found for this profile version; "
            "install the llvm-NN matching the coverage build's clang"
        )
    assert cov_tool is not None  # paired with profdata_tool by _pick_llvm

    run([profdata_tool, "merge", "-sparse", str(profraw), "-o", str(profdata)], timeout=300)

    # Per-file + total numbers from JSON (robust vs. the wide, wrapping text table).
    files, totals = _cov_export(cov_tool, hb, profdata)
    # Annotate only PROJECT source the harness actually REACHED (covered > 0).
    # This is a reachability guard: a 0%-coverage file may be genuinely
    # unreachable from this harness, not a gap worth chasing — coverage can't
    # distinguish "not hit yet" from "can't be hit", so we don't surface it as a
    # target. Order by MISSED lines (count - covered) so the biggest reachable
    # gaps lead; fully-covered files sort to the bottom (missed = 0). (A full
    # `llvm-cov show` would also dump 0%-coverage files and system headers — noise.)
    gap_files = sorted(
        (f for f in files if f["covered"] > 0 and _is_project_src(f["name"])),
        key=lambda f: (f["count"] - f["covered"], -f["pct"]), reverse=True)

    full_report = run([cov_tool, "report", str(hb), f"-instr-profile={profdata}"], timeout=300)

    # Annotate ALL reached project files (no cap) — biggest gap first.
    show_files = [f["name"] for f in gap_files]
    detail = run([cov_tool, "show", str(hb), f"-instr-profile={profdata}",
                  "-format=text", "-use-color=0", *show_files], timeout=max(timeout, 600)) \
        if show_files else None

    report_txt = out_dir / f"{harness}.coverage.txt"
    report_txt.write_text(
        (full_report.stdout or "")
        + "\n\n=== line annotations (reached project files, biggest gap first) ===\n"
        + (detail.stdout if detail else "(no reached project source)\n"))

    _print_native_summary(harness, totals, gap_files)
    print(f"[crs-coverage] full report (summary + annotations) -> {report_txt}")
    return report_txt


# Coverage-mapping paths for the harness's own project live under /src; skip the
# toolchain/system headers that pad the report with `#define` noise.
_SYS_PREFIXES = ("/usr/", "/lib/", "/opt/", "/work/llvm", "/src/llvm-project")


def _is_project_src(path: str) -> bool:
    return bool(path) and not path.startswith(_SYS_PREFIXES)


def _cov_export(cov_tool: str, hb: Path, profdata: Path, *, timeout: int = 300):
    """Return (per-file list, totals dict) via `llvm-cov export -summary-only`."""
    r = run([cov_tool, "export", str(hb), f"-instr-profile={profdata}",
             "-summary-only"], timeout=timeout)
    try:
        data = json.loads(r.stdout or "{}")["data"][0]
    except (ValueError, KeyError, IndexError):
        return [], {}
    files = []
    for f in data.get("files", []):
        ln = (f.get("summary") or {}).get("lines") or {}
        files.append({"name": f.get("filename", ""),
                      "covered": ln.get("covered", 0), "count": ln.get("count", 0),
                      "pct": ln.get("percent", 0.0)})
    return files, data.get("totals", {})


def _print_native_summary(harness: str, totals: dict, gap_files: list) -> None:
    ln = totals.get("lines", {}); rg = totals.get("regions", {}); fn = totals.get("functions", {})
    print(f"[crs-coverage] {harness}: lines {ln.get('percent', 0):.1f}% "
          f"({ln.get('covered', 0)}/{ln.get('count', 0)}), "
          f"regions {rg.get('percent', 0):.1f}%, functions {fn.get('percent', 0):.1f}%")
    print(f"[crs-coverage] {len(gap_files)} reached project file(s); biggest gaps "
          f"(reached, but most lines still missed):")
    for f in gap_files[:12]:
        rel = f["name"].split("/src/", 1)[-1]
        missed = f["count"] - f["covered"]
        print(f"    {rel:<46} {f['pct']:5.1f}%  ({missed} missed of {f['count']} lines)")
    print(f"[crs-coverage] full per-file table + line annotations for ALL "
          f"{len(gap_files)} reached file(s) in the report.")


def generate(harness: str, corpus: Path, *, build_dir=None,
             out_dir: Path = Path("/work/coverage-report"), timeout: int = 900) -> Path:
    """Run coverage over `corpus`; write report to out_dir; return its path."""
    return _generate_native(harness, corpus, build_dir=build_dir, out_dir=out_dir, timeout=timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="crs-coverage", description="Generate an llvm-cov coverage report for a corpus.")
    ap.add_argument("--harness", default=os.environ.get("OSS_CRS_TARGET_HARNESS"),
                    help="Harness binary name (default: $OSS_CRS_TARGET_HARNESS)")
    ap.add_argument("--corpus", type=Path, default=None,
                    help="Directory of inputs to replay (default: canonical "
                         "/artifacts/corpus/<harness>, seeded from boot seeds)")
    ap.add_argument("--build-dir", default=None, help="Dir with coverage harness (default: coverage build via libCRS)")
    ap.add_argument("--out-dir", type=Path, default=Path("/work/coverage-report"))
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args(argv)
    if not args.harness:
        print("crs-coverage: no harness (pass --harness <H> or set $OSS_CRS_TARGET_HARNESS)",
              file=sys.stderr)
        return 2

    corpus = args.corpus or corpus_dir(args.harness)
    # Make sure the pool reflects at least the boot seeds before measuring.
    seed_from_boot(corpus)
    if not corpus.exists() or not any(p.is_file() for p in corpus.iterdir()):
        print(f"corpus is empty: {corpus} — run `crs-fuzz start` "
              f"first, or pass --corpus <dir>", file=sys.stderr)
        return 2
    generate(args.harness, corpus, build_dir=args.build_dir, out_dir=args.out_dir, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
