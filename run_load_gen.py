import subprocess
import time
import requests
import threading
import os
import signal
import sys

def start_service(cmd):
    # Capture output so we can show why it died (stderr/stdout). [3](https://docs.python.org/3/library/subprocess.html)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

def pump_stream(prefix, stream):
    for line in iter(stream.readline, ""):
        if line:
            print(f"{prefix}{line}", end="")
    stream.close()

# Start service_b first
service_b = start_service(["python3", "service_b.py"])
threading.Thread(target=pump_stream, args=("[service_b] ", service_b.stdout), daemon=True).start()
threading.Thread(target=pump_stream, args=("[service_b][err] ", service_b.stderr), daemon=True).start()
time.sleep(2)

# Start service_a next
service_a = start_service(["python3", "service_a.py"])
threading.Thread(target=pump_stream, args=("[service_a] ", service_a.stdout), daemon=True).start()
threading.Thread(target=pump_stream, args=("[service_a][err] ", service_a.stderr), daemon=True).start()
time.sleep(2)

def wait_for_service(url, timeout=15):
    for _ in range(timeout):
        # If either service already exited, fail fast with returncode
        if service_a.poll() is not None:
            print(f"service_a exited early with code {service_a.returncode}")
            return False
        if service_b.poll() is not None:
            print(f"service_b exited early with code {service_b.returncode}")
            return False

        try:
            r = requests.get(url, timeout=1)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False

print("Waiting for services to become available...")

# IMPORTANT: readiness should be a simple endpoint, not /trigger. [1](https://github.com/13angs/python/blob/main/samples/flask-liveness-readiness/README.md)[2](https://oneuptime.com/blog/post/2026-02-09-readiness-probes-downstream-deps/view)
ok_a = wait_for_service("http://127.0.0.1:5000/health")
ok_b = wait_for_service("http://127.0.0.1:5001/health")

if not ok_a or not ok_b:
    print("Error: One or both services failed to start.")
    service_a.terminate()
    service_b.terminate()
    sys.exit(1)

print("Services are up.")

def generate_load(count=200000, delay=0.5):
    print("Generating load...")
    for i in range(count):
        try:
            r = requests.get("http://127.0.0.1:5000/trigger", timeout=5)
            print(f"Request {i+1}: {r.status_code}")
        except Exception as e:
            print(f"Request {i+1} failed: {e}")
        time.sleep(delay)

load_thread = threading.Thread(target=generate_load)
load_thread.start()
load_thread.join()

print("Shutting down services...")
service_a.send_signal(signal.SIGINT)
service_b.send_signal(signal.SIGINT)
service_a.wait()
service_b.wait()
print("Done.")
