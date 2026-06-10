"""crs-codeql: author and run CodeQL queries against the target database.

The codeql-build phase indexes the target into a CodeQL database (submitted under
the `codeql` build-output key). This tool lets the bug-finding agent write its own `.ql` queries
and run them against that database to reason about the code semantically —
data-flow, taint, call graphs, reachability from the harness, etc. — which is
far more precise than grep for "where does attacker input reach a dangerous
sink".

Subcommands:
  info                 - print the DB language, the `import` to use, and an
                         example query (call this first).
  run <query.ql>       - compile and run a custom query, printing the results.
  analyze [--suite S]  - run a built-in security query suite over the DB.

The database is resolved from an explicit --db, a local download, or fetched via
libCRS (download-build-output codeql). The CodeQL CLI ships in the runner
image at /opt/codeql (also on PATH).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from .common import CODEQL_DB_LOCAL, CODEQL_DB_REMOTE, libcrs_download, run

# The standard C/C++ query pack shipped in the CodeQL bundle.
QUERY_PACK = "codeql/cpp-queries"
# ...and the standard *library* pack that provides `import cpp`.
LIB_PACK = "codeql/cpp-all"
# Built-in suites. `default` is the pack's own default suite.
SUITES = ("default", "security-extended", "security-and-quality", "code-scanning")


def _codeql_bin() -> str:
    exe = shutil.which("codeql") or "/opt/codeql/codeql"
    if not Path(exe).exists():
        raise SystemExit(
            "codeql CLI not found (expected on PATH or at /opt/codeql/codeql)"
        )
    return exe


def _resolve_db(explicit) -> Path:
    """Return the CodeQL database directory (explicit, local, or via libCRS)."""
    if explicit:
        db = Path(explicit)
        if not (db / "codeql-database.yml").exists():
            raise SystemExit(f"not a CodeQL database (no codeql-database.yml): {db}")
        return db

    if (CODEQL_DB_LOCAL / "codeql-database.yml").exists():
        return CODEQL_DB_LOCAL
    if (libcrs_download(CODEQL_DB_REMOTE, CODEQL_DB_LOCAL)
            and (CODEQL_DB_LOCAL / "codeql-database.yml").exists()):
        return CODEQL_DB_LOCAL

    raise SystemExit(
        "CodeQL database not found. Pass --db <dir>, or ensure the codeql-build "
        "phase ran (it is fetched from build output 'codeql')."
    )


_EXAMPLE_QUERY = (
    "import cpp\n\n"
    "from FunctionCall c\n"
    "where c.getTarget().getName() = \"memcpy\"\n"
    "select c, \"call to memcpy — check the length argument\"\n"
)


def info(db_arg=None) -> int:
    db = _resolve_db(db_arg)
    print(f"CodeQL database: {db}")
    print(f"Language:        cpp")
    print(f"Query pack:      {QUERY_PACK}")
    print()
    print("Start every query with `import cpp`. Example query:")
    print("-" * 60)
    print(_EXAMPLE_QUERY, end="")
    print("-" * 60)
    print("Write a query to a .ql file, then run:")
    print("  crs-codeql run <query.ql>")
    print("Or run a built-in security suite:")
    print("  crs-codeql analyze --suite security-extended")
    return 0


def run_query(query: Path, *, db_arg=None, threads: int = 0,
              timeout: int = 1200, output_bqrs: Path | None = None) -> int:
    """Compile and run a single .ql query against the DB; print the results."""
    db = _resolve_db(db_arg)
    if not query.exists():
        raise SystemExit(f"query file not found: {query}")
    lib = LIB_PACK

    # CodeQL only resolves `import cpp` for a query that lives in a pack declaring
    # the standard library pack as a dependency. The agent's .ql is standalone, so
    # wrap it in a scratch query pack. The library pack ships inside the bundle, so
    # this resolves offline — no network or `codeql pack install` step is needed.
    scratch = "/work" if Path("/work").is_dir() else None
    pack_dir = Path(tempfile.mkdtemp(prefix="crs-codeql-", dir=scratch))
    (pack_dir / "qlpack.yml").write_text(
        f'name: crs/adhoc\nversion: 0.0.1\ndependencies:\n  {lib}: "*"\n'
    )
    wrapped = pack_dir / query.name
    shutil.copy(query, wrapped)

    # Always evaluate to a BQRS, then decode it ourselves. `codeql query run`'s
    # default text table renders each entity column via its toString() label
    # (e.g. "call to memcpy") and DROPS the location — the DB has it, the table
    # just hides it. Decoding with --entities=string,url restores a file:line
    # column per entity so results are actually actionable.
    bqrs = output_bqrs if output_bqrs else (pack_dir / "results.bqrs")
    if output_bqrs:
        output_bqrs.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_codeql_bin(), "query", "run",
           f"--database={db}",
           f"--threads={threads}",
           f"--output={bqrs}",
           "--", str(wrapped)]

    r = run(cmd, timeout=timeout)
    if r.returncode != 0:
        # Compile/eval errors land on stderr — surface them so the agent can fix.
        sys.stderr.write(r.stderr or "")
        print(f"\n[crs-codeql] query failed (rc={r.returncode}). "
              "Fix compile errors above; run `crs-codeql info` for the import to use.",
              file=sys.stderr)
        return r.returncode

    # string = the entity's label; url = its file:line:col location.
    dec = run([_codeql_bin(), "bqrs", "decode", "--format=text",
               "--entities=string,url", str(bqrs)], timeout=600)
    sys.stdout.write(dec.stdout or "")
    if dec.returncode != 0:
        sys.stderr.write(dec.stderr or "")
        return dec.returncode
    if output_bqrs:
        print(f"\n[crs-codeql] raw results (BQRS) written to {output_bqrs}")
    return 0


def analyze(*, db_arg=None, suite: str = "security-extended",
            out_dir: Path = Path("/work/codeql-analyze"),
            threads: int = 0, timeout: int = 3600) -> int:
    """Run a built-in CodeQL query suite over the DB; write SARIF + summary."""
    db = _resolve_db(db_arg)
    pack = QUERY_PACK

    # `analyze <db> <pack>` runs the pack's default suite; a named suite is the
    # pack's cpp-<suite>.qls (e.g. codeql/cpp-queries:codeql-suites/cpp-security-extended.qls).
    if suite == "default":
        query_spec = pack
    else:
        query_spec = f"{pack}:codeql-suites/cpp-{suite}.qls"

    out_dir.mkdir(parents=True, exist_ok=True)
    sarif = out_dir / f"cpp-{suite}.sarif"
    cmd = [_codeql_bin(), "database", "analyze", str(db), query_spec,
           "--format=sarifv2.1.0", f"--output={sarif}",
           f"--threads={threads}", "--rerun"]
    r = run(cmd, timeout=timeout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        print(f"[crs-codeql] analyze failed (rc={r.returncode})", file=sys.stderr)
        return r.returncode

    _summarize_sarif(sarif)
    print(f"[crs-codeql] full SARIF results: {sarif}")
    return 0


def _summarize_sarif(sarif: Path) -> None:
    """Print a one-line-per-finding summary (rule + location) from a SARIF file."""
    import json
    try:
        data = json.loads(sarif.read_text(errors="replace"))
    except Exception as e:  # noqa: BLE001
        print(f"[crs-codeql] could not parse SARIF {sarif}: {e}", file=sys.stderr)
        return
    findings = []
    for runobj in data.get("runs", []):
        for res in runobj.get("results", []):
            rule = res.get("ruleId", "?")
            msg = (res.get("message", {}) or {}).get("text", "").splitlines()
            msg = msg[0] if msg else ""
            loc = ""
            locs = res.get("locations") or []
            if locs:
                pl = (locs[0].get("physicalLocation") or {})
                uri = (pl.get("artifactLocation") or {}).get("uri", "")
                line = (pl.get("region") or {}).get("startLine", "")
                loc = f"{uri}:{line}" if uri else ""
            findings.append((rule, loc, msg))
    print(f"[crs-codeql] {len(findings)} finding(s):")
    for rule, loc, msg in findings:
        print(f"  - {rule}  {loc}\n      {msg}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="crs-codeql",
        description="Author and run CodeQL queries against the target database.",
    )
    ap.add_argument("--db", default=None,
                    help="CodeQL database dir (default: 'codeql' build output via libCRS)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="Print DB language, the import to use, and an example query")

    p_run = sub.add_parser("run", help="Compile and run a custom .ql query")
    p_run.add_argument("query", type=Path, help="Path to a .ql query file")
    p_run.add_argument("--threads", type=int, default=0, help="0 = one per core")
    p_run.add_argument("--timeout", type=int, default=1200)
    p_run.add_argument("--output-bqrs", type=Path, default=None,
                       help="Also write raw results to this .bqrs file")

    p_an = sub.add_parser("analyze", help="Run a built-in security query suite")
    p_an.add_argument("--suite", default="security-extended", choices=SUITES)
    p_an.add_argument("--out-dir", type=Path, default=Path("/work/codeql-analyze"))
    p_an.add_argument("--threads", type=int, default=0)
    p_an.add_argument("--timeout", type=int, default=3600)

    args = ap.parse_args(argv)

    if args.cmd == "info":
        return info(db_arg=args.db)
    if args.cmd == "run":
        return run_query(args.query, db_arg=args.db, threads=args.threads,
                         timeout=args.timeout, output_bqrs=args.output_bqrs)
    # cmd is one of the above (subparser is required=True).
    return analyze(db_arg=args.db, suite=args.suite, out_dir=args.out_dir,
                   threads=args.threads, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
