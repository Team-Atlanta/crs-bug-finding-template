#!/usr/bin/env python3
"""CRS entrypoint — Claude Code native subagents (one orchestrator, two roles).

A variant of crs/entrypoint/run_crs.py that runs a SINGLE `claude -p` orchestrator which
delegates to two Claude Code subagents (defined in `.claude/agents/`):

  * seed-gen — seeds + fuzzing + the coverage loop.
  * pov-gen  — codeql -> targeted PoV -> `libCRS run-pov` verify.

The orchestrator spawns both via the Task tool (it is told to issue the two Task
calls in one turn, so they run concurrently as Claude Code subagents) and keeps
them busy. The subagents inherit the shared CLAUDE.md + skills and contribute to
the same corpus / PoV directories. This uses Claude Code's OWN multi-agent
machinery instead of LangGraph — contrast with crs/entrypoint/run_crs_langgraph.py, which
orchestrates two separate `claude -p` processes from a LangGraph.

Swap the runner ENTRYPOINT to this file for the subagent strategy.
"""

import logging
import sys

from crs.src import runtime, prompts
from crs.src.claude_node import run_claude_p

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_crs_subagents")

def main() -> None:
    ctx = runtime.boot()

    # Install the Claude Code subagent definitions into .claude/agents/ so the
    # orchestrator can delegate to them by name via the Task tool.
    installed = prompts.install_agents(ctx.source_dir, ctx.harness)
    logger.info("Installed %d subagent(s): %s", len(installed), ", ".join(installed))

    # The orchestrator's role/system prompt (crs/roles/orchestrator.md).
    orchestrator_role = prompts.load_role(
        "orchestrator", harness=ctx.harness, source_dir=ctx.source_dir)

    runtime.install_sigterm_handler()

    task = (
        f"Coordinate the seed-gen, pov-gen, and pov-gen-cov subagents to find "
        f"vulnerabilities in `{ctx.target}` through harness `{ctx.harness}`, saving "
        f"verified crashing inputs to `{ctx.pov_dir}`. Read CLAUDE.md. Launch all "
        f"three subagents in parallel now and keep going until killed."
    )

    logger.info("Invoking subagent orchestrator for harness %s", ctx.harness)
    # PoVs + seeds auto-submit via the dirs registered in boot() — no manual submit.
    result = run_claude_p(
        task,
        cwd=ctx.source_dir,
        system_prompt=orchestrator_role,
        log_path=ctx.agent_work_dir / "orchestrator_stream.jsonl",
    )
    logger.info("orchestrator finished: is_error=%s turns=%s cost=%s",
                result.is_error, result.num_turns, result.total_cost_usd)


if __name__ == "__main__":
    main()
