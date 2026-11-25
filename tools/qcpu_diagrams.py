# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Draws docs/images/qcpu_build.svg and qcpu_run.svg: the __qcpu__ workflow at build time and at run time.
Standalone SVG (light, and dark with prefers-color-scheme), shown by GitHub.
Run: python3 tools/qcpu_diagrams.py"""
import os
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "images")
STYLE = """<style>
svg{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.bg{fill:#FFFFFF;stroke:#D3D1C7}
.t{font-size:14px;font-weight:600}.s{font-size:12px}.l{font-size:12px;fill:#5F5E5A}
.arr{stroke:#888780;stroke-width:1.2;fill:none}.ah{fill:none;stroke:#888780;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.gray rect{fill:#F1EFE8;stroke:#5F5E5A}.gray .t{fill:#2C2C2A}.gray .s{fill:#5F5E5A}
.purple rect{fill:#EEEDFE;stroke:#534AB7}.purple .t{fill:#26215C}.purple .s{fill:#534AB7}
.teal rect{fill:#E1F5EE;stroke:#0F6E56}.teal .t{fill:#04342C}.teal .s{fill:#0F6E56}
.coral rect{fill:#FAECE7;stroke:#993C1D}.coral .t{fill:#4A1B0C}.coral .s{fill:#993C1D}
.head{font-size:13px;font-weight:600;fill:#444441}
@media (prefers-color-scheme: dark){
.bg{fill:#1F1F1E;stroke:#444441}.l,.head{fill:#B4B2A9}
.gray rect{fill:#444441;stroke:#B4B2A9}.gray .t{fill:#F1EFE8}.gray .s{fill:#D3D1C7}
.purple rect{fill:#3C3489;stroke:#AFA9EC}.purple .t{fill:#EEEDFE}.purple .s{fill:#CECBF6}
.teal rect{fill:#085041;stroke:#5DCAA5}.teal .t{fill:#E1F5EE}.teal .s{fill:#9FE1CB}
.coral rect{fill:#712B13;stroke:#F0997B}.coral .t{fill:#FAECE7}.coral .s{fill:#F5C4B3}}
</style>"""
DEFS = '<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="ah" d="M2 1L8 5L2 9"/></marker></defs>'

def box(c, x, y, w, title, sub=None):
    h = 56 if sub else 44
    s = f'<g class="{c}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" stroke-width="0.8"/>'
    if sub:
        s += f'<text class="t" x="{x+w/2}" y="{y+19}" text-anchor="middle" dominant-baseline="central">{title}</text>'
        s += f'<text class="s" x="{x+w/2}" y="{y+39}" text-anchor="middle" dominant-baseline="central">{sub}</text>'
    else:
        s += f'<text class="t" x="{x+w/2}" y="{y+22}" text-anchor="middle" dominant-baseline="central">{title}</text>'
    return s + '</g>'

def box2(c, x, y, w, title, sub1, sub2):
    """A box with a title and two subtitle lines (70 high)."""
    return (f'<g class="{c}"><rect x="{x}" y="{y}" width="{w}" height="70" rx="8" stroke-width="0.8"/>'
            f'<text class="t" x="{x+w/2}" y="{y+18}" text-anchor="middle" dominant-baseline="central">{title}</text>'
            f'<text class="s" x="{x+w/2}" y="{y+38}" text-anchor="middle" dominant-baseline="central">{sub1}</text>'
            f'<text class="s" x="{x+w/2}" y="{y+55}" text-anchor="middle" dominant-baseline="central">{sub2}</text></g>')


def line(x1, y1, x2, y2, head=True):
    return f'<line class="arr" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"' + (' marker-end="url(#a)"/>' if head else '/>')

def path(d, head=True):
    return f'<path class="arr" d="{d}"' + (' marker-end="url(#a)"/>' if head else '/>')

def svg(h, title, desc, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="680" height="{h}" viewBox="0 0 680 {h}" role="img">'
            f'<title>{title}</title><desc>{desc}</desc>{STYLE}{DEFS}'
            f'<rect class="bg" x="1" y="1" width="678" height="{h-2}" rx="12" stroke-width="1"/>{body}</svg>\n')

b = [
 '<text class="head" x="30" y="30">Build time: qcpucc (Python runs only here)</text>',
 box("gray", 230, 46, 220, "game.c with __qcpu__"), line(340, 90, 340, 120),
 box("purple", 210, 124, 260, "qcpucc.py", "splits the program in two"),
 path("M340 180 L340 200 L190 200 L190 218"), path("M340 200 L490 200 L490 218"),
 '<text class="l" x="190" y="240" text-anchor="middle">quantum-CPU side</text>',
 '<text class="l" x="490" y="240" text-anchor="middle">host side</text>',
 box("gray", 60, 252, 260, "clang for AArch64", "quantum-CPU machine code"), line(190, 308, 190, 328),
 box("purple", 60, 332, 260, "Loader and pruning", "keeps score() + callees"), line(190, 388, 190, 408),
 box2("purple", 60, 412, 260, "Circuit generator", "vcpu: the whole CPU as gates", "or qoracle: a circuit per function"),
 box("purple", 360, 252, 260, "Rename marked bodies", "score to qpu__native_score"), line(490, 308, 490, 328),
 box("gray", 360, 332, 260, "Your C compiler", "clang or gcc, for the PC"),
 path("M190 482 L190 504 L340 504 L340 522"), path("M490 388 L490 504 L340 504", False),
 box("gray", 210, 526, 260, "Link", "plus C engine qpu_engine.c"), line(340, 582, 340, 610),
 box("teal", 210, 614, 260, "game.exe", "contains no Python"),
 '<g class="purple"><rect x="150" y="692" width="14" height="14" rx="3" stroke-width="0.8"/></g><text class="l" x="172" y="703">our Python (build only)</text>',
 '<g class="gray"><rect x="360" y="692" width="14" height="14" rx="3" stroke-width="0.8"/></g><text class="l" x="382" y="703">ordinary compiler tools</text>',
]
open(f"{OUT}/qcpu_build.svg", "w").write(svg(728, "qcpucc build workflow",
    "game.c goes into qcpucc.py, which builds a quantum-CPU side (clang for AArch64, loader and pruning, and the circuits: "
    "the whole virtual CPU with --qcpu-mode vcpu, or with --qcpu-mode qoracle a small circuit per function) "
    "and a host side (renamed bodies, your C compiler); both are linked with the C engine into game.exe, which contains no Python.", "".join(b)))

r = [
 '<text class="head" x="30" y="30">Run time: one call to score() (no Python)</text>',
 box("gray", 200, 46, 280, "main() on the CPU", "fgets, printf, rand"), line(340, 102, 340, 128),
 box("purple", 200, 132, 280, "Generated wrapper score()", "arguments into qubit registers"), line(340, 188, 340, 214),
 box("purple", 200, 218, 280, "C engine runs the circuit", "vcpu: 35M gates; qoracle: 1844"), line(340, 274, 340, 300),
 box("gray", 200, 304, 280, "qpu__native_score()", "same function on the CPU (E1)"), line(340, 360, 340, 386),
 box("gray", 240, 390, 200, "Compare results"),
 path("M340 434 L340 456 L190 456 L190 482"), path("M340 456 L490 456 L490 482"),
 box("teal", 80, 486, 220, "Equal: QCPU_OK", "value back to main()"),
 box("coral", 380, 486, 220, "Differ: QCPU_MISMATCH", "stop, exit 70 (default)"),
]
open(f"{OUT}/qcpu_run.svg", "w").write(svg(572, "One __qcpu__ call at run time",
    "main() on the CPU calls score(); the generated wrapper loads the arguments, the C engine runs the quantum-CPU circuit, "
    "the same function runs natively, and the results are compared: equal returns the value (QCPU_OK), different stops "
    "the program with QCPU_MISMATCH.", "".join(r)))
