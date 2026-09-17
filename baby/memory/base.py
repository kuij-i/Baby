"""Memory interfaces and implementations for BABY."""

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from baby.core import AgentId, MemoryRecord, TaskId


class MemoryStore(ABC):
    """Replaceable storage interface for BABY memory records."""

    @abstractmethod
    def save(self, record: MemoryRecord) -> MemoryRecord:
        """Persist and return a memory record."""
        raise NotImplementedError

    @abstractmethod
    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Retrieve a record by UUID string."""
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        *,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        record_type: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[MemoryRecord]:
        """Retrieve records matching the supplied scope."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, record_id: str) -> bool:
        """Delete a record and return whether it existed."""
        raise NotImplementedError
