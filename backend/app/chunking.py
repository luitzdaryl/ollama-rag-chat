import re

CHUNK_SIZE = 800
OVERLAP = 150
MIN_SECTION = 200

# Numbered headings ("3. Installation") and Markdown headings ("## Install")
SECTION_HEADER_PATTERN = re.compile(
    r"^(?:\d{1,2}\.\s+[A-Z][^\n]*|#{1,3}\s+\S[^\n]*)$", re.MULTILINE
)
_WS = re.compile(r"\s")
_SENTENCE_END = re.compile(r"[.!?。！？]+[\"')\]]*(?:\s|$)")


def is_low_signal(chunk: str, min_alpha_ratio: float = 0.4) -> bool:
    """Flag chunks that are mostly punctuation/numbers/whitespace."""
    if not chunk:
        return True
    alpha_count = sum(c.isalpha() for c in chunk)
    return (alpha_count / len(chunk)) < min_alpha_ratio


def split_by_sections(text: str) -> list[str]:
    matches = list(SECTION_HEADER_PATTERN.finditer(text))
    if not matches:
        return [text]

    sections = []
    if matches[0].start() > 0:
        sections.append(text[: matches[0].start()].strip())
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[match.start() : end].strip())
    return [s for s in sections if s]


def merge_short_sections(sections: list[str], min_length: int = MIN_SECTION) -> list[str]:
    """Merge tiny sections into the next one (this is what absorbs a table of contents)."""
    merged, buffer = [], ""
    for section in sections:
        buffer = (buffer + "\n\n" + section).strip() if buffer else section
        if len(buffer) >= min_length:
            merged.append(buffer)
            buffer = ""
    if buffer:
        if merged:
            merged[-1] += "\n\n" + buffer
        else:
            merged.append(buffer)
    return merged


def _find_cut(window: str, min_pos: float) -> int:
    """Best place to cut `window`: paragraph break > sentence end > line break > space.
    Returns the (exclusive) cut index, or -1 if nothing suitable exists."""
    i = window.rfind("\n\n")
    if i >= min_pos:
        return i + 2
    ends = [m.end() for m in _SENTENCE_END.finditer(window)]
    if ends and ends[-1] >= min_pos:
        return ends[-1]
    i = window.rfind("\n")
    if i >= min_pos:
        return i + 1
    i = max(window.rfind(" "), window.rfind("\t"))
    if i >= min_pos:
        return i + 1
    return -1


def _split_long(text: str, size: int, overlap: int) -> list[tuple[int, str]]:
    """Split text into pieces of at most `size` characters, cutting at natural
    boundaries instead of mid-word. Each piece starts `overlap` characters before
    the previous one ended, so a fact sitting on a boundary lands whole in at
    least one piece. Returns (start_offset, piece) pairs."""
    pieces, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            cut = _find_cut(text[start:end], size * 0.5)
            if cut != -1:
                end = start + cut
        piece = text[start:end].strip()
        if piece:
            pieces.append((start, piece))
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)  # always move forward
        m = _WS.search(text, nxt, end)  # snap the overlap to a word start
        start = m.end() if m else nxt
    return pieces


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """Structure-aware chunking: split on headings first; only sections still too
    big for one chunk are split further, at natural boundaries, with overlap.
    Continuation pieces get their section heading prepended, so every chunk
    says which section it belongs to."""
    text = text.strip()
    if not text:
        return []

    chunks = []
    for section in merge_short_sections(split_by_sections(text)):
        if len(section) <= chunk_size:
            if not is_low_signal(section):
                chunks.append(section)
            continue

        headers = [(m.start(), m.group().strip()) for m in SECTION_HEADER_PATTERN.finditer(section)]
        for offset, piece in _split_long(section, chunk_size, overlap):
            if offset > 0:
                above = [h for pos, h in headers if pos < offset]
                if above and not piece.startswith(above[-1]):
                    piece = f"{above[-1]}\n{piece}"
            if not is_low_signal(piece):
                chunks.append(piece)
    return chunks