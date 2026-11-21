# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
# Output stub for RISC-V (RV32IM and RV64IM; the instruction words are the same for both).
# What printf / puts / putchar are bound to when a RISC-V program runs on the quantum machine.
# Same record format as the AArch64 stub in output_stub.s:
#   [DATA_BASE]         record count
#   [DATA_BASE + 32*k]  kind (1 printf, 2 puts, 3 putchar), a0..a6
# Variadic integer arguments are passed in a1..a7 like ordinary arguments.
# Assembled once; the words are in qcpu/loader.py (OUTPUT_STUB_RV). "capacity" is patched.
# Return values: putchar returns its argument, puts returns 0, printf returns 0.
        .text
printf_:  li   t0, 1
          li   t1, 0                  # printf returns 0 (the character count is not known on the QPU)
          j    common
puts_:    li   t0, 2
          li   t1, 0                  # puts returns 0 (a non-negative value, as C requires)
          j    common
putchar_: li   t0, 3
          andi t1, a0, 0xff           # putchar returns the character written
          j    common
common:   lui  t2, 0x10000            # t2 = DATA_BASE (0x10000000)
          lw   t3, 0(t2)              # count
          li   t4, 2047               # capacity
          bgeu t3, t4, 1f             # log full: drop the call
          addi t3, t3, 1
          sw   t3, 0(t2)
          slli t3, t3, 5
          add  t3, t3, t2             # record address
          sw   t0, 0(t3)
          sw   a0, 4(t3)
          sw   a1, 8(t3)
          sw   a2, 12(t3)
          sw   a3, 16(t3)
          sw   a4, 20(t3)
          sw   a5, 24(t3)
          sw   a6, 28(t3)
1:        mv   a0, t1
          ret
