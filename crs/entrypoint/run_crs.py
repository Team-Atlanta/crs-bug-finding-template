#!/usr/bin/env python3
"""CRS runner entrypoint (run phase).

Launched once per harness during the oss-crs run phase. Drives a single
Claude Code LangGraph node to find vulnerabilities and submit PoVs.

Flow:
  1. Fetch boot-time evidence (diff / seeds / bug-candidates) via libCRS.
  2. Download the build (-> /out) and source (-> /src) outputs.
  3. Configure Claude Code auth, write CLAUDE.md, and run a one-node LangGraph
     whose node is `claude -p` (see crs/claude_node.py).
  4. Submit each PoV the agent wrote to /artifacts/povs via libCRS submit
     (also on SIGTERM, so PoVs survive a run --timeout / --early-exit kill).

Mount points (oss-crs convention):
    /out        - Built fuzzers / harness binaries
    /src        - Project source (downloaded build output)
    /work       - Scratch working directory
    /artifacts  - Persisted output artifacts (HOST_ARTIFACT_DIR)
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from libCRS.base import DataType
from libCRS.cli.main import init_crs_utils

from crs.src.claude_node import ClaudeCodeNode, ClaudeState, configure_claude_env
from crs.src import prompts
from crs.src.runtime import register_submit_dirs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_crs")

# --- Mount points ----------------------------------------------------------
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


def harness_name() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    name = os.environ.get("OSS_CRS_TARGET_HARNESS")
    if not name:
        sys.exit("[run_crs] no harness name (argv[1] / OSS_CRS_TARGET_HARNESS)")
    return name


def _list_files(d: Path, *, non_empty_only: bool = False) -> list[Path]:
    if not d.exists():
        return []
    files = sorted(f for f in d.rglob("*") if f.is_file() and not f.name.startswith("."))
    if not non_empty_only:
        return files
    return [f for f in files if f.read_text(errors="replace").strip()]


def fetch_inputs(crs) -> tuple[list[Path], list[Path], list[Path]]:
    """One-shot fetch of boot-time evidence from the shared FETCH_DIR."""
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
        _list_files(DIFF_DIR, non_empty_only=True),
        _list_files(SEED_DIR),
        _list_files(BUG_CANDIDATE_DIR),
    )


def setup_source(crs) -> Path | None:
    """Download the `src` build output to /src and make it a git repo."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        capture_output=True,
    )
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


def build_graph(node: ClaudeCodeNode):
    """A single-node LangGraph: START -> claude -> END."""
    g = StateGraph(ClaudeState)
    g.add_node("claude", node)
    g.add_edge(START, "claude")
    g.add_edge("claude", END)
    return g.compile()


def find_bugs(harness: str, source_dir: Path,
              diffs: list[Path], seeds: list[Path], bug_candidates: list[Path]) -> bool:
    """Run the Claude Code node to find vulnerabilities; return True if PoVs produced."""
    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    target = os.environ.get("OSS_CRS_TARGET", source_dir.name)

    # Write the per-run CLAUDE.md into the source tree (Claude's cwd).
    claude_md = prompts.build_claude_md(
        language=LANGUAGE, sanitizer=SANITIZER,
        source_dir=source_dir, build_dir=OUT_DIR, work_dir=AGENT_WORK_DIR,
        harness=harness, pov_dir=POV_OUT,
        diffs=diffs, seeds=seeds, bug_candidates=bug_candidates,
    )
    (source_dir / "CLAUDE.md").write_text(claude_md)

    # Install the runner tools as Claude Code skills under source_dir/.claude/skills
    # so the agent discovers and invokes them (cwd is source_dir).
    installed = prompts.install_skills(source_dir, harness)
    logger.info("Installed %d skill(s): %s", len(installed), ", ".join(installed))

    # Role = targeted PoV generation (crs/roles/pov-gen.md) as the system prompt;
    # CLAUDE.md (auto-loaded from cwd, inherited by any Task sub-agents) carries the
    # shared environment/tools/rules. The user prompt carries only the per-run task.
    role_prompt = prompts.load_role("pov-gen", harness=harness, source_dir=source_dir)
    user_prompt = prompts.build_user_prompt(
        target=target, harness=harness, pov_dir=POV_OUT,
        diffs=diffs, seeds=seeds, bug_candidates=bug_candidates,
    )

    # Persist agent inputs for debugging.
    (AGENT_WORK_DIR / "agent_role.md").write_text(role_prompt)
    (AGENT_WORK_DIR / "agent_prompt.txt").write_text(user_prompt)
    (AGENT_WORK_DIR / "agent_claude_md.md").write_text(claude_md)

    node = ClaudeCodeNode(
        cwd=source_dir,
        system_prompt=role_prompt,
        log_path=AGENT_WORK_DIR / "claude_stream.jsonl",
    )
    app = build_graph(node)

    logger.info("Invoking Claude Code node for harness %s", harness)
    result = app.invoke({"messages": [HumanMessage(content=user_prompt)]})
    final = result["messages"][-1]
    meta = getattr(final, "additional_kwargs", {})
    logger.info(
        "Claude Code node finished (is_error=%s, returncode=%s, turns=%s, cost=%s)",
        meta.get("is_error"), meta.get("returncode"),
        meta.get("num_turns"), meta.get("total_cost_usd"),
    )

    povs = _list_files(POV_OUT)
    if povs:
        logger.info("Produced %d PoV(s): %s", len(povs), [p.name for p in povs])
        return True
    logger.info("No PoVs produced")
    return False


def main() -> None:
    harness = harness_name()
    logger.info("Starting CRS: harness=%s language=%s sanitizer=%s", harness, LANGUAGE, SANITIZER)
    crs = init_crs_utils()

    # 1. Fetch boot-time evidence (delta diff, seeds, bug-candidates).
    diffs, seeds, bug_candidates = fetch_inputs(crs)

    # 2. PoVs are submitted explicitly after the run (see below), not via a
    #    watchdog daemon.
    POV_OUT.mkdir(parents=True, exist_ok=True)

    # 3. Persist agent logs to the host via LOG_DIR symlink.
    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)
        AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)

    # 4. Download build (harness binaries) and source.
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

    # 5. Configure Claude Code auth (OAuth token > LiteLLM proxy).
    configure_claude_env(
        {"llm_api_url": LLM_API_URL, "llm_api_key": LLM_API_KEY},
        source_dir=source_dir,
    )

    # 6. Register the PoV + seed dirs for auto-submission. libCRS's batch submitter
    #    then submits anything the agent / its crs-fuzz writes to /artifacts/povs
    #    and /artifacts/corpus/<harness> — no manual submission needed.
    register_submit_dirs(crs, harness)

    # 7. Run the bug-finding node; PoVs and seeds auto-submit as they appear.
    find_bugs(harness, source_dir, diffs, seeds, bug_candidates)


if __name__ == "__main__":
    main()
