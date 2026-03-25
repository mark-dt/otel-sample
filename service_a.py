# service_a.py
from flask import Flask, jsonify
import logging
import time
import requests

from otel_setup import setup_telemetry
from opentelemetry import trace, metrics

app = Flask(__name__)
base_attrs = setup_telemetry("service-a", app, dt_cost_center="team_a", dt_cost_product="service_topology")

SERVICE_B_URL = "http://localhost:5001"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("service-a")

tracer = trace.get_tracer("service-a")
meter = metrics.get_meter("service-a")

trigger_requests = meter.create_counter(
    name="service_a_trigger_requests_total",
    description="Number of /trigger calls",
    unit="1",
)
trigger_latency_ms = meter.create_histogram(
    name="service_a_trigger_latency_ms",
    description="Latency of /trigger handler in ms",
    unit="ms",
)
service_b_calls = meter.create_counter(
    name="service_a_service_b_calls_total",
    description="Outbound calls from A to B",
    unit="1",
)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/trigger", methods=["GET"])
def trigger():
    start = time.time()

    logger.info("Trigger called")

    with tracer.start_as_current_span("trigger_request") as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.route", "/trigger")
        for k, v in base_attrs.items():
            span.set_attribute(k, v)

        trigger_requests.add(1, {**base_attrs, "route": "/trigger", "http.method": "GET"})

        data = {"message": "Hello from Service A!"}

        service_b_calls.add(1, {**base_attrs, "method": "POST", "peer.service": "service-b"})
        post_resp = requests.post(f"{SERVICE_B_URL}/store", json=data, timeout=5)
        logger.info("POST /store status=%s", post_resp.status_code)

        service_b_calls.add(1, {**base_attrs, "method": "GET", "peer.service": "service-b"})
        get_resp = requests.get(f"{SERVICE_B_URL}/data", timeout=5)
        logger.info("GET /data status=%s", get_resp.status_code)

        elapsed_ms = (time.time() - start) * 1000.0
        trigger_latency_ms.record(elapsed_ms, {**base_attrs, "route": "/trigger"})

    return jsonify({"status": "ok", "latency_ms": round(elapsed_ms, 2)}), 200


if __name__ == '__main__':
    app.run(port=5000)

