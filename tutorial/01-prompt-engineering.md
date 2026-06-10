# 1. Prompt engineering the PoV-generation agent

This template, stripped down, is a Proof-of-Vulnerability generation agent.
The main entrypoint is at `run_crs` which drives one Claude Code agent
whose behavior is set by several prompt layers.

| Layer | Where it lives |
|-------|----------------|
| **System prompt** (the role) | `crs/roles/pov-gen.md` |
| **CLAUDE.md** (standing instructions) | `crs/CLAUDE.md.j2` → rendered to `<source>/CLAUDE.md` |
| **Skills** (tools, ch.2–4) | `crs/skills/<tool>/SKILL.md` → `.claude/skills/` |

The split is deliberate: the **role** is the job ("craft targeted PoVs"), while
**CLAUDE.md** is the role-neutral environment, tools, and rules every agent
shares and every Task subagent inherits.

## 1. The system prompt — [`crs/roles/pov-gen.md`](../crs/roles/pov-gen.md)

The role prompt defines the agent's job. It does this by laying out a specific,
numbered workflow — find candidates, analyze reachability, craft an input,
verify it, save it, repeat — and gating the final step behind an explicit
"before saving" verification checklist the agent must satisfy.

This structure is the point. A loose instruction like "find vulnerabilities"
leaves the agent to improvise its own process; an ordered workflow plus a
checklist gives it a procedure to follow and a concrete bar to clear before it
records a result. `load_role` substitutes the `{harness}` placeholder and injects
the result as the system prompt.

Edit this file to change *what the agent does*: reorder or add a workflow step,
or tighten the checklist.

## 2. CLAUDE.md — [`crs/CLAUDE.md.j2`](../crs/CLAUDE.md.j2)

`crs/CLAUDE.md.j2` is a Jinja template rendered per run by `build_claude_md()`
into `<source_dir>/CLAUDE.md` — the file Claude Code auto-loads from its working
directory at the start of every conversation, and which every Task subagent
inherits. Where the role prompt is the job, CLAUDE.md is the shared, role-neutral
context: the environment layout, the available tools, and the rules.

The `{{ ... }}` placeholders are filled at run time (harness, sanitizer);
these placeholders consume information passed by OSS-CRS.
The rendered output is written to `<work_dir>/agent_claude_md.md` for inspection.

Anthropic's [best practices for an effective CLAUDE.md](https://code.claude.com/docs/en/best-practices#write-an-effective-claude-md)
apply directly here:

- **Keep it short.** CLAUDE.md loads every session, so for each line ask whether
  its removal would cause a mistake. A bloated file dilutes the rules that matter
  and the agent starts ignoring them.
- **Include only what the agent cannot infer from the code** — the environment
  layout and the non-obvious rules (`run-pov` is the only PoV oracle; seeds are
  not POVs). Exclude anything it can read for itself or already knows.
- **Move sometimes-relevant knowledge into skills.** Broadly applicable rules
  belong here; per-tool instructions belong in a skill loaded on demand, which is
  exactly what the remaining chapters add.
- **Use emphasis to enforce adherence.** Directives such as "don't default to
  grep" in the tool-discipline section keep the agent from falling back to manual
  searches.
- **Treat it like code.** When the agent misbehaves, suspect the file has grown
  too long and a rule was lost; prune it and re-test by observing whether the
  behavior actually shifts.

## 3. Enhance the pov-gen agent with a fuzzing skill

**Tasks start here**

The role prompt permits the agent to drive the fuzzers and CLAUDE.md names
`crs-fuzz`, but neither explains how to operate it. That belongs in a **skill** —
a `SKILL.md` Claude Code loads on demand. Skills are the fourth prompt layer and
the pattern every remaining chapter follows.

Start by driving the tool yourself to build intuition. Boot the runner in
**idle** mode (it boots, then sleeps so you can explore) and shell in:

```bash
CRS_ENTRYPOINT=idle uv run oss-crs run \
  --compose-file example/crs-bug-finding-template/compose.oauth.yaml \
  --fuzz-proj-path ../benchmarks/atlanta-libavc-full-01 \
  --target-harness mvc_dec_fuzzer

docker ps --format '{{.Names}}' | grep main   # the runner container
docker exec -it <container> bash
```

`crs-fuzz` drives **libFuzzer** against the ASan harness from the `build`
artifact. It is one long-running fuzzer you *feed*: start once, then add seeds
while it runs. Crashes land in `/artifacts/povs` and auto-submit.

```bash
H=mvc_dec_fuzzer
crs-fuzz start --harness $H            # launches the background fuzzer
crs-fuzz status --harness $H           # corpus size, crashes, alive?
crs-fuzz add-seeds --harness $H /work/seeds   # feed seeds (no restart)
crs-fuzz stop --harness $H
```

Note the behaviors worth capturing for the agent: `start` returns immediately
because the fuzzer runs in the background, and `add-seeds` grows the live corpus
without restarting it.

Now capture that knowledge as `crs/skills/crs-fuzz/SKILL.md` (assume no skills
exist yet):

- Follow the Claude [skill format](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).
- Keep it minimal — `name`, `description`, and a short body. The `description` is
  all the agent sees when deciding whether to load the skill, so lead with when
  to use it.

```md
---
name: crs-fuzz
description: Fuzz the harness with libFuzzer when ...
---

Write how the agent should use the crs-fuzz interface
```

## 4. Verify the agent uses it

Run the real entrypoint (the default `run_crs`, which drives the pov-gen agent)
and confirm the fuzzer fired:

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
grep "\[crs-fuzz\]" $LOG_DIR/agent/claude_stream.jsonl
```

For the full picture, read `<work_dir>/agent_role.md` and
`agent_claude_md.md` — the exact prompt layers the agent received this run. 
If the agent started the fuzzer and fed it seeds, the skill worked. 