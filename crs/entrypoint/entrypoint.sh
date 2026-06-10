#!/bin/bash
#
# CRS entrypoint multiplexer.
#
# Picks which runner entrypoint to exec based on $CRS_ENTRYPOINT, so it can be
# chosen from the compose file's `additional_env` without rebuilding the image.
# Defaults to run_crs (normal bug finding). The value is a <name>.py stem of a
# script in this dir (a trailing ".py" is tolerated). Example compose additional_env:
#
#     additional_env:
#       CRS_ENTRYPOINT: run_crs            # default — single Claude Code agent
#       CRS_ENTRYPOINT: run_crs_langgraph  # coverage-guided LangGraph loop
#                                          #   (warmup->coverage->explore->dispatch)
#       CRS_ENTRYPOINT: run_crs_subagents  # orchestrator -> Claude Code subagents
#       CRS_ENTRYPOINT: draw_graph         # render run_crs_langgraph graph to
#                                          #   $OSS_CRS_LOG_DIR (.mmd + .png), exit
#       CRS_ENTRYPOINT: test_gdb           # isolated tool probes:
#       CRS_ENTRYPOINT: test_codeql
#       CRS_ENTRYPOINT: test_fuzz
#       CRS_ENTRYPOINT: test_afl
#       CRS_ENTRYPOINT: test_coverage
#       CRS_ENTRYPOINT: idle               # boot, then sleep — `docker exec` in to
#                                          #   run the crs-* tools by hand
#
# Any extra args (e.g. the harness name) are forwarded to the chosen script.
set -e

EP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ep="${CRS_ENTRYPOINT:-run_crs}"
ep="${ep%.py}"   # tolerate a trailing ".py"
script="${EP_DIR}/${ep}.py"

if [ ! -f "$script" ]; then
  echo "[entrypoint] unknown CRS_ENTRYPOINT='${CRS_ENTRYPOINT:-}' (resolved '${ep}.py' not found)" >&2
  echo "[entrypoint] available entrypoints:" >&2
  for f in "$EP_DIR"/run_crs*.py "$EP_DIR"/test_*.py "$EP_DIR"/idle.py; do
    [ -f "$f" ] && echo "  - $(basename "${f%.py}")" >&2
  done
  exit 2
fi

echo "[entrypoint] CRS_ENTRYPOINT=${ep} -> exec python3 ${script} $*" >&2
exec python3 "$script" "$@"
