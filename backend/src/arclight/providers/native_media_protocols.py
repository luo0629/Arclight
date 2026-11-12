"""Native media request shapes and conservative installed adapter capabilities."""

import base64
import re
from math import gcd
from typing import Literal, cast
from urllib.parse import quote

from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationSpec
from arclight.domain.media_capabilities import MediaCapabilities, validate_capabilities

KINDS: dict[str, Literal["IMAGE", "VIDEO", "AUDIO"]] = {
    "openai_image": "IMAGE",
    "cogview": "IMAGE",
    "seedream": "IMAGE",
    "siliconflow_image": "IMAGE",
    "gemini_image": "IMAGE",
    "seedance": "VIDEO",
    "cogvideo": "VIDEO",
    "wan": "VIDEO",
    "veo": "VIDEO",
    "gemini_video": "VIDEO",
    "xai_video": "VIDEO",
    "qwen_tts": "AUDIO",
    "siliconflow_tts": "AUDIO",
    "openai_tts": "AUDIO",
}
ASYNC = frozenset({"seedance", "cogvideo", "wan", "veo", "xai_video"})
PARAMETERS = {
    "openai_image": frozenset({"quality"}),
    "cogview": frozenset({"quality", "watermark_enabled"}),
    "seedream": frozenset({"watermark"}),
    "siliconflow_image": frozenset({"num_inference_steps", "guidance_scale"}),
    "gemini_image": frozenset(),
    "seedance": frozenset({"generate_audio", "watermark"}),
    "cogvideo": frozenset({"quality", "with_audio"}),
    "wan": frozenset({"prompt_extend", "watermark", "audio"}),
    "veo": frozenset(),
    "gemini_video": frozenset(),
    "xai_video": frozenset(),
    "qwen_tts": frozenset({"voice", "language_type"}),
    "siliconflow_tts": frozenset({"voice", "speed"}),
    "openai_tts": frozenset({"voice", "speed", "instructions"}),
}


def capabilities(protocol: str) -> MediaCapabilities:
    task = KINDS[protocol]
    return MediaCapabilities(
        provider=protocol,
        version=f"{protocol}:1",
        task_types=frozenset({task}),
        max_reference_images=0,
        supports_seed=False,
        min_width=256,
        min_height=256,
        max_width=4096,
        max_height=4096,
        max_pixels=16777216,
        max_batch_size=1,
        supports_query=protocol in ASYNC,
        supports_cancel=False,
        supports_idempotency=False,
        execution_mode="ASYNC" if protocol in ASYNC else "SYNC",
        max_video_seconds=15 if task == "VIDEO" else None,
    )


def validate_spec(spec: GenerationSpec) -> None:
    from arclight.providers.media_capabilities import resolve_parameters

    task = KINDS[spec.provider]
    validate_capabilities(capabilities(spec.provider), spec, task, 0)
    output = spec.output_spec
    allowed = {"format"} | (
        {"width", "height"}
        if task == "IMAGE"
        else {"width", "height", "duration_seconds"}
        if task == "VIDEO"
        else {"sample_rate", "channels"}
    )
    if (
        set(spec.parameters) - resolve_parameters(spec.provider, spec.model)
        or set(output) - allowed
    ):
        raise DomainError("PROVIDER_PARAMETERS_UNSUPPORTED", "当前协议不支持这些参数", 422)
    for key, value in spec.parameters.items():
        if key in {
            "watermark",
            "watermark_enabled",
            "with_audio",
            "generate_audio",
            "prompt_extend",
            "audio",
        }:
            valid = type(value) is bool
        elif key in {"voice", "language_type", "quality", "instructions"}:
            valid = isinstance(value, str) and 0 < len(value) <= (
                2000 if key == "instructions" else 160
            )
        elif key == "speed":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0.25 <= value <= 4
            )
        elif key == "num_inference_steps":
            valid = type(value) is int and 1 <= value <= 100
        else:
            valid = (
                isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 30
            )
        if not valid:
            raise DomainError("PROVIDER_PARAMETERS_UNSUPPORTED", "服务商参数值不受支持", 422)
    if task == "AUDIO":
        if not spec.parameters.get("voice"):
            raise DomainError("PROVIDER_VOICE_REQUIRED", "语音任务需要选择该模型支持的音色", 422)
        if (
            output.get("format", "wav") not in {"wav", "mp3", "opus", "flac"}
            or output.get("channels", 1) != 1
        ):
            raise DomainError("PROVIDER_PARAMETERS_UNSUPPORTED", "语音输出格式不受支持", 422)
        if spec.provider == "qwen_tts" and (
            output.get("format", "wav") != "wav" or "sample_rate" in output
        ):
            raise DomainError(
                "PROVIDER_PARAMETERS_UNSUPPORTED", "通义语音使用模型原生 WAV 和采样率", 422
            )
        if "sample_rate" in output and (
            spec.provider != "siliconflow_tts"
            or output["sample_rate"] not in {8000, 16000, 24000, 32000, 44100, 48000}
        ):
            raise DomainError("PROVIDER_PARAMETERS_UNSUPPORTED", "当前协议不支持该采样率", 422)
        if spec.provider == "siliconflow_tts" and spec.model in {
            "FunAudioLLM/CosyVoice2-0.5B",
            "fnlp/MOSS-TTSD-v0.5",
        }:
            sample_rates = {
                "wav": {8000, 16000, 24000, 32000, 44100},
                "mp3": {32000, 44100},
                "opus": {48000},
            }
            kind = cast(str, output.get("format", "wav"))
            if kind not in sample_rates or (
                "sample_rate" in output and output["sample_rate"] not in sample_rates[kind]
            ):
                raise DomainError(
                    "PROVIDER_PARAMETERS_UNSUPPORTED",
                    "当前 SiliconFlow 模型不支持该音频格式或采样率",
                    422,
                )
    elif task == "VIDEO":
        duration = output.get("duration_seconds")
        if (
            type(duration) is not int
            or not 1 <= duration <= 15
            or output.get("format", "mp4") != "mp4"
        ):
            raise DomainError(
                "PROVIDER_DURATION_UNSUPPORTED", "视频任务需要明确整数秒时长及 MP4 输出", 422
            )
        width, height = cast(int, output["width"]), cast(int, output["height"])
        if (width, height) not in {(1280, 720), (720, 1280), (1920, 1080), (1080, 1920)}:
            raise DomainError(
                "PROVIDER_SIZE_UNSUPPORTED", "当前视频适配支持 720p 或 1080p 横竖屏", 422
            )
        if spec.provider == "veo" and duration not in {4, 6, 8}:
            raise DomainError("PROVIDER_DURATION_UNSUPPORTED", "Veo 协议仅支持 4、6、8 秒", 422)
        if spec.provider == "veo" and min(width, height) == 1080 and duration != 8:
            raise DomainError("PROVIDER_DURATION_UNSUPPORTED", "Veo 1080p 输出需要 8 秒", 422)
        if spec.provider == "cogvideo":
            legacy = spec.model in {"cogvideox-2", "cogvideox-flash"}
            if duration not in ({5} if legacy else {5, 10}):
                raise DomainError(
                    "PROVIDER_DURATION_UNSUPPORTED", "当前 CogVideo 模型不支持该时长", 422
                )
            if legacy and min(width, height) != 1080:
                raise DomainError(
                    "PROVIDER_SIZE_UNSUPPORTED", "当前 CogVideo 模型需要 1080p 输出", 422
                )
        if spec.provider == "seedance":
            minimum, maximum = (2, 12) if "seedance-1-" in spec.model else (4, 15)
            if not minimum <= duration <= maximum:
                raise DomainError(
                    "PROVIDER_DURATION_UNSUPPORTED", "当前 Seedance 模型不支持该时长", 422
                )
            if (
                any(marker in spec.model for marker in ("seedance-2-0-fast", "seedance-2-0-mini"))
                and min(width, height) != 720
            ):
                raise DomainError(
                    "PROVIDER_SIZE_UNSUPPORTED", "当前 Seedance 模型需要 720p 输出", 422
                )
        if spec.provider == "gemini_video" and duration < 3:
            raise DomainError("PROVIDER_DURATION_UNSUPPORTED", "Gemini 视频最短 3 秒", 422)
        if spec.provider == "wan":
            if spec.model.startswith("wan2.7-"):
                valid_duration = duration >= 2
            elif spec.model.startswith(("wan2.1-", "wan2.2-")):
                valid_duration = duration == 5
            else:
                valid_duration = duration in (
                    {5, 10, 15} if spec.model.startswith("wan2.6-") else {5, 10}
                )
            if not valid_duration:
                raise DomainError("PROVIDER_DURATION_UNSUPPORTED", "当前万相模型不支持该时长", 422)
    elif output.get("format", "png") not in {"png", "jpeg", "jpg"}:
        raise DomainError("PROVIDER_PARAMETERS_UNSUPPORTED", "图像输出仅支持 PNG 或 JPEG", 422)


def submission(spec: GenerationSpec) -> tuple[str, dict, dict]:
    """Return a relative API path, body and non-secret protocol headers."""
    validate_spec(spec)
    protocol, output = spec.provider, spec.output_spec
    prompt = spec.prompt + ("\nAvoid:\n" + spec.negative_prompt if spec.negative_prompt else "")
    model = quote(spec.model.removeprefix("models/"), safe="")
    params = dict(spec.parameters)
    task = KINDS[protocol]
    if task == "IMAGE":
        size = f"{output['width']}x{output['height']}"
        body = {"model": spec.model, "prompt": prompt, "size": size, **params}
        if protocol == "openai_image":
            body.update(
                n=1, output_format="jpeg" if output.get("format") in {"jpeg", "jpg"} else "png"
            )
        elif protocol == "seedream":
            body["response_format"] = "b64_json"
        elif protocol == "siliconflow_image":
            body.pop("size")
            body["image_size"] = size
        elif protocol == "gemini_image":
            width, height = cast(int, output["width"]), cast(int, output["height"])
            divisor = gcd(width, height)
            return (
                f"/models/{model}:generateContent",
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseModalities": ["TEXT", "IMAGE"],
                        "imageConfig": {"aspectRatio": f"{width // divisor}:{height // divisor}"},
                    },
                },
                {},
            )
        return "/images/generations", body, {}
    if task == "AUDIO":
        kind = output.get("format", "wav")
        if protocol == "qwen_tts":
            return (
                "/services/aigc/multimodal-generation/generation",
                {"model": spec.model, "input": {"text": prompt, **params}},
                {},
            )
        body = {"model": spec.model, "input": prompt, "response_format": kind, **params}
        if "sample_rate" in output:
            body["sample_rate"] = output["sample_rate"]
        return "/audio/speech", body, {}
    width, height = cast(int, output["width"]), cast(int, output["height"])
    ratio = "16:9" if width > height else "9:16"
    resolution = "1080p" if min(width, height) == 1080 else "720p"
    duration = output["duration_seconds"]
    if protocol == "seedance":
        return (
            "/contents/generations/tasks",
            {
                "model": spec.model,
                "content": [{"type": "text", "text": prompt}],
                "ratio": ratio,
                "resolution": resolution,
                "duration": duration,
                **params,
            },
            {},
        )
    if protocol == "cogvideo":
        return (
            "/videos/generations",
            {
                "model": spec.model,
                "prompt": prompt,
                "size": f"{width}x{height}",
                **(
                    {}
                    if spec.model in {"cogvideox-2", "cogvideox-flash"}
                    else {"duration": duration}
                ),
                **params,
            },
            {},
        )
    if protocol == "wan":
        size_parameters = (
            {"resolution": resolution.upper(), "ratio": ratio}
            if spec.model.startswith("wan2.7-")
            else {"size": f"{width}*{height}"}
        )
        return (
            "/services/aigc/video-generation/video-synthesis",
            {
                "model": spec.model,
                "input": {"prompt": prompt},
                "parameters": {**size_parameters, "duration": duration, **params},
            },
            {"X-DashScope-Async": "enable"},
        )
    if protocol == "veo":
        return (
            f"/models/{model}:predictLongRunning",
            {
                "instances": [{"prompt": prompt}],
                "parameters": {
                    "aspectRatio": ratio,
                    "durationSeconds": duration,
                    "resolution": resolution,
                },
            },
            {},
        )
    if protocol == "gemini_video":
        return (
            "/interactions",
            {
                "model": spec.model,
                "input": prompt,
                "background": False,
                "store": False,
                "response_format": {
                    "type": "video",
                    "delivery": "uri",
                    "duration": f"{duration}s",
                    "resolution": resolution,
                    "aspect_ratio": ratio,
                },
            },
            {},
        )
    return (
        "/videos/generations",
        {
            "model": spec.model,
            "prompt": prompt,
            "aspect_ratio": ratio,
            "resolution": resolution,
            "duration": duration,
        },
        {},
    )


def query_path(protocol: str, request_id: str) -> str:
    identity = quote(request_id, safe="")
    if protocol == "seedance":
        return "/contents/generations/tasks/" + identity
    if protocol == "cogvideo":
        return "/async-result/" + identity
    if protocol == "wan":
        return "/tasks/" + identity
    if protocol == "veo":
        encoded = request_id.removeprefix("op_")
        name = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        if not re.fullmatch(r"(?:models/[A-Za-z0-9_-]+/)?operations/[A-Za-z0-9_-]+", name):
            raise ValueError("Invalid operation identity")
        return "/" + name
    return "/videos/" + identity
