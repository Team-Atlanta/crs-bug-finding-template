# CodeQL build: traces the project compile to produce a CodeQL database for
# semantic queries (data-flow / taint / call graphs). Selected via BUILD_TYPE=codeql.
ARG target_base_image
FROM $target_base_image

# curl + ca-certificates for fetching the CodeQL bundle (present on most
# base-builders, installed defensively in case a slim base image lacks them).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# CodeQL CLI bundle (CLI + language extractors + precompiled standard query
# packs). Pinned to a stable release URL rather than apt so the version stays
# fixed as the target base image evolves (cf. PLAN.md note on bear). The bundle
# unpacks to /opt/codeql/.
ARG CODEQL_BUNDLE_VERSION=v2.25.6
RUN curl -fsSL \
      "https://github.com/github/codeql-action/releases/download/codeql-bundle-${CODEQL_BUNDLE_VERSION}/codeql-bundle-linux64.tar.gz" \
      | tar -xz -C /opt \
    && /opt/codeql/codeql --version
ENV PATH="/opt/codeql:${PATH}"

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

COPY builder/compile_codeql /usr/local/bin/compile_codeql

CMD ["compile_codeql"]
