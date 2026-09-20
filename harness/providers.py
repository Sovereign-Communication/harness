"""Modular external provider and CLI adapter interface.

Establishes an extensible plugin architecture for future platform integrations
(e.g., Claude CLI, Codex CLI, local LLM runners) while preserving OpenRouter as the
active core engine.

Design principles:
1. Token-minimal context scoping: Every external invocation receives tightly bounded,
   surgical micro-briefs produced by Harness's context condenser, never raw repository dumps.
2. Isolated execution & gates: Regardless of provider backend, mutations run within
   hermetic git worktrees and are verified against local gates before merging.
3. Extensible registration: Future plugins register via entry points or driver configs
   without mutating core orchestration.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Sequence


class ProviderAdapter(ABC):
    """Abstract interface for external model providers and tool CLIs."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider slug (e.g. 'openrouter', 'claude_cli', 'codex_cli')."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider CLI or API is present and authenticated on the system."""
        pass

    @abstractmethod
    def execute_node(
        self,
        instruction: str,
        target_file: Optional[str],
        context_brief: str,
        verification_gate: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Execute a single atomic DAG node with token-minimal context."""
        pass


class ProviderRegistry:
    """Registry for modular model providers and external CLI drivers."""

    _providers: Dict[str, ProviderAdapter] = {}

    @classmethod
    def register(cls, adapter: ProviderAdapter) -> None:
        cls._providers[adapter.name] = adapter

    @classmethod
    def get(cls, name: str) -> Optional[ProviderAdapter]:
        return cls._providers.get(name)

    @classmethod
    def available_providers(cls) -> Sequence[str]:
        return [k for k, p in cls._providers.items() if p.is_available()]
