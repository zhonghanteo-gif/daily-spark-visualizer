import urllib.request
import urllib.parse
import json
import time
import io
import sys
from PIL import Image

BASE_URL = "https://universal-file-converter-xmxl.onrender.com"

def run_tests():
    print(f"=== E2E INDEPENDENT CLOUD VERIFICATION AT {BASE_URL} ===")

    # 1. Root & Diagnostics Check
    print("\n[1/4] Checking Root & Diagnostics API...")
    t0 = time.time()
    req = urllib.request.Request(f"{BASE_URL}/api/diagnostics", headers={"User-Agent": "E2ETester/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        diag = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - t0
        print(f" -> Latency: {elapsed:.2f}s")
        print(f" -> Status: {diag.get('status')}")
        print(f" -> Version: {diag.get('version')}")
        print(f" -> Photo Gallery Active: {diag.get('photo_gallery')}")
        print(f" -> Mobile Shield Active: {diag.get('mobile_shield')}")
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        assert diag.get("status") == "healthy", f"Diagnostics unhealthy: {diag}"
        assert diag.get("photo_gallery") is True, "photo_gallery flag missing!"

    # 2. Image Creation & Upload & Conversion
    print("\n[2/4] Testing Image Upload & Conversion (JPG -> WebP)...")
    img = Image.new("RGB", (320, 240), color=(50, 120, 200))
    img_buf = io.BytesIO()
    img.save(img_buf, format="JPEG", quality=90)
    img_bytes = img_buf.getvalue()
    print(f" -> Created test image: {len(img_bytes)} bytes")

    file_id = f"cloud_e2e_{int(time.time())}"
    filename = f"cloud_photo_{int(time.time())}.jpg"
    boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
    body = bytearray()
    fields = [
        ("file_id", file_id),
        ("chunk_index", "0"),
        ("total_chunks", "1"),
        ("filename", filename),
        ("target_format", "webp"),
    ]
    for k, v in fields:
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chunk\"; filename=\"blob\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    body.extend(img_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        f"{BASE_URL}/api/upload-chunk",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15",
        },
        method="POST"
    )

    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp:
        upload_elapsed = time.time() - t0
        res = json.loads(resp.read().decode("utf-8"))
        print(f" -> Upload & Conversion Latency: {upload_elapsed:.2f}s")
        print(f" -> Status: {res.get('status')}")
        assert res.get("status") == "complete", f"Unexpected status: {res}"
        item = res["results"][0]
        assert item["status"] == "ok", f"Item conversion error: {item}"
        hosted_name = item["hosted_name"]
        webp_url = f"{BASE_URL}{item['url']}"
        print(f" -> Converted Hosted Name: {hosted_name}")
        print(f" -> Converted File URL: {webp_url}")

    # 3. Direct Download & Integrity Check
    print("\n[3/4] Testing Direct Cloud File Download...")
    t0 = time.time()
    with urllib.request.urlopen(webp_url, timeout=30) as resp:
        dl_elapsed = time.time() - t0
        content = resp.read()
        print(f" -> Downloaded: {len(content)} bytes in {dl_elapsed:.2f}s (HTTP {resp.status})")
        assert resp.status == 200, f"Download failed: {resp.status}"
        assert len(content) > 100, "Downloaded content too small"
        # Validate that the downloaded file is a valid WebP image
        downloaded_img = Image.open(io.BytesIO(content))
        assert downloaded_img.format == "WEBP", f"Expected WEBP format, got {downloaded_img.format}"
        print(f" -> Validated Image: Format={downloaded_img.format}, Dimensions={downloaded_img.size}")

    # 4. Gallery API View & Verification
    print("\n[4/4] Testing Gallery API (/api/files)...")
    t0 = time.time()
    with urllib.request.urlopen(f"{BASE_URL}/api/files", timeout=30) as resp:
        gallery_elapsed = time.time() - t0
        files = json.loads(resp.read().decode("utf-8"))
        print(f" -> Retrieved {len(files)} files in {gallery_elapsed:.2f}s")
        matching = [f for f in files if f["name"] == hosted_name]
        assert len(matching) > 0, f"File {hosted_name} not found in gallery response!"
        found = matching[0]
        print(f" -> Found Gallery Entry: Name={found['name']}, Ext={found['ext']}, Size={found['size_formatted']}, Modified={found['modified']}")

    print("\n=== ALL END-TO-END CLOUD INDEPENDENCE TESTS PASSED (100% OK) ===")

if __name__ == "__main__":
    run_tests()
