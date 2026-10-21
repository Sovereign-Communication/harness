"""Runtime composition root for the request lifecycle lanes."""

from pathlib import Path
from typing import Optional

from ._http import HttpTransport
from .chat_lifecycle import ChatLifecycleMixin
from .config import Settings, load_settings
from .edit_lifecycle import AutoEditLifecycleMixin
from .execution_stages import ExecutionStageMixin
from .lifecycle import LifecycleCoordinator
from .request_router import RequestRoutingMixin
from .simple_action_lifecycle import SimpleActionLifecycleMixin
from .task_lifecycle import TaskLaneLifecycleMixin

__all__ = ["AutonomousAgent"]


class AutonomousAgent(
    RequestRoutingMixin,
    SimpleActionLifecycleMixin,
    ChatLifecycleMixin,
    AutoEditLifecycleMixin,
    TaskLaneLifecycleMixin,
    ExecutionStageMixin,
):
    """Compose route adapters around one typed lifecycle coordinator."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        root_dir: Optional[Path] = None,
        history_dir: Optional[Path] = None,
        transport: Optional[HttpTransport] = None,
    ):
        self.settings = settings or load_settings()
        self.root_dir = Path(root_dir).resolve() if root_dir is not None else None
        self.history_dir = history_dir
        self.transport = transport or HttpTransport()
        self.lifecycle = LifecycleCoordinator(history_dir=history_dir)
        from .lifecycle_guards import RateCircuit

        self._rate_circuit = RateCircuit()
