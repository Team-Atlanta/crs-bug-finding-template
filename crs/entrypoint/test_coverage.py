#!/usr/bin/env python3
"""DEBUG runner entrypoint: corpus + coverage flow probe.

Exercises the shared-corpus management end-to-end on a real target and makes the
whole flow observable in the logs:

  1. Seed the canonical corpus (/artifacts/corpus/<harness>) from the boot seeds
     (and a synthetic input if there are none), so it's never empty.
  2. **Deterministic flow** (this entrypoint drives the tools directly and logs
     each observation):
       - count the corpus,
       - `crs-fuzz --seconds N`            -> corpus grows (coverage-dedup on add),
       - `crs-fuzz --merge`                -> corpus minimized (before -> after),
       - `crs-coverage`                    -> llvm-cov summary + line annotations,
     then logs the coverage summary and a sample of *non-zero* annotated source
     lines (the part that's easy to get wrong — proves llvm-cov resolved the
     instrumented profile against real /src source).
  3. **Agent integration**: a Claude Code node restricted to the coverage/fuzz
     tools re-runs `crs-coverage` on the now-populated corpus and reports a
     verdict, confirming the agent can drive it.

Native (libFuzzer + llvm-cov). Swap the runner ENTRYPOINT back to crs/entrypoint/run_crs.py
to restore normal bug finding.
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from libCRS.base import DataType
from libCRS.cli.main import init_crs_utils

from crs.src.claude_node import ClaudeCodeNode, ClaudeState, configure_claude_env
from crs.tools.common import corpus_dir, seed_from_boot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_coverage")

SRC_DIR = Path("/src")
WORK_DIR = Path("/work")
AGENT_WORK_DIR = WORK_DIR / "agent"
SEED_DIR = WORK_DIR / "seeds"
REPORT_DIR = WORK_DIR / "coverage-report"

LANGUAGE = os.environ.get("FUZZING_LANGUAGE", "c")
FUZZ_SECONDS = int(os.environ.get("COVERAGE_PROBE_FUZZ_SECONDS", "45"))
LLM_API_URL = os.environ.get("OSS_CRS_LLM_API_URL", "")
LLM_API_KEY = (
    open(os.environ["OSS_CRS_LLM_API_KEY_FILE"]).read().strip()
    if os.environ.get("OSS_CRS_LLM_API_KEY_FILE")
    else os.environ.get("OSS_CRS_LLM_API_KEY", "")
)

ALLOWED_TOOLS = ["Bash(crs-coverage:*)", "Bash(crs-fuzz:*)",
                 "Bash(ls:*)", "Bash(wc:*)", "Bash(head:*)", "Bash(grep:*)"]

SYSTEM_PROMPT = (
    "You are a coverage-tooling probe in a CRS runner container. The corpus has "
    "already been built and minimized for you. Your available shell tools are "
    "`crs-coverage` / `crs-fuzz` (+ `ls`/`wc`/`head` to observe). Prove the "
    "coverage tool produces a real report with line-level source annotations. Do "
    "not try to find bugs."
)


# --------------------------------------------------------------------------
# Deterministic flow (the observable backbone)
# --------------------------------------------------------------------------
def _count(d: Path) -> int:
    return sum(1 for p in d.iterdir() if p.is_file()) if d.is_dir() else 0


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    logger.info("$ %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        logger.info("stdout:\n%s", out)
    if err:
        logger.info("stderr (tail):\n%s", "\n".join(err.splitlines()[-15:]))
    return r


def _ensure_nonempty_corpus(harness: str) -> Path:
    cdir = corpus_dir(harness)
    added = seed_from_boot(cdir)
    logger.info("seeded %d boot seed(s) into %s", added, cdir)
    if _count(cdir) == 0:
        synth = cdir / "synthetic_probe_seed"
        synth.write_bytes(b"AFC-COV-PROBE\x00" + bytes(range(64)))
        logger.info("no boot seeds; wrote a synthetic seed (%d bytes)", synth.stat().st_size)
    return cdir


def _sample_annotations(report: Path, n: int = 12) -> list[str]:
    """Return a few llvm-cov `show` lines that have a NON-ZERO hit count.

    llvm-cov text format is `<line>|<count>|<source>`; a covered line proves the
    profile mapped onto real source. We grab the file header before each sample
    so the path is visible.
    """
    if not report.exists():
        return []
    # line like: "   4607|     12|    {"  -> group1=lineno group2=count group3=src
    row = re.compile(r"^\s*\d+\|\s*([\d.]+[kKmM]?)\|")
    samples, current_file, skip_file = [], None, False
    for line in report.read_text(errors="replace").splitlines():
        if line.endswith(":") and "/" in line and "|" not in line:
            current_file = line  # a "path/to/file.c:" header
            # Prefer real .c/.cpp logic; headers are mostly #define macro noise.
            skip_file = current_file.rstrip(":").endswith((".h", ".hpp", ".hh"))
            continue
        if skip_file:
            continue
        m = row.match(line)
        if m and m.group(1) not in ("0", "0.00"):
            if current_file and (not samples or samples[-1] != current_file):
                samples.append(current_file)
            samples.append(line.rstrip())
            if sum(1 for s in samples if "|" in s) >= n:
                break
    return samples


def observe_flow(harness: str) -> dict:
    """Drive seed -> fuzz -> merge -> coverage directly; log every observation."""
    result = {"baseline": 0, "after_fuzz": 0, "after_merge": 0,
              "cov_ok": False, "annotated": False, "summary": ""}

    cdir = _ensure_nonempty_corpus(harness)
    result["baseline"] = _count(cdir)
    logger.info("=== [1] corpus baseline: %d input(s) ===", result["baseline"])

    logger.info("=== [2] crs-fuzz --seconds %d (grow the corpus) ===", FUZZ_SECONDS)
    _run(["crs-fuzz", "--harness", harness, "--seconds", str(FUZZ_SECONDS)],
         timeout=FUZZ_SECONDS + 300)
    result["after_fuzz"] = _count(cdir)
    logger.info("corpus after fuzz: %d input(s) (was %d)",
                result["after_fuzz"], result["baseline"])

    logger.info("=== [3] crs-fuzz --merge (minimize to a covering set) ===")
    _run(["crs-fuzz", "--harness", harness, "--merge"], timeout=600)
    result["after_merge"] = _count(cdir)
    logger.info("corpus after merge: %d input(s) (was %d)",
                result["after_merge"], result["after_fuzz"])

    logger.info("=== [4] crs-coverage (report + line annotations) ===")
    cov = _run(["crs-coverage", "--harness", harness], timeout=1200)
    result["cov_ok"] = cov.returncode == 0
    result["summary"] = (cov.stdout or "").strip()

    report = REPORT_DIR / f"{harness}.coverage.txt"
    samples = _sample_annotations(report)
    result["annotated"] = bool(samples)
    logger.info("=== [5] sample of NON-ZERO line annotations (%s) ===", report)
    if samples:
        logger.info("\n%s", "\n".join(samples))
    else:
        logger.warning("no non-zero annotated source lines found in the report "
                       "(coverage may be empty, or /src not at the recorded paths)")
    return result


# --------------------------------------------------------------------------
# Agent integration
# --------------------------------------------------------------------------
def build_user_prompt(harness: str) -> str:
    h = harness
    return "\n".join([
        f"The native libFuzzer harness `{h}` already has a populated, minimized "
        f"corpus at `/artifacts/corpus/{h}`. Prove the coverage tool works.",
        "",
        "## Steps",
        f"1. `crs-coverage --harness {h}` — it replays the corpus through the "
        f"coverage build and prints a headline: TOTAL lines/regions/functions % "
        f"and the biggest-gap project files (reached, most lines missed). Note the "
        f"report path it prints.",
        f"2. Look at the line annotations in the report — they live after the "
        f"`=== line annotations` marker (reached project files, biggest gap "
        f"first): `grep -A 40 'line annotations' /work/coverage-report/"
        f"{h}.coverage.txt | head -40`. Confirm real `<lineno>| <hit-count>| "
        f"<source>` rows against actual decoder source, with non-zero counts.",
        "",
        "## Report",
        "Quote the summary's TOTAL line and 2-3 annotated source lines with "
        "non-zero hit counts. Then end with exactly one line:",
        "  `COVERAGE_FLOW: PASS — <evidence: total %, a couple annotated lines>`, or",
        "  `COVERAGE_FLOW: FAIL — <reason>`",
        "",
        "Use the tools now; they are the only commands available to you.",
    ])


def setup_source(crs) -> Path | None:
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"],
                   capture_output=True)
    try:
        crs.download_build_output("src", SRC_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to download /src (annotations need source): %s", e)
        return None
    return SRC_DIR.resolve()


def fetch_seeds(crs) -> None:
    try:
        fetched = crs.fetch(DataType.SEED, SEED_DIR)
        if fetched:
            logger.info("Fetched %d boot seed(s) into %s", len(fetched), SEED_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("seed fetch failed: %s", e)


def build_graph(node: ClaudeCodeNode):
    g = StateGraph(ClaudeState)
    g.add_node("cov_probe", node)
    g.add_edge(START, "cov_probe")
    g.add_edge("cov_probe", END)
    return g.compile()


def main() -> None:
    logger.info("=== COVERAGE FLOW DEBUG ENTRYPOINT (language=%s, fuzz=%ds) ===",
                LANGUAGE, FUZZ_SECONDS)
    if len(sys.argv) > 1 and sys.argv[1]:
        harness = sys.argv[1]
    else:
        harness = os.environ.get("OSS_CRS_TARGET_HARNESS") or sys.exit(
            "[test_coverage] no harness (argv[1] / OSS_CRS_TARGET_HARNESS)")

    AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    crs = init_crs_utils()
    try:
        crs.register_log_dir(AGENT_WORK_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_log_dir failed: %s", e)

    source_dir = setup_source(crs)
    fetch_seeds(crs)

    # --- deterministic, fully-logged flow ---------------------------------
    flow = observe_flow(harness)
    grew = flow["after_fuzz"] >= flow["baseline"] and flow["after_fuzz"] > 0
    merged = 0 < flow["after_merge"] <= max(flow["after_fuzz"], 1)
    det_pass = grew and merged and flow["cov_ok"] and flow["annotated"]
    logger.info(
        "=== DETERMINISTIC RESULT: %s (baseline=%d fuzz=%d merge=%d cov_ok=%s annotated=%s) ===",
        "PASS" if det_pass else "FAIL", flow["baseline"], flow["after_fuzz"],
        flow["after_merge"], flow["cov_ok"], flow["annotated"])

    # --- agent integration check ------------------------------------------
    configure_claude_env(
        {"llm_api_url": LLM_API_URL, "llm_api_key": LLM_API_KEY},
        source_dir=source_dir or WORK_DIR,
    )
    user_prompt = build_user_prompt(harness)
    (AGENT_WORK_DIR / "cov_probe_prompt.txt").write_text(user_prompt)
    node = ClaudeCodeNode(
        cwd=source_dir or WORK_DIR,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        skip_permissions=False,
        log_path=AGENT_WORK_DIR / "cov_probe_stream.jsonl",
    )
    app = build_graph(node)
    logger.info("Invoking coverage agent (allowed tools: %s)", ALLOWED_TOOLS)
    out = app.invoke({"messages": [HumanMessage(content=user_prompt)]})
    verdict = out["messages"][-1].content or ""
    logger.info("=== AGENT VERDICT ===\n%s", verdict)

    agent_pass = "COVERAGE_FLOW: PASS" in verdict
    logger.info("=== RESULT: deterministic=%s, agent=%s ===",
                "PASS" if det_pass else "FAIL",
                "PASS" if agent_pass else ("FAIL" if "COVERAGE_FLOW: FAIL" in verdict
                                           else "inconclusive"))


if __name__ == "__main__":
    main()
