"""Runner-environment analysis helpers for the CRS (C/C++ targets).

Small, self-contained tools the bug-finding agent (claude -p) or LangGraph nodes
can call to triage and explore the target:

    crs-coverage    - llvm-cov coverage report for a corpus
    crs-fuzz        - run a libFuzzer harness -> PoVs (asan build)
    crs-afl         - run an AFL++ campaign (afl build) -> PoVs; live add-seeds
    crs-codeql      - author + run CodeQL queries against the codeql/db index
                      (semantic data-flow / taint / call-graph analysis)

Each tool resolves the build artifacts it needs from an explicit --build-dir or,
inside an oss-crs run, by downloading them via libCRS.

There is no gdb wrapper: the agent drives `gdb` directly on the debug build (see
the `gdb` skill at crs/skills/gdb/SKILL.md).
"""
