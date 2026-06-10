"""Claude Code (`claude -p`) wrapped as a LangGraph node.

This module turns a one-shot, headless Claude Code invocation into a callable
that drops straight into a LangGraph: it reads the conversation off the graph
state, runs `claude -p` in agentic mode (parsing the stream-json event log),
and appends the assistant's final answer back onto the state.

Plumbing (env/auth, .claude.json, stream-json parsing, process-group timeout)
mirrors the patterns in ~/post/crs-claude-code so the node behaves the same way
inside an oss-crs container.

Typical use:

    from langgraph.graph import StateGraph, START, END
    from crs.src.claude_node import ClaudeState, ClaudeCodeNode, configure_claude_env

    configure_claude_env(config)               # once, at startup
    node = ClaudeCodeNode(cwd=source_dir, system_prompt="You are ...")

    g = StateGraph(ClaudeState)
    g.add_node("claude", node)
    g.add_edge(START, "claude")
    g.add_edge("claude", END)
    app = g.compile()
    app.invoke({"messages": [HumanMessage("Find a bug in harness X")]})
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

logger = logging.getLogger("crs.claude_node")

# 0 = no timeout (run until the model stops or the budget is exhausted).
try:
    DEFAULT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "0"))
except ValueError:
    DEFAULT_TIMEOUT = 0
if DEFAULT_TIMEOUT < 0:
    DEFAULT_TIMEOUT = 0


# ---------------------------------------------------------------------------
# Environment / auth setup
# ---------------------------------------------------------------------------
def configure_claude_env(config: dict | None = None, source_dir: Path | None = None) -> None:
    """Configure Claude Code auth + on-disk config. Call once at startup.

    Auth precedence (matches crs-claude-code):
      1. CLAUDE_CODE_OAUTH_TOKEN already in the environment -> use OAuth.
      2. config["llm_api_url"] + config["llm_api_key"] -> point Claude at a
         LiteLLM/Anthropic-compatible proxy via ANTHROPIC_BASE_URL/AUTH_TOKEN.
      3. Otherwise leave the environment untouched (ANTHROPIC_API_KEY, etc.).
    """
    config = config or {}

    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
        logger.info("Claude Code CLI version: %s", ver.stdout.strip() or ver.stderr.strip())
    except Exception as e:  # noqa: BLE001 - informational only
        logger.warning("Failed to get Claude Code version: %s", e)

    os.environ["IS_SANDBOX"] = "1"

    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    llm_api_url = config.get("llm_api_url", "")
    llm_api_key = config.get("llm_api_key", "")
    if oauth_token:
        logger.info("CLAUDE_CODE_OAUTH_TOKEN found, using OAuth authentication")
    elif llm_api_url and llm_api_key:
        os.environ["ANTHROPIC_BASE_URL"] = llm_api_url
        os.environ["ANTHROPIC_AUTH_TOKEN"] = llm_api_key
        os.environ["ANTHROPIC_API_KEY"] = ""
        logger.info("Claude Code configured with LLM proxy: %s", llm_api_url)
    else:
        logger.warning("No CLAUDE_CODE_OAUTH_TOKEN or llm_api_url/key; Claude Code may not authenticate")

    # Pre-seed Claude's JSON config so it skips onboarding/trust prompts.
    projects: dict[str, Any] = {}
    if source_dir is not None:
        projects[str(source_dir)] = {
            "hasTrustDialogAccepted": True,
            "hasCompletedProjectOnboarding": True,
        }
    claude_config = {
        "numStartups": 0,
        "autoUpdaterStatus": "disabled",
        "userID": "-",
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "1.0.0",
        "projects": projects,
    }
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text(json.dumps(claude_config))
    claude_json.chmod(0o600)
    logger.info("Wrote Claude config to %s", claude_json)


# ---------------------------------------------------------------------------
# Running `claude -p` and parsing its stream-json event log
# ---------------------------------------------------------------------------
@dataclass
class ClaudeResult:
    """Outcome of a single `claude -p` invocation."""

    text: str = ""                      # final assistant answer (result event)
    is_error: bool = False
    returncode: int | None = None
    timed_out: bool = False
    session_id: str | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None
    assistant_texts: list[str] = field(default_factory=list)  # every assistant turn


def _block_text(content: Any) -> str:
    """Flatten an Anthropic message ``content`` field (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def run_claude_p(
    prompt: str,
    *,
    cwd: Path | str | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    allowed_tools: Sequence[str] | None = None,
    disallowed_tools: Sequence[str] | None = None,
    skip_permissions: bool = True,
    extra_args: Sequence[str] | None = None,
    timeout: int | None = None,
    log_path: Path | str | None = None,
) -> ClaudeResult:
    """Run Claude Code headlessly and return its final answer.

    Streams ``--output-format stream-json`` so each event is parsed as it
    arrives: assistant text is logged live and the terminal ``result`` event
    supplies the final answer plus cost/turn metadata. On timeout the whole
    process group is terminated.

    ``skip_permissions`` (default True) passes ``--dangerously-skip-permissions``
    so every tool runs unprompted. Set it False to actually enforce
    ``allowed_tools`` / ``disallowed_tools`` (e.g. to restrict the agent to a
    single tool) — disallowed tool calls are then auto-denied rather than run.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    cmd: list[str] = [
        "claude",
        "-p",
        "--verbose",
        "--output-format", "stream-json",
    ]
    if skip_permissions:
        cmd += ["--dangerously-skip-permissions"]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    if disallowed_tools:
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    if extra_args:
        cmd += list(extra_args)

    result = ClaudeResult()
    raw_log = open(log_path, "w") if log_path else None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            start_new_session=True,
        )
    except FileNotFoundError:
        logger.error("`claude` CLI not found on PATH")
        result.is_error = True
        if raw_log:
            raw_log.close()
        return result

    # PIPE was requested for all three, so these are never None.
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    deadline = time.monotonic() + timeout if timeout else None

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()

        for line in proc.stdout:
            if raw_log:
                raw_log.write(line)
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            if etype == "assistant":
                text = _block_text(event.get("message", {}).get("content"))
                if text:
                    result.assistant_texts.append(text)
                    logger.info("[claude] %s", text)
            elif etype == "result":
                result.text = event.get("result", "") or ""
                result.is_error = bool(event.get("is_error", False))
                result.session_id = event.get("session_id")
                result.num_turns = event.get("num_turns")
                result.total_cost_usd = event.get("total_cost_usd")

            if deadline and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(cmd, timeout)

        proc.wait(timeout=deadline - time.monotonic() if deadline else None)
        result.returncode = proc.returncode
    except subprocess.TimeoutExpired:
        logger.warning("claude -p timed out (%ds); killing process group", timeout)
        result.timed_out = True
        result.is_error = True
        _kill_group(proc)
        result.returncode = proc.returncode
    except Exception as e:  # noqa: BLE001
        logger.error("Error running claude -p: %s", e)
        result.is_error = True
        _kill_group(proc)
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            stderr = proc.stderr.read()
            if stderr and raw_log:
                raw_log.write(stderr)
            proc.stderr.close()
        if raw_log:
            raw_log.close()

    # Fall back to the last assistant turn if no terminal result event arrived.
    if not result.text and result.assistant_texts:
        result.text = result.assistant_texts[-1]

    if result.returncode not in (0, None):
        logger.warning("claude -p exited rc=%s (is_error=%s)", result.returncode, result.is_error)
    return result


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(2)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------
class ClaudeState(TypedDict):
    """Minimal LangGraph state for a Claude Code conversation.

    Extend this with your own keys (harness, pov_dir, findings, ...) in your
    graph; ClaudeCodeNode only reads/writes ``messages``.
    """

    messages: Annotated[list[BaseMessage], add_messages]


def _latest_human_prompt(messages: Sequence[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return _block_text(msg.content)
    # No explicit human turn: concatenate whatever text is present.
    return "\n".join(_block_text(m.content) for m in messages).strip()


class ClaudeCodeNode:
    """A LangGraph node that runs `claude -p` once per invocation.

    The node reads the latest human message from ``state["messages"]`` (or uses
    ``prompt_builder(state)`` if supplied), runs Claude Code in ``cwd``, and
    returns ``{"messages": [AIMessage(final_answer)]}`` so the ``add_messages``
    reducer appends it to the conversation.

    Construct one per distinct role/working-dir; it is safe to reuse across
    invocations (it holds only configuration).
    """

    def __init__(
        self,
        *,
        cwd: Path | str | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        allowed_tools: Sequence[str] | None = None,
        disallowed_tools: Sequence[str] | None = None,
        skip_permissions: bool = True,
        extra_args: Sequence[str] | None = None,
        timeout: int | None = None,
        log_path: Path | str | None = None,
        prompt_builder: Callable[[ClaudeState], str] | None = None,
    ) -> None:
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.model = model
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.skip_permissions = skip_permissions
        self.extra_args = extra_args
        self.timeout = timeout
        self.log_path = log_path
        self.prompt_builder = prompt_builder

    def __call__(self, state: ClaudeState) -> dict[str, Any]:
        if self.prompt_builder is not None:
            prompt = self.prompt_builder(state)
        else:
            prompt = _latest_human_prompt(state.get("messages", []))

        if not prompt.strip():
            logger.warning("ClaudeCodeNode invoked with an empty prompt")

        result = run_claude_p(
            prompt,
            cwd=self.cwd,
            system_prompt=self.system_prompt,
            model=self.model,
            allowed_tools=self.allowed_tools,
            disallowed_tools=self.disallowed_tools,
            skip_permissions=self.skip_permissions,
            extra_args=self.extra_args,
            timeout=self.timeout,
            log_path=self.log_path,
        )

        ai = AIMessage(
            content=result.text,
            additional_kwargs={
                "is_error": result.is_error,
                "timed_out": result.timed_out,
                "returncode": result.returncode,
                "session_id": result.session_id,
                "num_turns": result.num_turns,
                "total_cost_usd": result.total_cost_usd,
            },
        )
        return {"messages": [ai]}
