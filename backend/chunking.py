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
        if chunk:
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