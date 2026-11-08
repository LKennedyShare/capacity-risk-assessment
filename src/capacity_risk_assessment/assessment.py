"""PTU capacity assessment: reads hourly usage and produces an HTML report.

Input CSV columns:
  timestamp (ISO 8601, with time zone), deployment, app, tier, requests,
  input_tokens, output_tokens, utilization_percent (0-100+).
The "app" column may be blank; the report is then deployment-level.

Each app's traffic is replayed through the tier policy to show what would stay
on PTU, spill to pay-as-you-go, queue or be blocked at each hour's utilisation.
"""

from __future__ import annotations

import csv
import html
import math
import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO

from capacity_risk_assessment.policy import Action, App, SpendState, Tier, Utilization, evaluate

REQUIRED = ["timestamp", "deployment", "app", "tier", "requests", "input_tokens", "output_tokens",
            "utilization_percent"]
FORBIDDEN_WORDS = ("prompt", "content", "text", "user", "ip", "message", "response")
TARGETS = (0.75, 0.90)


class InvalidExport(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Pay-as-you-go price per million tokens."""

    model: str
    paygo_input_per_million: Decimal
    paygo_output_per_million: Decimal


@dataclass(frozen=True, slots=True)
class Row:
    at: datetime
    deployment: str
    app: str
    tier: Tier
    requests: int
    input_tokens: int
    output_tokens: int
    utilization: float


@dataclass(slots=True)
class Assessment:
    hours: int = 0
    attribution: bool = False
    utilization: dict[str, float] = field(default_factory=dict)
    saturated_hours: int = 0
    baseline_critical_throttled: int = 0
    policy_critical_throttled: int = 0
    actions: dict[str, dict[str, int]] = field(default_factory=dict)
    spilled_tokens: int = 0
    paygo_cost_estimate: Decimal = Decimal(0)
    peak_share: dict[str, float] = field(default_factory=dict)
    ptu_now: float = 0.0
    ptu_range: tuple[float, float] = (0.0, 0.0)
    idle_ptu_cost_estimate: Decimal = Decimal(0)
    assumptions: list[str] = field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def parse_export(text: str) -> list[Row]:
    reader = csv.DictReader(StringIO(text))
    columns = [c.strip() for c in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        raise InvalidExport(f"missing columns: {', '.join(missing)}")
    for column in columns:
        if any(word in column.lower() for word in FORBIDDEN_WORDS):
            raise InvalidExport("export contains a column that may hold content or identifiers")
    rows: list[Row] = []
    for number, record in enumerate(reader, start=2):
        try:
            at = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("naive timestamp")
            rows.append(Row(
                at=at.astimezone(UTC), deployment=record["deployment"], app=record["app"] or "(unattributed)",
                tier=Tier(record["tier"]), requests=int(record["requests"]),
                input_tokens=int(record["input_tokens"]), output_tokens=int(record["output_tokens"]),
                utilization=float(record["utilization_percent"]) / 100.0,
            ))
        except (ValueError, KeyError) as error:
            raise InvalidExport(f"row {number}: {type(error).__name__}") from None
    if not rows:
        raise InvalidExport("export has no rows")
    return rows


def assess(rows: Iterable[Row], *, ptu_count: int, ptu_hour_price: Decimal, price: ModelPrice,
               ladder_tiers: dict[Tier, App] | None = None) -> Assessment:
    rows = list(rows)
    out = Assessment()
    out.hours = len({r.at for r in rows})
    apps = {r.app for r in rows}
    out.attribution = apps != {"(unattributed)"}
    by_hour: dict[datetime, list[Row]] = defaultdict(list)
    for r in rows:
        by_hour[r.at].append(r)
    pool = {h: max(x.utilization for x in items) for h, items in by_hour.items()}
    util_values = sorted(pool.values())
    avg = sum(util_values) / len(util_values)
    out.utilization = {"average": avg, "p95": _percentile(util_values, 0.95),
                       "p99": _percentile(util_values, 0.99), "peak": util_values[-1]}
    out.saturated_hours = sum(1 for u in util_values if u >= 1.0)

    peak_cut = _percentile(util_values, 0.9)
    peak_tokens: dict[str, int] = defaultdict(int)
    for h, items in by_hour.items():
        if pool[h] >= peak_cut:
            for r in items:
                peak_tokens[r.app] += r.input_tokens + r.output_tokens
    total_peak = sum(peak_tokens.values()) or 1
    out.peak_share = {app: tokens / total_peak for app, tokens in sorted(peak_tokens.items())}

    apps_by_name: dict[str, App] = {}
    for r in rows:
        apps_by_name.setdefault(r.app, App(name=r.app, tier=r.tier))
    prior: dict[str, int] = {}
    for h in sorted(by_hour):
        for r in by_hour[h]:
            app = apps_by_name[r.app]
            if ladder_tiers and r.tier in ladder_tiers:
                app = ladder_tiers[r.tier]
            decision = evaluate(app, Utilization(value=pool[h]), SpendState(), h, prior_band=prior.get(r.app))
            prior[r.app] = decision.band if decision.band is not None else prior.get(r.app, 0)
            tally = out.actions.setdefault(r.tier.value, defaultdict(int))  # type: ignore[arg-type]
            tally[decision.action.value] += r.requests
            if r.tier is Tier.CRITICAL and pool[h] >= 1.0:
                out.baseline_critical_throttled += 1
                if decision.action is not Action.ALLOW_PTU:
                    out.policy_critical_throttled += 1
            if decision.action is Action.SPILL_PAYGO:
                tokens = r.input_tokens + r.output_tokens
                out.spilled_tokens += tokens
                out.paygo_cost_estimate += (Decimal(r.input_tokens) * price.paygo_input_per_million
                                            + Decimal(r.output_tokens) * price.paygo_output_per_million) / Decimal(1_000_000)
    out.actions = {k: dict(v) for k, v in out.actions.items()}
    out.paygo_cost_estimate = out.paygo_cost_estimate.quantize(Decimal("0.01"))

    out.ptu_now = float(ptu_count)
    low = ptu_count * out.utilization["p95"] / TARGETS[1]
    high = ptu_count * out.utilization["p95"] / TARGETS[0]
    out.ptu_range = (round(low, 1), round(high, 1))
    idle = max(0.0, ptu_count - ptu_count * avg)
    out.idle_ptu_cost_estimate = (Decimal(str(idle)) * ptu_hour_price * Decimal(out.hours)).quantize(Decimal("0.01"))

    out.assumptions = [
        "Pool utilisation per hour is the highest deployment reading in that hour.",
        "Policy decisions use the default ladder (or the override supplied) with hysteresis.",
        "Critical traffic is assumed to keep PTU in saturated hours, so it is not throttled by the policy.",
        "Without the policy, critical requests in hours at or above 100% utilisation count as throttled.",
        f"Pay-as-you-go cost uses the configured {price.model} price list and is an estimate.",
        f"PTU sizing targets {int(TARGETS[0] * 100)}% to {int(TARGETS[1] * 100)}% P95 utilisation; indicative only.",
        "Idle PTU cost is the unused share of PTUs over the period, at the configured hourly price.",
        ("Per-app attribution is present." if out.attribution else
         "No per-app attribution: results are deployment-level only."),
    ]
    return out


def render_html(a: Assessment, *, title: str = "Capacity risk assessment") -> str:
    def row(label: str, value: object) -> str:
        return f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"

    actions = "".join(
        f"<tr><td>{html.escape(tier)}</td>" + "".join(
            f"<td>{counts.get(k, 0)}</td>" for k in ("allow_ptu", "spill_paygo", "queue", "block", "downgrade_model"))
        + "</tr>" for tier, counts in a.actions.items())
    share = "".join(row(app, f"{pct:.0%} of peak-window tokens") for app, pct in a.peak_share.items())
    assumptions = "".join(f"<li>{html.escape(x)}</li>" for x in a.assumptions)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1d2330}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0}}th,td{{border:1px solid #d9dee8;padding:.35rem .6rem;text-align:left}}</style>
</head><body><h1>{html.escape(title)}</h1>
<p>Generated from the usage data supplied.</p>
<h2>Utilisation profile</h2><table>{row("Hours analysed", a.hours)}{row("Average", f"{a.utilization['average']:.0%}")}
{row("P95", f"{a.utilization['p95']:.0%}")}{row("P99", f"{a.utilization['p99']:.0%}")}{row("Peak hour", f"{a.utilization['peak']:.0%}")}
{row("Saturated hours (100% or more)", a.saturated_hours)}</table>
<h2>Critical protection</h2><table>{row("Critical throttle events without policy", a.baseline_critical_throttled)}
{row("Critical throttle events with policy", a.policy_critical_throttled)}</table>
<h2>What-if actions by tier (requests)</h2><table><tr><th>Tier</th><th>PTU</th><th>Paygo spill</th><th>Queue</th><th>Block</th><th>Downgrade</th></tr>{actions}</table>
<h2>Spillover and cost</h2><table>{row("Spilled tokens", f"{a.spilled_tokens:,}")}{row("Paygo cost (estimate)", f"{a.paygo_cost_estimate}")}</table>
<h2>Per-app share during peak windows</h2><table>{share or "<tr><td>No attribution in export.</td></tr>"}</table>
<h2>PTU sizing and idle cost</h2><table>{row("Current PTUs", a.ptu_now)}{row("Indicative range", f"{a.ptu_range[0]} to {a.ptu_range[1]} PTUs")}
{row("Idle PTU cost (estimate)", a.idle_ptu_cost_estimate)}</table>
<h2>Assumptions</h2><ul>{assumptions}</ul></body></html>"""


def synthetic_export(*, seed: int = 7, hours: int = 168, start: datetime | None = None) -> str:
    """Demo dataset: one deployment, four apps, a daily peak."""
    rng = random.Random(seed)
    start = start or datetime(2026, 10, 16, tzinfo=UTC)
    apps = [("checkout", "critical", 0.30), ("search", "standard", 0.30),
            ("batch-reports", "batch", 0.25), ("lab", "experimental", 0.15)]
    out = StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(REQUIRED)
    for h in range(hours):
        at = start + timedelta(hours=h)
        load = 0.55 + 0.45 * math.sin((h % 24 - 8) / 24 * 2 * math.pi) ** 2
        for app, tier, share in apps:
            util_share = share * load * rng.uniform(0.9, 1.1)
            requests = max(1, int(900 * util_share))
            writer.writerow([at.isoformat(), "ptu-main", app, tier, requests,
                             requests * 900, requests * 180, round(min(1.4, util_share * 2.2) * 100, 1)])
    return out.getvalue()


def _azure_metrics_client():  # pragma: no cover
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import MetricsQueryClient

    return MetricsQueryClient(DefaultAzureCredential())


def main(argv: list[str] | None = None, *, metrics_client=None) -> int:
    """Command-line entry point: ``capacity-risk-assessment``."""
    import argparse
    import sys
    from datetime import UTC, datetime
    from pathlib import Path

    from .collection import collect_rows

    parser = argparse.ArgumentParser(prog="capacity-risk-assessment",
                                     description="PTU capacity assessment report.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="CSV of hourly usage (see README for columns)")
    source.add_argument("--demo", action="store_true", help="Use the synthetic demo dataset")
    source.add_argument("--azure-resource-id", help="Azure OpenAI resource ID to read metrics from")
    parser.add_argument("--deployments", help="Comma-separated PTU deployment names (with --azure-resource-id)")
    parser.add_argument("--days", type=int, default=14, help="Days of history to read (7 to 30)")
    parser.add_argument("--output", type=Path, default=Path("capacity-assessment.html"))
    parser.add_argument("--ptus", type=int, default=20, help="PTUs currently deployed")
    parser.add_argument("--ptu-hour-price", type=Decimal, default=Decimal("1.20"),
                        help="Price per PTU-hour")
    parser.add_argument("--paygo-input-per-million", type=Decimal, required=True)
    parser.add_argument("--paygo-output-per-million", type=Decimal, required=True)
    args = parser.parse_args(argv)

    price = ModelPrice(model="configured", paygo_input_per_million=args.paygo_input_per_million,
                       paygo_output_per_million=args.paygo_output_per_million)
    if args.azure_resource_id:
        names = [name.strip() for name in (args.deployments or "").split(",") if name.strip()]
        if not names:
            print("--deployments is required with --azure-resource-id", file=sys.stderr)
            return 2
        try:
            client = metrics_client if metrics_client is not None else _azure_metrics_client()
            rows = collect_rows(client, args.azure_resource_id, names, end=datetime.now(UTC), days=args.days)
        except ValueError as error:
            print(f"collection refused: {error}", file=sys.stderr)
            return 2
    else:
        text = synthetic_export() if args.demo else args.input.read_text(encoding="utf-8")
        try:
            rows = parse_export(text)
        except InvalidExport as error:
            print(f"export refused: {error}", file=sys.stderr)
            return 2
    result = assess(rows, ptu_count=args.ptus, ptu_hour_price=args.ptu_hour_price, price=price)
    args.output.write_text(render_html(result), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
