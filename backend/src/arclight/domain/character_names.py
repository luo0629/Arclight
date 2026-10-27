"""Conservative identity matching: compatibility forms and case, never fuzzy names."""

import unicodedata


def canonical_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()
