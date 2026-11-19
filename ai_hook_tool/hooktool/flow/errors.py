"""统一失败契约：code / message / stage / retryable / blocking_issues。"""
from __future__ import annotations

CONFLICT_CODES = {"REVISION_CONFLICT", "PROJECT_BUSY", "RUN_STATE_CHANGED",
                  "PLAN_NOT_CURRENT", "ALREADY_EXISTS"}
NOT_FOUND_CODES = {"PROJECT_NOT_FOUND", "JOB_NOT_FOUND", "PLAN_NOT_FOUND",
                   "TASK_NOT_FOUND", "ASSET_NOT_FOUND", "TIMELINE_NOT_FOUND",
                   "REVIEW_NOT_FOUND", "EVIDENCE_NOT_FOUND", "PROPOSAL_NOT_FOUND"}


class FlowError(Exception):
    def __init__(self, code: str, message: str, *, stage: str | None = None,
                 retryable: bool = False, blocking_issues: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.blocking_issues = blocking_issues or []

    @property
    def http_status(self) -> int:
        if self.code in CONFLICT_CODES:
            return 409
        if self.code in NOT_FOUND_CODES:
            return 404
        if self.code in ("NEEDS_INPUT", "INSUFFICIENT_MATERIAL"):
            return 422
        return 422

    def to_dict(self) -> dict:
        return {"status": "failed", "code": self.code, "message": self.message,
                "stage": self.stage, "retryable": self.retryable,
                "blocking_issues": self.blocking_issues}


def as_flow_error(exc: Exception, *, stage: str | None = None,
                  code: str = "INTERNAL_ERROR") -> FlowError:
    if isinstance(exc, FlowError):
        return exc
    if isinstance(exc, ValueError):
        raw = str(exc)
        head = raw.split(":", 1)[0].strip()
        known = head if head.isupper() and " " not in head else code
        return FlowError(known, raw, stage=stage, retryable=False)
    return FlowError(code, f"{type(exc).__name__}: {exc}", stage=stage, retryable=True)
