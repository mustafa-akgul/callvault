"""Step error types, shared by step handlers and the services they call.

  TransientError  the third party flaked (5xx, 429, timeout, network): retried with
                  backoff; FAILED only after max_attempts.
  PermanentError  retrying cannot help (bad audio, invalid API key): FAILED at once,
                  resumable with `reprocess` once the cause is fixed.
"""

from __future__ import annotations


class StepError(Exception):
    def __init__(self, code: str, message: str, failed_step: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        # More precise than the job's step, e.g. CRM_BOOKING inside the CRM step.
        self.failed_step = failed_step


class TransientError(StepError):
    def __init__(self, code: str, message: str, retry_after: float | None = None,
                 failed_step: str | None = None):
        super().__init__(code, message, failed_step)
        self.retry_after = retry_after


class PermanentError(StepError):
    pass
