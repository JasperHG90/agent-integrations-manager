#!/usr/bin/env python3
"""Aggregate grading.json files into benchmark.json and benchmark.md."""

import json
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(__file__).parent


def aggregate():
    runs = []
    configs = {"with_skill": [], "without_skill": []}

    for eval_dir in sorted(WORKSPACE.glob("eval-*")):
        metadata_path = eval_dir / "eval_metadata.json"
        if not metadata_path.exists():
            continue
        metadata = json.loads(metadata_path.read_text())

        for variant in ("with_skill", "without_skill"):
            grading_path = eval_dir / variant / "grading.json"
            if not grading_path.exists():
                continue
            grading = json.loads(grading_path.read_text())
            summary = grading.get("summary", {})
            result = {
                "eval_id": metadata["eval_id"],
                "configuration": variant,
                "run_number": 1,
                "result": {
                    "pass_rate": summary.get("pass_rate", 0.0),
                    "passed": summary.get("passed", 0),
                    "failed": summary.get("failed", 0),
                    "total": summary.get("total", 0),
                    "time_seconds": 0.0,
                    "tokens": 0,
                    "tool_calls": 0,
                    "errors": 0,
                },
                "expectations": grading.get("expectations", []),
                "notes": [],
            }
            runs.append(result)
            configs[variant].append(summary.get("pass_rate", 0.0))

    def stats(values):
        if not values:
            return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
        n = len(values)
        mean = sum(values) / n
        if n > 1:
            variance = sum((x - mean) ** 2 for x in values) / (n - 1)
            stddev = variance ** 0.5
        else:
            stddev = 0.0
        return {"mean": round(mean, 4), "stddev": round(stddev, 4), "min": round(min(values), 4), "max": round(max(values), 4)}

    run_summary = {
        "with_skill": {"pass_rate": stats(configs["with_skill"])},
        "without_skill": {"pass_rate": stats(configs["without_skill"])},
    }
    delta = run_summary["with_skill"]["pass_rate"]["mean"] - run_summary["without_skill"]["pass_rate"]["mean"]
    run_summary["delta"] = {"pass_rate": f"{delta:+.2f}"}

    benchmark = {
        "metadata": {
            "skill_name": "readme-creator",
            "skill_path": "skills/readme-creator",
            "executor_model": "claude-opus-4-7",
            "analyzer_model": "claude-opus-4-7",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": sorted(set(r["eval_id"] for r in runs)),
            "runs_per_configuration": 1,
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": [],
    }

    (WORKSPACE / "benchmark.json").write_text(json.dumps(benchmark, indent=2))
    print("Generated benchmark.json")

    with_mean = run_summary["with_skill"]["pass_rate"]["mean"] * 100
    without_mean = run_summary["without_skill"]["pass_rate"]["mean"] * 100
    delta_pct = delta * 100
    md = f"""# Skill Benchmark: readme-creator

| Metric | With Skill | Without Skill | Delta |
|--------|-----------|---------------|-------|
| Pass Rate | {with_mean:.0f}% | {without_mean:.0f}% | {delta_pct:+.0f}% |
"""
    (WORKSPACE / "benchmark.md").write_text(md)
    print("Generated benchmark.md")


if __name__ == "__main__":
    aggregate()
