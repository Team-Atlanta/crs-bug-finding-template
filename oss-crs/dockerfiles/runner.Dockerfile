# =============================================================================
# crs-bug-finding-template runner module (run phase)
# =============================================================================
# Builds FROM the framework-provided base_runner_image (oss-crs #258) so the
# runtime OS matches the target's builder — harness binaries executed in the
# runner (crs-fuzz / crs-coverage, and gdb on the debug build) load under a
# matching glibc/ABI instead of failing with `GLIBC_2.xx not found`. All tooling is
# installed here; there is no separate prepare-phase base image.
# =============================================================================

# Injected by `oss-crs run` from the target's base_os_version; the default keeps
# the Dockerfile buildable standalone.
ARG base_runner_image=gcr.io/oss-fuzz-base/base-runner:latest
FROM ${base_runner_image}

# Declared by the framework template (unused here, but avoids build-arg warnings).
ARG crs_version

ENV DEBIAN_FRONTEND=noninteractive
# Some base-runner OSes mark system Python as externally managed (PEP 668);
# we pip-install our package system-wide, so opt out globally.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# System + analysis tooling for the runner helpers (crs/tools) + the agent:
#   gdb                  - C/C++ crash debugging. The agent runs gdb directly as a
#                          subprocess (no wrapper; see the `gdb` skill).
#   llvm-18              - C/C++ coverage (crs-coverage probes llvm-cov/profdata
#                          versions); best-effort, the base-runner's bundled llvm
#                          is used first so this is only a fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git rsync unzip \
        python3 python3-pip python3-venv \
        gdb \
    && { apt-get install -y --no-install-recommends llvm-18 \
         || echo "WARN: llvm-18 unavailable on this base OS (coverage may degrade)"; } \
    && rm -rf /var/lib/apt/lists/*

# Node.js + Claude Code CLI (pinned: .claude.json schema changes across versions)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
ARG CLAUDE_CODE_CLI_VERSION=2.1.165
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_CLI_VERSION}

# CodeQL CLI bundle (CLI + extractors + precompiled standard query packs) so the
# agent's crs-codeql tool can run queries against the codeql/db build output.
# Pinned to match the codeql-build phase; unpacks to /opt/codeql.
ARG CODEQL_BUNDLE_VERSION=v2.25.6
RUN curl -fsSL \
      "https://github.com/github/codeql-action/releases/download/codeql-bundle-${CODEQL_BUNDLE_VERSION}/codeql-bundle-linux64.tar.gz" \
      | tar -xz -C /opt \
    && /opt/codeql/codeql --version
ENV PATH="/opt/codeql:${PATH}"

# Identity for any git operations the agent performs
RUN git config --global user.email "crs@oss-crs.dev" \
    && git config --global user.name "OSS-CRS Bug Finder" \
    && git config --global --add safe.directory '*'

# libCRS (CLI + importable Python package)
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh \
    && python3 -c "from libCRS.base import DataType; print('libCRS OK')"

# Our CRS package (LangGraph node + tools + deps). The run-phase entrypoints live
# in crs/entrypoint/ (shipped by `COPY crs/`); its entrypoint.sh multiplexer picks
# one at run time.
COPY pyproject.toml /opt/crs-bug-finding-template/pyproject.toml
COPY crs/ /opt/crs-bug-finding-template/crs/
RUN pip3 install /opt/crs-bug-finding-template \
    && chmod +x /opt/crs-bug-finding-template/crs/entrypoint/entrypoint.sh

# Entrypoint multiplexer (crs/entrypoint/entrypoint.sh): selects the runner
# entrypoint from $CRS_ENTRYPOINT (set in the compose file's additional_env),
# defaulting to run_crs. No rebuild needed to switch strategies/probes:
#   run_crs            - single Claude Code agent (default)
#   run_crs_langgraph  - seed-gen ∥ pov-gen as parallel LangGraph nodes
#   run_crs_subagents  - one orchestrator delegating to Claude Code subagents
#   test_{gdb,codeql,fuzz,afl,coverage} - isolated tool probes
ENTRYPOINT ["bash", "/opt/crs-bug-finding-template/crs/entrypoint/entrypoint.sh"]
