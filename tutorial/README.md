# Building an Agentic CRS — Tutorial

This tutorial teaches you to extend `crs-bug-finding-template` into an effective
**Cyber Reasoning System (CRS)**: an LLM agent (Claude Code) that reads a target's
source, drives real analysis tools, and produces verified crashing inputs (PoVs).

The template already runs end-to-end. You start by learning how the agent is
**guided** (its prompt layers), then learn its tools by **using them yourself** and teaching the agent to use each one 
— and finish by orchestrating several agents at once.

## Setup

You'll edit this template, so work from your own copy:

1. **Fork** [`Team-Atlanta/crs-bug-finding-template`](https://github.com/Team-Atlanta/crs-bug-finding-template) on GitHub.
2. **Clone your fork** locally:
   ```bash
   git clone git@github.com:<your-username>/crs-bug-finding-template.git
   ```
3. **Point oss-crs at your local checkout.** For local development, add a `source.local_path`
   to the CRS service in the example compose so oss-crs builds and runs from your
   working tree instead of a published image — apply this diff to
   `example/crs-bug-finding-template/compose.litellm.yaml`:

   ```diff
   --- a/example/crs-bug-finding-template/compose.litellm.yaml
   +++ b/example/crs-bug-finding-template/compose.litellm.yaml
   @@ -16,6 +16,9 @@ llm_config:

    # --- CRS (crs-bug-finding-template) ----------------------------------------
    crs-bug-finding-template:
   +  source:
   +    local_path:
   +      <PATH_TO_LOCAL_CRS>/crs-bug-finding-template
      cpuset: "2-7"
      memory: "16G"
      llm_budget: 10
   ```

   Replace `<PATH_TO_LOCAL_CRS>` with the absolute path to the directory you cloned into.
4. **Configure model access.** Pick one auth path — it decides which compose file
   you pass as `--compose-file` throughout the tutorial:

   - **LiteLLM proxy key (provisioned by us).** Use `compose.litellm.yaml`:
     ```bash
     uv run oss-crs setup    # switch the key env var to EXTERNAL_LITELLM_API_*
     export EXTERNAL_LITELLM_API_BASE=<our-proxy-url>
     export EXTERNAL_LITELLM_API_KEY=<your-provisioned-key>
     ```
   - **Claude Code subscription.** Use `compose.oauth.yaml`:
     ```bash
     claude token            # prints an OAuth token
     export CLAUDE_CODE_OAUTH_TOKEN=<token>
     ```

   The example commands below use `compose.oauth.yaml`; if you chose the LiteLLM
   path, swap in `compose.litellm.yaml`.

## The shape of every chapter

Chapter 1 covers prompt engineering: the role prompt, CLAUDE.md, and the skill
layer the rest build on. The tool chapters (2–4) then each introduce a capability
the same way:

1. **The concept** — what the capability is and which build variant it relies on.
2. **Enhance the agent** — drive the `crs-*` tool by hand in **idle** mode to
   build intuition, then capture that as a `crs/skills/<tool>/SKILL.md` the agent
   loads on demand. (Assume no skills exist yet — you're writing them.)
3. **Verify** — run the real `run_crs` entrypoint and check the agent's logs (in
   the oss-crs artifacts) to confirm the tool actually fired.

Chapter 5 steps up from one agent to a team, swapping the single-agent entrypoint
for two multi-agent orchestration strategies.

## The chapters

| # | Topic | What it covers | Build artifact |
|---|-------|----------------|----------------|
| [1](01-prompt-engineering.md) | prompt engineering | role prompt + CLAUDE.md + the skill layer (worked example: `crs-fuzz`) | `build` (ASan) |
| [2](02-instrumentation.md) | instrumentation | build variants + how `compile` instruments; a second engine, `crs-afl` | `afl` |
| [3](03-static-analysis.md) | static analysis | querying the CodeQL database; the `crs-codeql` tool | `codeql` |
| [4](04-dynamic-feedback.md) | dynamic feedback | corpus coverage (`crs-coverage`) + single-input inspection (`gdb`) | `coverage`, `debug` |
| [5](05-multi-agent-workflows.md) | multi-agent workflows | orchestrating a team: Claude Code subagents vs. a LangGraph loop | — |

## Build the target first

Every chapter runs against pre-built artifacts, so before any `oss-crs run` you
prepare the project and build the target once:

```bash
uv run oss-crs prepare \
  --compose-file example/crs-bug-finding-template/compose.oauth.yaml

uv run oss-crs build-target \
  --compose-file example/crs-bug-finding-template/compose.oauth.yaml \
  --fuzz-proj-path ../benchmarks/atlanta-libavc-full-01
```

`prepare` sets the project up for the framework; `build-target` runs the
`target_build_phase` in [`oss-crs/crs.yaml`](../oss-crs/crs.yaml), producing the
`build`, `coverage`, `afl`, `debug`, and `codeql` artifacts that
[chapter 2](02-instrumentation.md) dissects and the later chapters feed on. 

Any modification to the builder scripts or Dockerfiles requires a fresh invocation of `build-target`.

## The idle entrypoint

Every chapter's interactive playground step uses the `idle` entrypoint: the runner boots
(downloads build + source, puts `crs-*` on `PATH`) and then sleeps, so you can
`docker exec` in and run the tools exactly as the agent would. Select it with
`CRS_ENTRYPOINT=idle` (the compose multiplexes on this; default is `run_crs`).

Find the runner container and shell in (the CRS runner is named `main` in
`crs.yaml`, so its container name contains `main`):

```bash
CRS_ENTRYPOINT=idle uv run oss-crs run \
  --compose-file example/crs-bug-finding-template/compose.oauth.yaml \
  --fuzz-proj-path ../benchmarks/atlanta-libavc-full-01 \
  --target-harness mvc_dec_fuzzer

# In another terminal
docker ps --format '{{.Names}}' | grep main      # the runner container
docker exec -it <container> bash
```

## Submitting to oss-crs

When your CRS is ready to share:

1. **Rename your CRS** so it doesn't collide with the template or other
   submissions — pick a unique name and registry. This means both:
   - the `name` and `docker_registry` in [`oss-crs/crs.yaml`](../oss-crs/crs.yaml)
     (the registry must be your own, not `team-atlanta/crs-bug-finding-template`), and
   - the registry entry in `ossf/oss-crs` at `registry/<your-crs>.yaml` — name the
     file (and its entry) to match.
2. **Open a PR from your fork** against the **`svcc-tutorial`** branch of
   [`ossf/oss-crs`](https://github.com/ossf/oss-crs).
