#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# The release tarball for this machine's architecture:
#
#   packaging/build.sh [--qemu DIR] [--out DIR] [--version V]
#
# --qemu DIR: QEMU's installation to bundle; built there first when DIR has no bin/qemu-aarch64 (so a CI cache of
# DIR saves the build). Without it, QEMU is built in a temporary directory.
#
# downloads QEMU's release tarball (QEMU_VERSION, default 10.2.4, from download.qemu.org), builds it with plugins,
# installs Q-ARM with that QEMU bundled
# (packaging/install.sh), and writes OUT/qarm-V-linux-ARCH.tar.gz, its .sha256, and the QEMU tarball itself as
# OUT/qemu-VERSION.tar.xz: QEMU is GPL-2.0, and its complete source goes with the binaries. Runs on Linux with curl,
# cmake, ninja, a C compiler, Python 3.9+ and glib's headers (QEMU's build needs them).
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
QEMU_VERSION=${QEMU_VERSION:-10.2.4}
qemu= out=$ROOT/dist version=
while [ $# -gt 0 ]; do
    case $1 in
        --qemu) qemu=$2; shift 2 ;;
        --out) out=$2; shift 2 ;;
        --version) version=$2; shift 2 ;;
        *) echo "build.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$version" ] || version=1.1-$(git -C "$ROOT" rev-parse --short HEAD)
arch=$(uname -m)
[ "$arch" = arm64 ] && arch=aarch64
mkdir -p "$out"
out=$(cd "$out" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

src=$out/qemu-$QEMU_VERSION.tar.xz
[ -f "$src" ] || curl -fsSL -o "$src" "https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz"
[ -n "$qemu" ] || qemu=$work/qemu
mkdir -p "$qemu"
qemu=$(cd "$qemu" && pwd)
if [ ! -x "$qemu/bin/qemu-aarch64" ]; then
    tar -C "$work" -xJf "$src"
    mkdir "$work/qemu-$QEMU_VERSION/build"
    (cd "$work/qemu-$QEMU_VERSION/build" && ../configure --prefix="$qemu" --target-list=aarch64-linux-user,x86_64-linux-user \
        --enable-plugins --disable-docs --disable-tools --disable-werror >/dev/null && ninja install >/dev/null)
fi

name=qarm-$version-linux-$arch
"$ROOT/packaging/install.sh" --prefix "$work/$name" --qemu-include "$qemu/include" --bundle-qemu "$qemu" --version "$version"
echo "$QEMU_VERSION" > "$work/$name/share/doc/qarm/QEMU-VERSION"
cat > "$work/$name/README" <<EOT
Q-ARM $version for Linux on $arch: run AArch64 and x86-64 programs with quantum instructions.

  bin/qarm-cc hello.c -o hello       # compile with qarm.h (AArch64 by default; --x86-64)
  bin/qarm-run ./hello               # run it on the Q-ARM runtime (exact simulator by default)
  bin/qarm-run --info                # what was found

Needs glib 2.66+, libcurl and libgomp (Debian, Ubuntu: libglib2.0-0 libcurl4 libgomp1). For the oracle
instructions (QSEARCH, QMIN, ...): python3 -m pip install qoracle lief capstone numpy unicorn.
Specification: share/doc/qarm/qarm_isa.md. QEMU's license and source: share/doc/qarm/QEMU-LICENSE.md.
EOT
tar -C "$work" -czf "$out/$name.tar.gz" "$name"
(cd "$out" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
echo "$out/$name.tar.gz"
