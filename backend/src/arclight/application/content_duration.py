"""文本时长准入：整数毫秒、闭区间；读取旧稿不触发准入校验。"""

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.core.errors import DomainError


def duration_bounds(target_ms: int, tolerance_percent: int) -> tuple[int, int]:
    if target_ms <= 0 or not 0 <= tolerance_percent <= 100:
        raise ValueError("目标时长必须为正数，容差必须在 0–100 之间")
    # 下界向上取整，上界向下取整，不能多放过 1ms。
    return (
        (target_ms * (100 - tolerance_percent) + 99) // 100,
        target_ms * (100 + tolerance_percent) // 100,
    )


def validate_duration(
    content: ScriptContent | StoryboardContent, target_ms: int, tolerance_percent: int
) -> None:
    if isinstance(content, ScriptContent):
        actual_ms = content.estimated_duration_ms
        if actual_ms is None:
            raise DomainError(
                "SCRIPT_DURATION_REQUIRED", "旧剧本缺少估算时长，请填写后另存新版本", 422
            )
        label, code = "剧本估算时长", "SCRIPT_DURATION_OUT_OF_RANGE"
    else:
        actual_ms = sum(shot.duration_ms for shot in content.shots)
        label, code = "镜头总时长", "STORYBOARD_DURATION_OUT_OF_RANGE"
    lower, upper = duration_bounds(target_ms, tolerance_percent)
    if not lower <= actual_ms <= upper:
        raise DomainError(
            code,
            f"{label} {actual_ms}ms 不在目标 {target_ms}ms 的允许范围 "
            f"[{lower}, {upper}]ms 内（容差 ±{tolerance_percent}%）",
            422,
        )
