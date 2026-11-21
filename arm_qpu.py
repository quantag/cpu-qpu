#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Run an AArch64 binary on a quantum stored-program (von Neumann) processor.

    python3 arm_qpu.py hello.o
    python3 arm_qpu.py factor.o --entry factorize --args 84 '&0'
    python3 arm_qpu.py hello.o -v

This is the original command name, kept so that existing scripts keep working. It is the same tool
as cpu_qpu.py, which also runs RISC-V binaries (see its --help).
"""
from cpu_qpu import *                                          # noqa: F401,F403
from cpu_qpu import main, load_config, compile_c, find_compiler  # noqa: F401

if __name__ == "__main__":
    main()
