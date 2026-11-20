# Output stub for x86-64 (System V calling convention), AT&T syntax.
# What printf / puts / putchar are bound to when an x86-64 program runs on the quantum machine.
# Same record format as the other stubs (output_stub.s):
#   [DATA_BASE]         record count
#   [DATA_BASE + 32*k]  kind (1 printf, 2 puts, 3 putchar), then seven 32-bit values:
#                       rdi, rsi, rdx, rcx, r8, r9 and the first stack argument
# Assembled once; the bytes are in qcpu/loader.py (OUTPUT_STUB_X86). The capacity (0x7ff here) is patched.
# Return values: putchar returns its argument, puts returns 0, printf returns 0.
# Scratch registers: rax, r10, r11 (not used for arguments).
        .text
printf_:  movl  $1, %r10d
          xorl  %r11d, %r11d              # printf returns 0 (the character count is not known on the QPU)
          jmp   common
puts_:    movl  $2, %r10d
          xorl  %r11d, %r11d              # puts returns 0 (a non-negative value, as C requires)
          jmp   common
putchar_: movl  $3, %r10d
          movzbl %dil, %r11d              # putchar returns the character written
common:   movl  $0x10000000, %eax         # rax = DATA_BASE
          cmpl  $0x7ff, (%rax)            # capacity
          jae   done                      # log full: drop the call
          incl  (%rax)
          movl  (%rax), %eax              # new count
          shll  $5, %eax
          addl  $0x10000000, %eax         # record address
          movl  %r10d, (%rax)
          movl  %edi, 4(%rax)
          movl  %esi, 8(%rax)
          movl  %edx, 12(%rax)
          movl  %ecx, 16(%rax)
          movl  %r8d, 20(%rax)
          movl  %r9d, 24(%rax)
          movl  8(%rsp), %r10d            # seventh value: the first argument passed on the stack
          movl  %r10d, 28(%rax)
done:     movl  %r11d, %eax
          retq
