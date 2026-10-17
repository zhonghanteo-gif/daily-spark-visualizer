import urllib.request
import urllib.parse
import json
import time
import sys
from pathlib import Path

BASE_URL = "https://universal-file-converter-xmxl.onrender.com"
PDF_PATH = Path(r"G:\My Drive\Antigravity\FILE CONVERTER\storage\uploads\Chapter 3 - Perception of the Self and Others in Interpersonal Communication (Notes).pdf")

def upload_live_chunk(file_id, chunk_index, total_chunks, filename, target_format, chunk_bytes):
    boundary = "----WebKitFormBoundaryMobileShield" + str(int(time.time() * 1000))
    body = bytearray()
    
    fields = [
        ("file_id", file_id),
        ("chunk_index", str(chunk_index)),
        ("total_chunks", str(total_chunks)),
        ("filename", filename),
        ("target_format", target_format),
    ]
    for k, v in fields:
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
    
    body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chunk\"; filename=\"blob\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    body.extend(chunk_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        f"{BASE_URL}/api/upload-chunk",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    print(f"=== TESTING LIVE PRODUCTION AT {BASE_URL} ===", flush=True)
    assert PDF_PATH.exists(), f"PDF missing: {PDF_PATH}"
    pdf_bytes = PDF_PATH.read_bytes()
    print(f"Loaded source PDF: {len(pdf_bytes)} bytes", flush=True)

    # 1. Test GET / Anti-Cache Headers
    print("\n1. Verifying Anti-Cache headers on GET /...", flush=True)
    req = urllib.request.Request(f"{BASE_URL}/", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        print(f"   Cache-Control: {headers.get('cache-control')}")
        print(f"   Pragma: {headers.get('pragma')}")
        print(f"   Expires: {headers.get('expires')}")
        assert "no-store" in headers.get("cache-control", ""), "Cache-Control missing no-store!"
        content = resp.read().decode("utf-8")
        assert "v1.3.0 • Mobile Shield Active" in content, "Mobile Shield badge missing!"
    print("   -> PASS: GET / returned correct headers and v1.3.0 badge!")

    # 2. Test Single Chunk PDF Upload
    print("\n2. Testing 89KB PDF single-chunk upload (target_format=pdf)...", flush=True)
    file_id = f"live_single_{int(time.time())}"
    res = upload_live_chunk(file_id, 0, 1, PDF_PATH.name, "pdf", pdf_bytes)
    print(f"   Response: {res}")
    assert res.get("status") == "complete", f"Expected complete, got {res}"
    result = res["results"][0]
    assert result["status"] == "ok"
    pdf_url = BASE_URL + result["url"]
    print(f"   Hosted URL: {pdf_url}")
    
    # Verify download of hosted PDF
    with urllib.request.urlopen(pdf_url, timeout=30) as resp:
        downloaded = resp.read()
        assert len(downloaded) == len(pdf_bytes), f"Length mismatch: {len(downloaded)} vs {len(pdf_bytes)}"
    print(f"   -> PASS: Hosted PDF verified intact ({len(downloaded)} bytes)!")

    # 3. Test Multi-Chunk WebP Conversion
    print("\n3. Testing 89KB PDF multi-chunk upload (target_format=webp)...", flush=True)
    file_id = f"live_multi_{int(time.time())}"
    chunk_size = 40000
    chunks = [pdf_bytes[i:i + chunk_size] for i in range(0, len(pdf_bytes), chunk_size)]
    for idx, c in enumerate(chunks):
        res = upload_live_chunk(file_id, idx, len(chunks), PDF_PATH.name, "webp", c)
        if idx + 1 < len(chunks):
            assert res.get("status") == "chunk_received"
        else:
            assert res.get("status") == "complete"
            assert res["results"][0]["status"] == "ok"
            webp_url = BASE_URL + res["results"][0]["url"]
            print(f"   Hosted WebP URL: {webp_url}")
            with urllib.request.urlopen(webp_url, timeout=30) as resp:
                webp_bytes = resp.read()
                print(f"   WebP size: {len(webp_bytes)} bytes")
                assert len(webp_bytes) > 1000
    print("   -> PASS: Multi-chunk WebP conversion verified!")

    print("\n=== ALL LIVE PRODUCTION CHECKS PASSED ===", flush=True)

if __name__ == "__main__":
    main()
