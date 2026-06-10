#!/usr/bin/env python3
"""CRS entrypoint — render the run_crs_langgraph graph, copy it to OSS_CRS_LOG_DIR,
then exit.

A utility entrypoint (CRS_ENTRYPOINT=draw_graph): it builds the SAME StateGraph
wiring as run_crs_langgraph via its build_graph() (single source of topology), with
stub nodes — the diagram depends only on node names + edges — and writes the
Mermaid source (.mmd) plus a PNG to $OSS_CRS_LOG_DIR (the host-mounted log dir),
then exits. No boot, no agents, no LLM cost. The .mmd is always written (offline);
the PNG is best-effort (draw_mermaid_png renders via the mermaid.ink API).
"""

import logging
import os
import sys
from pathlib import Path

from crs.entrypoint.run_crs_langgraph import NODE_NAMES, build_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("draw_graph")


def main() -> None:
    out = Path(os.environ.get("OSS_CRS_LOG_DIR") or "/work/agent")
    out.mkdir(parents=True, exist_ok=True)

    # Stub nodes + a constant branch: only the names/edges matter for the diagram.
    app = build_graph({name: (lambda _s: {}) for name in NODE_NAMES},
                      lambda _s: "continue")
    gr = app.get_graph()

    mmd = out / "run_crs_langgraph.mmd"
    mmd.write_text(gr.draw_mermaid())
    logger.info("Wrote Mermaid source -> %s", mmd)

    png = out / "run_crs_langgraph.png"
    try:
        gr.draw_mermaid_png(output_file_path=str(png))
        logger.info("Wrote graph PNG -> %s", png)
    except Exception as e:  # noqa: BLE001 - PNG needs the mermaid.ink API (network)
        logger.warning("PNG render failed (%s); the .mmd is still available", e)

    logger.info("draw_graph done; exiting.")


if __name__ == "__main__":
    main()
