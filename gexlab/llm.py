"""The local claude CLI, run with no tools, no MCP servers, no settings and no API keys in its
environment. Shared by the recap and the trade guidance narration; callers pass their own system prompt."""
from __future__ import annotations

import os


class CLIUnavailable(RuntimeError):
    pass


def child_env() -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
        env.pop(k, None)
    return env


def claude_cmd(exe: str, system_prompt: str) -> list[str]:
    cmd = [exe, "-p", "--output-format", "text",
           "--tools", "",                                   # no built-in tools at all
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',   # no MCP servers
           "--setting-sources", "", "--restricted",         # no user/project/local settings
           "--safe-mode", "--disable-slash-commands",       # no CLAUDE.md, skills, plugins, hooks
           "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--no-session-persistence", "--no-chrome",
           "--system-prompt", system_prompt]
    model = os.getenv("GEXLAB_RECAP_MODEL")
    if model:
        cmd += ["--model", model]
    return cmd
