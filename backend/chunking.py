def is_low_signal(chunk: str, min_alpha_ratio: float = 0.4) -> bool:
    """Flag chunks that are mostly punctuation/numbers/whitespace — unlikely
    to carry retrievable meaning."""
    if not chunk:
        return True
    alpha_count = sum(c.isalpha() for c in chunk)
    return (alpha_count / len(chunk)) < min_alpha_ratio


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Split text into overlapping fixed-size chunks.

    chunk_size and overlap are both character counts, not tokens — simpler
    to reason about for a first pass, close enough in practice.
    """
    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk and not is_low_signal(chunk):
            chunks.append(chunk)
        start += chunk_size - overlap  # step forward, but overlap with the previous chunk

    return chunks

if __name__ == "__main__":
    from document_processing import extract_text

    text = extract_text("sample.txt")
    chunks = chunk_text(text)
    print(f"Produced {len(chunks)} chunk(s)")
    for i, c in enumerate(chunks):
        print(f"--- chunk {i} ({len(c)} chars) ---")
        print(c)