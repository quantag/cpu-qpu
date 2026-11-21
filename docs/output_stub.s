// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
// Output stub: what printf / puts / putchar are bound to when a program runs on arm-qpu.
// These instructions execute on the quantum machine (and on the CPU reference). Each call
// appends a 32-byte record to the output log at the start of data memory (DATA_BASE):
//   [DATA_BASE]         record count
//   [DATA_BASE + 32*k]  kind (1 printf, 2 puts, 3 putchar), w0..w6
// arm_qpu.py reads the log after the run and formats the text (qcpu/console.py).
// Assembled once; the words are in qcpu/loader.py (OUTPUT_STUB). "capacity" is patched.
// Return values: putchar returns its argument, puts returns 0; printf returns 0, not the number
// of characters, because the text is only formatted after the run.
        .text
printf_:  mov  w12, #1
          mov  w13, #0                   // printf returns 0 (the character count is not known on the QPU)
          b    common
puts_:    mov  w12, #2
          mov  w13, #0                   // puts returns 0 (a non-negative value, as C requires)
          b    common
putchar_: mov  w12, #3
          and  w13, w0, #0xff            // putchar returns the character written
          b    common
common:   movz x9, #0x1000, lsl #16      // x9 = DATA_BASE (0x10000000)
          ldr  w10, [x9]                 // count
          cmp  w10, #4095                // capacity
          b.hs 1f                        // log full: drop the call
          add  w11, w10, #1
          str  w11, [x9]
          add  x10, x9, x11, lsl #5      // record address
          str  w12, [x10]
          str  w0, [x10, #4]
          str  w1, [x10, #8]
          str  w2, [x10, #12]
          str  w3, [x10, #16]
          str  w4, [x10, #20]
          str  w5, [x10, #24]
          str  w6, [x10, #28]
1:        mov  w0, w13
          ret
// Apple arm64 ABI: variadic arguments are passed on the stack, not in x1..x7
apple_printf_:
          ldr  w1, [sp]
          ldr  w2, [sp, #8]
          ldr  w3, [sp, #16]
          ldr  w4, [sp, #24]
          ldr  w5, [sp, #32]
          ldr  w6, [sp, #40]
          b    printf_
