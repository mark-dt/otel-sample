# OTel Sample — Two-Service Topology with Dynatrace

Two Python Flask services instrumented with OpenTelemetry, exporting traces, metrics, and logs directly to Dynatrace via OTLP/HTTP.

## Architecture

```
                        ┌─────────────────────────────────────────┐
                        │              Dynatrace                  │
                        │  /api/v2/otlp/v1/traces                 │
                        │  /api/v2/otlp/v1/metrics                │
                        │  /api/v2/otlp/v1/logs                   │
                        └──────────▲──────────────▲───────────────┘
                                   │              │
                          OTLP/HTTP (protobuf)    │
                                   │              │
┌──────────────────┐  HTTP  ┌──────┴───────────┐  │
│   Load Generator ├───────►│   Service A      │  │
│  (run_load_gen)  │        │   :5000          │  │
└──────────────────┘        │                  │  │
                            │  GET /trigger    │  │
                            │  GET /health     │  │
                            └──┬───────────────┘  │
                               │                  │
                          POST /store             │
                          GET  /data              │
                               │                  │
                            ┌──▼───────────────┐  │
                            │   Service B      ├──┘
                            │   :5001          │
                            │                  │
                            │  POST /store     │
                            │  GET  /data      │
                            │  GET  /health    │
                            └──────────────────┘
```

**Service A** (port 5000) — Entry point. `GET /trigger` calls Service B's `/store` and `/data` endpoints, producing a distributed trace across both services.

**Service B** (port 5001) — Backend store. Accepts items via `POST /store` and returns them via `GET /data`. Maintains an in-memory list.

## OpenTelemetry Setup

All telemetry configuration lives in `otel_setup.py`. Each service calls `setup_telemetry()` at startup, which configures:

### Traces
- `TracerProvider` with `ALWAYS_ON` sampler
- `BatchSpanProcessor` → `OTLPSpanExporter` to `{DT_ENDPOINT}/v1/traces`
- Auto-instrumentation for Flask (inbound) and Requests (outbound) via `FlaskInstrumentor` and `RequestsInstrumentor`
- Custom spans (`trigger_request`, `store`, `data`) with semantic attributes

### Metrics
- `MeterProvider` with `PeriodicExportingMetricReader` (5s interval)
- `OTLPMetricExporter` to `{DT_ENDPOINT}/v1/metrics`
- Delta temporality for Counter, Histogram, and ObservableCounter (required by Dynatrace)
- Cumulative temporality for UpDownCounter, ObservableUpDownCounter, and ObservableGauge
- `TraceBasedExemplarFilter` enabled — attaches trace/span IDs to metric data points when recorded inside an active span

#### Metric-to-Service Correlation

Dynatrace does not automatically promote OTel resource attributes (like `service.name`) into metric dimensions. The `service.name` on the resource identifies the service for traces and logs, but metrics need it as an explicit data point attribute to be filterable/splittable by service in the Dynatrace metrics explorer.

To solve this, `setup_telemetry()` returns a `base_attrs` dict containing `{"service.name": "<name>"}`. Each service merges this into every `.add()` and `.record()` call:

```python
base_attrs = setup_telemetry("service-a", app, ...)
# ...
trigger_requests.add(1, {**base_attrs, "route": "/trigger"})
```

Additionally, the `TraceBasedExemplarFilter` on the `MeterProvider` attaches `trace_id`/`span_id` as exemplars to metric data points recorded inside an active span. This allows Dynatrace to link individual metric samples back to specific traces. All metric recordings are placed inside `start_as_current_span()` blocks to ensure exemplars are captured.

| Metric | Type | Service |
|--------|------|---------|
| `service_a_trigger_requests_total` | Counter | A |
| `service_a_trigger_latency_ms` | Histogram | A |
| `service_a_service_b_calls_total` | Counter | A |
| `service_b_store_requests_total` | Counter | B |
| `service_b_data_requests_total` | Counter | B |
| `service_b_store_latency_ms` | Histogram | B |
| `service_b_items_stored` | UpDownCounter | B |

### Logs
- `LoggerProvider` with `BatchLogRecordProcessor` → `OTLPLogExporter` to `{DT_ENDPOINT}/v1/logs`
- Python `logging` bridged to OTel via `LoggingHandler` — all `logger.info()` calls are exported as OTLP log records

#### Log-to-Trace Correlation

The `LoggingHandler` added to Python's root logger intercepts every stdlib `logging` call and converts it to an OTel `LogRecord`. During conversion, it reads the current span context from `opentelemetry.context`. If a span is active, the log record is automatically stamped with that span's `trace_id` and `span_id`. Dynatrace uses these fields to link logs to the matching distributed trace.

This means log correlation depends on **where** the log call happens relative to the span lifecycle:

```python
# service_a.py
logger.info("Trigger called")                        # NO trace context — outside any span
with tracer.start_as_current_span("trigger_request"):
    logger.info("POST /store status=%s", status)      # HAS trace context — inside active span
```

Logs emitted before or after a span will still be exported to Dynatrace but won't be linked to a trace. Only logs emitted inside a `start_as_current_span()` block get the correlation.

### Resource Attributes
Each service's telemetry carries:
- `service.name` — identifies the service in Dynatrace
- `service.version` — `1.0.1`
- `dt.cost.product` / `dt.cost.costcenter` — Dynatrace cost tracking attributes
- Dynatrace enrichment metadata (loaded from OneAgent files when running on a monitored host)

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Set Dynatrace environment
export DT_ENDPOINT="https://<env-id>.dynatrace.com/api/v2/otlp"
export DT_API_TOKEN="<token>"

# Run with load generator
python3 run_load_gen.py
```

The API token needs these scopes: `openTelemetryTrace.ingest`, `metrics.ingest`, `logs.ingest`.

## Files

| File | Purpose |
|------|---------|
| `otel_setup.py` | Shared OTel configuration (exporters, providers, instrumentation) |
| `service_a.py` | Flask service A — trigger endpoint, calls Service B |
| `service_b.py` | Flask service B — store/retrieve backend |
| `run_load_gen.py` | Starts both services and sends 200k requests to `/trigger` |
| `requirements.txt` | Python dependencies (OTel SDK 1.40.0, Flask, instrumentations) |
