#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Run a compiled binary on a quantum stored-program (von Neumann) processor.

    python3 cpu_qpu.py hello.o                          # runs main(); prints the program's output
    python3 cpu_qpu.py factor.o --entry factorize --args 84 '&0'
    python3 cpu_qpu.py hello.o -v                       # + CPU/QPU comparison, resources, report
    python3 cpu_qpu.py hello.c --arch riscv64           # compile a C file for an architecture, then run it
    python3 cpu_qpu.py hello.c --arch x86_64

Architectures: AArch64 (arm64), RISC-V (riscv64 = RV64IM, riscv32 = RV32IM), x86-64 (x86_64). The
architecture of an object file is read from its header; for a C file or a raw binary it comes from
--arch (default: arm64).

The machine code runs on the quantum interpreter circuit of that architecture (backend from
config.json: simulator | aer | ibm) and, as a reference, on a CPU emulator (Unicorn). printf, puts
and putchar work: the program's output is printed as if it had run locally.
With -v / --report: verdict, resources and a Markdown + JSON report.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qcpu.loader import ObjectImage, LinkError
from qcpu.bitsim import value, Streamer
from qcpu import console, isa as isas
from qcpu.reference import DATA_BASE, CODE_BASE

DEFAULTS = {
    "backend": "simulator",          # simulator | aer | ibm
    "arch": "arm64",                 # architecture for .c input and raw binaries: arm64 | riscv64 | riscv32 | x86_64
    "register_width": "auto",        # W: bits per register; auto = smallest W >= 8 the code layout allows
    "registers": "auto",             # auto | all | [0, 1, 2, ...]
    "memory_words": "auto",          # data memory cells (4 bytes each); auto = sized to log + data + stack
    "max_steps": "auto",             # auto = number of steps the CPU reference needs
    "isa": "full",                   # full = every implemented class in the decoder | auto = only classes used
    "memory_select": "tree",         # tree = binary decoder per address | comparator = one comparator per slot (the original circuit)
    "compiler": "auto",              # for .c input: auto | clang | gcc | a command name / path
    "cflags": ["-O1", "-fno-stack-protector"],
    "output_records": 32,            # printf/puts/putchar calls kept in the output log
    "stack_bytes": 256,
    "shots": 1024,
    "aer": {"method": "matrix_product_state"},
    "ibm": {"channel": "ibm_quantum_platform", "token": "env:IBM_QUANTUM_TOKEN", "instance": None,
            "backend": "least_busy", "optimization_level": 1},
}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        user = json.load(open(path))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def find_compiler(choice, isa):
    """Return the command prefix that compiles C to an object file for the architecture isa."""
    clang = shutil.which("clang")
    if not clang and os.name == "nt":        # LLVM's Windows installer does not add itself to PATH
        for d in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            cand = os.path.join(d, "LLVM", "bin", "clang.exe")
            if d and os.path.exists(cand):
                clang = cand
                break
    if choice in ("auto", "clang") and clang:
        return [clang] + isa.clang
    if choice in ("auto", "gcc"):
        for name in isa.gcc:
            if shutil.which(name):
                return [name] + isa.gcc_flags
        if choice == "gcc":
            sys.exit(f"error: no {isa.title} gcc found (tried " + ", ".join(isa.gcc) + "); see README")
    if choice not in ("auto", "clang", "gcc") and shutil.which(choice):
        return [choice] + (isa.clang if "clang" in os.path.basename(choice) else isa.gcc_flags)
    sys.exit(f"error: compiler '{choice}' not found; install clang or an {isa.title} gcc (see README)")


def compile_c(src, choice, cflags, arch="arm64"):
    cc = find_compiler(choice, isas.get(arch))
    out = os.path.join(tempfile.mkdtemp(prefix="cpu_qpu_"), os.path.splitext(os.path.basename(src))[0] + ".o")
    inc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "include")
    rt = os.path.join(os.path.dirname(inc), "runtime")              # qpu_dll.h, for QPU library sources
    cmd = cc + list(cflags) + ["-I" + inc, "-I" + rt, "-c", src, "-o", out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        sys.exit("compile failed: " + " ".join(cmd) + "\n" + p.stderr)
    return out, " ".join(cmd)


def sx(v, W):
    v &= (1 << W) - 1
    return v - (1 << W) if v >> (W - 1) else v


def parse_val(s):
    s = str(s).strip()
    if s.startswith("&"):
        return ("ptr", int(s[1:], 0))
    return ("int", int(s, 0))


def execute(binary, isa, cfg, entry=None, arg_strs=(), mem_init=(), source=None, compiled_with=None,
            machine_cache=None, round_steps=False, qpu=True):
    """Link a binary, run the entry function on the CPU reference and on the quantum machine, compare.
    Returns a dict: report (as written by -v), prog, ref (CPU), q (QPU state), W, ok (QPU == CPU in
    every register, flag, memory cell and PC), exact (no model-check issue), ret (QPU return value),
    qtext (program output), mem (final data cells), user_off (byte offset of the user cells),
    stop (None, or the exit code when the run could not be completed).
    Used by the command line and by qpu_server.py (machine_cache: reuse circuits between calls)."""
    # ---- link: output log at data offset 0, then the program's data sections
    try:
        obj = ObjectImage(binary, isa.name)
        probe = obj.link(entry)
        records = int(cfg["output_records"]) if probe["uses_output"] else 0
        log_bytes = console.RECORD * (records + 1) if probe["uses_output"] else 0
        prog = obj.link(entry, data_offset=log_bytes, log_capacity=records)
    except LinkError as e:
        sys.exit(f"error: {e}")
    code = prog["words"]
    n = len(code)
    slots = isa.min_slots
    while slots < n + 1:
        slots *= 2
    words = isa.layout(code, slots)
    listing = isa.listing(code, slots, CODE_BASE)       # one entry per instruction
    checks = [e["check"] for e in listing]
    names_at = {v: k for k, v in prog["functions"].items()}

    # ---- data memory: [output log][data sections][user cells][stack]
    raw_args = [parse_val(x) for x in arg_strs]
    mem_init = list(mem_init)
    user_off = (prog["data_end"] + 15) // 16 * 16
    user_cells = max(len(mem_init), 16 if any(t == "ptr" for t, _ in raw_args) else 0)
    uses_mem = (any(c.ok and c.cls in isa.MEMOPS for c in checks) or user_cells or prog["uses_output"]
                or prog["data_end"] > prog["data_offset"] or isa.always_mem)
    stack = int(cfg["stack_bytes"]) if uses_mem else 0
    need = user_off + 4 * user_cells + stack + (64 if uses_mem else 0)
    if cfg["memory_words"] == "auto":
        MEM = 0
        if uses_mem:
            MEM = 16
            while 4 * MEM < need: MEM *= 2
    else:
        MEM = int(cfg["memory_words"])
        if 4 * MEM < need:
            sys.exit(f"error: memory_words = {MEM} is too small; the layout needs {need} bytes")
    need_w = max((isa.unit * slots - 1).bit_length() + 1, (4 * MEM).bit_length() if MEM else 0)
    W = max(8, need_w) if cfg["register_width"] == "auto" else int(cfg["register_width"])
    if W < need_w:
        sys.exit(f"error: code/data addresses need register_width >= {need_w} (it is {W})")
    if W > 16:
        sys.exit(f"error: program too large ({n} {'instructions' if isa.unit == 4 else 'bytes of code'}, "
                 f"{4 * MEM} bytes of data) for the W <= 16 machine")
    cells = [0] * MEM
    img = prog["data_image"]
    for i in range(0, len(img), 4):
        cells[(prog["data_offset"] + i) // 4] = int.from_bytes(img[i:i + 4], "little")
    for i, v in enumerate(mem_init):
        cells[user_off // 4 + i] = v & 0xFFFFFFFF
    image = bytes(b"".join((c & 0xFFFFFFFF).to_bytes(4, "little") for c in cells))
    sp_off = 4 * MEM - 64 if MEM else 0
    cpu_args = [DATA_BASE + user_off + 4 * v if t == "ptr" else v for t, v in raw_args]
    qpu_args = [user_off + 4 * v if t == "ptr" else v for t, v in raw_args]

    # registers to instantiate: arguments, return value, link register / stack pointer, and what the code touches
    used = {isa.arg_regs[i] for i in range(len(raw_args))} | {isa.ret_reg} | set(isa.entry_regs)
    for c in checks:
        if c.ok:
            used |= set(c.reads) | set(c.writes)
    classes = isa.ALL_CLASSES if cfg["isa"] == "full" else tuple({c.cls for c in checks if c.ok} | {isa.halt_class})

    # ---- CPU reference
    ref = isa.run_reference(words, W, prog["entry_index"], cpu_args, MEM, cells, sp_off=sp_off, snapshots=True)
    executed = {}
    for i in ref["trace"]:
        executed[i] = executed.get(i, 0) + 1
    steps = ref["steps"] if cfg["max_steps"] == "auto" else int(cfg["max_steps"])
    if round_steps:                     # fewer distinct machines, so a cached circuit can serve many calls
        steps = max(16, 1 << (max(steps, 1) - 1).bit_length())
    report = dict(arch=dict(name=isa.name, title=isa.title, ret=isa.ret_label, flags=isa.flag_names), binary=binary, source=source,
                  compiled_with=compiled_with, entry=prog["entry"],
                  entry_index=prog["entry_index"], args=list(arg_strs), memory_init=mem_init, config=cfg,
                  unresolved_relocations=prog["unresolved"], externs=prog["externs"],
                  layout=dict(output_log=[0, log_bytes], data=[prog["data_offset"], prog["data_end"]],
                              user=[user_off, user_off + 4 * user_cells], stack_top=sp_off, memory_bytes=4 * MEM))
    report["code"] = [dict(index=e["index"], address=f"0x{CODE_BASE + isa.unit * e['index']:x}", word=e["word"], asm=e["asm"],
                           function=names_at.get(e["index"]), cls=e["check"].cls, supported=e["check"].ok,
                           reason=e["check"].reason, executed=executed.get(e["index"], 0)) for e in listing]
    report["unsupported"] = [x for x in report["code"] if not x["supported"]]
    report["cpu"] = dict(steps=ref["steps"], halted=ref["halted"], error=ref["error"], issues=ref["issues"],
                         x0=ref["regs"][isa.ret_reg])
    ptr_off = lambda v: v - 0x10000000 if v >= 0x10000000 else v
    if prog["uses_output"]:
        report["cpu"]["output"], _ = console.render(lambda i: ref["mem"][i], image, ptr_off, records)
    if prog["unresolved"]:
        print("warning: unresolved symbols/relocations: " + "; ".join(prog["unresolved"]), file=sys.stderr)
    ctx = dict(report=report, prog=prog, ref=ref, W=W, records=records, user_off=user_off, stop=None)
    if ref["error"]:
        report["verdict"] = f"NOT RUN on QPU: {ref['error']}"
        ctx["stop"] = 1
        return ctx
    if not qpu:                         # CPU reference only
        report["backend"] = dict(name="cpu", note="CPU reference only; the quantum machine was not run")
        ctx.update(q=None, ok=None, exact=not ref["issues"], qtext=report["cpu"].get("output"),
                   ret=sx(ref["regs"][isa.ret_reg], W), mem=[sx(v, W) for v in ref["mem"]])
        return ctx

    # ---- quantum machine
    t0 = time.time()
    if cfg["memory_select"] not in ("tree", "comparator"):
        sys.exit(f"error: memory_select must be 'tree' or 'comparator' (it is '{cfg['memory_select']}')")
    key = (isa.name, W, tuple(sorted(used)), slots, steps, MEM, tuple(classes), str(cfg["registers"]), cfg["memory_select"])
    if machine_cache is not None and key in machine_cache:
        m = machine_cache[key]
    else:
        try:
            m = isa.Machine(isa.config(W, used, slots, steps, MEM, classes, registers=cfg["registers"],
                                       select=cfg["memory_select"]), defer=True)
        except ValueError as e:
            sys.exit(f"error: {e}")
        if machine_cache is not None:
            machine_cache[key] = m
    init = {isa.arg_regs[i]: v for i, v in enumerate(qpu_args)}
    # the entry function returns to the halt slot: return address in the link register or on the stack
    init, cells0 = isa.entry(init, cells, slots, sp_off, isa.sp_reg in m.X)
    ones = m.load_bits(words, init, cells0, pc=prog["entry_index"])
    backend = cfg["backend"]
    if backend == "simulator":
        mk = (1 << W) - 1
        lock = dict(cycles=0, first_divergence=None)

        def on_cycle(k, st):
            # lockstep check: machine state after cycle k == CPU state after instruction k
            if k >= len(ref["snapshots"]) or lock["first_divergence"]:
                return
            sn = ref["snapshots"][k]
            for r, q in m.X.items():
                if value(st, q) != sn["regs"][r] & mk:
                    lock["first_divergence"] = f"cycle {k}: {isa.reg_name(r)}"; return
            if [st[x] for x in m.nzcv] != sn["flags"]:
                lock["first_divergence"] = f"cycle {k}: {isa.flag_names or 'flags'}"; return
            for i, cell in enumerate(m.ram):
                if value(st, cell) != int.from_bytes(sn["mem"][4 * i:4 * i + 4], "little") & mk:
                    lock["first_divergence"] = f"cycle {k}: memory cell {i}"; return
            lock["cycles"] = k + 1
        out = m.run_fast(ones, on_cycle=on_cycle)
        report["lockstep"] = lock
        st = out["state"]
        q = dict(regs={k: value(st, m.X[k]) for k in m.X}, flags=[st[x] for x in m.nzcv],
                 mem=[value(st, x) for x in m.ram], run=out["run"], trap=out["trap"],
                 trace=out["trace"][:ref["steps"]], probability=1.0)
        report["backend"] = dict(name="simulator", note="exact simulation of the reversible circuit on its basis-state input")
    else:
        m.build()
        q = run_sampled(m, ones, cfg, backend, report, isa)
    gc = m.counts()
    report["machine"] = dict(W=W, registers=[isa.reg_name(k) for k in m.X], program_slots=slots,
                             steps=steps, data_memory_cells=MEM, decoder_classes=list(m.cfg.classes), slot_bits=8 * isa.unit,
                             memory_select=getattr(m.cfg, "select", "tree"),
                             qubits=getattr(m, "n_total", m.n), gates=gc, toffoli=gc.get("ccx", 0))
    report["backend"]["seconds"] = round(time.time() - t0, 2)
    if q is None:
        ctx["stop"] = 1
        return ctx

    mk = (1 << W) - 1
    rows = []
    for k in m.X:
        cpu = ref["regs"][k]
        rows.append(dict(reg=isa.reg_name(k), cpu=cpu & mk, qpu=q["regs"][k],
                         cpu_signed=sx(cpu, W), qpu_signed=sx(q["regs"][k], W), match=(cpu & mk) == q["regs"][k]))
    mem_ok = [v & mk for v in ref["mem"]] == q["mem"]
    flags_ok = ref["flags"] == q["flags"]
    trace_ok = q.get("trace") is None or q["trace"] == ref["trace"]
    qtext = None
    if prog["uses_output"]:
        qtext, trunc = console.render(lambda i: sx(q["mem"][i], W) & 0xFFFFFFFF, image, ptr_off, records)
        report["output_truncated"] = trunc
    ok = all(r["match"] for r in rows) and mem_ok and flags_ok and trace_ok and q["run"] == 0 and not q["trap"]
    if report.get("lockstep"):
        ok = ok and report["lockstep"]["first_divergence"] is None
    report["result"] = dict(registers=rows, flags=dict(cpu=ref["flags"], qpu=q["flags"]), memory_match=mem_ok,
                            trace_match=trace_ok, halted=q["run"] == 0, trap=q["trap"], probability=q["probability"],
                            return_value=dict(cpu=sx(ref["regs"][isa.ret_reg], W), qpu=sx(q["regs"][isa.ret_reg], W)),
                            output=dict(qpu=qtext, cpu=report["cpu"].get("output"),
                                        match=qtext == report["cpu"].get("output")))
    exact = not ref["issues"]
    report["verdict"] = ("MATCH: the QPU produced the same result as the CPU" if ok else
                         "MISMATCH between QPU and CPU") + ("" if exact else
                         f" (warning: {len(ref['issues'])} value(s) did not fit the {W}-bit model, see model check)")
    ctx.update(q=q, ok=ok, exact=exact, qtext=qtext, ret=sx(q["regs"][isa.ret_reg], W),
               mem=[sx(v, W) for v in q["mem"]])
    return ctx


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", help="ELF/Mach-O object or executable (or raw .bin); a .c file is compiled first")
    ap.add_argument("--arch", help="architecture: " + " | ".join(isas.NAMES) + " (default: from the file header, else config 'arch')")
    ap.add_argument("--entry", help="function to run (default: main, else the first function)")
    ap.add_argument("--args", nargs="*", default=[], help="X0, X1, ... initial values; '&k' = pointer to user data cell k")
    ap.add_argument("--mem", default="", help="user data cells (32-bit ints), comma separated: --mem=1,2,3")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("-v", "--verbose", action="store_true", help="show the CPU/QPU comparison and resources; write a report")
    ap.add_argument("--report", help="write the report to PREFIX.md / PREFIX.json")
    ap.add_argument("--cc", help="compiler for .c input: clang | gcc | command (overrides config)")
    ap.add_argument("-O", dest="opt", help="optimization level for .c input: 0, 1, 2, s (overrides config cflags)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    say = (lambda *x: print(*x)) if a.verbose else (lambda *x: None)
    compiled_with = None
    source = a.binary
    try:
        found = None if a.binary.endswith(".c") else isas.detect(a.binary)
        if a.arch and found and isas.get(a.arch).name != found:
            sys.exit(f"error: {a.binary} is a {found} file, but --arch {a.arch} was given")
        isa = isas.get(a.arch or found or cfg["arch"])
    except (ValueError, OSError) as e:
        sys.exit(f"error: {e}")
    if a.binary.endswith(".c"):
        cflags = [f for f in cfg["cflags"] if not f.startswith("-O")] + [f"-O{a.opt}"] if a.opt else cfg["cflags"]
        obj_path, compiled_with = compile_c(a.binary, a.cc or cfg["compiler"], cflags, isa.name)
        say("compiled: " + compiled_with)
        default_prefix = os.path.splitext(a.binary)[0]
        a.binary = obj_path
    else:
        default_prefix = os.path.splitext(a.binary)[0]

    r = execute(a.binary, isa, cfg, a.entry, a.args,
                [int(x, 0) for x in a.mem.split(",") if x.strip()] if a.mem else [],
                source=source, compiled_with=compiled_with)
    if r["stop"] is not None:
        if r["ref"]["error"]:
            print(f"error: {r['ref']['error']}", file=sys.stderr)
        finish(r["report"], a, r["W"], default_prefix, r["records"])
        sys.exit(r["stop"])
    # ---- what the user sees: the program's own output, as if it ran locally
    report, prog, q, W, records = r["report"], r["prog"], r["q"], r["W"], r["records"]
    ok, exact, qtext, ret = r["ok"], r["exact"], r["qtext"], r["ret"]
    if prog["uses_output"]:
        sys.stdout.write(qtext)
        if qtext and not qtext.endswith("\n") and a.verbose:
            print()
        if report.get("output_truncated"):
            print(f"warning: output truncated after {records} printf/puts/putchar calls (config output_records)", file=sys.stderr)
    if prog["entry"] != "main" or not prog["uses_output"]:
        print(f"{prog['entry']}({', '.join(a.args)}) = {ret}")
    if not ok or not exact:
        print(f"warning: {report['verdict']} -- run with -v for details", file=sys.stderr)
    finish(report, a, W, default_prefix, records)
    sys.exit(ret & 0xFF if prog["entry"] == "main" else 0)


def run_sampled(m, ones, cfg, backend, report, isa):
    groups = {"r%d" % k: m.X[k] for k in m.X}
    groups["nzcv"] = m.nzcv
    groups.update({f"ram{i}": r for i, r in enumerate(m.ram)})
    groups["run"] = [m.run[-1]]
    groups["trap"] = m.trap
    layout_bits = [(g, q) for g, qs in groups.items() for q in qs]
    from qiskit import QuantumCircuit, ClassicalRegister
    qc = m.to_qiskit(ones)
    cr = ClassicalRegister(len(layout_bits), "out")
    qc.add_register(cr)
    for i, (_, qb) in enumerate(layout_bits):
        qc.measure(qb, cr[i])
    shots = int(cfg["shots"])
    if backend == "aer":
        from qiskit_aer import AerSimulator
        from qiskit import transpile
        sim = AerSimulator(method=cfg["aer"].get("method", "automatic"))
        tqc = transpile(qc, basis_gates=["x", "cx", "ccx", "cswap", "measure"], optimization_level=0)
        counts = sim.run(tqc, shots=shots).result().get_counts()
        report["backend"] = dict(name=f"aer ({cfg['aer'].get('method')})", shots=shots)
    elif backend == "ibm":
        from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2
        from qiskit.transpiler import generate_preset_pass_manager
        ic = cfg["ibm"]
        token = ic.get("token")
        if token and token.startswith("env:"):
            token = os.environ.get(token[4:])
        kw = dict(channel=ic.get("channel"))
        if token: kw["token"] = token
        if ic.get("instance"): kw["instance"] = ic["instance"]
        service = QiskitRuntimeService(**kw)
        bk = service.least_busy(operational=True, simulator=False) if ic.get("backend") in (None, "least_busy") \
            else service.backend(ic["backend"])
        report["backend"] = dict(name=f"ibm:{bk.name}", shots=shots, device_qubits=bk.num_qubits)
        if m.n > bk.num_qubits:
            report["backend"]["note"] = (f"NOT SUBMITTED: the circuit needs {m.n} qubits, {bk.name} has {bk.num_qubits}. "
                                         "Use a smaller register_width / isa=auto / fewer registers, or wait for larger devices.")
            report["verdict"] = "NOT RUN on QPU hardware: circuit larger than the device"
            return None
        pm = generate_preset_pass_manager(backend=bk, optimization_level=int(ic.get("optimization_level", 1)))
        isa = pm.run(qc)
        ops = isa.count_ops()
        report["backend"]["transpiled"] = dict(depth=isa.depth(), two_qubit_gates=sum(v for k, v in ops.items() if k in ("cz", "ecr", "cx")))
        job = SamplerV2(mode=bk).run([isa], shots=shots)
        report["backend"]["job_id"] = job.job_id()
        counts = job.result()[0].data.out.get_counts()
    else:
        raise SystemExit(f"unknown backend '{backend}'")
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: -kv[1])

    def decode(key):
        bits_ = key.replace(" ", "")[::-1]
        vals = {}
        for i, (g, _) in enumerate(layout_bits):
            vals.setdefault(g, []).append(int(bits_[i]))
        return vals
    best = decode(top[0][0])
    num = lambda bl: sum(b << i for i, b in enumerate(bl))
    report["backend"]["top_outcomes"] = [dict(probability=c / total, x0=sx(num(decode(k)["r%d" % isa.ret_reg]), m.cfg.W))
                                         for k, c in top[:5]]
    return dict(regs={k: num(best["r%d" % k]) for k in m.X}, flags=best.get("nzcv", []),
                mem=[num(best[f"ram{i}"]) for i in range(len(m.ram))], run=best["run"][0], trap=any(best["trap"]),
                trace=None, probability=top[0][1] / total)


def finish(report, a, W, default_prefix, records):
    if not (a.verbose or a.report):
        return
    prefix = a.report or f"{default_prefix}.{report['entry']}.report"
    json.dump(report, open(prefix + ".json", "w"), indent=2, default=str)
    open(prefix + ".md", "w").write(markdown(report, W))
    if a.verbose:
        print("-" * 60)
        print(report["verdict"])
        r = report.get("result")
        if r:
            print(f"return value {report['arch']['ret']}: CPU = {r['return_value']['cpu']}, QPU = {r['return_value']['qpu']}")
            if r["output"]["cpu"] is not None:
                print(f"program output: CPU and QPU {'identical' if r['output']['match'] else 'DIFFER'}")
        ls = report.get("lockstep")
        if ls:
            print("lockstep: " + (f"identical to the CPU after every one of {ls['cycles']} cycles" if not ls["first_divergence"]
                                  else f"first divergence at {ls['first_divergence']}"))
        mc = report.get("machine")
        if mc:
            print(f"quantum machine: {mc['qubits']} qubits, {sum(mc['gates'].values())} gates ({mc['toffoli']} Toffoli), "
                  f"{mc['steps']} steps, W = {mc['W']}")
        if report["unsupported"]:
            print(f"{len(report['unsupported'])} unsupported instruction(s) in the loaded code (see report)")
        print(f"report: {prefix}.md")


def markdown(r, W):
    arch = r.get("arch", dict(name="arm64", title="AArch64", ret="X0"))
    L = [f"# {'ARM' if arch['name'] == 'arm64' else arch['title']} on QPU: `{os.path.basename(r['binary'])}` :: `{r['entry']}`", "",
         f"**{r['verdict']}**", ""]
    enc = "bytes" if arch["name"] == "x86_64" else "word"
    res = r.get("result")
    if res:
        L += [f"Return value {arch['ret']}: CPU = **{res['return_value']['cpu']}**, QPU = **{res['return_value']['qpu']}**", ""]
    L += ["## Input", ""] + ([f"- source: `{r['source']}`, compiled with `{r['compiled_with']}`"] if r.get("compiled_with") else []) + [
          f"- binary: `{r['binary']}`, function `{r['entry']}` ({'byte offset' if arch['name'] == 'x86_64' else 'instruction'} {r['entry_index']})",
          f"- arguments: {' '.join(r['args']) or '(none)'}; data memory init: {r['memory_init'] or '(none)'}",
          f"- architecture: {arch['title']}; backend: `{r['config']['backend']}`, register width W = {W}", ""]
    out = (res or {}).get("output") or {}
    if out.get("qpu") is not None or r["cpu"].get("output") is not None:
        L += ["## Program output", "", f"QPU and CPU output {'identical' if out.get('match') else 'DIFFER'}.", "",
              "QPU:", "", "```", (out.get("qpu") or "").rstrip("\n"), "```", ""]
        if not out.get("match"):
            L += ["CPU:", "", "```", (r["cpu"].get("output") or "").rstrip("\n"), "```", ""]
    if r["unsupported"]:
        L += ["## Unsupported instructions", "",
              "These instructions are in the loaded code but not implemented by the quantum machine. "
              "A run that reaches one stops with a trap.", "",
              f"| # | address | {enc} | instruction | reason | executed by CPU |", "|---|---|---|---|---|---|"]
        for x in r["unsupported"]:
            L.append(f"| {x['index']} | {x['address']} | `{x['word']}` | `{x['asm']}` | {x['reason']} | {x['executed']} |")
        L.append("")
    if r.get("unresolved_relocations"):
        L += ["Unresolved relocations: " + "; ".join(r["unresolved_relocations"]), ""]
    if res:
        L += ["## Result: CPU vs QPU", "", "| register | CPU (low W bits, signed) | QPU | match |", "|---|---|---|---|"]
        for x in res["registers"]:
            L.append(f"| {x['reg']} | {x['cpu_signed']} | {x['qpu_signed']} | {'yes' if x['match'] else '**NO**'} |")
        fl = (arch.get("flags") or "NZCV") + " flags"
        L += [""] + ([f"- {fl}: CPU {res['flags']['cpu']}, QPU {res['flags']['qpu']}"] if res['flags']['cpu'] else []) + [
              f"- data memory equal: {res['memory_match']}",
              f"- control-flow trace (PC per step) equal: {res['trace_match']}",
              f"- lockstep (registers, flags and memory compared after every cycle): {r.get('lockstep')}",
              f"- halted: {res['halted']}, trap: {res['trap']}, probability of this outcome: {res['probability']:.3f}", ""]
    mc = r.get("machine")
    if mc:
        L += ["## Quantum machine", "",
              f"- one fixed interpreter circuit: {mc['qubits']} qubits, gates {mc['gates']} ({mc['toffoli']} Toffoli)",
              f"- {mc['steps']} machine steps, {mc['program_slots']} program-memory slots x {mc.get('slot_bits', 32)} qubits, "
              f"{mc['data_memory_cells']} data cells, registers {', '.join(mc['registers'])}",
              f"- data memory layout (byte offsets): {r.get('layout')}",
              f"- program slots and data cells are selected by: {mc.get('memory_select', 'comparator')}",
              f"- decoder implements {len(mc['decoder_classes'])} instruction classes: {', '.join(mc['decoder_classes'])}", ""]
    bk = r.get("backend")
    if bk:
        L += ["## Backend", "", "```", json.dumps(bk, indent=2, default=str), "```", ""]
    cpu = r["cpu"]
    L += ["## Model check (CPU reference)", "",
          f"CPU reference: {cpu['steps']} steps, halted: {cpu['halted']}" + (f", error: {cpu['error']}" if cpu['error'] else ""), ""]
    if cpu["issues"]:
        L += [f"Values that do not fit the {W}-bit sign-extended model (the QPU result may differ):", ""]
        L += [f"- {x}" for x in cpu["issues"][:30]]
    else:
        L += [f"Every value of the run fits the {W}-bit model, so the QPU computes the exact low {W} bits of the {arch['title']} result."]
    L += ["", "## Code", "", f"| # | address | {enc} | instruction | class | supported | executed |", "|---|---|---|---|---|---|---|"]
    for x in r["code"]:
        fn = f"**{x['function']}:** " if x["function"] else ""
        L.append(f"| {x['index']} | {x['address']} | `{x['word']}` | {fn}`{x['asm']}` | {x['cls'] or '-'} | "
                 f"{'yes' if x['supported'] else 'no: ' + x['reason']} | {x['executed']} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
