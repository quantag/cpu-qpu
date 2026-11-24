# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
# make            build libmathlib_qpu.so (functions on the QPU) and two host programs
# make run        run the host program; mathlib calls go to the QPU
# make ARCH=x86_64 run    QPU code for another architecture: arm64 riscv64 riscv32 x86_64
ROOT := ../..
ARCH ?= arm64

all: main_qpu main_cpu

libmathlib_qpu.so: mathlib.c mathlib.h
	python3 $(ROOT)/qpu_dll.py mathlib.c --header mathlib.h --arch $(ARCH) -o $@

main_qpu: main.c mathlib.h libmathlib_qpu.so
	$(CC) -Wall -I $(ROOT)/runtime main.c -L. -lmathlib_qpu -Wl,-rpath,'$$ORIGIN' -o $@

main_cpu: main.c mathlib.c mathlib.h
	$(CC) -Wall -I $(ROOT)/runtime main.c mathlib.c -o $@

run: main_qpu
	./main_qpu

clean:
	rm -f libmathlib_qpu.so main_qpu main_cpu

.PHONY: all run clean
