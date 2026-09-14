from pathlib import Path
from pypdf import PdfReader


def extract_text(file_path: str) -> str:
    """Extract plain text from a file, dispatching by extension."""
    suffix = Path(file_path).suffix.lower()

    if suffix in (".txt", ".md"):
        return Path(file_path).read_text(encoding="utf-8")

    if suffix == ".pdf":
        reader = PdfReader(file_path)
        pages_text = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages_text)

    raise ValueError(f"Unsupported file type: {suffix}")

if __name__ == "__main__":
    text = extract_text("sample.txt")
    print("Extracted text:")
    print(text)