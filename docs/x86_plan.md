# x86-64 on the quantum machine: design and plan

Status: in progress on branch `x86`.

- Done: `qcpu/x86.py` (instruction table, byte decoder, W-bit model of every instruction),
  `qcpu/x86_reference.py` (Unicorn reference with the per-instruction model check), ELF x86-64 loading
  and relocations, the output stub (`docs/output_stub_x86.s`). The decoder gives the same instruction
  lengths as capstone on all 2 396 instructions of the compiled examples (clang -O0/-O1/-O2, gcc -O1),
  and the model agrees with the CPU on every instruction of 13 compiled functions at W = 12 and 16.
- Next: the circuit `qcpu/x86_machine.py`, which implements the same decode and the same model with
  gates; then the command-line tool, tests and README.

Decisions taken (see the docstrings of `x86.py` and `x86_reference.py` for details):

- Program memory is bytes; the PC is a byte offset; the circuit fetches an 11-byte window, which
  holds the low 16 bits of every field of any supported instruction.
- 8-, 32- and 64-bit operand sizes. Byte operands must be registers. No 16-bit size, no other prefix.
- Registers are W bits. A byte write keeps the upper bits; 32-bit and 64-bit writes replace all W bits.
- Flags ZF, SF, CF, OF. CF and OF after shifts and all four after IMUL and DIV/IDIV are not modelled:
  the machine leaves them unchanged, and the reference reports a run that later reads one.
- DIV/IDIV use RAX only; RDX must be its zero or sign extension (the reference checks the result).
  Division by zero traps.
- Calls use the stack: the run starts with RSP pointing at a return address that leads to an HLT in
  the last byte of program memory, so every x86-64 run needs data memory.
- The model check compares every register, flag, memory cell and the next PC with the CPU after each
  instruction, in their low W bits, and checks that written values fit at the operation's width.

The rest of this note is the original plan.

## Why it is a separate step

| | AArch64 / RISC-V | x86-64 |
|---|---|---|
| instruction length | always 4 bytes | 1 to 15 bytes |
| program counter | counts instructions | counts bytes |
| operands | registers, one small immediate | register or memory in almost every instruction |
| result | separate destination register | overwrites the first operand |
| calls | return address in a register | return address pushed on the stack |
| flags | NZCV, set by a few instructions (RISC-V: none) | ZF/SF/CF/OF set by almost every instruction |
| register writes | whole register | also 8- and 16-bit parts (AL, AX) |

## Circuit

1. **Program memory in bytes.** `SLOTS` bytes of 8 qubits; the PC is a byte offset.
2. **Fetch window.** One address decode of the PC, then 11 byte lanes: lane k reads byte PC + k.
   Cost: about 8 x 11 x SLOTS Toffoli gates per cycle.
3. **Normalisation.** Conditional byte shifts (controlled swaps) bring every instruction into one
   layout: REX | opcode | ModRM | SIB | 32-bit displacement | 32-bit immediate. The controls are
   "has a REX prefix", "two-byte opcode (0F)", "has a SIB byte" and "displacement size", all decoded
   from the window. The instruction length falls out of the same signals and is added to the PC.
4. **Decode.** Exact (mask, value) patterns on the normalised bytes, as for RISC-V; anything else traps.
5. **Effective address.** base + index x scale + displacement, or RIP + displacement.
6. **Execute.** One memory read, the ALU from the shared core, one result written to a register or
   to memory, flags updated. Register writes of 8 bits keep the upper bits.
7. **Stack.** PUSH / POP / CALL / RET read or write memory at RSP and change RSP. The run starts with
   a return address on the stack that points to an HLT byte at the end of program memory.

## First instruction subset (what clang and gcc emit for integer code at -O1)

MOV (register, memory, immediate), MOVSXD, MOVZX/MOVSX (register), LEA, ADD, SUB, AND, OR, XOR, CMP,
TEST, NEG, NOT, INC, DEC, IMUL (two- and three-operand), CDQ/CQO + IDIV, DIV, SHL/SHR/SAR (immediate
and CL), SETcc, CMOVcc, Jcc (rel8, rel32), JMP (rel8, rel32, indirect), CALL (rel32, indirect), RET,
PUSH, POP, NOP (including the multi-byte forms), HLT (ends the run).

Not in the first subset: 16-bit operand size, byte and word memory access, string instructions,
x87 / SSE / AVX, segment registers, system instructions.

## Classical side

- Checker and disassembler: capstone (`CS_ARCH_X86`); Unicorn (`UC_ARCH_X86`, 64-bit mode) as the
  CPU reference, with the same per-instruction model check as for RISC-V (`rv_reference.py`).
- Loader: ELF x86-64 relocations PLT32 / PC32 (calls and RIP-relative data), 32 / 32S / 64.
- Output stub for `printf` / `puts` / `putchar` in x86-64 code (System V calling convention).
- Compiler flags: `--target=x86_64-linux-gnu -fno-pic -mno-sse -fno-stack-protector`, and no loop
  alignment so that padding NOPs stay rare.

## W-bit model

The same as for the other architectures. Extra points for x86-64: a 32-bit operation clears the upper
half of the 64-bit register, so a negative 32-bit result read as a 64-bit value is outside the model
(the same situation as W and X registers on AArch64); return addresses on the stack are code
pointers held as offsets.

## Order of work

1. Byte-level fetch, normalisation and length decode, tested alone against capstone's lengths.
2. Register-only subset (MOV, LEA, arithmetic, logic, shifts, flags, Jcc, SETcc, CMOVcc), fuzzed
   against Unicorn after every cycle.
3. Memory operands and the stack (PUSH, POP, CALL, RET), then IMUL / IDIV / DIV.
4. Loader, output stub, the C examples and the C test suite.
5. Fingerprint and report entries; README.

## Expected cost

Per cycle, the x86-64 front end is larger than the AArch64 one: the byte-level fetch, the
normalisation network, four flags written by most instructions, and memory read and write in the
same cycle. The number of cycles for the same C function is similar or lower, because x86-64 code
is denser. A measured comparison will go into the README table once it runs.
