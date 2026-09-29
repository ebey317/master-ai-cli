"""Tests for the scheduled-job prompt builder (recursion guard)."""

import pytest

from scripts.scheduler_prompt import SCHEDULED_JOB_HINT, build_job_prompt


class TestBuildJobPromptRecursionGuard:
    """Verify the RECURSION guard is always present and precedes the task."""

    def test_recursion_guard_always_present(self):
        prompt = build_job_prompt("Check for updates")
        assert "run of an EXISTING scheduled job" in prompt
        assert "NEVER create or update a cron job" in prompt

    def test_recurring_language_treated_as_context(self):
        task = (
            "Each Monday, review my calendar for the upcoming "
            "Monday-through-Sunday week and summarize it."
        )
        prompt = build_job_prompt(task)

        # Guard must appear before the task prompt so the model sees it first.
        guard_pos = prompt.index("run of an EXISTING scheduled job")
        task_pos = prompt.index("Each Monday, review my calendar")
        assert guard_pos < task_pos

        # The guard explicitly calls out example phrasing.
        assert 'phrasing like "each Monday"' in prompt

    def test_silent_hint_preserved(self):
        """Original SILENT behavior must remain intact."""
        prompt = build_job_prompt("Any task")
        assert "[SILENT]" in prompt
        assert "respond with exactly \"[SILENT]\"" in prompt

    def test_hint_ends_with_double_newline(self):
        """Formatting: hint block ends with a blank line before task text."""
        prompt = build_job_prompt("Task body")
        assert prompt.startswith(SCHEDULED_JOB_HINT)
        assert prompt[len(SCHEDULED_JOB_HINT):] == "Task body"
