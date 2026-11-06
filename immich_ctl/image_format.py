"""Recognise JPEG/PNG signatures without decoding or changing image data."""

from pathlib import Path


def corrected_suffix(path: Path) -> str | None:
    """Return a correction only for known content with a misleading extension."""
    with path.open("rb") as stream:
        header = stream.read(8)
    suffix = path.suffix.casefold()
    if header.startswith(b"\xff\xd8\xff") and suffix not in {".jpg", ".jpeg", ".jpe"}:
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n") and suffix != ".png":
        return ".png"
    return None
