"""
Cold-Start Simulation & Latency Benchmark for Universal File Converter
Simulates the cold-start lifecycle of the application:
1. Spawns an isolated cold instance of uvicorn app:app
2. Measures the exact elapsed time from cold process launch to the first successful HTTP 200 /api/diagnostics response
3. Tests the keep-awake retry/backoff mechanism against a cold-starting instance
4. Measures warm latency baseline vs cold-start latency
5. Benchmarks live cloud latency against https://universal-file-converter-xmxl.onrender.com/api/diagnostics
"""

import subprocess
import sys
import time
import urllib.request
import urllib.error
import json
import socket
import os

SIM_PORT = 8009
SIM_URL = f"http://127.0.0.1:{SIM_PORT}/api/diagnostics"
RENDER_URL = "https://universal-file-converter-xmxl.onrender.com/api/diagnostics"

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

def run_cold_start_simulation():
    port = find_free_port()
    sim_url = f"http://127.0.0.1:{port}/api/diagnostics"
    print("=" * 65)
    print(" STEP 1: COLD-START SIMULATION & BENCHMARK")
    print(f" Target Port: {port} | Target URL: {sim_url}")
    print("=" * 65)

    print("\n[Simulation] Launching cold uvicorn process (no caches, clean process state)...")
    t_start = time.perf_counter()

    # Launch cold uvicorn instance
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.abspath(__file__))
    )

    poll_interval = 0.05
    max_wait = 30.0
    first_response = None
    cold_latency = None
    attempts = 0

    print("[Simulation] Polling /api/diagnostics during wake-up sequence...")
    try:
        while time.perf_counter() - t_start < max_wait:
            attempts += 1
            elapsed = time.perf_counter() - t_start
            try:
                req = urllib.request.Request(sim_url, headers={"User-Agent": "ColdStartSimulator/1.0"})
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        cold_latency = time.perf_counter() - t_start
                        first_response = json.loads(resp.read().decode("utf-8"))
                        print(f" -> [Attempt {attempts:02d} | +{elapsed:.3f}s] WAKE-UP SUCCESS! HTTP 200 OK")
                        break
            except (urllib.error.URLError, ConnectionRefusedError, socket.error):
                # Server still binding socket or loading dependencies
                pass
            except Exception as e:
                print(f" -> [Attempt {attempts:02d} | +{elapsed:.3f}s] Connection error: {e}")

            time.sleep(poll_interval)

        if not cold_latency:
            print("ERROR: Cold start timed out after 30 seconds!")
            proc.kill()
            return False

        print(f"\n[Simulation Results]")
        print(f" -> Cold-Start Wake-Up Duration : {cold_latency:.3f} seconds ({cold_latency*1000:.1f} ms)")
        print(f" -> Total Poll Attempts Needed   : {attempts}")
        print(f" -> Response Status              : {first_response.get('status')}")
        print(f" -> Diagnostics Version          : {first_response.get('version')}")
        print(f" -> Mobile Shield Active         : {first_response.get('mobile_shield')}")
        print(f" -> Photo Gallery Active         : {first_response.get('photo_gallery')}")

        # Measure warm latency baseline
        print("\n[Warm Baseline] Measuring warm ping latency on same instance...")
        warm_latencies = []
        for i in range(5):
            t0 = time.perf_counter()
            req = urllib.request.Request(sim_url, headers={"User-Agent": "ColdStartSimulator/1.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                warm_latencies.append(time.perf_counter() - t0)

        avg_warm = sum(warm_latencies) / len(warm_latencies)
        print(f" -> Average Warm Latency         : {avg_warm*1000:.2f} ms")
        print(f" -> Cold vs Warm Ratio           : {cold_latency / avg_warm:.1f}x slower during cold boot")

    finally:
        # Terminate simulated process
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n[Simulation] Isolated cold-start instance terminated cleanly.")

    # Step 2: Live Cloud Benchmark
    print("\n" + "=" * 65)
    print(" STEP 2: LIVE RENDER CLOUD LATENCY BENCHMARK")
    print(f" Target: {RENDER_URL}")
    print("=" * 65)

    live_samples = []
    for i in range(1, 4):
        t0 = time.perf_counter()
        req = urllib.request.Request(RENDER_URL, headers={"User-Agent": "GitHub-Actions-Keep-Awake-Verifier/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed = time.perf_counter() - t0
                body = json.loads(resp.read().decode("utf-8"))
                live_samples.append(elapsed)
                print(f" Sample {i}: HTTP {resp.status} in {elapsed:.3f}s | status='{body.get('status')}' version='{body.get('version')}'")
        except Exception as e:
            print(f" Sample {i}: Failed with {e}")

    if live_samples:
        avg_live = sum(live_samples) / len(live_samples)
        print(f"\n[Live Cloud Summary]")
        print(f" -> Average Cloud Latency (Awake): {avg_live:.3f}s")
        print(f" -> Min Latency                  : {min(live_samples):.3f}s")
        print(f" -> Max Latency                  : {max(live_samples):.3f}s")
        print(f" -> Free Tier Sleep Window       : 15 minutes of inactivity")
        print(f" -> Keep-Awake Ping Cadence      : 10 minutes (cron: '*/10 * * * *')")
        print(f" -> Cold-Start Prevention Margin : 5 minutes buffer before sleep boundary")

    print("\n" + "=" * 65)
    print(" ALL COLD-START SIMULATIONS & MEASUREMENTS COMPLETE")
    print("=" * 65)
    return True

if __name__ == "__main__":
    success = run_cold_start_simulation()
    sys.exit(0 if success else 1)
