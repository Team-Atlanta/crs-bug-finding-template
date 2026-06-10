# Debug build: ASan harnesses compiled with -O0 -g3 -fno-omit-frame-pointer
# -fno-inline for clean stacks under gdb. Selected via BUILD_TYPE=debug.
ARG target_base_image
FROM $target_base_image

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

COPY builder/compile_debug /usr/local/bin/compile_debug

CMD ["compile_debug"]
