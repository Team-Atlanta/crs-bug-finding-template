#!/usr/bin/env python3
"""DEBUG runner entrypoint: agent-drives-gdb-directly probe.

The agent runs `gdb` itself as a subprocess (one-shot `-batch`, or a `-x` script
it extends and re-runs to go deeper). This probe verifies that path end-to-end:
it boots a Claude Code node with a full shell + the `gdb` skill and has it prove
SOURCE-LEVEL debugging works (real file:line, named args, a deeper frame), then
report PASS/FAIL.

gdb is native-only: JVM crashes are read from the Jazzer stack trace, so this
probe SKIPs for JVM. Swap crs/entrypoint/run_crs.py back into the ENTRYPOINT (or set
CRS_ENTRYPOINT) to restore normal bug-finding.
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

from crs.src import prompts
from crs.src.claude_node import ClaudeCodeNode, ClaudeState, configure_claude_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_gdb")

SRC_DIR = Path("/src")
WORK_DIR = Path("/work")
AGENT_WORK_DIR = WORK_DIR / "agent"
PROBE_DIR = WORK_DIR / "gdb-probe"
SEED_DIR = PROBE_DIR / "seeds"

LANGUAGE = os.environ.get("FUZZING_LANGUAGE", "c")
# Optional: a target-internal function on the harness's call path. Breaking here
# (one level below the libFuzzer entry) demonstrates a source-level stack in a
# project library. Set per target via GDB_PROBE_BREAK_FN (e.g. `jv_parse_custom_flags`
# for jq). Unset → the probe just exercises the harness entry (already proves
# source-level debugging); an unknown symbol degrades gracefully.
DEEP_BREAK_FN = os.environ.get("GDB_PROBE_BREAK_FN", "").strip()
LLM_API_URL = os.environ.get("OSS_CRS_LLM_API_URL", "")
LLM_API_KEY = (
    open(os.environ["OSS_CRS_LLM_API_KEY_FILE"]).read().strip()
    if os.environ.get("OSS_CRS_LLM_API_KEY_FILE")
    else os.environ.get("OSS_CRS_LLM_API_KEY", "")
)

# Full shell: running gdb subprocesses and writing `-x` command scripts needs
# ordinary process + file control, so this probe is not boxed to a single tool.
# The prompt keeps it on task.
ALLOWED_TOOLS = None

SYSTEM_PROMPT = (
    "You are a gdb-integration probe inside a CRS runner container. You have a "
    "normal shell and the `gdb` skill (read it). Your ONLY task is to prove you "
    "can drive gdb DIRECTLY — as a subprocess (`gdb -batch` / a `-x` script, "
    "re-running to go deeper) — to do source-level debugging of the target's "
    "debug build. Do not find bugs or modify the target."
)


def harness_name() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    name = os.environ.get("OSS_CRS_TARGET_HARNESS")
    if not name:
        sys.exit("[test_gdb] no harness name (argv[1] / OSS_CRS_TARGET_HARNESS)")
    return name


def build_user_prompt(harness: str, input_path: Path) -> str:
    h = harness
    deep = DEEP_BREAK_FN
    lines = [
        f"The target is a native ({LANGUAGE}) libFuzzer harness `{h}`. An input it "
        f"can process is at `{input_path}`. Source is at `{SRC_DIR}`.",
        "",
        "## Task: prove you can drive gdb directly, at source level",
        "",
        "Use the **gdb** skill for the exact setup (debug build download, "
        "ASAN_OPTIONS, follow-fork, source path). Run gdb as a subprocess "
        "(`gdb -batch -ex ...` or a `-x` script).",
        f"1. Break at `LLVMFuzzerTestOneInput` and `run` on `{h}` with the input.",
        "2. The stop must show a real **file:line** (e.g. "
        f"   `...{h}.c:NN`), not a bare address.",
        "3. `bt`, and read the entry's NAMED args (expect `data` and `size`) — "
        "   `info args` and `p size`. This proves DWARF/source-level inspection.",
    ]
    if deep:
        lines += [
            f"4. Extend your script: break deeper, in target library function `{deep}` "
            "   (if gdb says it's undefined here, say so and skip — don't invent one), "
            "   `continue` to it, `bt` (a source-level stack with the harness as a "
            "   caller frame), and read a named local/arg there. Re-run to reach it.",
        ]
    lines += [
        "",
        "## Report",
        "Quote the concrete evidence (the stop `file:line`, a named arg value like "
        + (f"`size`, and the `{deep}` source-level frame" if deep else "`size`")
        + "). Then end with a single line of exactly this form:",
        "  `GDB_DIRECT: PASS — <evidence: stop file:line, a named arg value>`, or",
        "  `GDB_DIRECT: FAIL — <reason>`",
        "",
        "Begin now.",
    ]
    return "\n".join(lines)


def prepare_input(crs) -> Path:
    """Return an input the harness can run: a fetched seed, else a synthetic blob."""
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fetched = crs.fetch(DataType.SEED, SEED_DIR)
        if fetched:
            logger.info("Fetched %d seed file(s) into %s", len(fetched), SEED_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("seed fetch failed: %s", e)

    for f in sorted(SEED_DIR.rglob("*")):
        if f.is_file() and not f.name.startswith(".") and f.stat().st_size > 0:
            logger.info("Using seed input: %s (%d bytes)", f, f.stat().st_size)
            return f.resolve()

    synthetic = PROBE_DIR / "synthetic_input.bin"
    synthetic.write_bytes(b"AFC-GDB-PROBE\x00" + bytes(range(64)))
    logger.info("No seed found; using synthetic input: %s (%d bytes)",
                synthetic, synthetic.stat().st_size)
    return synthetic.resolve()


def setup_source(crs) -> Path | None:
    """Download /src so gdb can show source-level frames (comp_dir matches /src)."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        capture_output=True,
    )
    try:
        crs.download_build_output("src", SRC_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to download /src (frames will lack source): %s", e)
        return None
    return SRC_DIR.resolve()


def build_graph(node: ClaudeCodeNode):
    g = StateGraph(ClaudeState)
    g.add_node("gdb_probe", node)
    g.add_edge(START, "gdb_probe")
    g.add_edge("gdb_probe", END)
    return g.compile()


def main() -> None:
    logger.info("=== GDB-DIRECT DEBUG ENTRYPOINT (language=%s, deep_break=%s) ===",
                LANGUAGE, DEEP_BREAK_FN or "(none)")

    if LANGUAGE.strip().lower() in ("jvm", "java", "kotlin", "scala"):
        logger.warning(
            "RESULT: SKIP — gdb is native-only; JVM crashes are read from the "
            "Jazzer stack trace.")
        return

    harness = harness_name()
    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    crs = init_crs_utils()

    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)

    source_dir = setup_source(crs) or WORK_DIR
    logger.info("Source directory: %s", source_dir)
    input_path = prepare_input(crs)

    configure_claude_env(
        {"llm_api_url": LLM_API_URL, "llm_api_key": LLM_API_KEY},
        source_dir=source_dir,
    )
    # Install the runner-tool skills (incl. `gdb`) so the agent can read it.
    try:
        prompts.install_skills(source_dir, harness)
    except Exception as e:  # noqa: BLE001
        logger.warning("install_skills failed: %s", e)

    user_prompt = build_user_prompt(harness, input_path)
    (AGENT_WORK_DIR / "gdb_probe_prompt.txt").write_text(user_prompt)
    (AGENT_WORK_DIR / "gdb_probe_system.txt").write_text(SYSTEM_PROMPT)

    node = ClaudeCodeNode(
        cwd=source_dir,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        skip_permissions=True,  # full shell: gdb subprocesses + file control
        log_path=AGENT_WORK_DIR / "gdb_probe_stream.jsonl",
    )
    app = build_graph(node)

    logger.info("Invoking gdb-direct probe agent for harness %s", harness)
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
    if "GDB_DIRECT: PASS" in verdict:
        logger.info("RESULT: gdb-direct PASS")
    elif "GDB_DIRECT: FAIL" in verdict:
        logger.warning("RESULT: gdb-direct FAIL")
    else:
        logger.warning("RESULT: inconclusive (no GDB_DIRECT verdict line)")


if __name__ == "__main__":
    main()
