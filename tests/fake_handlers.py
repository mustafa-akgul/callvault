"""Handlers for tests: real VALIDATION, pass-through later steps.

FAKE_STEP_DELAY=<seconds> makes TRANSCRIPTION slow, so a test can kill the worker
in the middle of a job.
"""

from __future__ import annotations

import os
import time

from app.steps import StepResult, default_handlers


def passthrough(ctx) -> StepResult:
    return StepResult()


def slow_handlers():
    delay = float(os.environ.get("FAKE_STEP_DELAY", "0"))

    def slow_transcription(ctx) -> StepResult:
        time.sleep(delay)
        return StepResult()

    handlers = default_handlers()
    handlers.update(TRANSCRIPTION=slow_transcription, MASKING=passthrough, CRM=passthrough)
    return handlers
