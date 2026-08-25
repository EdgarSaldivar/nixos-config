"""Human and machine-readable ingress evaluation rendering."""

import json

from .models import Evaluation


def render_human(evaluation: Evaluation) -> str:
    rows = [("CHECK", "HOST", "EXPECTED", "OBSERVED", "RESULT")]
    rows.extend(
        (check.check, check.hostname, check.expected, check.observed, check.outcome.upper())
        for check in evaluation.checks
    )
    widths = [max(len(row[index]) for row in rows) for index in range(5)]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    ]
    lines.append(evaluation.summary)
    return "\n".join(lines)


def render_json(evaluation: Evaluation) -> str:
    return json.dumps(
        {
            "passed": evaluation.ok,
            "counts": evaluation.counts,
            "summary": evaluation.summary,
            "checks": [
                {
                    "check": check.check,
                    "hostname": check.hostname,
                    "expected": check.expected,
                    "observed": check.observed,
                    "outcome": check.outcome,
                }
                for check in evaluation.checks
            ],
        },
        sort_keys=True,
    )
