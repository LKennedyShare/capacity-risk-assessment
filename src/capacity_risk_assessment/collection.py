"""Collects hourly PTU metrics per deployment from Azure Monitor.

Metrics are per deployment, so rows carry app '(unattributed)' and the report is
deployment-level. Needs only metric read access (Monitoring Reader).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .assessment import Row
from .policy import Tier

UTILIZATION = "AzureOpenAIProvisionedManagedUtilizationV2"
REQUESTS = "AzureOpenAIRequests"
PROMPT_TOKENS = "ProcessedPromptTokens"
GENERATED_TOKENS = "GeneratedTokens"
UNATTRIBUTED = "(unattributed)"
MAX_DAYS = 30
MIN_DAYS = 7
CHUNK = timedelta(days=1)


def _hour(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


def _points(client: Any, resource_id: str, metric: str, deployment: str, start: datetime,
            end: datetime, aggregation: str) -> list[tuple[datetime, float]]:
    escaped = deployment.replace("'", "''")
    out: list[tuple[datetime, float]] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + CHUNK, end)
        response = client.query_resource(
            resource_id,
            metric_names=[metric],
            timespan=(window_start, window_end),
            granularity=timedelta(minutes=1),
            aggregations=[aggregation],
            filter=f"ModelDeploymentName eq '{escaped}'",
        )
        for item in response.metrics:
            for series in item.timeseries:
                for point in series.data:
                    value = getattr(point, aggregation.lower(), None)
                    if value is not None:
                        out.append((point.timestamp, float(value)))
        window_start = window_end
    return out


def collect_rows(client: Any, resource_id: str, deployments: list[str], *, end: datetime, days: int) -> list[Row]:
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"days must be between {MIN_DAYS} and {MAX_DAYS}")
    if end.tzinfo is None:
        raise ValueError("end must be timezone-aware")
    start = end - timedelta(days=days)
    rows: list[Row] = []
    for deployment in deployments:
        util_max: dict[datetime, float] = defaultdict(float)
        for at, value in _points(client, resource_id, UTILIZATION, deployment, start, end, "Average"):
            util_max[_hour(at)] = max(util_max[_hour(at)], value)
        requests: dict[datetime, float] = defaultdict(float)
        for at, value in _points(client, resource_id, REQUESTS, deployment, start, end, "Total"):
            requests[_hour(at)] += value
        prompt: dict[datetime, float] = defaultdict(float)
        for at, value in _points(client, resource_id, PROMPT_TOKENS, deployment, start, end, "Total"):
            prompt[_hour(at)] += value
        generated: dict[datetime, float] = defaultdict(float)
        for at, value in _points(client, resource_id, GENERATED_TOKENS, deployment, start, end, "Total"):
            generated[_hour(at)] += value
        for hour in sorted(set(util_max) | set(requests) | set(prompt) | set(generated)):
            rows.append(Row(
                at=hour, deployment=deployment, app=UNATTRIBUTED, tier=Tier.STANDARD,
                requests=int(requests.get(hour, 0)), input_tokens=int(prompt.get(hour, 0)),
                output_tokens=int(generated.get(hour, 0)),
                utilization=min(util_max.get(hour, 0.0), 1000.0) / 100.0,
            ))
    return rows

