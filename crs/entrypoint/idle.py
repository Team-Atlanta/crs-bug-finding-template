#!/usr/bin/env python3
"""CRS entrypoint — idle dev shell: boot the environment, then sleep forever.

Selected with `CRS_ENTRYPOINT=idle` (the crs/entrypoint/entrypoint.sh multiplexer). It runs
the normal boot sequence — downloads the build to /out and the source to /src,
fetches boot-time seeds, configures Claude Code auth, writes CLAUDE.md, installs
the runner-tool skills — so every `crs-*` tool is ready, then sleeps indefinitely
so you can open a shell in the running container and drive the tools by hand:

    docker ps                                   # find the *_fuzzer-1 container
    docker exec -it <container> bash
    # then, inside:
    crs-fuzz start  --harness <H>               # background fuzzer (reload loop)
    crs-fuzz status --harness <H>
    crs-fuzz add-seeds --harness <H> <dir>
    crs-coverage --harness <H>
    libCRS run-pov <pov> <resp_dir> --harness <H>
    # debug a crash: download the debug build, then drive gdb directly
    libCRS download-build-output debug /work/debug-build
    gdb -q -batch -ex run -ex bt --args /work/debug-build/<H> <pov>

Paths: build=/out, source=/src, corpus=/artifacts/corpus/<H>, povs=/artifacts/povs,
scratch=/work. Stop it with `docker stop` or the run's --timeout.

Boot failures are non-fatal here (unlike the real entrypoints): the shell stays
up so you can investigate from inside.
"""

import logging
import sys
import time

from crs.src import runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("idle")


def main() -> None:
    logger.info("=== IDLE DEV SHELL: booting, then sleeping for manual tool use ===")
    harness = "<harness>"
    try:
        ctx = runtime.boot()
        harness = ctx.harness
        logger.info("Boot OK. harness=%s  source=%s  build=/out", ctx.harness, ctx.source_dir)
    except SystemExit as e:
        logger.warning("boot did not fully complete (%s) — shell still available to debug", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("boot failed (%s) — shell still available to debug", e)

    logger.info("Ready. Open a shell in this container and run the crs-* tools:")
    logger.info("    docker ps   # find the *_fuzzer-1 container")
    logger.info("    docker exec -it <container> bash")
    logger.info("    crs-fuzz start --harness %s   |   crs-coverage --harness %s   |   "
                "gdb -q -batch -ex run -ex bt --args /work/debug-build/%s <pov>",
                harness, harness, harness)
    logger.info("Sleeping forever; stop with `docker stop` or the run --timeout.")

    # Sleep in bounded chunks so a SIGTERM (docker stop / run --timeout) is handled
    # promptly by the default disposition.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
