import re
import zlib
from collections import Counter

K1 = 1.2
B = 0.75
AVG_DOC_LEN = 130  # rough average tokens per chunk (~800 characters)

STOPWORDS = frozenset(
    """a an and are as at be but by can do does for from had has have how i if in into is it
    its me my no not of on or our so that the their then there these they this to up us was
    we were what when where which who why will with you your""".split()
)

# CJK has no spaces between words, so it's tokenized separately (see tokenize)
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
_TOKEN = re.compile(rf"[{_CJK}]+|[^\W_{_CJK}]+")
_CJK_CHAR = re.compile(rf"[{_CJK}]")


def tokenize(text: str) -> list[str]:
    tokens = []
    for tok in _TOKEN.findall(text.lower()):
        if _CJK_CHAR.match(tok):
            # character bigrams ("天氣預報" -> 天氣 氣預 預報); a lone character stays as is
            if len(tok) == 1:
                tokens.append(tok)
            else:
                tokens.extend(tok[i : i + 2] for i in range(len(tok) - 1))
        elif tok not in STOPWORDS and (len(tok) > 1 or tok.isdigit()):
            tokens.append(tok)
    return tokens


def _index(token: str) -> int:
    # NOT Python's hash(): that is randomized per process, so the same word would get
    # a different index after every restart. crc32 is stable and fits Qdrant's u32 indices.
    return zlib.crc32(token.encode("utf-8"))


def doc_sparse(text: str) -> tuple[list[int], list[float]]:
    """Document side: BM25 term-frequency weights. The IDF half is applied by Qdrant
    itself (collection config: Modifier.IDF), so it tracks your corpus automatically
    as documents are added or removed."""
    tokens = tokenize(text)
    if not tokens:
        return [], []
    length_norm = 1 - B + B * len(tokens) / AVG_DOC_LEN
    weights: dict[int, float] = {}
    for tok, tf in Counter(tokens).items():
        idx = _index(tok)
        weights[idx] = weights.get(idx, 0.0) + tf * (K1 + 1) / (tf + K1 * length_norm)
    return list(weights), list(weights.values())


def query_sparse(text: str) -> tuple[list[int], list[float]]:
    indices = sorted({_index(t) for t in tokenize(text)})
    return indices, [1.0] * len(indices)