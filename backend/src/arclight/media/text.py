"""Legacy story text decoding inside the same bounded media worker."""

import unicodedata


class InvalidStoryText(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def decode_story(data: bytes, encoding: str) -> bytes:
    try:
        text = data.decode("utf-8-sig" if encoding == "utf-8" else "gb18030", errors="strict")
    except UnicodeError:
        raise InvalidStoryText("SOURCE_ENCODING") from None
    if any(
        (unicodedata.category(char) in {"Cc", "Cs"} and char not in "\t\n\r") or char == "\ufffd"
        for char in text
    ):
        raise InvalidStoryText("SOURCE_NOT_TEXT")
    if not text.strip():
        raise InvalidStoryText("SOURCE_EMPTY")
    return text.encode("utf-8")
