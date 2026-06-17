#!/usr/bin/env python3
"""Grade README outputs against the readme-creator eval assertions."""

import json
import re
from pathlib import Path

WORKSPACE = Path(__file__).parent


def check_logo_centered(text: str) -> bool:
    first_lines = "\n".join(text.splitlines()[:25])
    return bool(
        re.search(r'<p\s+align\s*=\s*["\']center["\']', first_lines)
        and re.search(r'<img\s+[^>]*src\s*=\s*["\'][^"\']*["\']', first_lines)
    )


def check_features_before_install(text: str) -> bool:
    features_match = re.search(r"(?i)^#{1,2}\s+features", text, re.MULTILINE)
    install_match = re.search(r"(?i)^#{1,2}\s+(install|installation|setup)", text, re.MULTILINE)
    if not features_match or not install_match:
        return False
    return features_match.start() < install_match.start()


def check_install_codeblock(text: str) -> bool:
    if not re.search(r"(?i)^#{1,2}\s+(install|installation|setup)", text, re.MULTILINE):
        return False
    match = re.search(
        r"(?i)^#{1,2}\s+(install|installation|setup).*?(```+\w*\n.*?)\n```+",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return bool(match)


def check_contributing_near_bottom(text: str) -> bool:
    match = re.search(r"(?i)^#{1,2}\s+(contributing|development|dev|license)", text, re.MULTILINE)
    if not match:
        return False
    total = len(text)
    return match.start() > total * 0.6


def check_under_80_lines(text: str) -> bool:
    return len(text.splitlines()) <= 80


def check_usage_before_or_with_install(text: str) -> bool:
    usage_match = re.search(r"(?i)^#{1,2}\s+(usage|quick[\s-]?start|how to use|try it)", text, re.MULTILINE)
    install_match = re.search(r"(?i)^#{1,2}\s+(install|installation|setup)", text, re.MULTILINE)
    if not usage_match or not install_match:
        return False
    return usage_match.start() <= install_match.start()


def check_no_unexplained_jargon(text: str) -> bool:
    # Very naive: flag common acronyms if not preceded by spelled-out form.
    acronyms = ["API", "SDK", "JWT", "ORM", "SQL", "HTTP", "URL", "JSON", "YAML"]
    for acronym in acronyms:
        if re.search(rf"\b{acronym}\b", text) and not re.search(
            rf"(?i)({''.join(c.lower() + '?' for c in acronym)})\b.*?\b{acronym}\b", text
        ):
            return False
    return True




def _extract_badge_colors(text: str) -> set[str]:
    """Extract shield badge accent colors from src attributes in the text."""
    colors: set[str] = set()
    # Match src URLs in <img ... src="...">
    for url in re.findall(r'<img\s+[^>]*src\s*=\s*"([^"]+)"', text):
        found = re.findall(r'-([0-9a-fA-F]{6})(?:\?|&|"|\s|$)', url)
        colors.update(found)
    # Match markdown image URLs ![alt](url)
    for url in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', text):
        found = re.findall(r'-([0-9a-fA-F]{6})(?:\?|&|\)|\s|$)', url)
        colors.update(found)
    return colors


def check_theme_reference_or_accent(text: str) -> bool:
    preset_names = [
        "ocean depths", "sunset boulevard", "forest canopy", "modern minimalist",
        "golden hour", "arctic frost", "desert rose", "tech innovation",
        "botanical garden", "midnight galaxy",
    ]
    if any(preset in text.lower() for preset in preset_names):
        return True
    badge_colors = _extract_badge_colors(text)
    explicit_hex = set(re.findall(r"#[0-9a-fA-F]{6}", text))
    return len(badge_colors) >= 1 or len(explicit_hex) >= 3


def check_cohesive_palette(text: str) -> bool:
    badge_colors = _extract_badge_colors(text)
    if badge_colors:
        return 1 <= len(badge_colors) <= 4
    explicit_hex = re.findall(r"#[0-9a-fA-F]{6}", text)
    from collections import Counter
    if len(explicit_hex) >= 3:
        most_common_count = Counter(explicit_hex).most_common(1)[0][1]
        return most_common_count >= 2 and len(set(explicit_hex)) <= 5
    return False


def check_ladder_order(text: str) -> bool:
    sections = [
        (r"(?i)^#{1,2}\s+(why|vision|purpose|about|what|problem)"),
        (r"(?i)^#{1,2}\s+features"),
        (r"(?i)^#{1,2}\s+(quick[\s-]?start|getting[\s-]started|usage|how to use|try it)"),
        (r"(?i)^#{1,2}\s+(install|installation|setup)"),
        (r"(?i)^#{1,2}\s+(screenshots?|how[- ]?to|examples?|demo|tutorial)"),
        (r"(?i)^#{1,2}\s+(contributing|development|dev|license)"),
    ]
    indices = []
    for pattern in sections:
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            indices.append(m.start())
    return len(indices) >= 3 and indices == sorted(indices)


ASSERTION_FUNCS = {
    "Logo appears in a centered <p align='center'> block at the top": check_logo_centered,
    "A 'Features' section appears before installation instructions": check_features_before_install,
    "Install section contains a fenced code block": check_install_codeblock,
    "Contributing or Dev section is near the bottom": check_contributing_near_bottom,
    "README is under 80 lines": check_under_80_lines,
    "Usage or quick start appears before or alongside install": check_usage_before_or_with_install,
    "No unexplained jargon": check_no_unexplained_jargon,
    "README references a named theme or applies a consistent accent color": check_theme_reference_or_accent,
    "Badges or visual elements use a cohesive color palette": check_cohesive_palette,
    "Ladder order is preserved": check_ladder_order,
}


def grade_eval(eval_dir: Path) -> dict:
    metadata_path = eval_dir / "eval_metadata.json"
    with_skill_path = eval_dir / "with_skill" / "outputs" / "README.md"
    without_skill_path = eval_dir / "without_skill" / "outputs" / "README.md"

    metadata = json.loads(metadata_path.read_text())
    results = {"eval_id": metadata["eval_id"], "eval_name": metadata["eval_name"]}

    for variant, path in [("with_skill", with_skill_path), ("without_skill", without_skill_path)]:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        expectations = []
        for assertion in metadata["assertions"]:
            fn = ASSERTION_FUNCS.get(assertion)
            passed = bool(fn(text)) if fn else False
            expectations.append({"text": assertion, "passed": passed, "evidence": "passed" if passed else "failed"})
        results[variant] = {"expectations": expectations}

    return results


def main():
    all_grades = []
    for eval_dir in sorted(WORKSPACE.glob("eval-*")):
        if not (eval_dir / "eval_metadata.json").exists():
            continue
        grades = grade_eval(eval_dir)
        all_grades.append(grades)
        for variant in ("with_skill", "without_skill"):
            out_path = eval_dir / variant / "grading.json"
            expectations = grades[variant]["expectations"]
            passed = sum(1 for e in expectations if e["passed"])
            total = len(expectations)
            grading = {
                "summary": {
                    "pass_rate": passed / total if total else 0.0,
                    "passed": passed,
                    "failed": total - passed,
                    "total": total,
                },
                "expectations": expectations,
            }
            out_path.write_text(json.dumps(grading, indent=2))

    (WORKSPACE / "all_grades.json").write_text(json.dumps(all_grades, indent=2))
    print("Grading complete. Wrote all_grades.json.")


if __name__ == "__main__":
    main()
