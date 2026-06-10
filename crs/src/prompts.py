"""Prompt construction for the Claude Code bug-finding node.

CLAUDE.md is the single source of standing instructions. Its prose lives in the
editable Jinja2 template ``crs/CLAUDE.md.j2`` (workflow + tools + conditional
diff/seed/bug-candidate sections); ``build_claude_md()`` renders that template
with the per-run variables. It auto-loads from the agent's cwd and is inherited
by any Task sub-agents. There is no separate system prompt —
``build_user_prompt()`` carries only the per-run task, and ``install_skills()``
drops the runner-tool skills into .claude/skills/.

To change the agent's standing instructions, edit ``crs/CLAUDE.md.j2`` (plain
Markdown with a few ``{{ ... }}`` / ``{% ... %}`` placeholders) — not this file.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Sequence

import jinja2

logger = logging.getLogger("crs.prompts")

# This module lives in crs/src/, but the prompt ASSETS it renders/installs live at
# the crs/ package root (one level up): CLAUDE.md.j2, skills/, agents/, roles/.
_CRS_ROOT = Path(__file__).resolve().parent.parent

# Tool how-tos live as Claude Code skills (crs/skills/<name>/SKILL.md), installed
# into the agent's .claude/skills at run time so it discovers and invokes them.
_SKILLS_DIR = _CRS_ROOT / "skills"

# The editable CLAUDE.md template (crs/CLAUDE.md.j2, rendered by build_claude_md()).
_CLAUDE_MD_TEMPLATE = "CLAUDE.md.j2"

# Role definitions for Claude Code subagents (crs/agents/<name>.md), installed
# into .claude/agents at run time by the subagent-orchestration entrypoint
# (crs/entrypoint/run_crs_subagents.py) so the orchestrator can delegate to them by name.
_AGENTS_DIR = _CRS_ROOT / "agents"

# Entrypoint role prompts (crs/roles/<name>.md): the system/role prompt an
# entrypoint injects directly into its top-level `claude -p` — e.g. `pov-gen` for
# crs/entrypoint/run_crs.py, `orchestrator` for crs/entrypoint/run_crs_subagents.py. Loaded (not
# installed) via load_role(); kept separate from the Task-tool subagents above.
_ROLES_DIR = _CRS_ROOT / "roles"


def load_role(name: str, *, harness: str, source_dir: Path | str) -> str:
    """Return the entrypoint role prompt crs/roles/<name>.md, placeholders filled."""
    path = _ROLES_DIR / f"{name}.md"
    return (path.read_text()
            .replace("{harness}", harness)
            .replace("{source_dir}", str(source_dir)))


def install_skills(source_dir: Path, harness: str) -> list[str]:
    """Copy crs/skills/* into source_dir/.claude/skills/, filling placeholders.

    Installs the runner-tool skills (codeql/coverage/fuzz/gdb) so `claude -p`
    discovers them as Agent Skills (run with cwd=source_dir). Returns the names
    installed.
    """
    target = Path(source_dir) / ".claude" / "skills"
    if not _SKILLS_DIR.exists():
        logger.warning("skills dir not found: %s", _SKILLS_DIR)
        return []
    installed: list[str] = []
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        name = skill_dir.name
        dest = target / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)
        skill_md = dest / "SKILL.md"
        if skill_md.exists():
            content = (skill_md.read_text()
                       .replace("{harness}", harness)
                       .replace("{source_dir}", str(source_dir)))
            skill_md.write_text(content)
        installed.append(name)
    logger.info("Installed skills into %s: %s", target, ", ".join(installed))
    return installed


def install_agents(source_dir: Path, harness: str) -> list[str]:
    """Copy crs/agents/*.md into source_dir/.claude/agents/, filling placeholders.

    Installs the Claude Code subagent definitions (seed-gen, pov-gen) the
    subagent-orchestration entrypoint delegates to via the Task tool. Returns the
    names installed.
    """
    target = Path(source_dir) / ".claude" / "agents"
    if not _AGENTS_DIR.exists():
        logger.warning("agents dir not found: %s", _AGENTS_DIR)
        return []
    target.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for md in sorted(_AGENTS_DIR.glob("*.md")):
        content = (md.read_text()
                   .replace("{harness}", harness)
                   .replace("{source_dir}", str(source_dir)))
        (target / md.name).write_text(content)
        installed.append(md.stem)
    logger.info("Installed agents into %s: %s", target, ", ".join(installed))
    return installed


def md_inline(value: str) -> str:
    """Return a markdown-safe inline code span for an arbitrary string."""
    value = str(value)
    ticks = 1
    while "`" * ticks in value:
        ticks += 1
    fence = "`" * ticks
    return f"{fence}{value}{fence}"


# --- CLAUDE.md rendering ----------------------------------------------------
# The prose lives in crs/CLAUDE.md.j2 (edit that file). We render it with Jinja2,
# exposing `md_inline` as a filter so path lists become safe inline code spans.
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_CRS_ROOT)),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,
)
_jinja_env.filters["md_inline"] = md_inline


def build_claude_md(
    *,
    language: str,
    sanitizer: str,
    source_dir: Path,
    build_dir: Path,
    work_dir: Path,
    harness: str,
    pov_dir: Path,
    diffs: Sequence[Path],
    seeds: Sequence[Path],
    bug_candidates: Sequence[Path],
) -> str:
    """Render crs/CLAUDE.md.j2 into the per-run CLAUDE.md.

    All standing-instruction prose lives in the template; this function only
    supplies the per-run variables (paths, harness, language, and the
    diff/seed/bug-candidate evidence that drives the conditional sections).
    """
    seed_dir = str(seeds[0].parent) if seeds else None
    template = _jinja_env.get_template(_CLAUDE_MD_TEMPLATE)
    return template.render(
        language=language,
        sanitizer=sanitizer,
        source_dir=str(source_dir),
        build_dir=str(build_dir),
        work_dir=str(work_dir),
        harness=harness,
        pov_dir=str(pov_dir),
        diffs=[str(d) for d in diffs],
        seeds=[str(s) for s in seeds],
        seed_dir=seed_dir,
        bug_candidates=[str(b) for b in bug_candidates],
    )


def build_user_prompt(
    *,
    target: str,
    harness: str,
    pov_dir: Path,
    diffs: Sequence[Path],
    seeds: Sequence[Path],
    bug_candidates: Sequence[Path],
) -> str:
    """Build the `claude -p` user prompt (mirrors the reference finder)."""
    lines = [
        f"Find vulnerabilities in project {md_inline(target)} through harness {md_inline(harness)}.",
        f"Write crashing inputs (POVs) to {md_inline(str(pov_dir))}.",
        "",
        "Available evidence:",
        f"- Diff files: {len(diffs)}",
        f"- Seed files: {len(seeds)}",
        f"- Bug-candidate files: {len(bug_candidates)}",
    ]
    if diffs:
        lines.append("- Diff files: " + " ".join(md_inline(str(p)) for p in diffs))
    if seeds:
        lines.append(f"- Seed directory: {md_inline(str(seeds[0].parent))}")
    if bug_candidates:
        lines.append(
            "- Bug-candidate report files: "
            + " ".join(md_inline(str(p)) for p in bug_candidates)
        )
    lines += [
        "",
        "Read CLAUDE.md for workflow, environment, and submission instructions.",
        "Keep going until killed — find as many distinct vulnerabilities as possible.",
    ]
    return "\n".join(lines)
