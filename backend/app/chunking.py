import re


def is_low_signal(chunk: str, min_alpha_ratio: float = 0.4) -> bool:
    """Flag chunks that are mostly punctuation/numbers/whitespace."""
    if not chunk:
        return True
    alpha_count = sum(c.isalpha() for c in chunk)
    return (alpha_count / len(chunk)) < min_alpha_ratio


SECTION_HEADER_PATTERN = re.compile(r'^\d{1,2}\.\s+[A-Z][^\n]*$', re.MULTILINE)


def split_by_sections(text: str) -> list[str]:
    """Split at lines that look like numbered headers (e.g. '3. Installation').
    Falls back to the whole text as one section if none are found — this
    pattern is specific to numbered-heading documents, not universal."""
    matches = list(SECTION_HEADER_PATTERN.finditer(text))
    if not matches:
        return [text]

    sections = []
    if matches[0].start() > 0:
        sections.append(text[:matches[0].start()].strip())  # anything before the first header

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[start:end].strip())

    return [s for s in sections if s]


def merge_short_sections(sections: list[str], min_length: int = 200) -> list[str]:
    """Merge sections shorter than min_length into the next one — this is
    what absorbs a table-of-contents' many tiny 'sections' into one block."""
    merged = []
    buffer = ""
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


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Section-aware chunking: split by document structure first; only fall
    back to fixed-size splitting for sections still too large to embed as one chunk."""
    text = text.strip()
    if not text:
        return []

    sections = merge_short_sections(split_by_sections(text))
    chunks = []

    for section in sections:
        if len(section) <= chunk_size:
            if not is_low_signal(section):
                chunks.append(section)
        else:
            start = 0
            while start < len(section):
                piece = section[start:start + chunk_size].strip()
                if piece and not is_low_signal(piece):
                    chunks.append(piece)
                start += chunk_size - overlap

    return chunks