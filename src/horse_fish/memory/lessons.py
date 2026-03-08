"""Lesson store for cross-session task complexity learning."""

from __future__ import annotations


class LessonStore:
    """Stores and retrieves task lessons for planning decisions."""

    async def get_lessons(self, task: str) -> str:
        """Query lessons relevant to a task description.

        Args:
            task: Task description to find relevant lessons for.

        Returns:
            String of relevant lessons, empty string if none found.
        """
        return ""
