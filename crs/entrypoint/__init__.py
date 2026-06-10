"""Run-phase entrypoints, selected by entrypoint.sh via $CRS_ENTRYPOINT.

run_crs[_langgraph|_subagents] are the bug-finding strategies; test_* are
isolated tool probes; idle boots then sleeps for manual `docker exec` tool use.
Build-phase scripts (compile_*/build_afl) live in the top-level builder/ dir.
"""
