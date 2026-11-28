# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# Q-ARM in a container: qarm-run, qarm-cc, QEMU 10.2.4 with the plugin, both cross compilers and the oracle helper
# (qoracle). It is made from the release tarball, so build that first, from the repository's root:
#   packaging/build.sh --out dist
#   docker build -f packaging/Dockerfile -t qarm .
# Then, in a directory with your program:
#   docker run --rm -v "$PWD:/work" qarm qarm-cc hello.c -o hello
#   docker run --rm -v "$PWD:/work" qarm qarm-run ./hello
# Published as ghcr.io/quantag/qarm (linux/amd64 and linux/arm64) by .github/workflows/release.yml.
FROM ubuntu:24.04
RUN apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
        ca-certificates libglib2.0-0t64 libcurl4t64 libgomp1 python3 python3-pip gcc libc6-dev \
        $(if [ "$(uname -m)" = x86_64 ]; then echo gcc-aarch64-linux-gnu libc6-dev-arm64-cross; \
          else echo gcc-x86-64-linux-gnu libc6-dev-amd64-cross; fi) \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install -q --no-cache-dir --break-system-packages unicorn lief capstone numpy \
        "qoracle @ https://github.com/quantag/qoracle/archive/refs/heads/main.tar.gz"
COPY dist/ /tmp/dist/
RUN arch=$(uname -m) && mkdir /opt/qarm \
    && tar -C /opt/qarm --strip-components=1 -xzf /tmp/dist/qarm-*-linux-$arch.tar.gz && rm -rf /tmp/dist
ENV PATH=/opt/qarm/bin:$PATH
WORKDIR /work
CMD ["qarm-run", "--info"]
