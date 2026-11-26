"""Verified speech model catalogs and account voice-list wire contracts."""

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from arclight.domain.audio_voices import AudioVoiceOption
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.media_io import JSON_RESPONSE_LIMIT, read_provider_response
from arclight.providers.network import PublicHTTPTransport


@dataclass(frozen=True)
class VoiceCatalogDefinition:
    options: tuple[AudioVoiceOption, ...]
    query: Callable[[dict, str, float, float], tuple[AudioVoiceOption, ...]] | None = None


def siliconflow_account_voices(
    configuration: dict, api_key: str, timeout: float, dns_timeout: float
) -> tuple[AudioVoiceOption, ...]:
    # https://docs.siliconflow.cn/docs/api/audio-voice-list-get
    # The endpoint lists account voices for every model. The URI is already the voice ID.
    with (
        httpx.Client(
            timeout=timeout,
            transport=PublicHTTPTransport(dns_timeout),
            trust_env=False,
            follow_redirects=False,
        ) as client,
        client.stream(
            "GET",
            configuration["base_url"] + "/audio/voice/list",
            headers={"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"},
        ) as response,
    ):
        body = json.loads(read_provider_response(response, maximum=JSON_RESPONSE_LIMIT))
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise GatewayFailure("INVALID_RESPONSE")
    options = {}
    for entry in results:
        if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
            raise GatewayFailure("INVALID_RESPONSE")
        if entry["model"] != configuration["model"]:
            continue
        voice_id = entry.get("uri")
        label = entry.get("customName")
        if (
            not isinstance(voice_id, str)
            or not voice_id.startswith("speech:")
            or not voice_id.isprintable()
            or not isinstance(label, str)
            or not label.isprintable()
            or not label.strip()
        ):
            raise GatewayFailure("INVALID_RESPONSE")
        options[voice_id] = AudioVoiceOption(id=voice_id, label=label)
    return tuple(options.values())


def options_from_rows(
    rows: tuple[tuple[str, str, str], ...], *, prefix: str = ""
) -> tuple[AudioVoiceOption, ...]:
    return tuple(
        AudioVoiceOption(id=prefix + voice_id, label=label, description=description)
        for voice_id, label, description in rows
    )


# Sources describe each published ID. These entries are exact routes, not model-name heuristics.
# https://developers.openai.com/api/docs/guides/text-to-speech#voice-options
_OPENAI_VOICES = tuple(
    AudioVoiceOption(id=voice, label=voice.title(), description="多语言，针对英语优化")
    for voice in (
        "alloy",
        "ash",
        "coral",
        "echo",
        "fable",
        "onyx",
        "nova",
        "sage",
        "shimmer",
        "ballad",
        "verse",
        "marin",
        "cedar",
    )
)

# https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list#non-realtime
_QWEN3_VOICES = options_from_rows(
    (
        ("Cherry", "芊悦", "自然开朗的女声，普通话及多语言"),
        ("Serena", "苏瑶", "轻柔女声，普通话及多语言"),
        ("Ethan", "晨煦", "温暖活泼的男声，略带北方口音"),
        ("Chelsie", "千雪", "柔软的动漫女声，普通话及多语言"),
        ("Momo", "茉兔", "俏皮女声，普通话及多语言"),
        ("Vivian", "十三", "直率而活泼的女声，普通话及多语言"),
        ("Moon", "月白", "清爽青年男声，普通话及多语言"),
        ("Maia", "四月", "知性女声，普通话及多语言"),
        ("Kai", "凯", "平和男声，普通话及多语言"),
        ("Nofish", "不吃鱼", "不卷舌的男声，普通话及多语言"),
        ("Bella", "萌宝", "活泼稚嫩的女声，普通话及多语言"),
        ("Jennifer", "詹妮弗", "有电影感的美式英语女声"),
        ("Ryan", "甜茶", "节奏鲜明、富有张力的男声"),
        ("Katerina", "卡捷琳娜", "成熟、富有韵律的女声"),
        ("Aiden", "艾登", "美式英语青年男声"),
        ("Eldric Sage", "沧明子", "平静的长者男声"),
        ("Mia", "乖小妹", "温顺的年轻女声"),
        ("Mochi", "沙小弥", "机灵的男童声"),
        ("Bellona", "燕铮莺", "吐字清晰、洪亮的女声"),
        ("Vincent", "田叔", "低哑的成熟男声"),
        ("Bunny", "萌小姬", "明快的女童声"),
        ("Neil", "阿闻", "吐字清晰、语调平稳的主持男声"),
        ("Elias", "墨讲师", "适合解说复杂内容的女声"),
        ("Arthur", "徐大爷", "从容而质朴的长者男声"),
        ("Nini", "邻家妹妹", "软糯甜美的女声"),
        ("Seren", "小婉", "舒缓的女声"),
        ("Pip", "顽屁小孩", "顽皮男童声"),
        ("Stella", "少女阿月", "甜美又有表现力的少女声"),
        ("Bodega", "博德加", "热情的西班牙语男声"),
        ("Sonrisa", "索尼莎", "开朗的拉美西语女声"),
        ("Alek", "阿列克", "冷静的俄语男声"),
        ("Dolce", "多尔切", "松弛的意大利语男声"),
        ("Sohee", "素熙", "亲切的韩语女声"),
        ("Ono Anna", "小野杏", "俏皮的日语女声"),
        ("Lenn", "莱恩", "理性的德语男声"),
        ("Emilien", "埃米尔安", "温和的法语男声"),
        ("Andre", "安德雷", "沉稳的男声"),
        ("Radio Gol", "拉迪奥·戈尔", "有运动解说感的葡萄牙语男声"),
        ("Jada", "上海-阿珍", "爽快的上海话女声"),
        ("Dylan", "北京-晓东", "北京话青年男声"),
        ("Li", "南京-老李", "耐心的南京话男声"),
        ("Marcus", "陕西-秦川", "厚实的陕西话男声"),
        ("Roy", "闽南-阿杰", "活泼的闽南语男声"),
        ("Peter", "天津-李彼得", "擅长相声风格的天津话男声"),
        ("Sunny", "四川-晴儿", "甜美的四川话女声"),
        ("Eric", "四川-程川", "自然的四川话男声"),
        ("Rocky", "粤语-阿强", "幽默的粤语男声"),
        ("Kiki", "粤语-阿清", "甜美的粤语女声"),
    )
)

# https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech
_SILICONFLOW_VOICES = (
    ("alex", "Alex", "沉稳男声"),
    ("benjamin", "Benjamin", "低沉男声"),
    ("charles", "Charles", "磁性男声"),
    ("david", "David", "明快男声"),
    ("anna", "Anna", "沉稳女声"),
    ("bella", "Bella", "热情女声"),
    ("claire", "Claire", "柔和女声"),
    ("diana", "Diana", "开朗女声"),
)

_CATALOGS = {
    ("openai", "openai_tts", "https://api.openai.com/v1", "tts-1"): VoiceCatalogDefinition(
        _OPENAI_VOICES[:9]
    ),
    ("openai", "openai_tts", "https://api.openai.com/v1", "tts-1-hd"): VoiceCatalogDefinition(
        _OPENAI_VOICES[:9]
    ),
    (
        "openai",
        "openai_tts",
        "https://api.openai.com/v1",
        "gpt-4o-mini-tts",
    ): VoiceCatalogDefinition(_OPENAI_VOICES),
    (
        "qwen",
        "qwen_tts",
        "https://dashscope.aliyuncs.com/api/v1",
        "qwen3-tts-flash",
    ): VoiceCatalogDefinition(_QWEN3_VOICES),
    **{
        ("siliconflow", "siliconflow_tts", endpoint, model): VoiceCatalogDefinition(
            options_from_rows(_SILICONFLOW_VOICES, prefix=f"{model}:"),
            query=siliconflow_account_voices,
        )
        for endpoint, model in (
            ("https://api.siliconflow.cn/v1", "FunAudioLLM/CosyVoice2-0.5B"),
            ("https://api.siliconflow.cn/v1", "fnlp/MOSS-TTSD-v0.5"),
        )
    },
}


def official_voice_catalog(configuration: dict) -> VoiceCatalogDefinition | None:
    return _CATALOGS.get(
        (
            configuration["supplier"],
            configuration["protocol"],
            configuration["base_url"],
            configuration["model"],
        )
    )
