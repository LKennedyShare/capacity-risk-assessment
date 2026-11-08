# capacity-risk-assessment

Produces an HTML capacity report for Azure OpenAI provisioned throughput (PTU) deployments.

Point it at your PTU deployments, or give it a CSV of hourly usage, and it reports:

- **Utilisation profile**: average, P95, P99 and peak utilisation, and the number of saturated hours.
- **Critical protection**: how often critical apps hit a saturated PTU, compared with a tiered policy that keeps them on PTU.
- **What-if actions by tier**: how many requests would stay on PTU, spill to pay-as-you-go, queue or be blocked.
- **Spillover cost**: tokens that would spill to pay-as-you-go, and what they would cost.
- **Peak-window share**: which apps use the most tokens during the busiest hours.
- **PTU sizing**: a suggested PTU range for 75% to 90% P95 utilisation.
- **Idle PTU cost**: what the unused share of your PTUs costs over the period.

## Install

```bash
pip install .            # CSV and demo input
pip install ".[azure]"   # adds reading metrics from Azure Monitor
```

Requires Python 3.11 or later.

## Usage

Try it with the built-in demo data:

```bash
capacity-risk-assessment --demo --ptus 50 \
  --paygo-input-per-million 2.50 --paygo-output-per-million 10.00
```

Read the last 14 days of metrics from Azure:

```bash
az login
capacity-risk-assessment \
  --azure-resource-id "<resource ID of your Azure OpenAI resource>" \
  --deployments gpt-ptu-main,gpt-ptu-eu \
  --days 14 --ptus 100 --ptu-hour-price 1.00 \
  --paygo-input-per-million 2.50 --paygo-output-per-million 10.00
```

Sign-in uses `DefaultAzureCredential`, so `az login`, a managed identity or environment credentials all work. The identity needs read access to metrics (Monitoring Reader).

Use your own CSV:

```bash
capacity-risk-assessment --input usage.csv --ptus 100 \
  --paygo-input-per-million 2.50 --paygo-output-per-million 10.00
```

The report is written to `capacity-assessment.html` unless you pass `--output`.

### Options

| Option | Description |
|---|---|
| `--demo` / `--input FILE` / `--azure-resource-id ID` | Data source (choose one) |
| `--deployments` | Comma-separated PTU deployment names (with `--azure-resource-id`) |
| `--days` | Days of history to read from Azure, 7 to 30 (default 14) |
| `--ptus` | PTUs currently deployed (default 20) |
| `--ptu-hour-price` | Price per PTU-hour (default 1.20) |
| `--paygo-input-per-million` | Pay-as-you-go price per million input tokens (required) |
| `--paygo-output-per-million` | Pay-as-you-go price per million output tokens (required) |
| `--output` | Report file (default `capacity-assessment.html`) |

## CSV format

One row per deployment, app and hour:

```csv
timestamp,deployment,app,tier,requests,input_tokens,output_tokens,utilization_percent
2026-10-23T12:00:00Z,ptu-main,checkout,critical,120,108000,21600,82.5
2026-10-23T12:00:00Z,ptu-main,search,standard,95,85500,17100,82.5
```

- `timestamp`: ISO 8601 with a time zone.
- `tier`: `critical`, `standard`, `batch` or `experimental`.
- `app` may be blank. The report then covers deployments only, without a per-app breakdown.
- Columns that might hold prompt text or user identifiers (for example `prompt`, `content`, `user`) are rejected.

Metrics read from Azure are per deployment, so those reports have no per-app breakdown.

## Tier policy

Each tier has utilisation thresholds that decide what happens to its traffic:

| Tier | Policy |
|---|---|
| `critical` | Always stays on PTU |
| `standard` | PTU below 75%, spills to pay-as-you-go at 75% and above |
| `batch` | PTU below 50%, queued from 50%, blocked from 90% |
| `experimental` | PTU below 75%, blocked at 75% and above |

A 5-point hysteresis band stops decisions flipping back and forth around a threshold.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT. See [LICENSE](LICENSE).
