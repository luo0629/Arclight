"""Bounded source viewports and allowlisted visual edits; no renderer expressions."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VisualContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CropRect(VisualContract):
    """Source-relative coordinates in ten-thousandths, before output aspect fitting."""

    x: int = Field(default=0, strict=True, ge=0, lt=10000)
    y: int = Field(default=0, strict=True, ge=0, lt=10000)
    width: int = Field(default=10000, strict=True, ge=1, le=10000)
    height: int = Field(default=10000, strict=True, ge=1, le=10000)

    @model_validator(mode="after")
    def bounded_rect(self):
        if self.x + self.width > 10000 or self.y + self.height > 10000:
            raise ValueError("裁切视窗必须完整位于源画面内")
        return self


class CameraMotion(VisualContract):
    kind: Literal["STATIC", "LINEAR"] = "STATIC"
    end_crop: CropRect | None = None

    @model_validator(mode="after")
    def precise_endpoint(self):
        if (self.kind == "LINEAR") != (self.end_crop is not None):
            raise ValueError("线性运镜必须提供结束视窗，静止画面不能带运动终点")
        return self


class ClipTransition(VisualContract):
    """Incoming transition: CUT, or FADE through black without changing clip timing."""

    kind: Literal["CUT", "FADE"] = "CUT"
    duration_ms: int = Field(default=0, strict=True, ge=0, le=2000)

    @model_validator(mode="after")
    def bounded_transition(self):
        if self.kind == "CUT" and self.duration_ms != 0:
            raise ValueError("硬切不能带转场时长")
        if self.kind == "FADE" and self.duration_ms < 100:
            raise ValueError("黑场淡出／淡入转场时长必须为100～2000毫秒")
        return self


class ClipVisual(VisualContract):
    crop: CropRect = Field(default_factory=CropRect)
    motion: CameraMotion = Field(default_factory=CameraMotion)
    transition: ClipTransition = Field(default_factory=ClipTransition)

    @model_validator(mode="after")
    def bounded_motion(self):
        end = self.motion.end_crop
        if end is None:
            return self
        start = self.crop
        if start == end:
            raise ValueError("运镜起止相同时应使用静止画面")
        if start.width * end.height != end.width * start.height:
            raise ValueError("运镜过程中必须保留裁切视窗的宽高比")
        if min(start.width, end.width) * 5 < max(start.width, end.width) * 4:
            raise ValueError("轻量推拉的最大缩放倍数为1.25")
        if abs((2 * end.x + end.width) - (2 * start.x + start.width)) * 2 > min(
            start.width, end.width
        ) or abs((2 * end.y + end.height) - (2 * start.y + start.height)) * 2 > min(
            start.height, end.height
        ):
            raise ValueError("轻量平移不能超过较小视窗宽高的四分之一")
        return self


@dataclass(frozen=True)
class FadeWindow:
    incoming_ms: int
    outgoing_ms: int
