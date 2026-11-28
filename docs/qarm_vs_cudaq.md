# Q-ARM and CUDA-Q

Both put quantum work into ordinary C/C++ programs, but they start from opposite ends. This note says
where each is ahead, as of November 2026. CUDA-Q facts are from NVIDIA's documentation
(nvidia.github.io/cuda-quantum).

## The difference in one line

- **CUDA-Q:** you write a quantum kernel, and a compiler lowers it to a target.
- **Q-ARM:** you write, or already have, classical code. An instruction names the algorithm, and the
  runtime builds the quantum part from the program's machine code.

## Side by side

| | CUDA-Q | Q-ARM |
|---|---|---|
| What the programmer writes | quantum kernels: gates, measurements, control flow (`__qpu__`, `@cudaq.kernel`) | a C function (`int f(int)`), or tables (Hamiltonian, ansatz, circuit), and one call: `qarm_search(f, …)`, `qarm_min`, `qarm_period`, `qarm_expect_n`, … |
| Oracles | written by hand as kernels | built from the function's compiled AArch64 or x86-64 code (qoracle) and verified on every input |
| Where the quantum part lives | MLIR (Quake) and QIR, produced by `nvq++` or the Python JIT | packets in the program's own machine code: 8 words that a plain CPU branches over |
| Without quantum hardware or a simulator | a kernel needs a target | the same binary runs on any AArch64 or x86-64 CPU, on its classical path |
| Answers | as returned by the target | checked in the program by calling the original function, where a check exists; otherwise the classical path |
| Binaries you cannot rebuild | — | `tools/qarm_patch.py` turns a brute-force search or minimum in a finished ELF or Mach-O into `QSEARCH` or `QMIN` |
| Specification | the CUDA-Q language specification (a C++ and Python API) | `docs/qarm_isa.md`: a binary ISA (packet, register ABI, tables, guarantees), a conformance program, profiles |
| Runtimes | NVIDIA's simulators and the hardware back ends of its targets | any program that executes packets: `qarm_exec.py` (Unicorn), a QEMU plugin, libqarm (native C); in future, a CPU extension or a hypervisor |
| Asynchrony | `sample_async`, `observe_async`, `run_async`, `get_state_async` | `QSUBMIT`, `QPOLL`, `QWAIT` on any instruction; QPU jobs stay queued while the program runs |
| Simulation scale | GPU state vectors and tensor networks, multi-GPU and multi-node | a CPU state vector: about 20 qubits for VQE, and 2^bits amplitudes for oracle searches (the oracle itself is evaluated classically) |
| Hardware | many vendors through targets (IonQ, IQM, OQC, Quantinuum, …) | IBM Quantum (REST, from C or from Python) |
| Algorithms as library calls | VQE, optimizers, gradients, dynamics (CUDA-Q libraries) | Grover, Dürr–Høyer, counting, amplitude estimation, period finding, Simon, Bernstein–Vazirani, Brassard–Høyer–Tapp, VQE with QGRAD, QAOA, Trotter evolution |

## Where Q-ARM is ahead

1. **The program does not change.** A search loop over a C predicate becomes a quantum search. No one
   writes the oracle: it comes from the same machine code that the CPU runs, and it is checked against
   that code.
2. **One binary for every machine.** The packets are no-ops on a plain CPU. A program built today runs
   unchanged on a laptop, under an emulator with a simulator, and on a QPU-backed runtime.
3. **Answers are verified by the program's own code.** A wrong quantum answer cannot reach the caller
   where a check exists: search, minimum, period, shift, collision.
4. **An open binary standard.** Anyone can implement a runtime, and the conformance program says
   whether it is right. That runtime could be an emulator, a hypervisor, an OS service or a future CPU
   with a quantum coprocessor.
5. **Existing software.** Binary rewriting reaches code whose sources are gone.

## Where CUDA-Q is ahead

1. **Scale and speed of simulation.** GPUs, multiple nodes, tensor networks. Q-ARM's simulators are CPU
   only.
2. **Hardware reach.** Many vendors. Q-ARM speaks to IBM only.
3. **Hand-written kernels.** For algorithms that have no classical function to start from (error
   correction, dynamics, custom ansätze), the kernel language is the natural form. Q-ARM's `QCIRC` takes
   circuit tables, but it is not a kernel language.
4. **Maturity.** CUDA-Q has a compiler (MLIR), optimizations, a community and libraries.

## What limits Q-ARM today, honestly

- **Oracle size.** Oracles built from machine code keep the CPU's register widths. A 12-bit key check
  takes 92 qubits; Code Breaker's predicate takes about 1,100. Searches therefore run on simulators,
  evaluated classically. Only small oracles fit today's devices, and even those are deep after routing.
- **The quantum speedups are asymptotic.** At the sizes that fit a simulator, a classical loop is
  faster. What the examples show is that the same binary is ready for hardware that does not exist yet.
- **AArch64 and x86-64** only, for oracles and packets. Oracle functions need `-O1` without vector
  instructions; on x86-64 the runtime is the QEMU plugin, since `qarm_exec.py` emulates AArch64.

## Where they meet

The two are complementary rather than exclusive, and they already work together. With
`qarm_exec.py --backend cudaq` (`--cudaq-target nvidia` for a GPU), CUDA-Q is the coprocessor of a Q-ARM
binary:
- `QVLOAD` builds a CUDA-Q kernel from the ansatz table, with Pauli rotations as `exp_pauli`, and a spin
  operator from the Hamiltonian;
- `QEXPECT` and `QEXPECTN` are `observe`;
- `QVSAMPLE`, `QCIRC` and `QRAND` are `sample`.

`h2_vqe.c`, `maxcut_qaoa.c`, `spin_chain.c` and `ghz.c` run on it unchanged and give the exact
simulator's answers. The oracle instructions stay on Q-ARM's own simulation, because their circuits are
far too wide for a state vector.

That puts CUDA-Q's simulators and targets behind Q-ARM's binaries and fallbacks.
