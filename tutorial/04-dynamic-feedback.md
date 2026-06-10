# 4. Dynamic feedback

Where static analysis (chapter 3) reasons about code without running it, 
dynamic feedback tells us about what code is executed and program state at runtime. 
This chapter covers two tools: coverage feedback, which reports what the corpus reaches, 
and `gdb`, which shows what a single input does step by step.

## 1. What `crs-coverage` measures

The `coverage` build (chapter 2's `compile_coverage`, `SANITIZER=coverage`)
instruments the harness with source-coverage counters. `crs-coverage` runs the
live corpus through it and reports per-file and per-function coverage — which
code the inputs you have actually exercised.

## 2. Enhance the pov-gen agent with coverage feedback

**Tasks start here**

`crs-coverage` tells the agent where the corpus has and hasn't reached, so it can
aim the next batch of seeds instead of fuzzing blind. As in the previous
chapters, this how-to belongs in a **skill** the agent loads on demand.

Start by driving the tool yourself, in the same **idle** container as chapter 1
(`docker exec -it <container> bash`):

```bash
H=mvc_dec_fuzzer
crs-coverage --harness $H              # per-file/function coverage over the live corpus
```

Note the behaviors worth capturing for the agent: it measures the current
`/artifacts/corpus/<harness>`. 

You can feed new seeds to the fuzzer through `crs-fuzz add-seeds`.
Coverage feedback is a good way to recognize which branches and code is lacking discovery.
The *reachable-but-unhit* functions is valuable feedback.

Now capture that knowledge as `crs/skills/crs-coverage/SKILL.md`:

- Follow the Claude [skill format](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).
- Keep it minimal — `name`, `description`, and a short body. The `description` is
  all the agent sees when deciding whether to load the skill, so lead with when
  to use it: after fuzzing, to find the coverage plateau and decide where to push.

```md
---
name: crs-coverage
description: Measure what the corpus reaches after fuzzing when ...
---

Write how the agent should use the crs-coverage interface
```

## 3. Verify the agent uses it

Run the real entrypoint and confirm the agent measured what it reached:

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
grep "\[crs-coverage\]" $LOG_DIR/agent/claude_stream.jsonl
```

## 4. Going deeper: single-input inspection with `gdb`

`gdb` is a debugger that can show what *one* input does, line by line. 
It runs on the `debug` build variant (chapter 2's `compile_debug`: DWARF-4, `-O0`, so stacks are clean and variables are named).

The GDB skill is already provided. **Your task** is to determine where this tool is useeful for our PoV generation agent.
Traditionally, GDB is used for crafting exploits, but maybe there is something to be gained from using it for code exploration. 