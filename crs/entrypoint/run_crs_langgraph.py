#!/usr/bin/env python3
"""CRS entrypoint — coverage-guided LangGraph loop.

A LangGraph state machine that drives the bug-finding loop:

  warm-up (start the fuzzers, let them warm up ~1 min)
    -> measure   (crs-coverage)
    -> explore   (code-explorer agent -> JSON bug candidates)
    -> dispatch  (route each candidate: COVERED -> pov-gen-cov subagent,
                  UNCOVERED -> seed-gen subagent; keep a markdown ledger)
    -> back to measure   (until the budget elapses, or SIGTERM)

The MACRO control flow is the LangGraph; the per-candidate routing and the actual
seed/PoV work are delegated to Claude Code subagents (pov-gen-cov, seed-gen) by
the dispatch node — so the "covered?" decision (steps 4/5) is an edge made by the
coverage skill inside a thin dispatcher prompt, not in Python. Reuses boot()'s
shared CLAUDE.md + skills + auto-submit, the existing subagent roles, and the role
prompts in crs/roles/ (explorer, dispatcher).

Tunables (env): CRS_GRAPH_WARMUP (s, default 60), CRS_GRAPH_ROUND_TIMEOUT (per
claude node, s, default 1800), CRS_GRAPH_BUDGET (whole loop, s, 0 = until killed).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from crs.src import prompts, runtime
from crs.src.claude_node import run_claude_p

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_crs_langgraph")

WARMUP_SECONDS = int(os.environ.get("CRS_GRAPH_WARMUP", "60"))
# Per-claude-node wall-clock cap (explore, dispatch). 0 => no cap (uses AGENT_TIMEOUT).
ROUND_TIMEOUT = int(os.environ.get("CRS_GRAPH_ROUND_TIMEOUT", "1800"))
# Whole-loop budget in seconds. 0 => run until SIGTERM (oss-crs run --timeout).
BUDGET = int(os.environ.get("CRS_GRAPH_BUDGET", "0"))
# Stop after this many explore/dispatch rounds. 0 => unlimited (budget/SIGTERM only).
MAX_ROUNDS = int(os.environ.get("CRS_GRAPH_MAX_ROUNDS", "0"))

CAND_DIR = runtime.WORK_DIR / "candidates"
LEDGER = runtime.WORK_DIR / "ledger.md"


class GraphState(TypedDict):
    round: int
    n_candidates: int


# Graph topology in ONE place (so the draw_graph entrypoint renders the real wiring).
NODE_NAMES = ("warm_up", "measure", "explore", "dispatch")


def build_graph(nodes: dict, should_continue):
    """Wire the coverage-guided loop and compile it.

    `nodes` maps each NODE_NAMES entry to its callable; `should_continue` returns
    "continue" (loop back to measure) or "stop" (END). main() passes the real
    closures; draw_graph passes stubs (the diagram depends only on names + edges).
    """
    g = StateGraph(GraphState)
    for name in NODE_NAMES:
        g.add_node(name, nodes[name])
    g.add_edge(START, "warm_up")
    g.add_edge("warm_up", "measure")
    g.add_edge("measure", "explore")
    g.add_edge("explore", "dispatch")
    g.add_conditional_edges("dispatch", should_continue,
                            {"continue": "measure", "stop": END})
    return g.compile()


# --- helpers ----------------------------------------------------------------
def _read_candidates(path: Path) -> list:
    """Parse the explorer's JSON candidate file (a list, or {"candidates": [...]})."""
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - missing / malformed => no candidates
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("candidates", [])
    return []


def _start_fuzzers(harness: str) -> None:
    """Start the long-running fuzzers up front (idempotent — a later `start` by the
    seed-gen subagent just sees they're running and no-ops)."""
    for tool in ("crs-fuzz", "crs-afl"):
        if not shutil.which(tool):
            continue
        try:
            r = subprocess.run([tool, "start", "--harness", harness],
                               capture_output=True, text=True, timeout=180)
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            logger.info("%s start: rc=%s %s", tool, r.returncode, tail[-1] if tail else "")
        except Exception as e:  # noqa: BLE001
            logger.warning("%s start failed: %s", tool, e)


def _explore_task(rnd: int, cand_file: Path, harness: str) -> str:
    return (
        f"Round {rnd}: survey the target for vulnerability candidates and write a "
        f"JSON array to `{cand_file}` (create its parent dir). Each item: "
        f'{{"file","function","line","bug_class","rationale"}}. Use crs-codeql, the '
        f"latest crs-coverage report, and your own reading of harness `{harness}` "
        f"and the code it reaches. Skip candidates already resolved in the ledger "
        f"`{LEDGER}` (if it exists). Your deliverable is that JSON file — keep it to "
        f"your strongest candidates."
    )


def _dispatch_task(rnd: int, cand_file: Path) -> str:
    return (
        f"Round {rnd}: the bug candidates are in `{cand_file}`; the ledger is "
        f"`{LEDGER}` (create it if missing). Route each candidate per your role — "
        f"decide covered-or-not with crs-coverage, spawn `pov-gen-cov` (covered) or "
        f"`seed-gen` (uncovered) scoped to that candidate, and record the outcome in "
        f"the ledger. Handle all candidates, then stop."
    )


# --- entrypoint -------------------------------------------------------------
def main() -> None:
    ctx = runtime.boot()
    runtime.install_sigterm_handler()
    # Make the pov-gen-cov / seed-gen subagents available to the dispatch node.
    installed = prompts.install_agents(ctx.source_dir, ctx.harness)
    logger.info("Installed %d subagent(s): %s", len(installed), ", ".join(installed))
    CAND_DIR.mkdir(parents=True, exist_ok=True)

    explorer_role = prompts.load_role("explorer", harness=ctx.harness, source_dir=ctx.source_dir)
    dispatcher_role = prompts.load_role("dispatcher", harness=ctx.harness, source_dir=ctx.source_dir)
    deadline = time.time() + BUDGET if BUDGET else None

    def warm_up(state: GraphState) -> dict:
        logger.info("=== warm-up: starting fuzzer(s), %ds to warm up ===", WARMUP_SECONDS)
        _start_fuzzers(ctx.harness)
        time.sleep(WARMUP_SECONDS)
        return {}

    def measure(state: GraphState) -> dict:
        logger.info("=== round %d: measuring coverage ===", state["round"])
        try:
            subprocess.run(["crs-coverage", "--harness", ctx.harness],
                           cwd=str(ctx.source_dir), capture_output=True, text=True,
                           timeout=ROUND_TIMEOUT or 1200)
        except Exception as e:  # noqa: BLE001
            logger.warning("crs-coverage failed: %s", e)
        return {}

    def explore(state: GraphState) -> dict:
        rnd = state["round"]
        cand_file = CAND_DIR / f"round_{rnd}.json"
        logger.info("=== round %d: explore -> %s ===", rnd, cand_file)
        res = run_claude_p(
            _explore_task(rnd, cand_file, ctx.harness),
            cwd=ctx.source_dir, system_prompt=explorer_role, timeout=ROUND_TIMEOUT,
            log_path=ctx.agent_work_dir / f"explore_{rnd}.jsonl")
        cands = _read_candidates(cand_file)
        logger.info("round %d explore done (is_error=%s turns=%s cost=%s): %d candidate(s)",
                    rnd, res.is_error, res.num_turns, res.total_cost_usd, len(cands))
        return {"n_candidates": len(cands)}

    def dispatch(state: GraphState) -> dict:
        rnd = state["round"]
        if state["n_candidates"] == 0:
            logger.info("round %d: no candidates; skipping dispatch", rnd)
            return {"round": rnd + 1}
        cand_file = CAND_DIR / f"round_{rnd}.json"
        logger.info("=== round %d: dispatch %d candidate(s) ===", rnd, state["n_candidates"])
        res = run_claude_p(
            _dispatch_task(rnd, cand_file),
            cwd=ctx.source_dir, system_prompt=dispatcher_role, timeout=ROUND_TIMEOUT,
            log_path=ctx.agent_work_dir / f"dispatch_{rnd}.jsonl")
        logger.info("round %d dispatch done (is_error=%s turns=%s cost=%s)",
                    rnd, res.is_error, res.num_turns, res.total_cost_usd)
        return {"round": rnd + 1}

    def should_continue(state: GraphState) -> str:
        # dispatch increments `round` at the end, so after N rounds round == N.
        if MAX_ROUNDS and state["round"] >= MAX_ROUNDS:
            logger.info("max rounds (%d) reached; ending the loop", MAX_ROUNDS)
            return "stop"
        if deadline and time.time() >= deadline:
            logger.info("budget (%ds) reached; ending the loop", BUDGET)
            return "stop"
        return "continue"

    app = build_graph(
        {"warm_up": warm_up, "measure": measure, "explore": explore, "dispatch": dispatch},
        should_continue)

    logger.info("Coverage-guided LangGraph loop for harness %s "
                "(warmup=%ds, round_timeout=%ds, budget=%s)",
                ctx.harness, WARMUP_SECONDS, ROUND_TIMEOUT, BUDGET or "until-killed")
    # PoVs + seeds auto-submit via the dirs registered in boot(); recursion_limit is
    # raised far above the loop count so the long-running loop isn't cut short.
    app.invoke({"round": 0, "n_candidates": 0}, config={"recursion_limit": 1_000_000})


if __name__ == "__main__":
    main()
