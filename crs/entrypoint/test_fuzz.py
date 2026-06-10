#!/usr/bin/env python3
"""DEBUG runner entrypoint: libFuzzer (crs-fuzz) interaction probe.

A stripped-down replacement for crs/entrypoint/run_crs.py used to exercise the agent<->fuzzer
integration in isolation. It does NOT do normal bug finding. Instead it drives ONE
Claude Code node (permissions enforced) through four fuzzing scenarios and reports
PASS/FAIL:

  1. **Baseline** — `crs-fuzz` on the harness with no extra inputs.
  2. **Agent-generated dictionary** — the agent reads the harness/format, writes a
     libFuzzer `.dict`, and fuzzes with `--dict`.
  3. **Agent-generated corpus + custom --max-len** — the agent writes seed inputs
     (incl. one LARGER than libFuzzer's default 4096-byte cap), then fuzzes with
     `--corpus` and a `--max-len` sized to its largest seed so it isn't dropped.
  4. **Long-running lifecycle** — `crs-fuzz start`/`status`/`add-seeds`/`stop`:
     start ONE long-running fuzzer on the shared corpus, then `add-seeds` to
     coverage-merge new seeds into the live corpus (the fuzzer auto-reloads them,
     no restart), confirming the async lifecycle works end-to-end.

Drives the C libFuzzer harness. Swap crs/entrypoint/run_crs.py back into the runner
Dockerfile ENTRYPOINT to restore normal bug-finding behaviour.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from libCRS.cli.main import init_crs_utils

from crs.src.claude_node import ClaudeCodeNode, ClaudeState, configure_claude_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_fuzz")

OUT_DIR = Path("/out")
SRC_DIR = Path("/src")
WORK_DIR = Path("/work")
AGENT_WORK_DIR = WORK_DIR / "agent"
PROBE_DIR = WORK_DIR / "fuzz-probe"

LANGUAGE = os.environ.get("FUZZING_LANGUAGE", "c")
# Per-scenario fuzz duration (seconds). Short by default — this probe exercises
# the interaction, it is not a real fuzzing campaign. Kept small so the corpus
# (and thus crs-fuzz's post-run per-seed libCRS submit) stays quick.
SECONDS = int(os.environ.get("FUZZ_PROBE_SECONDS", "12"))
# libFuzzer's default max input length; the scenario-3 seed must exceed this so
# that choosing --max-len actually matters.
DEFAULT_MAX_LEN = 4096
LLM_API_URL = os.environ.get("OSS_CRS_LLM_API_URL", "")
LLM_API_KEY = (
    open(os.environ["OSS_CRS_LLM_API_KEY_FILE"]).read().strip()
    if os.environ.get("OSS_CRS_LLM_API_KEY_FILE")
    else os.environ.get("OSS_CRS_LLM_API_KEY", "")
)

# Tools the agent may use (permissions enforced — everything else is auto-denied).
# Unlike the gdb probe (which only DRIVE a tool and can be boxed to it), the
# fuzzer probe must PRODUCE inputs — author a dictionary and a corpus, including a
# large seed. That needs ordinary file creation (printf/head/redirects), so the
# agent gets a full shell (permissions skipped) and we rely on the focused prompt
# to keep it on task. Restricting Bash here just starves seed/dict generation.
ALLOWED_TOOLS = None

SYSTEM_PROMPT = (
    "You are a fuzzing-integration probe running inside a CRS runner container. "
    "You have a normal shell and file tools. Your ONLY task is to exercise and "
    "prove three `crs-fuzz` usage modes work end-to-end: a plain run, a run with "
    "an agent-authored dictionary, and a run with an agent-authored corpus plus a "
    "custom --max-len. Stay strictly on that task — do not modify the target, build "
    "anything, or go looking for bugs."
)


def harness_name() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    name = os.environ.get("OSS_CRS_TARGET_HARNESS")
    if not name:
        sys.exit("[test_fuzz] no harness name (argv[1] / OSS_CRS_TARGET_HARNESS)")
    return name


def build_user_prompt(harness: str) -> str:
    h = harness
    s = SECONDS
    dict_path = PROBE_DIR / "agent.dict"
    c1, c2, c3 = (PROBE_DIR / "corpus-baseline",
                  PROBE_DIR / "corpus-dict",
                  PROBE_DIR / "corpus-agent")
    pov = PROBE_DIR / "povs"
    return "\n".join([
        f"The target is a **{LANGUAGE}** fuzz harness named `{h}` (built at "
        f"`{OUT_DIR}`, source at `{SRC_DIR}`). The `crs-fuzz` tool runs it in "
        f"libFuzzer fork mode and prints libFuzzer's log (corpus growth, crashes, "
        f"`Done N runs`). Common flags: `--harness`, `--seconds`, `--corpus <dir>`, "
        f"`--pov-out <dir>`, `--dict <file>`, `--max-len <bytes>`.",
        "",
        f"Use a short `--seconds {s}` for every run. Work under `{PROBE_DIR}` "
        f"(you have file access there and to `{SRC_DIR}`).",
        "",
        "IMPORTANT — how to run crs-fuzz: it is SYNCHRONOUS and can take up to ~2 "
        "minutes to return (after fuzzing it submits the generated corpus). Run "
        "each crs-fuzz command in the FOREGROUND and WAIT for its `[crs-fuzz] ...` "
        "summary line — do NOT background it. If your shell tool accepts a timeout, "
        "set it to 300000 ms for crs-fuzz commands.",
        "",
        "TIP — creating inputs: you have a normal shell, so generate files the "
        "easy way. For a large seed, prefer shell over the Write tool (which caps "
        "output): e.g. `printf '<valid-prefix>' > seed && head -c 6000 /dev/zero | "
        "tr '\\0' 'a' >> seed` (or a small python one-liner). Verify sizes with "
        "`wc -c`.",
        "",
        "Run these THREE scenarios in order and observe each one's output:",
        "",
        "### Scenario 1 — baseline (no extra inputs)",
        f"- `crs-fuzz --harness {h} --seconds {s} --corpus {c1} --pov-out {pov}`",
        "- Note from the libFuzzer log: did it execute (`Done N runs`)? How many "
        "  corpus entries did it end with, and were any crashes found?",
        "",
        "### Scenario 2 — agent-generated dictionary",
        f"- Read the harness `{h}` (and any input-parsing source under `{SRC_DIR}`) "
        "  to learn the input format. Identify meaningful keyword/byte tokens.",
        f"- Write a libFuzzer dictionary to `{dict_path}` — one token per line in "
        '  libFuzzer format, e.g. `"true"`, `"null"`, `kw="def"` (quoted values; '
        "  `\\xNN` allowed for non-printable bytes). Put several real tokens in it.",
        f"- Fuzz with it: `crs-fuzz --harness {h} --seconds {s} --dict {dict_path} "
        f"--corpus {c2} --pov-out {pov}`",
        "- Confirm from the log that the dictionary was accepted (e.g. libFuzzer "
        "  prints `Dictionary: N entries`) and the run executed.",
        "",
        "### Scenario 3 — agent-generated corpus + custom --max-len",
        f"- Create just TWO seed files under `{c3}` that match the harness's "
        "  expected format: one small/normal seed, and one **oversized** seed whose "
        f"  size clearly exceeds libFuzzer's default cap of {DEFAULT_MAX_LEN} bytes "
        "  (a valid input padded with a long run of characters — aim for ~6000 "
        "  bytes). Use the shell (see the TIP above) and confirm with `wc -c`.",
        "- Pick a `--max-len` value >= your oversized seed's size (so libFuzzer does "
        f"  not silently truncate/skip it — without it the cap is {DEFAULT_MAX_LEN}).",
        f"- Fuzz: `crs-fuzz --harness {h} --seconds {s} --corpus {c3} "
        "--max-len <chosen> --pov-out " + str(pov) + "`",
        "- Confirm from the log the run honored the larger inputs (read your seeds "
        "  without a 'too long' rejection, and used your `max_len`).",
        "",
        "### Scenario 4 — long-running fuzzer (start / status / add-seeds / stop)",
        "This mode is ASYNC (unlike scenarios 1-3): the lifecycle commands return "
        "immediately and ONE long-running fuzzer runs in the BACKGROUND on the "
        f"shared corpus at `/artifacts/corpus/{h}/`. Use the subcommand forms below.",
        f"- Start it: `crs-fuzz start --harness {h}`. It should print "
        "  `started long-running fuzzer ... pid=...` and return right away (do NOT "
        "  background this command yourself — it returns on its own).",
        f"- `crs-fuzz status --harness {h}` — confirm it reports `RUNNING pid=... for "
        "  Ns` with a corpus count.",
        f"- Write TWO small, valid seed files into a scratch dir `/work/agent/newseeds/` "
        "  (well-formed inputs for this harness).",
        f"- `crs-fuzz add-seeds --harness {h} /work/agent/newseeds` — confirm it "
        "  coverage-merges them into the live corpus (note the `corpus N -> M` line — "
        "  your new seeds being folded in) WITHOUT stopping the fuzzer. This is how "
        "  new seeds get loaded — the running fuzzer reloads them (no restart).",
        f"- `crs-fuzz status --harness {h}` — confirm still RUNNING (same pid), then "
        f"  `crs-fuzz stop --harness {h}` — confirm it stops with a corpus/crashes "
        "  summary.",
        "",
        "## Report",
        "",
        "Give a short per-scenario summary with concrete evidence quoted from the "
        "logs: scenario 1 (runs/corpus/crashes); scenario 2 (the tokens you wrote + "
        "the `Dictionary: N entries` line); scenario 3 (your largest seed size, the "
        "`--max-len` you chose, and proof the run used it); scenario 4 (the `started "
        "... pid=`, a `RUNNING pid=` status, the add-seeds `corpus N -> M` line, and "
        "the final `stopped` summary). Then end with a single line of exactly this "
        "form:",
        "  `FUZZ_PROBE: PASS — <one-line evidence for all four>`, or",
        "  `FUZZ_PROBE: FAIL — <which scenario failed and why>`",
        "",
        "Do not end your turn until Scenario 4's `crs-fuzz stop` has returned and you "
        "have written the FUZZ_PROBE verdict line. Begin with scenario 1 now.",
    ])


def setup_source(crs) -> Path | None:
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        capture_output=True,
    )
    try:
        crs.download_build_output("src", SRC_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to download /src (agent can't read harness source): %s", e)
        return None
    return SRC_DIR.resolve()


def build_graph(node: ClaudeCodeNode):
    g = StateGraph(ClaudeState)
    g.add_node("fuzz_probe", node)
    g.add_edge(START, "fuzz_probe")
    g.add_edge("fuzz_probe", END)
    return g.compile()


def main() -> None:
    logger.info("=== FUZZ INTERACTION DEBUG ENTRYPOINT (language=%s, seconds=%d) ===",
                LANGUAGE, SECONDS)
    harness = harness_name()
    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    crs = init_crs_utils()

    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)

    # crs-fuzz needs the asan harness build (/out); the agent needs the source to
    # craft a dictionary/corpus.
    try:
        crs.download_build_output("build", OUT_DIR)
        logger.info("Downloaded build outputs to %s", OUT_DIR)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to download build outputs: %s", e)
        sys.exit(1)

    source_dir = setup_source(crs)
    if source_dir:
        logger.info("Source directory: %s", source_dir)

    configure_claude_env(
        {"llm_api_url": LLM_API_URL, "llm_api_key": LLM_API_KEY},
        source_dir=source_dir or WORK_DIR,
    )

    user_prompt = build_user_prompt(harness)
    (AGENT_WORK_DIR / "fuzz_probe_prompt.txt").write_text(user_prompt)
    (AGENT_WORK_DIR / "fuzz_probe_system.txt").write_text(SYSTEM_PROMPT)

    node = ClaudeCodeNode(
        cwd=source_dir or WORK_DIR,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,        # None -> all tools (see note above)
        skip_permissions=True,              # full shell: needs to author seeds/dict
        # cwd is /src (to read the harness); grant file-tool access to the probe
        # work area and the build dir, which live outside the cwd sandbox.
        extra_args=["--add-dir", str(WORK_DIR), "--add-dir", str(OUT_DIR)],
        log_path=AGENT_WORK_DIR / "fuzz_probe_stream.jsonl",
    )
    app = build_graph(node)

    logger.info("Invoking fuzz probe agent for harness %s (allowed tools: %s)",
                harness, ALLOWED_TOOLS)
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
    if "FUZZ_PROBE: PASS" in verdict:
        logger.info("RESULT: fuzz probe PASS")
    elif "FUZZ_PROBE: FAIL" in verdict:
        logger.warning("RESULT: fuzz probe FAIL")
    else:
        logger.warning("RESULT: inconclusive (no FUZZ_PROBE verdict line)")


if __name__ == "__main__":
    main()
