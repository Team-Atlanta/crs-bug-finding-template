"""Shared run-phase plumbing for the CRS entrypoints.

`crs/entrypoint/run_crs.py` is the self-contained base example. The multi-agent variants
(`crs/entrypoint/run_crs_langgraph.py`, `crs/entrypoint/run_crs_subagents.py`) reuse this module so
they only have to express their *orchestration*, not re-derive the boot sequence.

`boot()` performs the common prologue — fetch boot-time evidence, download the
build + source, configure Claude Code auth, write the shared CLAUDE.md, install
the runner-tool skills, and register the PoV/seed dirs for auto-submission — and
returns a `RunContext`. PoVs and seeds are submitted automatically by libCRS's
batch submitter (register-submit-dir) as the fuzzer/agent write them; entrypoints
do not submit by hand.

Mount points (oss-crs convention): /out build, /src source, /work scratch,
/artifacts persisted outputs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from libCRS.base import DataType
from libCRS.cli.main import init_crs_utils

from crs.src.claude_node import configure_claude_env
from crs.src import prompts

logger = logging.getLogger("crs.runtime")

# --- Mount points -----------------------------------------------------------
OUT_DIR = Path("/out")
SRC_DIR = Path("/src")
WORK_DIR = Path("/work")
ARTIFACT_DIR = Path("/artifacts")

POV_OUT = ARTIFACT_DIR / "povs"
DIFF_DIR = WORK_DIR / "diffs"
SEED_DIR = WORK_DIR / "seeds"
BUG_CANDIDATE_DIR = WORK_DIR / "bug-candidates"
AGENT_WORK_DIR = WORK_DIR / "agent"

LANGUAGE = os.environ.get("FUZZING_LANGUAGE", "c")
SANITIZER = os.environ.get("SANITIZER", "address")
LLM_API_URL = os.environ.get("OSS_CRS_LLM_API_URL", "")
LLM_API_KEY = (
    open(os.environ["OSS_CRS_LLM_API_KEY_FILE"]).read().strip()
    if os.environ.get("OSS_CRS_LLM_API_KEY_FILE")
    else os.environ.get("OSS_CRS_LLM_API_KEY", "")
)


@dataclass
class RunContext:
    """Everything an entrypoint needs after boot()."""

    crs: object
    harness: str
    source_dir: Path
    pov_dir: Path
    agent_work_dir: Path
    diffs: list = field(default_factory=list)
    seeds: list = field(default_factory=list)
    bug_candidates: list = field(default_factory=list)
    target: str = ""
    language: str = LANGUAGE


def harness_name() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    name = os.environ.get("OSS_CRS_TARGET_HARNESS")
    if not name:
        sys.exit("[crs.runtime] no harness name (argv[1] / OSS_CRS_TARGET_HARNESS)")
    return name


def list_files(d: Path, *, non_empty_only: bool = False) -> list[Path]:
    if not d.exists():
        return []
    files = sorted(f for f in d.rglob("*") if f.is_file() and not f.name.startswith("."))
    if not non_empty_only:
        return files
    return [f for f in files if f.read_text(errors="replace").strip()]


def fetch_inputs(crs) -> tuple[list[Path], list[Path], list[Path]]:
    """One-shot fetch of boot-time evidence (diff / seeds / bug-candidates)."""
    for dtype, dst, label in (
        (DataType.DIFF, DIFF_DIR, "diff"),
        (DataType.SEED, SEED_DIR, "seed"),
        (DataType.BUG_CANDIDATE, BUG_CANDIDATE_DIR, "bug-candidate"),
    ):
        try:
            fetched = crs.fetch(dtype, dst)
            if fetched:
                logger.info("Fetched %d %s file(s) into %s", len(fetched), label, dst)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s fetch failed: %s", label, e)
    return (
        list_files(DIFF_DIR, non_empty_only=True),
        list_files(SEED_DIR),
        list_files(BUG_CANDIDATE_DIR),
    )


def setup_source(crs) -> Path | None:
    """Download the `src` build output to /src and make it a git repo."""
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"],
                   capture_output=True)
    try:
        crs.download_build_output("src", SRC_DIR)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to download /src build output: %s", e)
        return None

    project_dir = SRC_DIR.resolve()
    if not (project_dir / ".git").exists():
        logger.info("Initializing git repo in %s", project_dir)
        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, timeout=60)
        subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True, timeout=120)
        subprocess.run(
            ["git", "-c", "user.name=crs", "-c", "user.email=crs@local",
             "commit", "-m", "initial source"],
            cwd=project_dir, capture_output=True, timeout=120,
        )
    return project_dir


def register_submit_dirs(crs, harness: str) -> None:
    """Register the PoV and seed dirs for auto-submission.

    libCRS's batch submitter then watches them and submits anything the fuzzer or
    agent writes — crashes to /artifacts/povs and corpus files to
    /artifacts/corpus/<harness> — with no per-file submit calls anywhere.

    libCRS's `register_submit_dir` blocks forever (it runs the watch loop inline),
    so each watcher runs in a daemon thread; otherwise boot() would never return.
    """
    from crs.tools.common import corpus_dir  # local import: avoids an import cycle
    for dtype, path, label in (
        (DataType.POV, POV_OUT, "pov"),
        (DataType.SEED, corpus_dir(harness), "seed"),
    ):
        path.mkdir(parents=True, exist_ok=True)
        threading.Thread(
            target=crs.register_submit_dir, args=(dtype, path),
            name=f"submit-{label}", daemon=True,
        ).start()
        logger.info("Auto-submit watcher started: %s -> %s", label, path)


def _start_chmod_daemon(path: Path, interval: int = 20) -> None:
    """Daemon-thread loop: keep `path` world-rwX so root-written agent transcripts
    stay host-readable. Claude Code creates session files 0600 regardless of umask."""
    import time

    def _loop():
        while True:
            if path.exists():
                for p in path.rglob("*"):
                    try:
                        p.chmod(0o777 if p.is_dir() else 0o666)
                    except OSError:
                        pass
                try:
                    path.chmod(0o777)
                    path.parent.chmod(0o777)
                    AGENT_WORK_DIR.chmod(0o777)
                except OSError:
                    pass
            time.sleep(interval)

    threading.Thread(target=_loop, name="chmod-claude", daemon=True).start()


def install_sigterm_handler() -> None:
    """SIGTERM (run --timeout / --early-exit) -> SystemExit so `finally` submits."""
    def _raise(signum, _frame):
        raise SystemExit(f"terminated by signal {signum}")
    signal.signal(signal.SIGTERM, _raise)


def boot() -> RunContext:
    """Common prologue: evidence + build + source + auth + CLAUDE.md + skills."""
    harness = harness_name()
    logger.info("Starting CRS: harness=%s language=%s sanitizer=%s",
                harness, LANGUAGE, SANITIZER)
    # Container runs as root; umask 0 makes every file/dir it (and the claude
    # subprocess) writes world-rw, so agent logs/PoVs/seeds in OSS_CRS_LOG_DIR are
    # host-readable without sudo. Survives timeout/kill (unlike a chmod at exit).
    os.umask(0o000)
    crs = init_crs_utils()

    diffs, seeds, bug_candidates = fetch_inputs(crs)
    POV_OUT.mkdir(parents=True, exist_ok=True)
    register_submit_dirs(crs, harness)  # auto-submit PoVs + seeds from here on

    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)
        AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)

    # Point Claude Code's config at the (symlinked-to-OSS_CRS_LOG_DIR) agent dir so
    # its session + per-subagent transcripts (projects/<h>/<sid>/subagents/*.jsonl)
    # persist live to the log dir instead of dying with the container.
    claude_cfg = AGENT_WORK_DIR / "claude"
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_cfg)
    # Claude Code writes its transcripts mode 0600 (ignores umask), and we run as
    # root — so the host user can't read them. Periodically relax the tree so the
    # subagent logs stay host-readable even if the run is killed on timeout.
    _start_chmod_daemon(claude_cfg)

    try:
        crs.download_build_output("build", OUT_DIR)
        logger.info("Downloaded build outputs to %s", OUT_DIR)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to download build outputs: %s", e)
        sys.exit(1)

    source_dir = setup_source(crs)
    if source_dir is None:
        sys.exit(1)
    logger.info("Source directory: %s", source_dir)

    configure_claude_env({"llm_api_url": LLM_API_URL, "llm_api_key": LLM_API_KEY},
                         source_dir=source_dir)

    # Shared standing instructions: one CLAUDE.md (auto-loaded from cwd, inherited
    # by any subagents) + the runner-tool skills. Identical for every entrypoint.
    target = os.environ.get("OSS_CRS_TARGET", source_dir.name)
    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    claude_md = prompts.build_claude_md(
        language=LANGUAGE, sanitizer=SANITIZER, source_dir=source_dir,
        build_dir=OUT_DIR, work_dir=AGENT_WORK_DIR, harness=harness, pov_dir=POV_OUT,
        diffs=diffs, seeds=seeds, bug_candidates=bug_candidates,
    )
    (source_dir / "CLAUDE.md").write_text(claude_md)
    (AGENT_WORK_DIR / "agent_claude_md.md").write_text(claude_md)
    installed = prompts.install_skills(source_dir, harness)
    logger.info("Installed %d skill(s): %s", len(installed), ", ".join(installed))

    return RunContext(
        crs=crs, harness=harness, source_dir=source_dir, pov_dir=POV_OUT,
        agent_work_dir=AGENT_WORK_DIR, diffs=diffs, seeds=seeds,
        bug_candidates=bug_candidates, target=target, language=LANGUAGE,
    )
