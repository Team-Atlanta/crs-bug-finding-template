# crs-bug-finding-template

A template for an agentic **Cyber Reasoning System (CRS)**: a Claude Code agent
that reads a target's source, drives analysis tools (fuzzing, static analysis,
coverage, a debugger), and produces verified crashing inputs (PoVs). It plugs
into the [oss-crs](https://github.com/ossf/oss-crs) framework as a bug-finding
CRS.

Start with the [tutorial](tutorial/README.md), which walks you through extending
this template one capability at a time.

## Layout

- **`builder/`**: build-phase scripts, each producing one build variant of the
  target via OSS-Fuzz `compile`.
- **`oss-crs/`**: the oss-crs integration manifest (`crs.yaml`, `dockerfiles/`,
  example compose).
- **`tutorial/`**: the 5-chapter hands-on tutorial for building this template
  into an effective CRS.
- **`crs/`**: the CRS implementation. `CLAUDE.md.j2` is the agent's
  standing-instructions template. Its subdirectories:
  - **`crs/entrypoint/`**: the run-phase entrypoints plus the `entrypoint.sh`
    multiplexer (`run_crs`, `run_crs_subagents`, `run_crs_langgraph`, `idle`,
    `draw_graph`, `test_*`).
  - **`crs/src/`**: runtime and prompt plumbing (`runtime.py`, `prompts.py`,
    `claude_node.py`).
  - **`crs/tools/`**: the `crs-*` CLI implementations (`crs-fuzz`, `crs-afl`,
    `crs-codeql`, `crs-coverage`).
  - **`crs/skills/`**: Claude Code skill docs.
  - **`crs/roles/`**: entrypoint role/system prompts.
  - **`crs/agents/`**: Task-tool subagent definitions.
