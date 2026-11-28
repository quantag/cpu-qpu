#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# The release tarball on a clean Debian or Ubuntu (CI runs it in ubuntu:22.04, ubuntu:24.04 and debian:12):
#   sh packaging/smoke.sh qarm-V-linux-ARCH.tar.gz
# Installs only the documented runtime dependencies (no Python packages: the oracle instructions are then not
# claimed) and a cross compiler, unpacks, compiles with qarm-cc and runs with qarm-run: ghz.c, quench.c (QEVOLVE),
# and the conformance program for both architectures, with no failure.
set -e
tarball=$1
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends libgomp1 gcc libc6-dev >/dev/null
apt-get install -y -q --no-install-recommends libglib2.0-0t64 libcurl4t64 >/dev/null 2>&1 \
    || apt-get install -y -q --no-install-recommends libglib2.0-0 libcurl4 >/dev/null
if [ "$(uname -m)" = x86_64 ]; then
    apt-get install -y -q --no-install-recommends gcc-aarch64-linux-gnu libc6-dev-arm64-cross >/dev/null
else
    apt-get install -y -q --no-install-recommends gcc-x86-64-linux-gnu libc6-dev-amd64-cross >/dev/null
fi
mkdir -p /tmp/smoke && cd /tmp/smoke
tar xzf "$tarball"
Q=$(ls -d /tmp/smoke/qarm-*)/bin
. /etc/os-release
echo "== $PRETTY_NAME, $(uname -m)"
"$Q/qarm-run" --info
S=$Q/../share/qarm
"$Q/qarm-cc" "$S/examples/ghz.c" -o ghz
"$Q/qarm-run" -q ./ghz | grep "on the coprocessor"
"$Q/qarm-cc" "$S/examples/quench.c" -o quench
"$Q/qarm-run" -q ./quench | grep "2. eps 1e-2: order 2, 273 steps"
for a in aarch64 x86-64; do
    "$Q/qarm-cc" --$a "$S/conformance/qarm_conformance.c" -o conf-$a
    "$Q/qarm-run" -q ./conf-$a > out-$a.txt || { cat out-$a.txt; exit 1; }
    grep -q "Core passed; Oracle not claimed; Variational passed; Circuit passed; Async passed; Dynamics passed; Open passed" out-$a.txt \
        && grep -q "9 passed, 0 failed, 8 skipped" out-$a.txt || { cat out-$a.txt; exit 1; }
    echo "conformance on $a: $(tail -n 1 out-$a.txt)"
done
"$Q/qarm-run" --cpu ./conf-aarch64 | grep -q "no runtime"
echo "smoke test passed"
