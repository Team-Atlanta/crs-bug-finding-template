"""crs-fuzz: run a libFuzzer harness in fork mode — long-running or foreground.

Drives the harness over the shared corpus (`/artifacts/corpus/<harness>`),
writing crashes to the PoV dir (auto-submitted):

  * Long-running (preferred): `crs-fuzz start --harness H` launches ONE background
    fuzzer that runs until stopped (or the run ends). It runs with `-reload=1`, so
    it CONTINUOUSLY picks up new seed files dumped into the corpus dir — no restart
    needed. Add seeds while it runs with:
      `crs-fuzz add-seeds --harness H <dir>`  (coverage-merges <dir> into the live
                                               corpus; the running fuzzer reloads)
    Manage with `crs-fuzz status|stop --harness H`.
  * Foreground, bounded:  `crs-fuzz --harness H --seconds N`  (blocks N seconds)
                          `crs-fuzz --harness H --merge`      (minimize the corpus)

The seed-generation loop is now: start the long-running fuzzer once, then keep
generating seeds and `add-seeds`-ing them while measuring coverage — the fuzzer
reloads the corpus on its own, so there is no constant stop/restart cycle.
"""

from __future__ import annotations

import argparse
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

from .common import (corpus_dir, require_harness, resolve_build_dir, run,
                     seed_from_boot, set_affinity, split_cpus)

# Where background-fuzzer pidfiles + logs live (one per harness).
FUZZ_RUNDIR = Path(os.environ.get("CRS_FUZZ_RUNDIR", "/work/agent/fuzz"))
POV_DEFAULT = Path(os.environ.get("CRS_POV_DIR", "/artifacts/povs"))
_CRASH_PREFIXES = ("crash", "oom", "timeout", "leak")


def _fuzz_cpus() -> list[int]:
    """The cores allotted to crs-fuzz (the first half of the cpuset; crs-afl gets
    the second half)."""
    return split_cpus()[0]


def _build_cmd(hb, corpus: Path, pov_out: Path, *, seconds: int, jobs: int,
               max_len, dict_path, extra) -> list:
    """The libFuzzer fork-mode argv shared by foreground and background runs."""
    cmd = [
        hb, str(corpus),
        f"-artifact_prefix={pov_out}/",
        f"-fork={jobs}",
        # Reload the corpus dir periodically so a long-running fuzzer picks up seeds
        # dumped in by `add-seeds` (or written directly) WITHOUT a restart.
        "-reload=1",
        "-ignore_crashes=1",
        "-ignore_timeouts=1",
        "-ignore_ooms=1",
    ]
    # seconds<=0 => run until stopped (the long-running fuzzer); otherwise cap it.
    if seconds and seconds > 0:
        cmd.append(f"-max_total_time={seconds}")
    if max_len is not None:
        # libFuzzer -max_len (bytes) caps the size of MUTATED inputs. With a
        # startup corpus and -max_len UNSET, libFuzzer auto-sizes it to the largest
        # seed (the 4096 default only applies with no seeds); setting it BELOW a
        # seed's size SILENTLY TRUNCATES that seed. Use it to ENLARGE the cap.
        cmd.append(f"-max_len={max_len}")
    if dict_path is not None:
        if not Path(dict_path).exists():
            print(f"[crs-fuzz] warning: dict not found, ignoring: {dict_path}", file=sys.stderr)
        else:
            cmd.append(f"-dict={dict_path}")
    # libFuzzer/ASan flags: don't fail on leaks, and isolate stdout/stderr fds.
    cmd += ["-detect_leaks=0", "-close_fd_mask=3"]
    if extra:
        cmd += list(extra)
    return cmd


def fuzz(harness: str, *, corpus: Path, pov_out: Path, build_dir=None,
         seconds: int = 300, jobs: int | None = None,
         max_len: int | None = None, dict_path: Path | None = None,
         extra=None) -> int:
    """Fuzz `harness` for `seconds` in the FOREGROUND; crashes land in pov_out."""
    bd = resolve_build_dir("asan", build_dir)
    hb = require_harness(bd, harness)
    corpus.mkdir(parents=True, exist_ok=True)
    pov_out.mkdir(parents=True, exist_ok=True)
    seed_from_boot(corpus)  # a fresh corpus shouldn't be empty
    fuzz_cpus = _fuzz_cpus()
    if jobs is None:
        jobs = len(fuzz_cpus)

    cmd = _build_cmd(hb, corpus, pov_out, seconds=seconds, jobs=jobs,
                     max_len=max_len, dict_path=dict_path, extra=extra)
    r = run(cmd, timeout=seconds + 120, capture=False)

    crashes = sorted(p.name for p in pov_out.glob("crash-*"))
    print(f"[crs-fuzz] {harness}: {len(crashes)} crash artifact(s); "
          f"corpus={_count(corpus)} (PoVs + seeds auto-submit)")
    return r.returncode


def merge_corpus(harness: str, *, build_dir=None, timeout: int = 900) -> int:
    """Minimize the canonical corpus in place via libFuzzer `-merge=1`.

    Keeps a minimal subset of inputs preserving the same edge coverage, then
    re-submits the minimized pool to the seed exchange. Returns the post-merge
    input count. Stops any running background fuzzer first (can't safely merge a
    live corpus).
    """
    _stop_if_running(harness)
    bd = resolve_build_dir("asan", build_dir)
    hb = require_harness(bd, harness)
    corpus = corpus_dir(harness)
    seed_from_boot(corpus)
    before = _count(corpus)

    # `-merge=1 OUT IN`: write the minimal covering subset of IN into OUT, then
    # swap it back in (merge into a sibling temp dir so the swap is a local move).
    tmp = Path(tempfile.mkdtemp(prefix=f".merge-{_slug(harness)}-", dir=str(corpus.parent)))
    try:
        cmd = [hb, str(tmp), str(corpus), "-merge=1", "-detect_leaks=0", "-close_fd_mask=3"]
        run(cmd, timeout=timeout, capture=False)
        merged = [p for p in tmp.iterdir() if p.is_file()]
        if merged:  # only swap if the merge produced a covering set
            for p in corpus.iterdir():
                if p.is_file():
                    p.unlink()
            for p in merged:
                shutil.move(str(p), str(corpus / p.name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = _count(corpus)
    print(f"[crs-fuzz] merge {harness}: {before} -> {after} input(s) "
          f"(minimal covering set)")
    return after


# ===========================================================================
# Long-running fuzzer lifecycle (start / status / add-seeds / stop)
# ===========================================================================
def _slug(harness: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", harness)


def _pidfile(harness: str) -> Path:
    return FUZZ_RUNDIR / f"{_slug(harness)}.json"


def _logfile(harness: str) -> Path:
    return FUZZ_RUNDIR / f"{_slug(harness)}.log"


def _read_state(harness: str) -> dict | None:
    pf = _pidfile(harness)
    if not pf.exists():
        return None
    try:
        return json.loads(pf.read_text())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not signalable by us (won't happen as root)
    return True


def _count(d: Path) -> int:
    return sum(1 for p in d.iterdir() if p.is_file()) if d.is_dir() else 0


def _count_crashes(pov_out: Path) -> int:
    if not pov_out.exists():
        return 0
    return sum(1 for p in pov_out.iterdir()
               if p.is_file() and p.name.split("-", 1)[0] in _CRASH_PREFIXES)


def _kill_group(pid: int) -> None:
    """SIGTERM then SIGKILL the fuzzer's process group (parent + fork workers)."""
    if not _alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(25):  # up to ~5s for a graceful stop
        if not _alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop_if_running(harness: str) -> None:
    state = _read_state(harness)
    if state and _alive(state.get("pid", -1)):
        _kill_group(state["pid"])
    _pidfile(harness).unlink(missing_ok=True)


def start(harness: str, *, seconds: int, jobs=None, max_len=None, dict_path=None,
          build_dir=None, pov_out: Path = POV_DEFAULT, extra=None) -> int:
    """Launch a background fuzzer for `harness` (detached process group)."""
    state = _read_state(harness)
    if state and _alive(state.get("pid", -1)):
        print(f"[crs-fuzz] a long-running fuzzer is already running for {harness} "
              f"(pid={state['pid']}). It auto-reloads new corpus files — just "
              f"`crs-fuzz add-seeds --harness {harness} <dir>` to feed it (no "
              f"restart). Use `crs-fuzz stop` to end it.", file=sys.stderr)
        return 1

    bd = resolve_build_dir("asan", build_dir)
    hb = require_harness(bd, harness)
    corpus = corpus_dir(harness)
    corpus.mkdir(parents=True, exist_ok=True)
    pov_out.mkdir(parents=True, exist_ok=True)
    seed_from_boot(corpus)
    fuzz_cpus = _fuzz_cpus()
    if jobs is None:
        jobs = len(fuzz_cpus)

    cmd = [str(c) for c in _build_cmd(hb, corpus, pov_out, seconds=seconds,
                                      jobs=jobs, max_len=max_len,
                                      dict_path=dict_path, extra=extra)]
    FUZZ_RUNDIR.mkdir(parents=True, exist_ok=True)
    log = open(_logfile(harness), "ab")
    # start_new_session=True -> the child leads a new session/process group, so its
    # pid is the group id and killpg(pid) reaps the fork workers too. Pin the whole
    # campaign to crs-fuzz's core half (fork workers inherit the affinity mask) so
    # it doesn't contend with crs-afl on the other half.
    proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True,
                            preexec_fn=lambda: set_affinity(fuzz_cpus))
    _pidfile(harness).write_text(json.dumps({
        "pid": proc.pid, "started": time.time(), "seconds": seconds,
        "log": str(_logfile(harness)), "corpus_at_start": _count(corpus),
    }))
    life = f"self-stops after {seconds}s" if seconds and seconds > 0 else "runs until stopped"
    print(f"[crs-fuzz] started long-running fuzzer for {harness}: pid={proc.pid}, "
          f"-fork={jobs} on cpus {fuzz_cpus}, -reload=1 ({life}), "
          f"corpus={_count(corpus)}, log {_logfile(harness)}. "
          f"Feed it with `crs-fuzz add-seeds`.")
    return 0


def status(harness: str, *, pov_out: Path = POV_DEFAULT) -> int:
    """Report whether the background fuzzer is running, and corpus/crash counts."""
    corpus = corpus_dir(harness)
    n, crashes = _count(corpus), _count_crashes(pov_out)
    state = _read_state(harness)
    if not state:
        print(f"[crs-fuzz] {harness}: no background fuzzer running. "
              f"corpus={n}, crashes={crashes}.")
        return 0
    elapsed = time.time() - state.get("started", time.time())
    if _alive(state.get("pid", -1)):
        grew = n - state.get("corpus_at_start", n)
        secs = state.get("seconds", 0)
        life = f"self-stops at {secs}s" if secs and secs > 0 else "runs until stopped"
        print(f"[crs-fuzz] {harness}: RUNNING pid={state['pid']} for {elapsed:.0f}s "
              f"({life}); corpus={n} (+{grew} since start), crashes={crashes}; "
              f"log {state.get('log')}. Feed it with `crs-fuzz add-seeds`.")
    else:
        _pidfile(harness).unlink(missing_ok=True)
        print(f"[crs-fuzz] {harness}: fuzzer not running (ended after ~{elapsed:.0f}s). "
              f"corpus={n}, crashes={crashes}. `crs-fuzz start` to run again.")
    return 0


def stop(harness: str, *, pov_out: Path = POV_DEFAULT) -> int:
    """Stop the long-running fuzzer (PoVs + seeds auto-submit on their own)."""
    state = _read_state(harness)
    if not state:
        print(f"[crs-fuzz] {harness}: no fuzzer to stop.")
        return 0
    _kill_group(state.get("pid", -1))
    _pidfile(harness).unlink(missing_ok=True)
    elapsed = time.time() - state.get("started", time.time())
    corpus = corpus_dir(harness)
    crashes = _count_crashes(pov_out)
    print(f"[crs-fuzz] stopped {harness} after {elapsed:.0f}s; "
          f"corpus={_count(corpus)}, crashes={crashes}")
    return 0


def add_seeds(harness: str, candidate, *, build_dir=None, timeout: int = 900) -> int:
    """Coverage-merge `candidate` seeds into the live corpus WITHOUT a restart.

    Runs `harness -merge=1 <corpus> <candidate>`: adds only the coverage-increasing
    inputs from `candidate` into the canonical corpus dir. A long-running fuzzer
    started with `-reload=1` then picks them up on its own — this is how you feed
    newly generated seeds to the running fuzzer (no stop/restart). Returns the
    post-merge corpus count.
    """
    cand = Path(candidate)
    if not cand.is_dir():
        raise SystemExit(f"add-seeds: candidate dir not found: {cand}")
    bd = resolve_build_dir("asan", build_dir)
    hb = require_harness(bd, harness)
    corpus = corpus_dir(harness)
    corpus.mkdir(parents=True, exist_ok=True)
    before = _count(corpus)

    # `-merge=1 OUT IN`: fold IN's coverage-increasing units into OUT (the live
    # corpus), keeping OUT's existing files. New files appear in the corpus dir,
    # which the running fuzzer reloads.
    cmd = [hb, str(corpus), str(cand), "-merge=1", "-detect_leaks=0", "-close_fd_mask=3"]
    run(cmd, timeout=timeout, capture=False)

    after = _count(corpus)
    print(f"[crs-fuzz] add-seeds {harness}: corpus {before} -> {after} "
          f"(+{after - before} coverage-increasing); the running fuzzer will reload them.")
    return after


# ===========================================================================
# CLI
# ===========================================================================
_LIFECYCLE = {"start", "status", "stop", "add-seeds"}


def _lifecycle_main(cmd: str, rest) -> int:
    ap = argparse.ArgumentParser(prog=f"crs-fuzz {cmd}")
    ap.add_argument("--harness", default=os.environ.get("OSS_CRS_TARGET_HARNESS"),
                    help="Harness binary name (default: $OSS_CRS_TARGET_HARNESS)")
    ap.add_argument("seeds_dir", nargs="?", default=None,
                    help="(add-seeds) directory of candidate seeds to merge into the live corpus")
    ap.add_argument("--seconds", type=int, default=0,
                    help="(start) optional self-stop cap; default 0 = run until stopped")
    ap.add_argument("--jobs", type=int, default=None, help="fork workers (default: OSS_CRS_CPUSET)")
    ap.add_argument("--max-len", type=int, default=None, help="libFuzzer -max_len (bytes)")
    ap.add_argument("--dict", dest="dict_path", type=Path, default=None, help="libFuzzer -dict file")
    ap.add_argument("--build-dir", default=None, help="Dir with asan harness (default: /out or libCRS)")
    ap.add_argument("--pov-out", type=Path, default=POV_DEFAULT)
    a, extra = ap.parse_known_args(rest)
    if not a.harness:
        print("crs-fuzz: no harness (pass --harness <H> or set $OSS_CRS_TARGET_HARNESS)",
              file=sys.stderr)
        return 2

    if cmd == "start":
        return start(a.harness, seconds=a.seconds, jobs=a.jobs, max_len=a.max_len,
                     dict_path=a.dict_path, build_dir=a.build_dir, pov_out=a.pov_out, extra=extra)
    if cmd == "status":
        return status(a.harness, pov_out=a.pov_out)
    if cmd == "stop":
        return stop(a.harness, pov_out=a.pov_out)
    if cmd == "add-seeds":
        if not a.seeds_dir:
            print("crs-fuzz add-seeds: missing <dir> of candidate seeds", file=sys.stderr)
            return 2
        return add_seeds(a.harness, a.seeds_dir, build_dir=a.build_dir)
    return 2  # unreachable


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _LIFECYCLE:
        return _lifecycle_main(argv[0], argv[1:])

    # Back-compat flag interface: foreground bounded run, or --merge.
    ap = argparse.ArgumentParser(
        prog="crs-fuzz",
        description="Fuzz a libFuzzer harness. Foreground: --seconds N / --merge. "
                    "Long-running: crs-fuzz start|status|add-seeds|stop --harness H.")
    ap.add_argument("--harness", default=os.environ.get("OSS_CRS_TARGET_HARNESS"),
                    help="Harness binary name (default: $OSS_CRS_TARGET_HARNESS)")
    ap.add_argument("--corpus", type=Path, default=None,
                    help="Corpus dir to fuzz (default: canonical /artifacts/corpus/<harness>)")
    ap.add_argument("--merge", action="store_true",
                    help="Minimize the canonical corpus (libFuzzer -merge=1) instead of fuzzing")
    ap.add_argument("--pov-out", type=Path, default=POV_DEFAULT)
    ap.add_argument("--build-dir", default=None, help="Dir with asan harness (default: /out or libCRS)")
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--jobs", type=int, default=None, help="fork workers (default: from OSS_CRS_CPUSET)")
    ap.add_argument("--max-len", type=int, default=None,
                    help="libFuzzer -max_len (bytes): caps MUTATED input size. With a startup "
                         "corpus libFuzzer auto-sizes it to the largest seed when unset; setting "
                         "it BELOW a seed's size SILENTLY TRUNCATES that seed. Use it to enlarge.")
    ap.add_argument("--dict", dest="dict_path", type=Path, default=None,
                    help="libFuzzer -dict: keyword dictionary file to bias mutations")
    args, extra = ap.parse_known_args(argv)
    if not args.harness:
        print("crs-fuzz: no harness (pass --harness <H> or set $OSS_CRS_TARGET_HARNESS)",
              file=sys.stderr)
        return 2

    if args.merge:
        merge_corpus(args.harness, build_dir=args.build_dir)
        return 0

    corpus = args.corpus or corpus_dir(args.harness)
    return fuzz(args.harness, corpus=corpus, pov_out=args.pov_out,
                build_dir=args.build_dir, seconds=args.seconds, jobs=args.jobs,
                max_len=args.max_len, dict_path=args.dict_path, extra=extra)


if __name__ == "__main__":
    raise SystemExit(main())
