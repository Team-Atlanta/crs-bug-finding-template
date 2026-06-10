---
name: gdb
description: Dynamically inspect a C/C++ target's internal state with gdb on the debug build — break at any function and read live values (sizes, offsets, indices, field values) to confirm a hypothesis about what the code does on a given input, check whether an input reaches a target function or a condition-gated branch, or root-cause a crash. Re-run with more commands to go deeper.
---

# Inspect internal state at runtime with gdb

Run `gdb` yourself as a subprocess to watch what the code *actually does* on a
given input — confirm a hypothesis about the runtime values, or check whether your
input reaches the function (or condition-gated branch) you're targeting. To go
deeper, extend your command list and re-run; replaying a fixed input is
deterministic, so re-reaching a breakpoint costs milliseconds.

## Setup
```bash
libCRS download-build-output debug /work/debug-build   # DWARF-4, -O0 -g: clean stacks + named vars
HB=/work/debug-build/{harness}
export ASAN_OPTIONS=abort_on_error=1:detect_leaks=0:handle_abort=1
```
Source is at `/src` and the debug info's compile dir matches it, so `list` /
`file:line` resolve. ASan forks a symbolizer — add
`-ex 'set follow-fork-mode parent'` to keep gdb on the harness.

## Confirm a hypothesis / check reachability (the main use)
Break where input bytes decide a size/offset/index, run your input, and read the
real values to confirm what you believe happens — and whether the input even
reaches this point (if the breakpoint never hits, your input is malformed; fix the
structure first). Seeing how input bytes map to the runtime values tells you what
an input must look like to reach deeper code under a given condition.
```bash
cat > /work/dbg.gdb <<'EOF'
set pagination off
set follow-fork-mode parent
break foo
run
p var_a
p buf_b
EOF
gdb -q -batch -x /work/dbg.gdb --args "$HB" <input>
```
Read the output, add commands (`up`, `p <expr>`, `x/16xb buf`, a deeper
breakpoint, `watch <expr>`, `c` to the next hit …), and re-run. 
Use gdb to understand and steer the input.