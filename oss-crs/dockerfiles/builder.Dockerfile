# Default build: ASan harnesses + source snapshot.
ARG target_base_image
FROM $target_base_image

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

COPY builder/compile_target /usr/local/bin/compile_target

CMD ["compile_target"]
