# AFL++ build: AFL-instrumented harnesses (OSS-Fuzz FUZZING_ENGINE=afl) for the
# runner's afl-fuzz path. Selected via the `afl-build` phase in oss-crs/crs.yaml.
# The AFL++ toolchain ships in the OSS-Fuzz base-builder, so no extra install.
ARG target_base_image
FROM $target_base_image

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

# NB: do NOT name this `compile_afl` — OSS-Fuzz's `compile` sources
# `compile_${FUZZING_ENGINE}` (i.e. `compile_afl`), so that name self-recurses.
COPY builder/build_afl /usr/local/bin/build_afl

CMD ["build_afl"]
