import urllib.request
import time
import sys

def test_endpoint(url):
    print(f"Testing: {url}", flush=True)
    t0 = time.time()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            elapsed = time.time() - t0
            content = resp.read()
            print(f"-> STATUS: {resp.status} OK", flush=True)
            print(f"-> LATENCY: {elapsed:.2f}s", flush=True)
            print(f"-> CONTENT LENGTH: {len(content)} bytes", flush=True)
            return resp.status, content
    except Exception as e:
        print(f"-> ERROR: {e}", flush=True)
        return None, None

def main():
    print("=== LIVE CLOUD ENDPOINT VERIFICATION ===", flush=True)
    base_url = "https://universal-file-converter-xmxl.onrender.com"
    
    # 1. Test Dashboard Root
    status_root, content_root = test_endpoint(f"{base_url}/")
    if status_root != 200 or b"Universal File Converter" not in content_root:
        print("FAIL: Root endpoint check failed!", flush=True)
        sys.exit(1)
    if b"v1.4.0" not in content_root:
        print("FAIL: Root dashboard missing v1.4.0 badge!", flush=True)
        sys.exit(1)
    print("PASS: Root dashboard is live and valid with v1.4.0 badge!", flush=True)

    # 2. Test Diagnostics
    status_diag, content_diag = test_endpoint(f"{base_url}/api/diagnostics")
    if status_diag != 200 or b'"status":"healthy"' not in content_diag.replace(b" ", b""):
        print("FAIL: /api/diagnostics endpoint check failed!", flush=True)
        sys.exit(1)
    print("PASS: /api/diagnostics responded healthy!", flush=True)

    # 3. Test API Files
    status_api, content_api = test_endpoint(f"{base_url}/api/files")
    if status_api != 200:
        print("FAIL: /api/files endpoint check failed!", flush=True)
        sys.exit(1)
    print(f"PASS: /api/files responded: {content_api.decode('utf-8')[:100]}", flush=True)

    print("=== ALL LIVE VERIFICATION CHECKS PASSED (HTTP 200 OK) ===", flush=True)

if __name__ == "__main__":
    main()
