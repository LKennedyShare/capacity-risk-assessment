from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from capacity_risk_assessment.assessment import ModelPrice, assess, main
from capacity_risk_assessment.collection import UNATTRIBUTED, UTILIZATION, collect_rows

RESOURCE = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/accounts/acc"
END = datetime(2026, 10, 23, 12, tzinfo=UTC)


class FakeMetrics:
    """Answers the same query shape as azure-monitor-query, one point per minute per window."""

    def __init__(self) -> None:
        self.filters: list[str] = []
        self.windows = 0

    def query_resource(self, resource_id, *, metric_names, timespan, granularity, aggregations, filter):
        self.filters.append(filter)
        self.windows += 1
        start, end = timespan
        metric = metric_names[0]
        points = []
        at = start
        while at < end:
            if metric == UTILIZATION:
                value = 40.0 + (at.minute % 60)
                points.append(SimpleNamespace(timestamp=at, average=value, total=None))
            else:
                points.append(SimpleNamespace(timestamp=at, average=None, total=2.0))
            at += timedelta(minutes=1)
        return SimpleNamespace(metrics=[SimpleNamespace(timeseries=[SimpleNamespace(data=points)])])


def test_rows_are_hourly_deployment_level_and_labelled_unattributed() -> None:
    rows = collect_rows(FakeMetrics(), RESOURCE, ["ptu-main"], end=END, days=7)
    assert len(rows) == 7 * 24
    assert {row.app for row in rows} == {UNATTRIBUTED}
    assert {row.deployment for row in rows} == {"ptu-main"}
    assert all(0.0 <= row.utilization <= 1.0 for row in rows)
    assert all(row.requests == 120 for row in rows)        # 60 minutes at 2 requests
    assert all(row.input_tokens == 120 for row in rows)


def test_utilisation_is_the_hourly_peak_not_the_average() -> None:
    rows = collect_rows(FakeMetrics(), RESOURCE, ["ptu-main"], end=END, days=7)
    assert all(abs(row.utilization - 0.99) < 1e-9 for row in rows)  # minute 59 gives 40 + 59


def test_assessment_runs_on_collected_rows_and_says_it_is_deployment_level() -> None:
    rows = collect_rows(FakeMetrics(), RESOURCE, ["ptu-main"], end=END, days=7)
    price = ModelPrice(model="configured", paygo_input_per_million=Decimal("1"),
                       paygo_output_per_million=Decimal("2"))
    result = assess(rows, ptu_count=20, ptu_hour_price=Decimal("1.2"), price=price)
    assert result.attribution is False
    assert any("deployment-level" in line for line in result.assumptions)


def test_queries_are_chunked_per_day_and_filter_escapes_quotes() -> None:
    fake = FakeMetrics()
    collect_rows(fake, RESOURCE, ["o'brien"], end=END, days=14)
    assert fake.windows == 14 * 4                       # four metrics, one query per day
    assert set(fake.filters) == {"ModelDeploymentName eq 'o''brien'"}


@pytest.mark.parametrize("days", [0, 6, 31])
def test_day_range_is_enforced(days: int) -> None:
    with pytest.raises(ValueError):
        collect_rows(FakeMetrics(), RESOURCE, ["d"], end=END, days=days)


def test_naive_end_time_is_refused() -> None:
    with pytest.raises(ValueError):
        collect_rows(FakeMetrics(), RESOURCE, ["d"], end=datetime(2026, 10, 23, 12), days=7)


def test_cli_collects_from_azure_and_writes_the_report(tmp_path) -> None:
    out = tmp_path / "report.html"
    code = main(["--azure-resource-id", RESOURCE, "--deployments", "ptu-main", "--days", "7",
                 "--output", str(out), "--paygo-input-per-million", "1", "--paygo-output-per-million", "2"],
                metrics_client=FakeMetrics())
    assert code == 0
    html = out.read_text(encoding="utf-8")
    assert "deployment-level only" in html


def test_cli_refuses_azure_collection_without_deployments(tmp_path, capsys) -> None:
    code = main(["--azure-resource-id", RESOURCE, "--output", str(tmp_path / "x.html"),
                 "--paygo-input-per-million", "1", "--paygo-output-per-million", "2"],
                metrics_client=FakeMetrics())
    assert code == 2
    assert "--deployments is required" in capsys.readouterr().err

