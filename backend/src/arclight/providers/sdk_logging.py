"""Keep provider request content and credentials out of SDK diagnostics."""

import logging


def suppress_sdk_logging() -> None:
    for name in (
        "openai",
        "google.genai",
        "dashscope",
        "dashscope._logging",
        "httpx",
        "httpcore",
        "langchain_openai",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
