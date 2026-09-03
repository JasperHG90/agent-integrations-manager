"""Rewrite marker-delimited regions in AGENTS.md and its symlinks.

HTML dialect specialisation of `managed_regions`. Kept as a thin wrapper so
existing callers (init, doctor, agents_md tests) don't need to change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_BEGIN = r"<!-- BEGIN aim: (?P<name>[a-z0-9_-]+) -->"
_END = r"<!-- END aim: (?P=name) -->"
_REGION_RE = re.compile(
    rf"{_BEGIN}(?P<body>.*?){_END}",
    re.DOTALL,
)


class RegionError(ValueError):
    """Raised on malformed region markup (unbalanced or unknown markers)."""


class LegacyMarkerError(RegionError):
    """Raised when the file still uses pre-rename `agent-init` markers."""


@dataclass(frozen=True)
class Region:
    """A named marker region and the body of text it wraps."""

    name: str
    body: str  # content between markers (newline-padded; trim by caller if desired)


def parse(text: str) -> list[Region]:
    """Return the marker regions found in `text` in source order.

    Tolerant of missing markers (returns an empty list) and of regions wrapped
    around any content.

    Args:
        text: AGENTS.md content to scan for aim marker regions.

    Returns:
        The parsed regions in the order they appear.

    Raises:
        LegacyMarkerError: A pre-rename `agent-init` marker is present.
        RegionError: A BEGIN marker has no matching END (unbalanced markers).
    """
    # Give a clear diagnostic when the file still uses the old `agent-init`
    # marker string from before the aim rebrand.
    if re.search(r"<!--\s*(BEGIN|END)\s+agent-init:", text):
        raise LegacyMarkerError(
            "legacy agent-init markers detected; migrate to aim markers "
            "(e.g. '<!-- BEGIN aim: header -->')"
        )
    regions = [
        Region(name=m.group("name"), body=m.group("body")) for m in _REGION_RE.finditer(text)
    ]
    begins = re.findall(_BEGIN, text)
    ends = re.findall(r"<!-- END aim: ([a-z0-9_-]+) -->", text)
    if sorted(begins) != sorted(ends):
        raise RegionError(f"unbalanced aim markers: begins={sorted(begins)} ends={sorted(ends)}")
    return regions


def merge(existing: str, new_regions: dict[str, str]) -> str:
    """Replace each named region in `existing` with the corresponding new body.

    - Regions present in `existing` but not in `new_regions` are removed
      (markers + body). aim owns its marker regions: when the template no
      longer emits a region, it disappears from the output.
    - Regions in `new_regions` but missing from `existing` are appended at the
      end (each in its own marker pair, separated by a blank line).
    - Content outside any region is preserved verbatim.

    Args:
        existing: Current AGENTS.md content (validated before mutation).
        new_regions: Mapping of region name to its replacement body.

    Returns:
        The merged content with regions replaced, removed, or appended.

    Raises:
        RegionError: `existing` contains malformed or legacy markers.
    """
    # Validate existing markup before mutating.
    parse(existing)

    out = existing
    handled: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        """Substitute a matched region, dropping ones absent from `new_regions`."""
        name = match.group("name")
        if name not in new_regions:
            return ""
        handled.add(name)
        body = new_regions[name]
        if not body.startswith("\n"):
            body = "\n" + body
        if not body.endswith("\n"):
            body = body + "\n"
        return f"<!-- BEGIN aim: {name} -->{body}<!-- END aim: {name} -->"

    out = _REGION_RE.sub(_replace, out)

    missing = [(n, b) for n, b in new_regions.items() if n not in handled]
    if missing:
        suffix_parts: list[str] = []
        if out and not out.endswith("\n"):
            suffix_parts.append("\n")
        for name, body in missing:
            if not body.startswith("\n"):
                body = "\n" + body
            if not body.endswith("\n"):
                body = body + "\n"
            suffix_parts.append(f"\n<!-- BEGIN aim: {name} -->{body}<!-- END aim: {name} -->\n")
        out = out + "".join(suffix_parts)
    return out


def base_text(text: str) -> str:
    """Return `text` with every aim region (markers and body) removed.

    The remainder is the "base": the prose aim does not manage per-region.
    Used to fingerprint whether the base was authored by aim or hand-edited.
    """
    return _REGION_RE.sub("", text)


def build(regions: Iterable[tuple[str, str]]) -> str:
    """Construct an AGENTS.md body from scratch from ordered regions.

    Args:
        regions: Ordered (name, body) pairs to wrap in marker pairs.

    Returns:
        The assembled AGENTS.md content with a trailing newline.
    """
    parts: list[str] = []
    for name, body in regions:
        if not body.startswith("\n"):
            body = "\n" + body
        if not body.endswith("\n"):
            body = body + "\n"
        parts.append(f"<!-- BEGIN aim: {name} -->{body}<!-- END aim: {name} -->")
    return "\n".join(parts) + "\n"
