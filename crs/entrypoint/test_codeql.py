#!/usr/bin/env python3
"""DEBUG runner entrypoint: CodeQL query-authoring liveness probe.

A stripped-down replacement for crs/entrypoint/run_crs.py used to debug the agent<->CodeQL
integration in isolation. It does NOT find bugs or submit PoVs. Instead it:

  1. Downloads the prebuilt CodeQL database (the `codeql` build output) to
     /work/codeql/db, so the agent has a real index to query.
  2. Configures Claude Code auth.
  3. Runs ONE Claude Code node whose ONLY tools are `Write` (to author a .ql
     file) and `crs-codeql` (to run it). Permissions are enforced (not skipped),
     so every other tool — Read, Edit, arbitrary Bash, WebFetch — is auto-denied.
     The agent is heavily prompted to write its own query and prove it runs, then
     report PASS/FAIL.

Swap crs/entrypoint/run_crs.py back into the runner Dockerfile ENTRYPOINT to restore normal
bug-finding behaviour.
"""

import logging
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from libCRS.cli.main import init_crs_utils

from crs.src.claude_node import ClaudeCodeNode, ClaudeState, configure_claude_env
from crs.tools.common import CODEQL_DB_LOCAL, CODEQL_DB_REMOTE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_codeql")

WORK_DIR = Path("/work")
AGENT_WORK_DIR = WORK_DIR / "agent"

LANGUAGE = os.environ.get("FUZZING_LANGUAGE", "c")
LLM_API_URL = os.environ.get("OSS_CRS_LLM_API_URL", "")
LLM_API_KEY = (
    open(os.environ["OSS_CRS_LLM_API_KEY_FILE"]).read().strip()
    if os.environ.get("OSS_CRS_LLM_API_KEY_FILE")
    else os.environ.get("OSS_CRS_LLM_API_KEY", "")
)

# The ONLY tools the agent may use: Write (to author a .ql file) and the
# crs-codeql shell command (to run it). Permissions are enforced
# (skip_permissions=False), so every other tool is auto-denied — the agent can do
# nothing except write a query and run it.
ALLOWED_TOOLS = ["Bash(crs-codeql:*)", "Write"]

SYSTEM_PROMPT = (
    "You are a CodeQL probe running inside a CRS runner container. You have exactly "
    "TWO tools available: `Write` (to create a .ql query file) and the shell command "
    "`crs-codeql` (`info` / `run <query.ql>` / `analyze`), which queries a prebuilt "
    "CodeQL database of the target. Any other tool is blocked. Your job is to prove "
    "the agent can author its OWN CodeQL query and get real results back. Do not try "
    "to read source files, edit code, or find bugs — you cannot, and it is not your "
    "task."
)


def build_user_prompt(language: str) -> str:
    return "\n".join([
        f"The target is a **{language}** project indexed into a CodeQL database.",
        "",
        "## Your task: author a CodeQL query and prove it returns real results",
        "",
        "1. Run `crs-codeql info` to confirm the database is present and learn its "
        "   language and the `import cpp` to use.",
        f"2. Use `Write` to create a query file at `{AGENT_WORK_DIR}/probe.ql`. It "
        "   MUST start with `import cpp` and select something concrete that will "
        "   match real code — for example, find all calls to a well-known function "
        "   (e.g. a memory/string function like memcpy) and "
        "   select the call plus a message.",
        f"3. Run it: `crs-codeql run {AGENT_WORK_DIR}/probe.ql`. Confirm it compiles "
        "   and prints a results table with at least one row. If it fails to "
        "   compile, read the error, fix the query, and run it again.",
        "4. (Optional) Run `crs-codeql analyze --suite security-extended` and note "
        "   how many findings it reports.",
        "",
        "## Report",
        "",
        "Quote the concrete evidence you saw (the query you wrote, and a few real "
        "result rows / the result count). Then end with a single line of exactly "
        "this form:",
        "  `CODEQL_QUERIES: PASS — <evidence: the query + how many rows it returned>`, or",
        "  `CODEQL_QUERIES: FAIL — <reason>`",
        "",
        "Use the two tools now; they are the only ones available to you.",
    ])


def download_db(crs) -> bool:
    """Download the `codeql` build output to /work/codeql/db. Returns success."""
    try:
        crs.download_build_output(CODEQL_DB_REMOTE, CODEQL_DB_LOCAL)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to download CodeQL db (%s): %s", CODEQL_DB_REMOTE, e)
        return False
    ok = (CODEQL_DB_LOCAL / "codeql-database.yml").exists()
    if not ok:
        logger.error("Downloaded db but no codeql-database.yml at %s", CODEQL_DB_LOCAL)
    return ok


def build_graph(node: ClaudeCodeNode):
    g = StateGraph(ClaudeState)
    g.add_node("codeql_probe", node)
    g.add_edge(START, "codeql_probe")
    g.add_edge("codeql_probe", END)
    return g.compile()


def main() -> None:
    logger.info("=== CODEQL QUERY DEBUG ENTRYPOINT (language=%s) ===", LANGUAGE)
    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    crs = init_crs_utils()

    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)

    if not download_db(crs):
        logger.error("RESULT: CodeQL db unavailable — did the codeql-build phase run?")
        sys.exit(1)
    logger.info("CodeQL database ready at %s", CODEQL_DB_LOCAL)

    configure_claude_env(
        {"llm_api_url": LLM_API_URL, "llm_api_key": LLM_API_KEY},
        source_dir=AGENT_WORK_DIR,
    )

    user_prompt = build_user_prompt(LANGUAGE)
    (AGENT_WORK_DIR / "codeql_probe_prompt.txt").write_text(user_prompt)
    (AGENT_WORK_DIR / "codeql_probe_system.txt").write_text(SYSTEM_PROMPT)

    node = ClaudeCodeNode(
        cwd=AGENT_WORK_DIR,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        skip_permissions=False,  # enforce the allowlist so only Write + crs-codeql run
        log_path=AGENT_WORK_DIR / "codeql_probe_stream.jsonl",
    )
    app = build_graph(node)

    logger.info("Invoking CodeQL-only probe agent (allowed tools: %s)", ALLOWED_TOOLS)
    result = app.invoke({"messages": [HumanMessage(content=user_prompt)]})
    final = result["messages"][-1]
    meta = getattr(final, "additional_kwargs", {})
    logger.info(
        "Probe finished (is_error=%s, returncode=%s, turns=%s, cost=%s)",
        meta.get("is_error"), meta.get("returncode"),
        meta.get("num_turns"), meta.get("total_cost_usd"),
    )

    verdict = final.content or ""
    logger.info("=== AGENT VERDICT ===\n%s", verdict)
    if "CODEQL_QUERIES: PASS" in verdict:
        logger.info("RESULT: CodeQL queries PASS")
    elif "CODEQL_QUERIES: FAIL" in verdict:
        logger.warning("RESULT: CodeQL queries FAIL")
    else:
        logger.warning("RESULT: inconclusive (no CODEQL_QUERIES verdict line)")


if __name__ == "__main__":
    main()
