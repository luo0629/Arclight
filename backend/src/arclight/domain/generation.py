"""Legal media execution states and their public meaning."""

from enum import StrEnum

from arclight.core.errors import DomainError


class GenerationStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    RECONCILING = "RECONCILING"
    MANUAL_REVIEW = "MANUAL_REVIEW"


TRANSITIONS: dict[GenerationStatus, frozenset[GenerationStatus]] = {
    GenerationStatus.CREATED: frozenset({GenerationStatus.SUBMITTING, GenerationStatus.CANCELLED}),
    GenerationStatus.SUBMITTING: frozenset(
        {
            GenerationStatus.SUBMITTED,
            GenerationStatus.RUNNING,
            GenerationStatus.SUCCEEDED,
            GenerationStatus.FAILED,
            GenerationStatus.RESULT_UNKNOWN,
            GenerationStatus.CANCEL_REQUESTED,
        }
    ),
    GenerationStatus.SUBMITTED: frozenset(
        {
            GenerationStatus.RUNNING,
            GenerationStatus.SUCCEEDED,
            GenerationStatus.FAILED,
            GenerationStatus.RESULT_UNKNOWN,
            GenerationStatus.CANCEL_REQUESTED,
            GenerationStatus.CANCELLED,
        }
    ),
    GenerationStatus.RUNNING: frozenset(
        {
            GenerationStatus.SUCCEEDED,
            GenerationStatus.FAILED,
            GenerationStatus.RESULT_UNKNOWN,
            GenerationStatus.CANCEL_REQUESTED,
            GenerationStatus.CANCELLED,
        }
    ),
    GenerationStatus.FAILED: frozenset({GenerationStatus.RETRY_WAIT, GenerationStatus.CANCELLED}),
    GenerationStatus.RETRY_WAIT: frozenset(
        {GenerationStatus.SUBMITTING, GenerationStatus.CANCELLED}
    ),
    GenerationStatus.CANCEL_REQUESTED: frozenset(
        {
            GenerationStatus.CANCELLED,
            GenerationStatus.SUCCEEDED,
            GenerationStatus.FAILED,
            GenerationStatus.RESULT_UNKNOWN,
        }
    ),
    GenerationStatus.RESULT_UNKNOWN: frozenset({GenerationStatus.RECONCILING}),
    GenerationStatus.RECONCILING: frozenset(
        {
            GenerationStatus.RUNNING,
            GenerationStatus.SUCCEEDED,
            GenerationStatus.FAILED,
            GenerationStatus.RETRY_WAIT,
            GenerationStatus.MANUAL_REVIEW,
            GenerationStatus.CANCELLED,
            GenerationStatus.RESULT_UNKNOWN,
            GenerationStatus.CANCEL_REQUESTED,
        }
    ),
    GenerationStatus.MANUAL_REVIEW: frozenset({GenerationStatus.RECONCILING}),
    GenerationStatus.SUCCEEDED: frozenset(),
    GenerationStatus.CANCELLED: frozenset(),
}

STATUS_LABELS = dict(
    zip(
        GenerationStatus,
        (
            "等待提交",
            "正在提交",
            "供应商已接收",
            "生成中",
            "等待重试",
            "已成功",
            "已失败",
            "已请求取消，等待确认",
            "已确认取消",
            "结果未知，等待对账",
            "正在对账",
            "需要人工处理",
        ),
        strict=True,
    )
)

UNCERTAIN_STATUSES = frozenset(
    {
        GenerationStatus.RESULT_UNKNOWN,
        GenerationStatus.RECONCILING,
        GenerationStatus.MANUAL_REVIEW,
    }
)


def validate_transition(source: str, target: str) -> None:
    try:
        allowed = GenerationStatus(target) in TRANSITIONS[GenerationStatus(source)]
    except ValueError:
        allowed = False
    if not allowed:
        raise DomainError("INVALID_GENERATION_TRANSITION", "媒体任务不允许此状态迁移", 409)
