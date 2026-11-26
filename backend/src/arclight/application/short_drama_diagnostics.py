"""Keep actionable diagnostics separate from supplier payloads and credentials."""

from collections.abc import Iterator
from contextlib import contextmanager

from arclight.core.errors import DomainError
from arclight.domain.short_drama import ShortDramaIssue, ShortDramaTask

PROGRESS_EVENT = "short_drama.progress"
DETAIL = "issue_detail"
_PUBLIC_CODES = frozenset(
    {
        "ACCOUNT_BALANCE_INSUFFICIENT",
        "AUDIO_FILE_INVALID",
        "AUDIO_PERFORMANCE_UNSUPPORTED",
        "AUDIO_VOICE_CATALOG_QUERY_FAILED",
        "AUDIO_VOICE_CATALOG_UNAVAILABLE",
        "AUDIO_VOICE_LEDGER_NOT_APPROVED",
        "AUDIO_VOICE_PROFILE_CONFLICT",
        "AUDIO_VOICE_SCENE_UNMAPPED",
        "GATEWAY_CAPABILITY_REJECTED",
        "GATEWAY_PROVIDER_ACCESS_DENIED",
        "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
        "GATEWAY_PROVIDER_FAILED",
        "GATEWAY_PROVIDER_RATE_LIMITED",
        "GATEWAY_QUOTA_EXHAUSTED",
        "GATEWAY_RECONCILIATION_REQUIRED",
        "INVALID_PROTOCOL",
        "MEDIA_SERVICE_CONFIG_CHANGED",
        "PROVIDER_ACCESS_DENIED",
        "PROVIDER_AUTHENTICATION_FAILED",
        "PROVIDER_ASPECT_UNSUPPORTED",
        "PROVIDER_DURATION_UNSUPPORTED",
        "PROVIDER_PARAMETERS_UNSUPPORTED",
        "PROVIDER_QUOTA_EXHAUSTED",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_REFERENCES_UNSUPPORTED",
        "PROVIDER_REJECTED",
        "PROVIDER_SIZE_UNSUPPORTED",
        "PROVIDER_TASK_UNSUPPORTED",
        "PROVIDER_VOICE_REQUIRED",
        "SERVICE_INCOMPLETE",
        "SERVICE_KEY_UNAVAILABLE",
        "SHORT_DRAMA_ART_PROTECTED",
        "SHORT_DRAMA_AUDIO_COVERAGE",
        "SHORT_DRAMA_AUDIO_DURATION",
        "SHORT_DRAMA_AUDIO_REQUIRED",
        "SHORT_DRAMA_BOARD_IMPACT",
        "SHORT_DRAMA_DELIVERY_QA",
        "SHORT_DRAMA_EXPORT_WARNINGS",
        "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED",
        "SHORT_DRAMA_INPUT_CHANGED",
        "SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED",
        "SHORT_DRAMA_PLAN_BLOCKED",
        "SHORT_DRAMA_PLAN_SCOPE",
        "SHORT_DRAMA_REVIEW_CAPABILITY_REQUIRED",
        "SHORT_DRAMA_SFX_REQUIRED",
        "SHORT_DRAMA_SHOT_LIMIT",
        "SHORT_DRAMA_SPECIALIST_BLOCKED",
        "SHORT_DRAMA_SPEECH_SERVICE_REQUIRED",
        "SHORT_DRAMA_TEXT_CAPABILITY_REQUIRED",
        "SHORT_DRAMA_TEXT_SERVICE_REQUIRED",
        "SHORT_DRAMA_TTS_FAILED",
        "SHORT_DRAMA_TTS_UNCERTAIN",
        "SHORT_DRAMA_VIDEO_FORMAT_UNSUPPORTED",
        "SHORT_DRAMA_VISUAL_HUMAN_REQUIRED",
        "SHORT_DRAMA_VISUAL_RESULT_UNKNOWN",
        "SHORT_DRAMA_VISUAL_RETRY_EXHAUSTED",
        "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED",
        "SHORT_DRAMA_VOICE_REQUIRED",
        "SHORT_DRAMA_VOICE_SELECTION_INVALID",
    }
)


def public_short_drama_error(code: str | None) -> str | None:
    from arclight.application.producer_diagnostics import public_diagnostic

    return code if code in _PUBLIC_CODES else public_diagnostic(code)


_MEDIA_RECEIPT_CODES = {
    "PROVIDER_INSUFFICIENT_BALANCE": "ACCOUNT_BALANCE_INSUFFICIENT",
    "PROVIDER_QUOTA_EXHAUSTED": "GATEWAY_QUOTA_EXHAUSTED",
    "PROVIDER_RATE_LIMITED": "GATEWAY_PROVIDER_RATE_LIMITED",
    "PROVIDER_AUTHENTICATION_FAILED": "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
    "PROVIDER_ACCESS_DENIED": "GATEWAY_PROVIDER_ACCESS_DENIED",
    "INPUT_INVALID": "INPUT_INVALID",
}


def media_failure_code(receipt_code: str | None, operation_code: str | None) -> str | None:
    if receipt_code is not None and receipt_code in _MEDIA_RECEIPT_CODES:
        return _MEDIA_RECEIPT_CODES[receipt_code]
    return operation_code


class ShortDramaFailure(DomainError):
    def __init__(
        self,
        error: DomainError,
        *,
        service_kind: str | None,
        service: dict | None = None,
        service_owner: str | None = None,
        task: ShortDramaTask | None = None,
    ):
        super().__init__(error.code, error.message, error.status_code)
        self.phase: str | None = None
        self.service_kind = service_kind
        self.service_owner = service_owner
        self.task = task
        service = service or {}
        self.supplier = service.get("supplier_name") or service.get("supplier")
        self.model = service.get("model")

    def record(self, phase: str) -> dict:
        return {
            "phase": self.phase or phase,
            "code": public_short_drama_error(self.code),
            "service_kind": self.service_kind,
            "supplier": self.supplier,
            "model": self.model,
            "service_owner": self.service_owner,
            **({"task": self.task.model_dump(mode="json")} if self.task else {}),
        }


def text_failure(
    error: DomainError,
    owner: str,
    service: dict | None,
    *,
    review: bool = False,
    task: ShortDramaTask | None = None,
) -> ShortDramaFailure:
    return ShortDramaFailure(
        error,
        service_kind="REVIEW" if review else "TEXT",
        service=service,
        service_owner=owner,
        task=task,
    )


def public_issue(record: dict, actor: str) -> ShortDramaIssue:
    owns_service = record.get("service_owner") == actor
    return ShortDramaIssue(
        phase=record["phase"],
        code=record["code"],
        service_kind=record.get("service_kind"),
        supplier=record.get("supplier") if owns_service else None,
        model=record.get("model") if owns_service else None,
        task=record.get("task") if owns_service else None,
    )


@contextmanager
def at_phase(phase: str) -> Iterator[None]:
    try:
        yield
    except DomainError as error:
        if isinstance(error, ShortDramaFailure):
            error.phase = error.phase or phase
            raise
        failure = ShortDramaFailure(error, service_kind=None)
        failure.phase = phase
        raise failure from error
