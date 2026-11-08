from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from capacity_risk_assessment.assessment import (
    ModelPrice,
    InvalidExport,
    assess,
    parse_export,
    render_html,
    synthetic_export,
)
from capacity_risk_assessment.policy import Tier

PRICE = ModelPrice(model="demo-model", paygo_input_per_million=Decimal("0.25"),
                   paygo_output_per_million=Decimal("2.00"))
HEADER = "timestamp,deployment,app,tier,requests,input_tokens,output_tokens,utilization_percent\n"


def export(*lines: str) -> str:
    return HEADER + "\n".join(lines) + "\n"


def test_demo_dataset_produces_a_full_assessment_and_html() -> None:
    rows = parse_export(synthetic_export(hours=72))
    result = assess(rows, ptu_count=20, ptu_hour_price=Decimal("1.20"), price=PRICE)
    assert result.hours == 72
    assert result.attribution is True
    assert 0 < result.utilization["p95"] <= result.utilization["peak"]
    assert result.policy_critical_throttled == 0
    page = render_html(result)
    assert "Critical protection" in page and "Assumptions" in page
    assert "<script" not in page.lower()


def test_critical_throttles_drop_to_zero_under_the_policy() -> None:
    rows = parse_export(export(
        "2026-10-23T12:00:00Z,ptu-1,checkout,critical,100,1000,100,120",
        "2026-10-23T12:00:00Z,ptu-1,search,standard,100,1000,100,120",
        "2026-10-23T13:00:00Z,ptu-1,checkout,critical,100,1000,100,80",
    ))
    result = assess(rows, ptu_count=10, ptu_hour_price=Decimal("1"), price=PRICE)
    assert result.baseline_critical_throttled == 1
    assert result.policy_critical_throttled == 0


def test_standard_traffic_spills_and_is_costed_as_an_estimate() -> None:
    rows = parse_export(export("2026-10-23T12:00:00Z,ptu-1,search,standard,10,1000000,0,95"))
    result = assess(rows, ptu_count=10, ptu_hour_price=Decimal("1"), price=PRICE)
    assert result.actions["standard"] == {"spill_paygo": 10}
    assert result.paygo_cost_estimate == Decimal("0.25")


def test_missing_attribution_degrades_and_says_so() -> None:
    rows = parse_export(export("2026-10-23T12:00:00Z,ptu-1,,standard,1,1,1,50"))
    result = assess(rows, ptu_count=5, ptu_hour_price=Decimal("1"), price=PRICE)
    assert result.attribution is False
    assert any("deployment-level" in line for line in result.assumptions)


def test_export_with_content_like_columns_is_refused() -> None:
    text = HEADER.replace("requests", "requests,prompt_text") + "2026-10-23T12:00:00Z,d,a,standard,1,1,1,1,hi\n"
    with pytest.raises(InvalidExport, match="content or identifiers"):
        parse_export(text)


@pytest.mark.parametrize("text", [
    "timestamp,deployment\n2026-10-23T12:00:00Z,d\n",
    HEADER,
    export("2026-10-23 12:00:00,ptu-1,a,standard,1,1,1,1"),
    export("2026-10-23T12:00:00Z,ptu-1,a,gold,1,1,1,1"),
    export("2026-10-23T12:00:00Z,ptu-1,a,standard,many,1,1,1"),
])
def test_malformed_exports_are_rejected(text: str) -> None:
    with pytest.raises(InvalidExport):
        parse_export(text)


def test_very_small_dataset_still_reports_sensible_percentiles() -> None:
    rows = parse_export(export("2026-10-23T12:00:00Z,ptu-1,a,batch,1,1,1,40"))
    result = assess(rows, ptu_count=1, ptu_hour_price=Decimal("1"), price=PRICE)
    assert result.utilization["p95"] == pytest.approx(0.40)
    assert result.hours == 1


def test_idle_cost_and_sizing_range_are_reported() -> None:
    rows = parse_export(export(
        f"{datetime(2026, 10, 23, 12, tzinfo=UTC).isoformat()},ptu-1,a,batch,1,1,1,50",
        "2026-10-23T13:00:00Z,ptu-1,a,batch,1,1,1,50",
    ))
    result = assess(rows, ptu_count=10, ptu_hour_price=Decimal("2"), price=PRICE)
    assert result.idle_ptu_cost_estimate == Decimal("20.00")
    assert result.ptu_range == (5.6, 6.7)


def test_command_line_demo_writes_a_report(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from capacity_risk_assessment.assessment import main

    out = tmp_path / "report.html"
    code = main(["--demo", "--output", str(out), "--paygo-input-per-million", "0.25",
                 "--paygo-output-per-million", "2"])
    assert code == 0
    assert "Capacity risk assessment" in out.read_text(encoding="utf-8")


def test_command_line_refuses_content_columns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from capacity_risk_assessment.assessment import main

    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER.replace("requests", "requests,prompt") + "x\n", encoding="utf-8")
    assert main(["--input", str(bad), "--paygo-input-per-million", "1",
                 "--paygo-output-per-million", "1"]) == 2