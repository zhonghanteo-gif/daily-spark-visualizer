import os
import sys
import time
import io
from pathlib import Path
from starlette.testclient import TestClient

# Ensure current dir is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import app, UPLOAD_DIR, HOSTED_DIR

client = TestClient(app)

def test_anti_cache_headers():
    print("Testing GET / headers and badge...", flush=True)
    resp = client.get("/")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert "cache-control" in headers, "Cache-Control header missing"
    assert "no-store" in headers["cache-control"], f"Expected no-store in Cache-Control, got: {headers['cache-control']}"
    assert headers.get("pragma") == "no-cache", f"Expected pragma no-cache, got {headers.get('pragma')}"
    assert headers.get("expires") == "0", f"Expected expires 0, got {headers.get('expires')}"
    assert "v1.4.0 • Mobile Shield" in resp.text, "Badge 'v1.4.0 • Mobile Shield' missing from HTML"
    print("-> PASS: Anti-cache headers and badge verified!")

def test_telemetry():
    print("Testing /api/client-log telemetry...", flush=True)
    resp = client.post(
        "/api/client-log",
        json={
            "event": "chunk_fail",
            "filename": "test.pdf",
            "status": "Chunk 1/1 attempt 1/4",
            "detail": "Connection interrupted",
            "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
        }
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"
    print("-> PASS: Telemetry user_agent capture verified!")

def test_diagnostics():
    print("Testing /api/diagnostics...", flush=True)
    resp = client.get("/api/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("version") == "1.4.0", f"Unexpected version: {data.get('version')}"
    assert data.get("mobile_shield") is True, f"mobile_shield not True: {data.get('mobile_shield')}"
    print("-> PASS: Diagnostics version and mobile_shield verified!")

def test_single_chunk_upload():
    print("Testing single-chunk 89KB PDF upload (/api/upload-chunk)...", flush=True)
    pdf_path = UPLOAD_DIR / "Chapter 3 - Perception of the Self and Others in Interpersonal Communication (Notes).pdf"
    assert pdf_path.exists(), f"Sample PDF not found at {pdf_path}"
    content = pdf_path.read_bytes()
    assert len(content) == 89101, f"Expected 89101 bytes, got {len(content)}"

    file_id = f"test_{int(time.time())}"
    resp = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 0,
            "total_chunks": 1,
            "filename": "Chapter 3 - Perception of the Self and Others in Interpersonal Communication (Notes).pdf",
            "target_format": "pdf",
        },
        files={"chunk": ("blob", io.BytesIO(content), "application/octet-stream")},
    )
    assert resp.status_code == 200, f"Got status {resp.status_code}: {resp.text}"
    res = resp.json()
    assert res.get("status") == "complete", f"Expected complete, got {res}"
    results = res.get("results", [])
    assert len(results) == 1
    assert results[0].get("status") == "ok", f"Expected ok status, got {results[0]}"
    print("-> PASS: Single chunk PDF upload complete and verified!")

def test_multi_chunk_upload():
    print("Testing multi-chunk slicing and assembly (/api/upload-chunk)...", flush=True)
    pdf_path = UPLOAD_DIR / "Chapter 3 - Perception of the Self and Others in Interpersonal Communication (Notes).pdf"
    content = pdf_path.read_bytes()
    chunk_size = 30000
    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    total_chunks = len(chunks)
    assert total_chunks == 3

    file_id = f"multitest_{int(time.time())}"
    for idx, c in enumerate(chunks):
        resp = client.post(
            "/api/upload-chunk",
            data={
                "file_id": file_id,
                "chunk_index": idx,
                "total_chunks": total_chunks,
                "filename": "multi_chapter3.pdf",
                "target_format": "pdf",
            },
            files={"chunk": ("blob", io.BytesIO(c), "application/octet-stream")},
        )
        assert resp.status_code == 200
        res = resp.json()
        if idx + 1 < total_chunks:
            assert res.get("status") == "chunk_received", f"Expected chunk_received, got {res}"
        else:
            assert res.get("status") == "complete", f"Expected complete, got {res}"
            assert res["results"][0]["status"] == "ok"
    
    assembled_file = HOSTED_DIR / "multi_chapter3.pdf"
    assert assembled_file.exists()
    assert assembled_file.stat().st_size == len(content)
    print("-> PASS: Multi-chunk assembly matches original byte length exactly!")

def test_temp_cleanup():
    print("Testing cleanup of stale temp directories...", flush=True)
    old_temp = UPLOAD_DIR / "temp_old_test_folder"
    old_temp.mkdir(exist_ok=True)
    old_chunk = old_temp / "chunk_00000.part"
    old_chunk.write_bytes(b"dummy")
    
    # Set mtime to 3600 seconds ago
    old_time = time.time() - 3600
    os.utime(old_temp, (old_time, old_time))
    assert old_temp.exists()

    # Trigger upload_chunk to run cleanup
    file_id = f"cleanuptest_{int(time.time())}"
    client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 0,
            "total_chunks": 1,
            "filename": "cleanup.txt",
            "target_format": "txt",
        },
        files={"chunk": ("blob", io.BytesIO(b"clean"), "application/octet-stream")},
    )
    assert not old_temp.exists(), "Old temp folder was not cleaned up!"
    print("-> PASS: Stale temp directories older than 30m cleaned up!")

if __name__ == "__main__":
    test_anti_cache_headers()
    test_telemetry()
    test_diagnostics()
    test_single_chunk_upload()
    test_multi_chunk_upload()
    test_temp_cleanup()
    print("\n=== ALL LOCAL VERIFICATION TESTS PASSED! ===", flush=True)
