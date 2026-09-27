"""MiniMax-specific command construction over a generic sandbox runtime."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from ai_meeting_room.runtime.sandbox import DockerSandboxRuntime
from ai_meeting_room.runtime.sandbox import SandboxSpec


class MiniMaxSandboxRuntime(DockerSandboxRuntime):
    def __init__(self, spec: SandboxSpec, *args, **kwargs) -> None:
        # MiniMax Code 0.4.2 uses Node's built-in fetch. Keep proxy activation
        # explicit in the provider runtime; the generic sandbox remains
        # provider-neutral and does not assume a Node network stack.
        if not spec.node_use_env_proxy:
            spec = replace(spec, node_use_env_proxy=True)
        super().__init__(spec, *args, **kwargs)

    def build_mcode_command(self, prompt: str, *, permission: str = "off", timeout_seconds: float = 600, max_steps: int = 50) -> list[str]:
        if permission not in {"smart", "full", "off"}:
            raise ValueError("permission must be smart, full, or off")
        return ["mcode", "exec", "--cwd", self.spec.workspace_path, "--permission", permission, "--output-format", "stream-json", "--timeout", f"{max(1, int(timeout_seconds))}s", "--max-steps", str(max_steps), prompt]

    def exec_mcode(self, prompt: str, *, permission: str = "off", timeout_seconds: float = 600, max_steps: int = 50):
        return self.exec(self.build_mcode_command(prompt, permission=permission, timeout_seconds=timeout_seconds, max_steps=max_steps))
