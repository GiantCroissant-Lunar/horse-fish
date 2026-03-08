"""Memory module for cross-session learning."""

from horse_fish.memory.lessons import Lesson, LessonStore
from horse_fish.memory.store import MemoryHit, MemoryStore

try:
    from horse_fish.memory.cognee_store import CogneeHit, CogneeMemory
except ImportError:
    CogneeMemory = None  # type: ignore[assignment,misc]
    CogneeHit = None  # type: ignore[assignment,misc]

__all__ = ["MemoryStore", "MemoryHit", "LessonStore", "Lesson", "CogneeMemory", "CogneeHit"]
