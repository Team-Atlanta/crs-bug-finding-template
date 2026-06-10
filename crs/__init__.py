"""crs-bug-finding-template: build-your-own CRS package.

Submodules are imported explicitly to keep imports lightweight:
    from crs.src.claude_node import ClaudeCodeNode, ClaudeState, configure_claude_env
    from crs.src import prompts
    from crs.tools import coverage, fuzzer, codeql   # no langchain dependency
"""
