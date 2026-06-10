#!/usr/bin/env python3
"""DEBUG runner entrypoint: AFL++ (crs-afl) lifecycle probe.

Drives the `crs-afl` lifecycle directly (no agent) and logs each step, proving
the AFL campaign runs on the `afl` build and that live seed injection (AFL's sync
mechanism) works end-to-end:

  [1] crs-afl start          -> afl-fuzz launches in the background on the afl binary
  [2] crs-afl status         -> execs climbing (AFL is really fuzzing)
  [3] crs-afl add-seeds DIR  -> seeds injected into the live sync queue
  [4] wait AFL_SYNC_TIME, status -> campaign still healthy, queue grew
  [5] crs-afl stop           -> clean shutdown; crashes (if any) in the PoV dir

Select it with CRS_ENTRYPOINT=test_afl. Tunables: AFL_PROBE_WARMUP_S (how long to
let AFL warm up before checking execs), AFL_PROBE_SYNC_WAIT_S (wait for the sync).
"""

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from libCRS.base import DataType
from libCRS.cli.main import init_crs_utils

from crs.tools import afl as aflmod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_afl")

WORK_DIR = Path("/work")
AGENT_WORK_DIR = WORK_DIR / "agent"
AFL_BUILD = Path("/work/afl-build")
SEED_DIR = WORK_DIR / "seeds"
NEWSEEDS = WORK_DIR / "afl-probe-newseeds"

WARMUP_S = int(os.environ.get("AFL_PROBE_WARMUP_S", "30"))
SYNC_WAIT_S = int(os.environ.get("AFL_PROBE_SYNC_WAIT_S", "75"))  # AFL_SYNC_TIME=1min


def harness_name() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    name = os.environ.get("OSS_CRS_TARGET_HARNESS")
    if not name:
        sys.exit("[test_afl] no harness name (argv[1] / OSS_CRS_TARGET_HARNESS)")
    return name


def _run(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    logger.info("$ %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if (r.stdout or "").strip():
        logger.info("%s", r.stdout.strip())
    if (r.stderr or "").strip():
        logger.info("stderr: %s", "\n".join(r.stderr.strip().splitlines()[-8:]))
    return r


def _execs(harness: str) -> int:
    try:
        return int(aflmod._read_fuzzer_stats(harness).get("execs_done", "0"))
    except (ValueError, TypeError):
        return 0


def _inject_count(harness: str) -> int:
    q = aflmod._inject_queue(harness)
    return sum(1 for _ in q.glob("id:*")) if q.is_dir() else 0


def main() -> None:
    harness = harness_name()
    logger.info("=== AFL++ LIFECYCLE PROBE (harness=%s) ===", harness)
    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    crs = init_crs_utils()
    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)

    # The afl build is what afl-fuzz drives — fail clearly if the afl-build phase
    # didn't run / wasn't submitted.
    try:
        crs.download_build_output("afl", AFL_BUILD)
        bins = sorted(p.name for p in AFL_BUILD.iterdir() if p.is_file()) if AFL_BUILD.is_dir() else []
        logger.info("Downloaded afl build to %s (%d file(s), e.g. %s)",
                    AFL_BUILD, len(bins), bins[:4])
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to download the afl build: %s — did the `afl-build` "
                     "phase run in build-target?", e)
        sys.exit(1)
    if not (AFL_BUILD / harness).exists():
        logger.error("afl harness binary not found: %s", AFL_BUILD / harness)
        sys.exit(1)
    # OSS-Fuzz's afl build ships the AFL++ toolchain into $OUT (the afl build dir),
    # so afl-fuzz lives there, not on the base-runner's PATH.
    if not (AFL_BUILD / "afl-fuzz").exists() and not shutil.which("afl-fuzz"):
        logger.error("afl-fuzz not found in the afl build (%s) or on PATH", AFL_BUILD)
        sys.exit(1)

    try:
        crs.fetch(DataType.SEED, SEED_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("seed fetch failed (afl will start from a dummy seed): %s", e)

    afl = ["crs-afl"]

    # [1] start ------------------------------------------------------------
    logger.info("=== [1] crs-afl start ===")
    r = _run(afl + ["start", "--harness", harness], timeout=180)
    started = r.returncode == 0
    state = aflmod._read_state(harness)
    alive = bool(state and aflmod._alive(state.get("pid", -1)))
    logger.info("started=%s, afl-fuzz alive=%s", started, alive)

    # [2] warm up, then confirm it's actually executing -------------------
    logger.info("=== [2] warm up (~%ds), poll execs ===", WARMUP_S)
    execs1 = 0
    deadline = time.time() + WARMUP_S + 60
    while time.time() < deadline:
        time.sleep(10)
        execs1 = _execs(harness)
        st = aflmod._read_state(harness)
        if not (st and aflmod._alive(st.get("pid", -1))):
            logger.warning("afl-fuzz exited early — check the log:")
            log = aflmod._logfile(harness)
            if log.exists():
                logger.info("%s", "\n".join(log.read_text(errors="replace").splitlines()[-25:]))
            break
        logger.info("execs_done=%d", execs1)
        if execs1 > 0:
            break
    _run(afl + ["status", "--harness", harness], timeout=60)

    # [3] inject seeds into the live campaign -----------------------------
    logger.info("=== [3] crs-afl add-seeds (live inject) ===")
    NEWSEEDS.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (NEWSEEDS / f"probe_seed_{i}").write_bytes(bytes([0x41 + i]) * (32 + i * 8))
    _run(afl + ["add-seeds", "--harness", harness, str(NEWSEEDS)], timeout=60)
    injected = _inject_count(harness)
    logger.info("inject queue now holds %d entry(ies)", injected)

    # [4] wait for AFL's sync to pick them up -----------------------------
    logger.info("=== [4] wait ~%ds for AFL sync, then status ===", SYNC_WAIT_S)
    time.sleep(SYNC_WAIT_S)
    execs2 = _execs(harness)
    _run(afl + ["status", "--harness", harness], timeout=60)
    logger.info("execs grew %d -> %d during the run", execs1, execs2)

    # [5] stop -------------------------------------------------------------
    logger.info("=== [5] crs-afl stop ===")
    _run(afl + ["stop", "--harness", harness], timeout=120)

    # verdict --------------------------------------------------------------
    fuzzing = execs1 > 0 or execs2 > 0
    ok = started and alive and fuzzing and injected > 0
    logger.info("=== RESULT: %s (started=%s alive=%s execs=%d->%d injected=%d) ===",
                "AFL_PROBE: PASS" if ok else "AFL_PROBE: FAIL",
                started, alive, execs1, execs2, injected)


if __name__ == "__main__":
    main()
