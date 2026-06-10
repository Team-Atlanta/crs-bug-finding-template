# 3. Static analysis

`crs-codeql` runs **CodeQL** queries against a database built from the target. 
Where fuzzing finds bugs by *running* the code, 
static analysis finds candidate sites (such as calls to a dangerous API, a value flowing into an index, size, or allocation)
without executing anything.

Static analysis is an efficient method of enumerating potential buggy sites,
which are then furthered triaged to determine whether a vulnerability truly exists.

## 1. The CodeQL database and its queries

The `codeql` build (chapter 2's `compile_codeql`) is a queryable model of the
program: its functions, calls, types, and data flow, extracted while the code
compiled. You query it in **QL**, a logic language where each query selects the
program elements that match a condition — see CodeQL's
[writing queries](https://codeql.github.com/docs/writing-codeql-queries/codeql-queries/)
guide.

A query's output is one row per match: the selected element and, via
`getLocation()`, its `file:line`. Those rows are candidate sites to open and
reason about — the query tells you *where* to look, not whether a bug is there.

## 2. Enhance the pov-gen agent with static analysis

**Tasks start here**

`crs-codeql` lets the agent enumerate candidate sites with a query instead of
hand-grepping the source. As in the previous chapters, this how-to belongs in a
**skill** the agent loads on demand.

Start by driving the tool yourself, in the same **idle** container as chapter 1
(`docker exec -it <container> bash`):

```bash
cat > /tmp/q.ql <<'EOF'
import cpp
from FunctionCall c
where c.getTarget().getName() = "memcpy"
select c, c.getLocation()
EOF
crs-codeql run /tmp/q.ql               # prints matching sites with file:line
```

Now capture that knowledge as `crs/skills/crs-codeql/SKILL.md`:

- Follow the Claude [skill format](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).
- Keep it minimal — `name`, `description`, and a short body. The `description` is
  all the agent sees when deciding whether to load the skill, so lead with when
  to use it: enumerate candidate sites (a sink, a tainted index/size) before
  reading code.

```md
---
name: crs-codeql
description: Enumerate candidate sites with a CodeQL query when ...
---

Write how the agent should use the crs-codeql interface
```

## 3. Verify the agent uses it

Run the real entrypoint and confirm a query actually ran:

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
grep "\[crs-codeql\]" $LOG_DIR/agent/claude_stream.jsonl
```

Try to observe at least one `crs-codeql run` in the log. 
You may have to tweak `roles/pov-gen.md` to push the agent toward CodeQL 
(possibly to the detriment of PoV generation performance).