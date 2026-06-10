"""crs-afl: run an AFL++ harness as a long-running background campaign.

Drives the AFL-instrumented build (the `afl-build` phase) with `afl-fuzz`,
mirroring crs-fuzz's lifecycle: `start | status | add-seeds | stop`. Crashes are
copied into the PoV dir (auto-submitted).

Live seed loading (the AFL analog of libFuzzer's `-reload=1`): AFL reads `-i` only
at startup, but a running instance imports new inputs through its parallel SYNC
mechanism. crs-afl runs the campaign as the `-M main` instance in an output/sync
dir, and `add-seeds` drops new seeds into a sibling `inject/queue/`; `main` scans
sibling queues and imports them on its next sync (we set `AFL_SYNC_TIME=1` minute).

    crs-afl start     --harness H            # launch the AFL campaign (background)
    crs-afl add-seeds --harness H <dir>      # inject seeds into the live campaign
    crs-afl status    --harness H            # execs / corpus / crashes from fuzzer_stats
    crs-afl stop      --harness H            # stop; crashes already in the PoV dir
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .common import (BUILD_VARIANTS, corpus_dir, libcrs_download, require_harness,
                     seed_from_boot, set_affinity, split_cpus)

AFL_RUNDIR = Path(os.environ.get("CRS_AFL_RUNDIR", "/work/agent/afl"))   # pidfiles + logs
AFL_OUT_ROOT = Path(os.environ.get("CRS_AFL_OUT", "/work/afl-out"))      # campaign/sync dirs
POV_DEFAULT = Path(os.environ.get("CRS_POV_DIR", "/artifacts/povs"))

# Env to run afl-fuzz headless in a container: skip CPU-governor/affinity tuning,
# tolerate the missing core_pattern handler, no TUI, and sync sibling-queue seeds
# every ~1 minute so `add-seeds` lands promptly.
_AFL_ENV = {
    "AFL_SKIP_CPUFREQ": "1",
    "AFL_NO_AFFINITY": "1",
    "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
    "AFL_AUTORESUME": "1",
    "AFL_NO_UI": "1",
    "AFL_SYNC_TIME": "1",
    "AFL_FAST_CAL": "1",
    "AFL_IMPORT_FIRST": "1",
}


# --- small process/dir helpers (kept independent of crs-fuzz) ---------------
def _slug(harness: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", harness)


def _pidfile(harness: str) -> Path:
    return AFL_RUNDIR / f"{_slug(harness)}.json"


def _logfile(harness: str) -> Path:
    return AFL_RUNDIR / f"{_slug(harness)}.log"


def _inst_log(harness: str, name: str) -> Path:
    """Per-instance log; the `main` instance keeps the canonical {slug}.log path."""
    slug = _slug(harness)
    return AFL_RUNDIR / (f"{slug}.log" if name == "main" else f"{slug}.{name}.log")


def _out_dir(harness: str) -> Path:
    return AFL_OUT_ROOT / _slug(harness)


def _inject_queue(harness: str) -> Path:
    return _out_dir(harness) / "inject" / "queue"


def _read_state(harness: str) -> dict | None:
    pf = _pidfile(harness)
    if not pf.exists():
        return None
    try:
        return json.loads(pf.read_text())
    except (ValueError, OSError):
        return None


def _merge_opts(existing: str, **overrides: str) -> str:
    """Merge `key=value` overrides into a colon-separated sanitizer-options string,
    replacing any existing value for those keys."""
    pairs = {}
    for tok in existing.split(":"):
        if "=" in tok:
            k, v = tok.split("=", 1)
            pairs[k] = v
        elif tok:
            pairs[tok] = ""
    pairs.update(overrides)
    return ":".join(f"{k}={v}" if v != "" else k for k, v in pairs.items())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A crashed afl-fuzz that nobody reaps (PID 1 in a container often doesn't)
    # lingers as a zombie, for which os.kill(pid, 0) still succeeds — treat that
    # as not alive so `status` reflects reality.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0]
        if state == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def _count(d: Path) -> int:
    return sum(1 for p in d.iterdir() if p.is_file()) if d.is_dir() else 0


def _kill_group(pid: int) -> None:
    """SIGTERM then SIGKILL the afl-fuzz process group."""
    if not _alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(25):
        if not _alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _afl_build_dir(build_dir) -> Path:
    """Resolve the AFL-instrumented build (no silent fallback to the libFuzzer build)."""
    if build_dir:
        return Path(build_dir)
    key, local = BUILD_VARIANTS["afl"]
    if local.is_dir() and any(local.iterdir()):
        return local
    if libcrs_download(key, local) and local.is_dir() and any(local.iterdir()):
        return local
    raise SystemExit(
        "[crs-afl] AFL build not found (/work/afl-build) and the libCRS download "
        "failed. Build it with the `afl-build` phase (`oss-crs build-target ...`). "
        "afl-fuzz needs the AFL-instrumented binary, not the libFuzzer build.")


def _state_pids(state: dict | None) -> list[int]:
    """All afl-fuzz pids for the campaign (new multi-instance `pids`, or legacy `pid`)."""
    if not state:
        return []
    pids = state.get("pids")
    if pids:
        return list(pids)
    p = state.get("pid")
    return [p] if p is not None else []


def _parse_stats_file(f: Path) -> dict:
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text(errors="replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _read_fuzzer_stats(harness: str) -> dict:
    """The `-M main` instance's fuzzer_stats (single-instance view)."""
    return _parse_stats_file(_out_dir(harness) / "main" / "fuzzer_stats")


def _aggregate_stats(harness: str) -> dict:
    """Sum execs / exec-per-sec across every instance's fuzzer_stats."""
    execs, eps, n = 0, 0.0, 0
    for sf in sorted(_out_dir(harness).glob("*/fuzzer_stats")):
        d = _parse_stats_file(sf)
        try:
            execs += int(d.get("execs_done", "0"))
        except ValueError:
            pass
        try:
            eps += float(d.get("execs_per_sec", "0"))
        except ValueError:
            pass
        n += 1
    return {"execs": execs, "eps": eps, "instances": n}


def _collect_crashes(harness: str, pov_out: Path) -> int:
    """Copy AFL crash inputs (<out>/*/crashes/id:*) into the PoV dir, deduped.

    AFL writes crashes under its own output dir; the run entrypoint only submits
    the PoV dir, so mirror them there. Returns the total AFL crash count present.
    """
    out = _out_dir(harness)
    if out.is_dir():
        pov_out.mkdir(parents=True, exist_ok=True)
        for cdir in out.glob("*/crashes"):
            for f in cdir.glob("id:*"):
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                dst = pov_out / f"afl-{_slug(harness)}-{hashlib.sha1(data).hexdigest()[:16]}"
                if not dst.exists():
                    dst.write_bytes(data)
    return sum(1 for _ in pov_out.glob(f"afl-{_slug(harness)}-*")) if pov_out.is_dir() else 0


def _sync_queue_to_corpus(harness: str) -> None:
    """Fold AFL's discovered queue into the shared corpus (content-deduped)."""
    corpus = corpus_dir(harness)
    seen = {p.name for p in corpus.iterdir() if p.is_file()}
    for qdir in _out_dir(harness).glob("*/queue"):
        for f in qdir.glob("id:*"):
            try:
                data = f.read_bytes()
            except OSError:
                continue
            h = hashlib.sha1(data).hexdigest()
            if h not in seen and data:
                (corpus / h).write_bytes(data)
                seen.add(h)


# --- lifecycle --------------------------------------------------------------
def start(harness: str, *, build_dir=None, dict_path=None,
          pov_out: Path = POV_DEFAULT, extra=None) -> int:
    state = _read_state(harness)
    if state and any(_alive(p) for p in _state_pids(state)):
        print(f"[crs-afl] a campaign is already running for {harness} "
              f"(pids={_state_pids(state)}). Feed it with `crs-afl add-seeds "
              f"--harness {harness} <dir>`; `crs-afl stop` to end it.", file=sys.stderr)
        return 1
    bd = _afl_build_dir(build_dir)
    # OSS-Fuzz's afl build ships the whole AFL++ toolchain (afl-fuzz, afl-showmap,
    # ...) into $OUT, so it lives in the build dir — it is NOT on the base-runner's
    # PATH. Prefer the build-dir copy; fall back to PATH only if absent.
    afl = bd / "afl-fuzz"
    if not afl.exists():
        which = shutil.which("afl-fuzz")
        if not which:
            raise SystemExit(
                f"[crs-afl] afl-fuzz not found in the afl build ({bd}) or on PATH "
                "— did the afl-build phase run?")
        afl = Path(which)
    hb = require_harness(bd, harness)
    corpus = corpus_dir(harness)
    corpus.mkdir(parents=True, exist_ok=True)
    pov_out.mkdir(parents=True, exist_ok=True)
    seed_from_boot(corpus)
    if _count(corpus) == 0:  # AFL refuses to start with an empty input dir
        (corpus / "seed0").write_bytes(b"\n")

    out = _out_dir(harness)
    out.mkdir(parents=True, exist_ok=True)
    AFL_RUNDIR.mkdir(parents=True, exist_ok=True)

    # Put the build-dir AFL toolchain on PATH so afl-fuzz finds afl-showmap etc.
    # afl-fuzz refuses to start unless a custom *SAN_OPTIONS conforms: ASAN needs
    # abort_on_error=1 + symbolize=0, MSAN needs exit_code=86 + symbolize=0 (so the
    # sanitizer aborts and AFL records the crash; AFL symbolizes itself). OSS-Fuzz's
    # runner sets both env vars non-conformingly, so merge the required keys in,
    # overriding conflicting values rather than just appending.
    child_env = {**os.environ, **_AFL_ENV,
                 "ASAN_OPTIONS": _merge_opts(os.environ.get("ASAN_OPTIONS", ""),
                                             abort_on_error="1", symbolize="0"),
                 "PATH": f"{bd}:{os.environ.get('PATH', '')}"}
    if os.environ.get("MSAN_OPTIONS"):
        child_env["MSAN_OPTIONS"] = _merge_opts(os.environ["MSAN_OPTIONS"],
                                                exit_code="86", symbolize="0")

    # crs-afl owns the SECOND half of the cpuset (crs-fuzz gets the first). A single
    # afl-fuzz is single-threaded, so to actually use the half we run one instance
    # PER core — `-M main` + `-S sec*` secondaries — all sharing the `-o` dir and
    # syncing through it (and from add-seeds' inject/queue/). Pin each to its core.
    afl_cpus = split_cpus()[1]
    pids = []
    for i, cpu in enumerate(afl_cpus):
        name = "main" if i == 0 else f"sec{i}"
        role = "-M" if i == 0 else "-S"
        cmd = [str(afl), "-i", str(corpus), "-o", str(out), role, name, "-m", "none"]
        if dict_path and Path(dict_path).exists():
            cmd += ["-x", str(dict_path)]
        if extra:
            cmd += list(extra)
        cmd += ["--", str(hb)]  # oss-fuzz aflpp driver: persistent/stdin, no @@
        log = open(_inst_log(harness, name), "ab")
        proc = subprocess.Popen([str(c) for c in cmd], stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True, env=child_env,
                                preexec_fn=lambda c=cpu: set_affinity([c]))
        pids.append(proc.pid)
        if i == 0:
            time.sleep(1)  # let -M main create its dir before secondaries sync
    _pidfile(harness).write_text(json.dumps({
        "pid": pids[0], "pids": pids, "instances": len(pids),
        "started": time.time(), "out": str(out), "log": str(_logfile(harness))}))
    print(f"[crs-afl] started AFL++ campaign for {harness}: {len(pids)} instance(s) "
          f"on cpus {afl_cpus} (pids={pids}), out {out}, corpus={_count(corpus)}, "
          f"log {_logfile(harness)}. Feed live seeds with `crs-afl add-seeds`.")
    return 0


def add_seeds(harness: str, candidate) -> int:
    """Inject `candidate` seeds into the live campaign via the AFL sync queue.

    Drops the files into the campaign's `inject/queue/`; the running `-M main`
    instance imports them on its next sync (~1 min, AFL_SYNC_TIME). Also folds
    them into the shared corpus (deduped) for reuse / the next start.
    """
    cand = Path(candidate)
    if not cand.is_dir():
        raise SystemExit(f"[crs-afl] add-seeds: dir not found: {cand}")
    state = _read_state(harness)
    running = bool(state and any(_alive(p) for p in _state_pids(state)))

    q = _inject_queue(harness)
    q.mkdir(parents=True, exist_ok=True)
    corpus = corpus_dir(harness)
    corpus.mkdir(parents=True, exist_ok=True)
    seen = {p.name for p in corpus.iterdir() if p.is_file()}
    n = sum(1 for _ in q.glob("id:*"))
    added = 0
    for f in sorted(cand.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        (q / f"id:{n:06d}").write_bytes(data)
        n += 1
        added += 1
        h = hashlib.sha1(data).hexdigest()
        if h not in seen:
            (corpus / h).write_bytes(data)
            seen.add(h)

    if running:
        print(f"[crs-afl] add-seeds {harness}: injected {added} seed(s) into the sync "
              f"queue; the running AFL imports them within ~1 min (AFL_SYNC_TIME).")
    else:
        print(f"[crs-afl] add-seeds {harness}: staged {added} seed(s) "
              f"(no campaign running; they load on `crs-afl start`).")
    return 0


def status(harness: str, *, pov_out: Path = POV_DEFAULT) -> int:
    corpus = corpus_dir(harness)
    crashes = _collect_crashes(harness, pov_out)
    # Fold AFL's discoveries into the shared corpus on every poll (not just stop),
    # so newly-found seeds reach the seed exchange (auto-submitted from the corpus)
    # and cross-pollinate crs-fuzz during a long-running campaign.
    _sync_queue_to_corpus(harness)
    state = _read_state(harness)
    if not state:
        print(f"[crs-afl] {harness}: no AFL campaign running. "
              f"corpus={_count(corpus)}, crashes={crashes}.")
        return 0
    pids = _state_pids(state)
    alive = [p for p in pids if _alive(p)]
    elapsed = time.time() - state.get("started", time.time())
    if alive:
        agg = _aggregate_stats(harness)
        print(f"[crs-afl] {harness}: RUNNING {len(alive)}/{len(pids)} instance(s) "
              f"for {elapsed:.0f}s; execs={agg['execs']}, corpus={_count(corpus)}, "
              f"crashes={crashes}, exec/s={agg['eps']:.0f} (pids={pids}); "
              f"log {state.get('log')}.")
    else:
        _pidfile(harness).unlink(missing_ok=True)
        print(f"[crs-afl] {harness}: not running (ended after ~{elapsed:.0f}s). "
              f"corpus={_count(corpus)}, crashes={crashes}. `crs-afl start` to run again.")
    return 0


def stop(harness: str, *, pov_out: Path = POV_DEFAULT) -> int:
    state = _read_state(harness)
    crashes = _collect_crashes(harness, pov_out)
    if not state:
        print(f"[crs-afl] {harness}: no campaign to stop. crashes={crashes}.")
        return 0
    pids = _state_pids(state)
    for p in pids:
        _kill_group(p)
    _pidfile(harness).unlink(missing_ok=True)
    _sync_queue_to_corpus(harness)
    elapsed = time.time() - state.get("started", time.time())
    print(f"[crs-afl] stopped {harness} ({len(pids)} instance(s)) after {elapsed:.0f}s; "
          f"corpus={_count(corpus_dir(harness))}, crashes={crashes} (in {pov_out}).")
    return 0


# --- CLI --------------------------------------------------------------------
_COMMANDS = ("start", "status", "stop", "add-seeds")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    ap = argparse.ArgumentParser(
        prog=f"crs-afl {cmd}" if cmd in _COMMANDS else "crs-afl",
        description="Run an AFL++ campaign: start | status | add-seeds | stop --harness H")
    ap.add_argument("--harness", default=os.environ.get("OSS_CRS_TARGET_HARNESS"),
                    help="Harness binary name (default: $OSS_CRS_TARGET_HARNESS)")
    ap.add_argument("seeds_dir", nargs="?", default=None,
                    help="(add-seeds) directory of seeds to inject into the live campaign")
    ap.add_argument("--dict", dest="dict_path", type=Path, default=None,
                    help="(start) AFL dictionary file (-x)")
    ap.add_argument("--build-dir", default=None,
                    help="Dir with the AFL harness (default: /work/afl-build via libCRS)")
    ap.add_argument("--pov-out", type=Path, default=POV_DEFAULT)

    if cmd not in _COMMANDS:
        print(f"crs-afl: command must be one of {' | '.join(_COMMANDS)}\n"
              f"  e.g. crs-afl start --harness <H>", file=sys.stderr)
        return 2
    a, extra = ap.parse_known_args(argv[1:])
    if not a.harness:
        print("crs-afl: no harness (pass --harness <H> or set $OSS_CRS_TARGET_HARNESS)",
              file=sys.stderr)
        return 2

    if cmd == "start":
        return start(a.harness, build_dir=a.build_dir, dict_path=a.dict_path,
                     pov_out=a.pov_out, extra=extra)
    if cmd == "status":
        return status(a.harness, pov_out=a.pov_out)
    if cmd == "stop":
        return stop(a.harness, pov_out=a.pov_out)
    if cmd == "add-seeds":
        if not a.seeds_dir:
            print("crs-afl add-seeds: missing <dir> of seeds to inject", file=sys.stderr)
            return 2
        return add_seeds(a.harness, a.seeds_dir)
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
