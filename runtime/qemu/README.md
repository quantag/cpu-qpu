<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Quantag IT Solutions GmbH -->

# Q-ARM for QEMU

A TCG plugin that runs Q-ARM programs in `qemu-aarch64`: the ordinary arm64 build, with Linux's C library,
and its Q-ARM packets executed by libqarm (`../libqarm`). Design: [`docs/qarm_v2.md`](../../docs/qarm_v2.md),
section 8.

## QEMU 10.1 or newer, with plugins

Distributions ship older versions (Ubuntu 24.04 ships 8.2), so build it. Only the arm64 user-mode emulator is needed,
and it takes a few minutes:

```
sudo apt-get install ninja-build libglib2.0-dev python3-venv gcc-aarch64-linux-gnu libc6-dev-arm64-cross libcurl4-openssl-dev
git clone --depth 1 --branch v10.2.4 https://github.com/qemu/qemu.git qemu-src
mkdir qemu-src/build && cd qemu-src/build
../configure --prefix=$HOME/qemu --target-list=aarch64-linux-user --enable-plugins --disable-docs --disable-tools
ninja install                      # $HOME/qemu/bin/qemu-aarch64, $HOME/qemu/include/qemu-plugin.h
```

## The plugin

```
cmake -S runtime/qemu -B build/qemu -DQEMU_PLUGIN_INCLUDE=$HOME/qemu/include
cmake --build build/qemu           # build/qemu/libqarm-qemu.so
```

## Running a program

```
aarch64-linux-gnu-gcc -O1 -iquote include examples/qarm/h2_vqe.c -o h2_vqe -lm
~/qemu/bin/qemu-aarch64 -L /usr/aarch64-linux-gnu -plugin build/qemu/libqarm-qemu.so ./h2_vqe
~/qemu/bin/qemu-aarch64 -L /usr/aarch64-linux-gnu -plugin build/qemu/libqarm-qemu.so,config=config.json ./h2_vqe
```

| option | meaning |
|---|---|
| `config=FILE` | the backend configuration, `config.json`'s format (as `QARM_CONFIG`) |
| `backend=NAME` | `exact` (the default), `simulator` or `ibm` (as `QARM_BACKEND`) |
| `quiet=on` | no `[qarm: ...]` reports |

`-L` is for an x86-64 host; on an arm64 Linux host leave it out. QEMU's user mode runs Linux programs on Linux: on a
Mac, use a Linux VM or container.

Test: `QARM_QEMU=$HOME/qemu/bin/qemu-aarch64 python3 -m tests.test_qarm_qemu`.
