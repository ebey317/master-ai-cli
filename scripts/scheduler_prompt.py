"""
Prompt-building utilities for Sensei's scheduled-task runner.

Port of the recursion-guard idea from NousResearch/hermes-agent#0877decd:
when a scheduled task's own prompt contains cadence phrasing ("each Monday",
"every day at 9"), the agent must treat it as context for *this* run, not as
a request to create a sibling cron job.
"""

from __future__ import annotations

# The always-injected header that goes before every scheduled-task prompt.
# It combines the original SILENT hint with the new RECURSION guard.
SCHEDULED_JOB_HINT = (
    "SILENT: If there is genuinely nothing new to report, respond "
    "with exactly \"[SILENT]\" (nothing else) to suppress delivery. "
    "Never combine [SILENT] with content — either report your "
    "findings normally, or say [SILENT] and nothing more. "
    "RECURSION: This is a run of an EXISTING scheduled job — execute "
    "the task now. NEVER create or update a cron job because of "
    "recurring or future-schedule language in the task prompt below; "
    "treat phrasing like \"each Monday\" or \"every day at 9\" as "
    "context for this run, not as a request to schedule another job.]\n\n"
)


def build_job_prompt(task_prompt: str) -> str:
    """
    Return the full prompt sent to the model for a scheduled-task execution.

    The RECURSION guard precedes the user's task prompt so the model reads
    the "do not re-schedule" instruction first.
    """
    return f"{SCHEDULED_JOB_HINT}{task_prompt}"
