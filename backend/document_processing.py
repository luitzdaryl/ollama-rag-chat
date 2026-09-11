from pathlib import Path

def extract_text(file_path: str) -> str:
    """Extract plain text from a file, dispatching by extension."""
    suffix = Path(file_path).suffix.lower()

    if suffix in (".txt", ".md"):
        return Path(file_path).read_text(encoding="utf-8")

    raise ValueError(f"Unsupported file type: {suffix}")

if __name__ == "__main__":
    # Quick manual test — create a sample file first:
    # echo "Hello, this is a test document about weather patterns." > sample.txt
    text = extract_text("sample.txt")
    print("Extracted text:")
    print(text)