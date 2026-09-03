"""Security guards for content and network transport.

- Hidden Unicode scanning: zero-width characters, bidi formatting controls,
  and tag characters are common vectors for invisible prompt injection.
- Insecure transport blocking: plain http:// is rejected unless explicitly
  allowed.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


class HiddenUnicodeError(ValueError):
    """Raised when hidden/invisible Unicode characters are found."""


class InsecureTransportError(ValueError):
    """Raised when a plain http:// transport is used without opt-in."""


# Unicode code points treated as hidden/invisible.
_HIDDEN_RANGES: tuple[tuple[int, int], ...] = (
    # Zero-width / invisible formatting characters
    (0x200B, 0x200F),  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    (0x2060, 0x2064),  # word joiner, invisible times/separator/plus (2061-2064)
    (0x206A, 0x206F),  # deprecated formatting controls (ISS, ASS, …)
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # combining grapheme joiner
    # Bidirectional formatting controls
    (0x202A, 0x202E),  # LRE, RLE, PDF, LRO, RLO
    (0x2066, 0x2069),  # LRI, RLI, FSI, PDI
    # Invisible filler letters (render as blank glyphs, carry no text)
    (0x115F, 0x1160),  # Hangul choseong/jungseong fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels
    (0x3164, 0x3164),  # Hangul filler
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0x2800, 0x2800),  # Braille blank pattern
    # Variation selectors — the current byte-smuggling vector (each VS encodes
    # data while rendering as nothing when unpaired with a base glyph).
    # VS15/VS16 (FE0E/FE0F) are deliberately EXCLUDED: they are how ordinary
    # emoji select their presentation (e.g. U+26A0 U+FE0F), so blocking them
    # would reject innocent docs wholesale.
    (0xFE00, 0xFE0D),  # VS1-14
    (0xE0100, 0xE01EF),  # VS17-256 (the supplement — just past the tag range)
    # Tag characters (rendered invisibly by most software)
    (0xE0000, 0xE007F),
)

# Surrogate code points should never appear in valid Unicode text; the
# U+DB40 block is sometimes used to smuggle tag-like characters.
_SURROGATE_RANGE = (0xD800, 0xDFFF)

# Byte order mark is also a zero-width character and a common stowaway.
_BOM = 0xFEFF


def _is_hidden(codepoint: int) -> bool:
    """Return whether a code point is treated as hidden/invisible.

    Args:
        codepoint: The Unicode code point to classify.

    Returns:
        True if the code point is a BOM, surrogate, or in a hidden range.
    """
    if codepoint == _BOM:
        return True
    if _SURROGATE_RANGE[0] <= codepoint <= _SURROGATE_RANGE[1]:
        return True
    for start, end in _HIDDEN_RANGES:
        if start <= codepoint <= end:
            return True
    return False


def _char_description(codepoint: int) -> str:
    """Return a human-readable label for a hidden code point.

    Args:
        codepoint: The Unicode code point to describe.

    Returns:
        A ``U+XXXX`` label, annotated with a mnemonic when one is known.
    """
    name = {
        0x200B: "ZWSP",
        0x200C: "ZWNJ",
        0x200D: "ZWJ",
        0x200E: "LRM",
        0x200F: "RLM",
        0x202A: "LRE",
        0x202B: "RLE",
        0x202C: "PDF",
        0x202D: "LRO",
        0x202E: "RLO",
        0x2060: "WJ",
        0x2066: "LRI",
        0x2067: "RLI",
        0x2068: "FSI",
        0x2069: "PDI",
        0x180E: "MVS",
        0xFEFF: "BOM/ZWNBSP",
    }.get(codepoint)
    if name:
        return f"U+{codepoint:04X} ({name})"
    return f"U+{codepoint:04X}"


def _find_hidden(text: str, source: str | None = None) -> list[str]:
    """Collect human-readable findings for hidden characters in text.

    Args:
        text: The text to scan character by character.
        source: Optional label prefixed to each finding for context.

    Returns:
        A list of findings, each naming the character and its line/column.
    """
    findings: list[str] = []
    line = 1
    col = 1
    prefix = f"{source}: " if source else ""
    for char in text:
        codepoint = ord(char)
        if _is_hidden(codepoint):
            findings.append(
                f"{prefix}hidden character {_char_description(codepoint)} at line {line}, column {col}"
            )
        if char == "\n":
            line += 1
            col = 1
        else:
            col += 1
    return findings


def scan_text(text: str, *, source: str | None = None) -> list[str]:
    """Return human-readable hidden-Unicode findings in ``text``.

    Args:
        text: The text to scan.
        source: Optional label prefixed to each finding for context.

    Returns:
        A list of findings, empty when no hidden characters are present.
    """
    return _find_hidden(text, source)


def scan_file(path: Path, *, source: str | None = None) -> list[str]:
    """Scan a single UTF-8 file for hidden Unicode characters.

    Args:
        path: The file to read and scan.
        source: Optional label for findings; defaults to the file path.

    Returns:
        A list of findings, empty when no hidden characters are present.
    """
    label = source or str(path)
    text = path.read_text(encoding="utf-8")
    return _find_hidden(text, label)


def scan_directory(root: Path, *, pattern: str = "**/*") -> list[str]:
    """Scan every UTF-8 file under ``root`` matching ``pattern``.

    Args:
        root: The directory whose files are scanned recursively.
        pattern: A glob pattern selecting which files to scan.

    Returns:
        A list of findings aggregated across all scanned files.
    """
    # Files that are not valid UTF-8 are skipped rather than treated as attacks.
    findings: list[str] = []
    for path in root.glob(pattern):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_find_hidden(text, str(path)))
    return findings


def assert_no_hidden_unicode(text: str, *, source: str | None = None) -> None:
    """Raise if hidden Unicode characters are found in ``text``.

    Args:
        text: The text to validate.
        source: Optional label prefixed to findings for context.

    Raises:
        HiddenUnicodeError: If any hidden Unicode characters are present.
    """
    findings = _find_hidden(text, source)
    if findings:
        raise HiddenUnicodeError("; ".join(findings))


def require_secure_url(url: str, *, allow_insecure: bool = False) -> None:
    """Reject plain http:// URLs unless ``allow_insecure`` is True.

    Allows https://, file://, ssh://, git@host:path, and any other non-HTTP
    scheme.

    Args:
        url: The URL whose transport scheme is checked.
        allow_insecure: When True, permit plain http:// instead of rejecting.

    Raises:
        InsecureTransportError: If the scheme is http:// and not allowed.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme == "http" and not allow_insecure:
        raise InsecureTransportError(
            f"insecure transport {url!r}: plain http:// is blocked; use https:// or pass --allow-insecure"
        )
