# 2. Instrumentation

Many tools the agent uses needs the target compiled a particular way. 
The `build-target` phase compiles the same source several times over, each variant instrumented
for a different analysis. Chapter 1's `crs-fuzz` ran against the default `compile`;
this chapter adds the `afl` variant as the second fuzzing engine.

| Build variant | Builder | Instrumentation |
|---------------|---------|-----------------|
| `build` | `builder/compile_target` | libFuzzer + ASan |
| `afl` | `builder/build_afl` | AFL++ edge coverage + ASan (this chapter) |
| `coverage` | `builder/compile_coverage` | source-coverage counters |
| `codeql` | `builder/compile_codeql` | a CodeQL database |
| `debug` | `builder/compile_debug` | DWARF debug info for `gdb` |

## 1. How a build variant is produced — [`builder/`](../builder)

OSS-Fuzz ships a `compile` wrapper that runs the project's own `build.sh`
against the standard `$CC` / `$CXX` / `$CFLAGS` / `$CXXFLAGS`, then dispatches on
two environment variables: `SANITIZER` (which sanitizer to compile in) and
`FUZZING_ENGINE` (which it uses to source `compile_${FUZZING_ENGINE}`). Changing
either changes what the compiler emits.

Each script in `builder/` may override compiler flags and call `compile`:

- [`compile_target`](../builder/compile_target) takes the defaults — libFuzzer
  and ASan — and produces the `build` artifact from chapter 1.
- [`build_afl`](../builder/build_afl) **is your task to figure out**.
- [`compile_coverage`](../builder/compile_coverage) sets `SANITIZER=coverage`.
- [`compile_debug`](../builder/compile_debug) overrides `$CFLAGS` / `$CXXFLAGS`
  directly — `-O0 -g3 -gdwarf-4 -fno-inline` — so `gdb` gets full DWARF debug
  info and an un-optimized binary to step through.

(See OSS-Fuzz's [`compile`](https://github.com/google/oss-fuzz/blob/master/infra/base-images/base-builder/compile)
and [`compile_afl`](https://github.com/google/oss-fuzz/blob/master/infra/base-images/base-builder/compile_afl)
for how the engine selection overrides the compiler.)

Sometimes setting the environment variables isn't enough.
[`compile_codeql`](../builder/compile_codeql), for instance, doesn't run
`compile` directly — it runs `codeql database create` and hands `compile` to it
as the `--command` to trace. 

## 2. What the `afl` variant instruments

`compile_afl` replaces `$CC` / `$CXX` with AFL++'s `afl-clang-fast` / `afl-clang-fast++` wrappers. 
These run an LLVM compiler pass that injects, 
at every edge in the control-flow graph, a few instructions that
record the branch taken into a shared-memory coverage bitmap. 
After each input, `afl-fuzz` reads that bitmap to decide whether the run reached new code: its coverage feedback mechanism. 

The result is submitted under the `afl` build key, which the runner downloads for the second fuzzing engine below.

## 3. Enhance the pov-gen agent with a second fuzzing engine

**Tasks start here**

`crs-afl` runs **AFL++** against the `afl`-instrumented harness. 
A different mutation engine on the same target finds inputs libFuzzer's mutator misses.
Similar to chapter 1, you will create a **skill** for AFL for the agent loads on demand.

Start by driving the tool yourself, in the same **idle** container as chapter 1
(`docker exec -it <container> bash`):

```bash
H=mvc_dec_fuzzer
crs-afl start --harness $H             # launches AFL++ instances in the background
crs-afl status --harness $H            # instances alive, queue/corpus, crashes
crs-afl add-seeds --harness $H /work/seeds
crs-afl stop --harness $H
```

Note the behaviors worth capturing for the agent: `crs-afl` runs one AFL++
instance per core on its half of the cpuset, crashes sync into `/artifacts/povs`
exactly like `crs-fuzz`, and running `crs-fuzz start` and `crs-afl start`
together grows the one shared corpus from both engines at once.

Now capture that knowledge as `crs/skills/crs-afl/SKILL.md` (assume no skills
exist yet):

- Follow the Claude [skill format](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).
- Keep it minimal — `name`, `description`, and a short body. The `description` is
  all the agent sees when deciding whether to load the skill, so lead with when
  to use it: a second engine to run *with* `crs-fuzz`, not instead of it.

```md
---
name: crs-afl
description: Fuzz the harness with AFL++ alongside crs-fuzz when ...
---

Write how the agent should use the crs-afl interface
```

## 4. Verify the agent uses it

Run the real entrypoint and confirm AFL++ fired:

```bash
uv run oss-crs run \
  --compose-file example/crs-bug-finding-template/compose.oauth.yaml \
  --fuzz-proj-path ../benchmarks/atlanta-libavc-full-01 \
  --target-harness mvc_dec_fuzzer

LOG_DIR=$(uv run oss-crs artifacts \
  --compose-file example/crs-bug-finding-template/compose.oauth.yaml \
  --fuzz-proj-path ../benchmarks/atlanta-libavc-full-01 \
  --target-harness mvc_dec_fuzzer --latest | jq -r '.crs."crs-bug-finding-template".log_dir')

# inspect the agent's log
tail $LOG_DIR/agent/claude_stream.jsonl
grep "\[crs-afl\]" $LOG_DIR/agent/claude_stream.jsonl
```

You want to see the agent start *both* engines and feed seeds to each. 