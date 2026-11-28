#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# Installs Q-ARM into a prefix, relocatable (every path is found from bin/qarm-run's own place):
#
#   packaging/install.sh --prefix DIR --qemu-include DIR [--bundle-qemu DIR] [--version V]
#
#   DIR/bin/qarm-run, qarm-cc         the launcher and the compiler wrapper
#   DIR/lib/qarm/libqarm-qemu.so      the QEMU plugin with libqarm (every profile), libqarm-minimal.so, and
#                                     QArmAuto-llvmN.so when clang N and its llvm-config are here (else
#                                     qarm-cc --auto builds it on first use from share/qarm/compiler)
#   DIR/libexec/qarm/qemu-aarch64     with --bundle-qemu: that QEMU's qemu-aarch64 and qemu-x86_64
#   DIR/include/qarm/                 qarm.h and the headers it includes
#   DIR/share/qarm/                   qarm_exec.py and its modules, tools/qarm_oracle.py (the oracle helper),
#                                     tools/qarm_patch.py, examples/, the conformance program
#   DIR/share/doc/qarm/               the specification, the runtime guide, LICENSE, NOTICE
#
# --qemu-include: QEMU 10.1+'s include directory (qemu-plugin.h). Needs cmake, a C compiler and glib's headers.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
prefix= inc= bundle= version=
while [ $# -gt 0 ]; do
    case $1 in
        --prefix) prefix=$2; shift 2 ;;
        --qemu-include) inc=$2; shift 2 ;;
        --bundle-qemu) bundle=$2; shift 2 ;;
        --version) version=$2; shift 2 ;;
        *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$prefix" ] && [ -n "$inc" ] || { echo "usage: install.sh --prefix DIR --qemu-include DIR [--bundle-qemu DIR]" >&2; exit 2; }
[ -n "$version" ] || version=1.1-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo src)
mkdir -p "$prefix"
prefix=$(cd "$prefix" && pwd)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT

cmake -S "$ROOT/runtime/qemu" -B "$build/qemu" -DCMAKE_BUILD_TYPE=Release -DQEMU_PLUGIN_INCLUDE="$inc" >/dev/null
cmake --build "$build/qemu" --parallel >/dev/null
cmake -S "$ROOT/runtime/minimal" -B "$build/minimal" -DCMAKE_BUILD_TYPE=Release -DQEMU_PLUGIN_INCLUDE="$inc" >/dev/null
cmake --build "$build/minimal" --parallel >/dev/null

mkdir -p "$prefix/bin" "$prefix/lib/qarm" "$prefix/include/qarm" "$prefix/share/qarm/tools" \
         "$prefix/share/qarm/examples" "$prefix/share/qarm/conformance" "$prefix/share/doc/qarm"
for f in qarm-run qarm-cc; do
    sed "s/@QARM_VERSION@/$version/" "$ROOT/packaging/$f" > "$prefix/bin/$f"
    chmod 755 "$prefix/bin/$f"
done
cp "$build/qemu/libqarm-qemu.so" "$build/minimal/libqarm-minimal.so" "$prefix/lib/qarm/"
for h in qarm.h qarm_isa.h qarm_sv.h qarm_rt.h qarm_opt.h qarm.inc qarm_auto.h; do cp "$ROOT/include/$h" "$prefix/include/qarm/"; done
mkdir -p "$prefix/share/qarm/compiler"
cp "$ROOT/compiler/QArmAuto.cpp" "$ROOT/compiler/CMakeLists.txt" "$ROOT/compiler/qarm_auto.c" "$prefix/share/qarm/compiler/"
if command -v clang >/dev/null 2>&1; then              # the pass, for this machine's clang, when its LLVM is here
    v=$(clang -dumpversion | cut -d. -f1)
    for c in "llvm-config-$v" "/usr/lib/llvm-$v/bin/llvm-config" llvm-config; do
        if command -v "$c" >/dev/null 2>&1 && [ "$("$c" --version | cut -d. -f1)" = "$v" ]; then
            if cmake -S "$ROOT/compiler" -B "$build/auto" -DCMAKE_BUILD_TYPE=Release -DLLVM_DIR="$("$c" --cmakedir)" >/dev/null 2>&1 \
                && cmake --build "$build/auto" >/dev/null 2>&1; then
                cp "$build/auto/QArmAuto.so" "$prefix/lib/qarm/QArmAuto-llvm$v.so"
            fi
            break
        fi
    done
fi
for f in qarm_exec.py qarm_vqe.py qarm_evolve.py; do cp "$ROOT/$f" "$prefix/share/qarm/"; done
for f in qarm_oracle.py qarm_patch.py qarm_patch_x86.py; do cp "$ROOT/tools/$f" "$prefix/share/qarm/tools/"; done
cp "$ROOT"/examples/qarm/*.c "$ROOT"/examples/qarm/*.h "$prefix/share/qarm/examples/" 2>/dev/null || true
cp "$ROOT/tests/conformance/qarm_conformance.c" "$ROOT/tests/conformance/README.md" "$prefix/share/qarm/conformance/"
cp "$ROOT/config.example.json" "$prefix/share/qarm/"
cp "$ROOT/docs/qarm_isa.md" "$ROOT/docs/qarm_runtime_guide.md" "$ROOT/docs/qarm_v2.md" "$ROOT/LICENSE" "$ROOT/NOTICE" \
   "$prefix/share/doc/qarm/"
if [ -n "$bundle" ]; then
    mkdir -p "$prefix/libexec/qarm"
    for a in aarch64 x86_64; do
        [ -x "$bundle/bin/qemu-$a" ] && cp "$bundle/bin/qemu-$a" "$prefix/libexec/qarm/"
    done
    cp "$ROOT/packaging/QEMU-LICENSE.md" "$prefix/share/doc/qarm/"
fi
echo "Q-ARM $version installed in $prefix: $prefix/bin/qarm-run --info"
