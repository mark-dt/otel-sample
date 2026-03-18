import json
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

from opentelemetry import trace, metrics
from opentelemetry.trace import set_tracer_provider
from opentelemetry._logs import set_logger_provider

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, sampling
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from opentelemetry.sdk.metrics import (
    MeterProvider,
    Counter,
    UpDownCounter,
    Histogram,
    ObservableCounter,
    ObservableUpDownCounter,
    ObservableGauge,
)
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, AggregationTemporality
from opentelemetry.sdk.metrics._internal.exemplar import TraceBasedExemplarFilter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter  # supports preferred_temporality [2](https://opentelemetry-python.readthedocs.io/en/latest/exporter/otlp/otlp.html)

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter  # OTLP HTTP/protobuf logs exporter [3](https://opentelemetry-python.readthedocs.io/en/latest/_modules/opentelemetry/exporter/otlp/proto/http/_log_exporter.html)

from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor


def _load_json_file(path: str) -> Dict[str, Any]:
    try:
        p = Path(path)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_dynatrace_enrichment() -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    candidates = [
        "dt_metadata_e617c525669e072eebe3d0f08212e8f2.json",
        "/var/lib/dynatrace/enrichment/dt_metadata.json",
        "/var/lib/dynatrace/enrichment/dt_host_metadata.json",
    ]
    for name in candidates:
        data = _load_json_file(name)
        if not data:
            continue
        if isinstance(data, str):
            data = _load_json_file(data)
        if isinstance(data, dict):
            merged.update(data)
    return merged


_BASE_RESOURCE = _load_dynatrace_enrichment()
_BASE_RESOURCE.update(
    {
        "service.name": "python-quickstart",
        "service.version": "1.0.1",
        "dt.cost.product": "null",
        "dt.cost.costcenter": "null",
    }
)


def setup_telemetry(
    service_name: str,
    app=None,
    dt_cost_center: Optional[str] = None,
    dt_cost_product: Optional[str] = None,
    enable_flask_instrumentation: bool = True,
    enable_requests_instrumentation: bool = True,
) -> None:
    """
    Configures OTLP/HTTP protobuf exporters for:
      - traces  -> {DT_ENDPOINT}/v1/traces
      - metrics -> {DT_ENDPOINT}/v1/metrics   (delta temporality)
      - logs    -> {DT_ENDPOINT}/v1/logs
    """

    # Dynatrace OTLP base endpoint (example: https://<env>.sprint.dynatracelabs.com/api/v2/otlp)
    dt_endpoint = os.getenv("DT_ENDPOINT", "").rstrip("/")
    dt_token = os.getenv("DT_API_TOKEN", "")

    if not dt_endpoint:
        raise RuntimeError("DT_ENDPOINT is not set (example: https://<env>.sprint.dynatracelabs.com/api/v2/otlp)")
    if not dt_token:
        raise RuntimeError("DT_API_TOKEN is not set")

    headers = {"Authorization": f"Api-Token {dt_token}"}

    # Resource attributes
    attrs = dict(_BASE_RESOURCE)
    attrs["service.name"] = service_name
    if dt_cost_product is not None:
        attrs["dt.cost.product"] = dt_cost_product
    if dt_cost_center is not None:
        attrs["dt.cost.costcenter"] = dt_cost_center
    resource = Resource.create(attrs)

    # ---------------- TRACES ----------------
    tracer_provider = TracerProvider(sampler=sampling.ALWAYS_ON, resource=resource)
    set_tracer_provider(tracer_provider)

    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{dt_endpoint}/v1/traces",
                headers=headers,
            )
        )
    )

    # ---------------- METRICS (DELTA) ----------------
    # Dynatrace requires delta temporality for monotonic sums; configure exporter accordingly. [4](https://community.dynatrace.com/t5/Troubleshooting/How-to-set-up-OpenTelemetry-metrics-with-delta-temporality/ta-p/269292)[5](https://opentelemetry.io/docs/specs/otel/metrics/sdk_exporters/otlp/)
    preferred_temporality = {
        Counter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
        ObservableCounter: AggregationTemporality.DELTA,
        UpDownCounter: AggregationTemporality.CUMULATIVE,
        ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
        ObservableGauge: AggregationTemporality.CUMULATIVE,
    }

    metric_exporter = OTLPMetricExporter(
        endpoint=f"{dt_endpoint}/v1/metrics",
        headers=headers,
        preferred_temporality=preferred_temporality,
    )

    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
        exemplar_filter=TraceBasedExemplarFilter(),
    )
    metrics.set_meter_provider(meter_provider)

    # ---------------- LOGS ----------------
    # Set up an OpenTelemetry logs pipeline (provider + processor + exporter) to OTLP /v1/logs. [3](https://opentelemetry-python.readthedocs.io/en/latest/_modules/opentelemetry/exporter/otlp/proto/http/_log_exporter.html)[1](https://docs.dynatrace.com/docs/ingest-from/opentelemetry/otlp-api/ingest-logs)
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)

    log_exporter = OTLPLogExporter(
        endpoint=f"{dt_endpoint}/v1/logs",
        headers=headers,
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

    # Bridge Python logging -> OTel logs pipeline
    otel_logging_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(otel_logging_handler)
    root_logger.setLevel(logging.INFO)

    # ---------------- INSTRUMENTATION ----------------
    if app is not None and enable_flask_instrumentation:
        FlaskInstrumentor().instrument_app(app)

    if enable_requests_instrumentation:
        RequestsInstrumentor().instrument()


# Backwards-compatible alias
def setup_tracing(service_name: str, app=None, dt_cost_center: Optional[str] = None, dt_cost_product: Optional[str] = None) -> None:
    setup_telemetry(
        service_name=service_name,
        app=app,
        dt_cost_center=dt_cost_center,
        dt_cost_product=dt_cost_product,
    )

