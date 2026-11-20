import re

import snowballstemmer  # type: ignore[import-untyped]

_STEMMER = snowballstemmer.stemmer("russian")


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zа-я0-9]+", text.casefold().replace("ё", "е"))
    return [
        str(_STEMMER.stemWord(word))
        for word in words
        if len(word) > 2 and word not in {"как", "для", "или", "это", "мне", "что", "где", "есть", "про"}
    ]

