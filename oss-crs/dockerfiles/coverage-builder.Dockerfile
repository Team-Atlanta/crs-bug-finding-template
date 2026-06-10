# Coverage build: llvm-cov-instrumented harnesses for coverage feedback/reports.
# Shares builder/compile_target, selected via BUILD_TYPE=coverage.
ARG target_base_image
FROM $target_base_image

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

COPY builder/compile_coverage /usr/local/bin/compile_coverage

CMD ["compile_coverage"]
