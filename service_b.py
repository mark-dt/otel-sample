from flask import Flask, request, jsonify
import logging
import time
from otel_setup import setup_telemetry
from opentelemetry import trace, metrics

app = Flask(__name__)
base_attrs = setup_telemetry("service-b", app, dt_cost_center="team_b", dt_cost_product="service_topology")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("service-b")
tracer = trace.get_tracer("service-b")
meter = metrics.get_meter("service-b")

store_requests = meter.create_counter(
    name="service_b_store_requests_total",
    description="Number of /store calls",
    unit="1",
)
data_requests = meter.create_counter(
    name="service_b_data_requests_total",
    description="Number of /data calls",
    unit="1",
)
store_latency_ms = meter.create_histogram(
    name="service_b_store_latency_ms",
    description="Latency of /store handler in ms",
    unit="ms",
)
items_stored = meter.create_up_down_counter(
    name="service_b_items_stored",
    description="Current number of stored items",
    unit="1",
)

_store = []


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/store", methods=["POST"])
def store():
    start = time.time()
    with tracer.start_as_current_span("store") as span:
        for k, v in base_attrs.items():
            span.set_attribute(k, v)
        body = request.get_json(force=True)
        _store.append(body)
        items_stored.add(1, base_attrs)
        store_requests.add(1, {**base_attrs, "route": "/store"})
        span.set_attribute("store.item_count", len(_store))
        logger.info("stored item count=%d", len(_store))
        elapsed_ms = (time.time() - start) * 1000.0
        store_latency_ms.record(elapsed_ms, {**base_attrs, "route": "/store"})
        return jsonify({"stored": True, "count": len(_store)})


@app.route("/data", methods=["GET"])
def data():
    with tracer.start_as_current_span("data") as span:
        for k, v in base_attrs.items():
            span.set_attribute(k, v)
        data_requests.add(1, {**base_attrs, "route": "/data"})
        span.set_attribute("store.item_count", len(_store))
        logger.info("returning item count=%d", len(_store))
        return jsonify({"items": _store})

if __name__ == "__main__":
    app.run(port=5001)

