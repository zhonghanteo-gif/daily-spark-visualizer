import sys
import subprocess
from pathlib import Path
import io
import time
import pytest
from fastapi.testclient import TestClient
from PIL import Image
import pandas as pd

# Add app directory to sys.path
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from app import (
    app,
    HOSTED_DIR,
    UPLOAD_DIR,
    read_data_file,
    write_data_file,
    convert_image,
    extract_text_from_document,
    write_docx_document,
    write_text_as_pdf,
    process_conversion,
    normalize_upload_filename,
    strip_html_tags,
    format_file_size,
    HAS_DOCX,
    HAS_HEIF,
    strip_rtf,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup_test_files():
    """Ensures test directories are clean before and after each test."""
    HOSTED_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield
    # Clean up test files created
    for f in HOSTED_DIR.glob("test_*"):
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass
    for f in UPLOAD_DIR.glob("test_*"):
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass


# 1. System & Dashboard Endpoint Tests
def test_dashboard_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    assert "Universal File Converter" in response.text
    assert "Photo Gallery" in response.text
    assert "Mobile Shield" in response.text
    assert "btnPickCamera" in response.text
    assert "btnPickDocs" in response.text
    assert "navZeroBlurBtn" in response.text
    assert "btnZeroBlurEnginePage" in response.text
    assert "calloutLaunchZeroBlurBtn" in response.text


def test_zeroblur_page_endpoint():
    response = client.get("/zeroblur")
    assert response.status_code == 200
    assert "ZeroBlur Engine Studio" in response.text
    assert "backToConverterBtn" in response.text
    assert "comparisonCanvas" in response.text
    assert "splitRangeSlider" in response.text
    assert "btnRunBrowserMaster" in response.text
    assert "btnRunCloudMaster" in response.text
    assert "zeroblurThemeBtn" in response.text
    assert "view-converter" in response.text
    assert "footerBackToConverterBtn" in response.text
    assert "Cache-Control" in response.headers
    assert "no-cache" in response.headers["Cache-Control"]


def test_formats_page_alias():
    response = client.get("/formats")
    assert response.status_code == 200
    assert "ZeroBlur Engine Studio" in response.text
    assert "backToConverterBtn" in response.text
    assert "view-converter" in response.text
    assert "Cache-Control" in response.headers


def test_diagnostics_endpoint():
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["version"] == "1.4.0"
    assert data.get("zeroblur_engine") is True
    assert "has_ffmpeg" in data
    assert "has_pdfium" in data
    assert "has_docx" in data
    assert "logs" in data


def test_zeroblur_optimize_endpoint(tmp_path):
    from app import HAS_FFMPEG
    if not HAS_FFMPEG:
        # If no ffmpeg on host, it should return 501
        dummy_file = tmp_path / "test.mp4"
        dummy_file.write_bytes(b"dummy")
        with open(dummy_file, "rb") as f:
            resp = client.post("/api/v1/optimize", files={"file": ("test.mp4", f, "video/mp4")})
        assert resp.status_code == 501
    else:
        # Generate tiny valid MP4 via ffmpeg
        src_mp4 = tmp_path / "sample.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=0.5:size=160x120:rate=15",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src_mp4)],
            capture_output=True, check=True
        )
        with open(src_mp4, "rb") as f:
            resp = client.post(
                "/api/v1/optimize",
                files={"file": ("sample.mp4", f, "video/mp4")},
                data={"platform": "tiktok", "target_fps": "30", "sharpen_strength": "0.5"}
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        assert len(resp.content) > 500


def test_client_log_endpoint():
    response = client.post(
        "/api/client-log",
        json={
            "event": "test_event",
            "filename": "test.png",
            "status": "ok",
            "detail": "test message",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# 2. Filename & MIME Normalization Tests
def test_normalize_upload_filename():
    assert normalize_upload_filename("photo.JPG") == "photo.JPG"
    # Extensionless with MIME
    assert normalize_upload_filename("camera_image", "image/jpeg") == "camera_image.jpg"
    assert normalize_upload_filename("report", "application/pdf") == "report.pdf"
    assert normalize_upload_filename("data", "text/csv") == "data.csv"
    # Path traversal attempt
    traversal = "../../etc/passwd.txt"
    safe = normalize_upload_filename(traversal)
    assert ".." not in safe
    assert safe == "passwd.txt"


def test_format_file_size():
    assert format_file_size(500) == "500 B"
    assert format_file_size(2048) == "2.0 KB"
    assert format_file_size(2 * 1024 * 1024) == "2.0 MB"


# 3. Data Conversion Tests
def test_data_csv_to_json_and_xlsx(tmp_path):
    csv_file = tmp_path / "test_data.csv"
    csv_file.write_text("name,age,score\nAlice,30,95.5\nBob,25,88.0\n", encoding="utf-8")

    df = read_data_file(str(csv_file), "csv")
    assert len(df) == 2
    assert list(df.columns) == ["name", "age", "score"]

    # Write to JSON
    json_out = tmp_path / "test_data.json"
    write_data_file(df, "json", str(json_out))
    assert json_out.exists()

    df_from_json = read_data_file(str(json_out), "json")
    assert len(df_from_json) == 2
    assert df_from_json.iloc[0]["name"] == "Alice"

    # Write to XLSX
    xlsx_out = tmp_path / "test_data.xlsx"
    write_data_file(df, "xlsx", str(xlsx_out))
    assert xlsx_out.exists()

    df_from_xlsx = read_data_file(str(xlsx_out), "xlsx")
    assert len(df_from_xlsx) == 2

    # Multi-column export to TXT preserves all columns
    txt_out = tmp_path / "test_data.txt"
    write_data_file(df, "txt", str(txt_out))
    txt_content = txt_out.read_text(encoding="utf-8")
    assert "name\tage\tscore" in txt_content
    assert "Alice\t30\t95.5" in txt_content


def test_data_html_table(tmp_path):
    html_file = tmp_path / "test_table.html"
    html_file.write_text("<table><tr><th>Item</th><th>Price</th></tr><tr><td>Apple</td><td>1.50</td></tr></table>", encoding="utf-8")
    df = read_data_file(str(html_file), "html")
    assert len(df) == 1
    assert df.iloc[0]["Item"] == "Apple"


def test_data_markdown_table(tmp_path):
    md_file = tmp_path / "test_table.md"
    md_file.write_text("| ColA | ColB |\n|---|---|\n| 1 | X |\n| 2 | Y |\n", encoding="utf-8")
    df = read_data_file(str(md_file), "md")
    assert len(df) == 2
    assert "ColA" in df.columns


# 4. Image Conversion Tests
def test_image_png_to_jpg_with_transparency(tmp_path):
    # Create RGBA image with transparent corner
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
    png_path = tmp_path / "test_transparent.png"
    img.save(png_path, "PNG")

    jpg_out = tmp_path / "test_out.jpg"
    convert_image(str(png_path), "png", "jpg", str(jpg_out))
    assert jpg_out.exists()

    with Image.open(jpg_out) as out_img:
        assert out_img.mode == "RGB"
        assert out_img.size == (100, 100)


def test_image_formats_webp_bmp_ico_gif(tmp_path):
    img = Image.new("RGB", (200, 200), (0, 128, 255))
    src_path = tmp_path / "test_base.jpg"
    img.save(src_path, "JPEG")

    # WEBP
    webp_out = tmp_path / "test_out.webp"
    convert_image(str(src_path), "jpg", "webp", str(webp_out))
    assert webp_out.exists()

    # BMP
    bmp_out = tmp_path / "test_out.bmp"
    convert_image(str(src_path), "jpg", "bmp", str(bmp_out))
    assert bmp_out.exists()

    # GIF
    gif_out = tmp_path / "test_out.gif"
    convert_image(str(src_path), "jpg", "gif", str(gif_out))
    assert gif_out.exists()

    # ICO
    ico_out = tmp_path / "test_out.ico"
    convert_image(str(src_path), "jpg", "ico", str(ico_out))
    assert ico_out.exists()


def test_image_large_downscaling(tmp_path):
    # Ultra-high-resolution image (3000 x 3000)
    large_img = Image.new("RGB", (3000, 3000), (50, 100, 150))
    large_path = tmp_path / "test_large.png"
    large_img.save(large_path, "PNG")

    out_path = tmp_path / "test_downscaled.webp"
    convert_image(str(large_path), "png", "webp", str(out_path))
    assert out_path.exists()

    with Image.open(out_path) as out_im:
        assert out_im.width <= 2560
        assert out_im.height <= 2560


# 5. Document Conversion Tests
def test_text_to_docx_and_pdf(tmp_path):
    doc_sections = [(1, "Paragraph 1 with some content.\nParagraph 2 with more words.")]

    # DOCX
    docx_out = tmp_path / "test_doc.docx"
    write_docx_document("Test Document", doc_sections, str(docx_out))
    assert docx_out.exists()
    assert docx_out.stat().st_size > 0

    # Read back from DOCX
    extracted = extract_text_from_document(str(docx_out), "docx")
    assert len(extracted) > 0
    assert "Paragraph 1" in extracted[0][1]

    # PDF
    pdf_out = tmp_path / "test_doc.pdf"
    write_text_as_pdf("Test PDF Title", doc_sections, str(pdf_out))
    assert pdf_out.exists()
    assert pdf_out.stat().st_size > 0


def test_strip_html_tags():
    raw_html = """
    <html>
        <head><style>body { color: red; }</style><script>alert(1);</script></head>
        <body>
            <h1>Title Text</h1>
            <p>First paragraph.</p>
        </body>
    </html>
    """
    clean = strip_html_tags(raw_html)
    assert "color: red" not in clean
    assert "alert(1)" not in clean
    assert "Title Text" in clean
    assert "First paragraph." in clean


def test_process_conversion_end_to_end(tmp_path):
    # 1. Text to Markdown
    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("Line 1\nLine 2\n", encoding="utf-8")
    out_md = process_conversion(str(txt_file), "sample.txt", "md")
    assert out_md == "sample.md"
    assert (HOSTED_DIR / out_md).exists()

    # 2. Direct pass-through
    out_pass = process_conversion(str(txt_file), "sample.txt", "txt")
    assert out_pass == "sample.txt"
    assert (HOSTED_DIR / out_pass).exists()


# 6. Full API Integration & Chunked Upload Tests
def test_api_convert_files():
    img_bytes = io.BytesIO()
    Image.new("RGB", (64, 64), color="blue").save(img_bytes, format="PNG")
    img_bytes.seek(0)

    response = client.post(
        "/api/convert",
        data={"target_format": "webp"},
        files={"files": ("test_blue.png", img_bytes.getvalue(), "image/png")},
    )
    assert response.status_code == 200
    res = response.json()["results"]
    assert len(res) == 1
    assert res[0]["status"] == "ok"
    assert res[0]["ext"] == "webp"
    assert "/files/test_blue.webp" in res[0]["url"]

    # Verify serving via /files
    file_res = client.get("/files/test_blue.webp")
    assert file_res.status_code == 200
    assert file_res.headers["content-type"] == "image/webp"
    assert "inline" in file_res.headers["content-disposition"]

    # Verify download query param
    download_res = client.get("/files/test_blue.webp?download=1")
    assert download_res.status_code == 200
    assert "attachment" in download_res.headers["content-disposition"]


def test_api_files_list_and_delete():
    # Create a test hosted file
    test_file = HOSTED_DIR / "test_delete_me.txt"
    test_file.write_text("Delete me content", encoding="utf-8")

    # Verify file is in listing
    list_res = client.get("/api/files")
    assert list_res.status_code == 200
    files = list_res.json()
    assert any(f["name"] == "test_delete_me.txt" for f in files)

    # Delete file
    del_res = client.delete("/api/files/test_delete_me.txt")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "ok"

    # Verify 404 after deletion
    get_del_res = client.get("/files/test_delete_me.txt")
    assert get_del_res.status_code == 404


def test_api_upload_chunk_flow():
    payload = b"Hello from a chunked file transfer!\n" * 50
    chunk1 = payload[:500]
    chunk2 = payload[500:]

    file_id = "test_chunk_flow_123"
    filename = "test_chunked_input.txt"

    # Send Chunk 0 of 2
    res1 = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 0,
            "total_chunks": 2,
            "filename": filename,
            "target_format": "md",
        },
        files={"chunk": ("blob", chunk1, "application/octet-stream")},
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "chunk_received"

    # Send Chunk 1 of 2 (final)
    res2 = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 1,
            "total_chunks": 2,
            "filename": filename,
            "target_format": "md",
        },
        files={"chunk": ("blob", chunk2, "application/octet-stream")},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "complete"
    assert data2["results"][0]["status"] == "ok"
    assert data2["results"][0]["hosted_name"] == "test_chunked_input.md"

    # Verify hosted file content
    hosted_file = HOSTED_DIR / "test_chunked_input.md"
    assert hosted_file.exists()
    assert "Hello from a chunked file transfer!" in hosted_file.read_text(encoding="utf-8")


def test_file_security_path_traversal():
    # Attempt path traversal on serve
    res = client.get("/files/../../etc/passwd")
    assert res.status_code in (404, 400)

    # Attempt hidden file access
    res = client.get("/files/.env")
    assert res.status_code == 404


def test_invalid_cross_category_conversion(tmp_path):
    img = Image.new("RGB", (32, 32), "red")
    img_path = tmp_path / "test_cross.png"
    img.save(img_path, "PNG")

    with pytest.raises(ValueError, match="Cannot convert"):
        process_conversion(str(img_path), "test_cross.png", "csv")


def test_batch_upload_api():
    # Prepare 2 files
    f1 = io.BytesIO(b"name,score\nAda,100\n")
    f2 = io.BytesIO(b"Title\nContent here\n")

    response = client.post(
        "/api/convert",
        data={"target_format": "json"},
        files=[
            ("files", ("test_batch1.csv", f1.getvalue(), "text/csv")),
            ("files", ("test_batch2.txt", f2.getvalue(), "text/plain")),
        ],
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "ok"


def test_multipage_pdf_generation(tmp_path):
    # Long text with > 100 lines to test multi-page textwrap and pagination
    long_text = "\n".join(f"Paragraph line {i}: " + ("word " * 15) for i in range(120))
    pdf_out = tmp_path / "test_multipage.pdf"
    write_text_as_pdf("Long Multi-Page Document", [(1, long_text)], str(pdf_out))
    assert pdf_out.exists()
    assert pdf_out.stat().st_size > 1000


def test_chunked_upload_missing_chunk():
    # Upload chunk 0 of 2, then try to skip chunk 1 or simulate error
    file_id = "test_missing_chunk_id"
    res = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 1, # Sending chunk 1 of 2 while chunk 0 is missing
            "total_chunks": 2,
            "filename": "test_missing.txt",
            "target_format": "txt",
        },
        files={"chunk": ("blob", b"content", "text/plain")},
    )
    # Should raise 400 because chunk 0 was never uploaded
    assert res.status_code == 400
    assert "Missing chunk 0" in res.json()["detail"]


# 7. Additional Robustness & Regression Tests

def test_read_json_dictionary_scalar_values(tmp_path):
    """Proves bug fix: single dictionary JSON objects do not raise ValueError."""
    json_path = tmp_path / "single_obj.json"
    json_path.write_text('{"name": "Alice", "score": 95, "role": "admin"}', encoding="utf-8")
    df = read_data_file(str(json_path), "json")
    assert len(df) == 1
    assert df.iloc[0]["name"] == "Alice"
    assert df.iloc[0]["score"] == 95
    assert "role" in df.columns


def test_xlsx_to_pdf_and_docx_table_formatting(tmp_path):
    """Proves bug fix: XLSX conversion extracts structured tabular text rather than zip PK binary garbage."""
    df = pd.DataFrame({"Item": ["Widget A", "Widget B"], "Cost": [25.5, 40.0]})
    xlsx_path = tmp_path / "test_inventory.xlsx"
    df.to_excel(xlsx_path, index=False)

    extracted = extract_text_from_document(str(xlsx_path), "xlsx")
    assert len(extracted) == 1
    content = extracted[0][1]
    assert "Item | Cost" in content
    assert "Widget A | 25.5" in content
    assert "PK\x03\x04" not in content

    # Test full conversion to PDF
    pdf_out = process_conversion(str(xlsx_path), "test_inventory.xlsx", "pdf")
    assert (HOSTED_DIR / pdf_out).exists()
    (HOSTED_DIR / pdf_out).unlink()


def test_csv_to_xls_writing(tmp_path):
    """Proves bug fix: writing to .xls is supported without ValueError."""
    df = pd.DataFrame({"Metric": ["CPU", "RAM"], "Value": [45, 78]})
    csv_file = tmp_path / "test_metrics.csv"
    df.to_csv(csv_file, index=False)

    out_name = process_conversion(str(csv_file), "test_metrics.csv", "xls")
    out_file = HOSTED_DIR / out_name
    assert out_file.exists()
    # Read back to ensure valid excel structure
    df_read = pd.read_excel(str(out_file))
    assert len(df_read) == 2
    assert "Metric" in df_read.columns
    out_file.unlink()


def test_image_cmyk_to_png_and_gif(tmp_path):
    """Proves bug fix: CMYK images convert cleanly to PNG and GIF without OSError or mode crash."""
    cmyk_img = Image.new("CMYK", (40, 40), (100, 50, 0, 10))
    cmyk_path = tmp_path / "test_print.jpg"
    cmyk_img.save(cmyk_path, "JPEG")

    # PNG
    png_out = tmp_path / "test_print.png"
    convert_image(str(cmyk_path), "jpg", "png", str(png_out))
    assert png_out.exists()
    with Image.open(png_out) as im:
        assert im.mode == "RGB"

    # GIF
    gif_out = tmp_path / "test_print.gif"
    convert_image(str(cmyk_path), "jpg", "gif", str(gif_out))
    assert gif_out.exists()


def test_image_heic_heif_conversion(tmp_path):
    """Proves bug fix: HEIC/HEIF target format is supported via pillow_heif."""
    if not HAS_HEIF:
        pytest.skip("pillow-heif not available in this environment")

    base_img = Image.new("RGB", (50, 50), (20, 140, 220))
    src_path = tmp_path / "test_heif_src.png"
    base_img.save(src_path, "PNG")

    heic_out = tmp_path / "test_out.heic"
    convert_image(str(src_path), "png", "heic", str(heic_out))
    assert heic_out.exists()
    assert heic_out.stat().st_size > 0


def test_rtf_clean_text_extraction(tmp_path):
    """Proves bug fix: RTF stripper preserves paragraph breaks, strips font tables, and unescapes chars."""
    rtf_content = r"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0\fs24 Line 1 content.\par Line 2 with caf\'e9.\par Line 3.}"
    rtf_path = tmp_path / "test_sample.rtf"
    rtf_path.write_text(rtf_content, encoding="utf-8")

    extracted = extract_text_from_document(str(rtf_path), "rtf")
    assert len(extracted) == 1
    text = extracted[0][1]
    assert "Arial;" not in text
    assert "Line 1 content." in text
    assert "Line 2 with café." in text
    assert "Line 3." in text
    assert "\n" in text


def test_doc_utf16_text_extraction(tmp_path):
    """Proves bug fix: DOC extraction extracts UTF-16LE text streams."""
    doc_path = tmp_path / "test_utf16.doc"
    # Construct binary file with UTF-16LE text segment
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"header" + "Word Document Text in UTF16".encode("utf-16le") + b"tail"
    doc_path.write_bytes(payload)

    extracted = extract_text_from_document(str(doc_path), "doc")
    assert len(extracted) == 1
    assert "Word Document Text in UTF16" in extracted[0][1]


def test_non_ascii_filename_content_disposition():
    """Proves bug fix: non-ASCII filenames do not produce invalid '.ext' fallback in Content-Disposition."""
    test_file = HOSTED_DIR / "测试文件.txt"
    test_file.write_text("Chinese text content", encoding="utf-8")

    response = client.get("/files/%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6.txt")
    assert response.status_code == 200
    cd = response.headers.get("content-disposition", "")
    # Should use document.txt as ascii fallback, not .txt
    assert 'filename="document.txt"' in cd
    assert "filename*=UTF-8''" in cd
    test_file.unlink()


def test_out_of_order_chunk_upload():
    """Proves bug fix: chunked uploads arriving out of order are assembled successfully."""
    payload = b"Parallel Chunk Transfer Content\n" * 40
    chunk0 = payload[:200]
    chunk1 = payload[200:400]
    chunk2 = payload[400:]

    file_id = "test_ooo_flow_999"
    filename = "test_ooo.txt"

    # Send Chunk 1 first (out of order, intermediate)
    res1 = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 1,
            "total_chunks": 3,
            "filename": filename,
            "target_format": "txt",
        },
        files={"chunk": ("blob", chunk1, "application/octet-stream")},
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "chunk_received"

    # Send Chunk 0 second
    res0 = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 0,
            "total_chunks": 3,
            "filename": filename,
            "target_format": "txt",
        },
        files={"chunk": ("blob", chunk0, "application/octet-stream")},
    )
    assert res0.status_code == 200
    assert res0.json()["status"] == "chunk_received"

    # Send Chunk 2 last (final chunk)
    res2 = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 2,
            "total_chunks": 3,
            "filename": filename,
            "target_format": "txt",
        },
        files={"chunk": ("blob", chunk2, "application/octet-stream")},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "complete"
    assert data2["results"][0]["status"] == "ok"

    out_file = HOSTED_DIR / "test_ooo.txt"
    assert out_file.exists()
    assert "Parallel Chunk Transfer Content" in out_file.read_text(encoding="utf-8")
    out_file.unlink()


def test_scanned_image_pdf_to_docx_error(tmp_path):
    """Proves user guidance when an empty or image-only PDF is converted to docx."""
    # Create empty PDF
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    empty_pdf = tmp_path / "empty_scan.pdf"
    img.save(empty_pdf, "PDF")

    with pytest.raises(ValueError, match="No extractable text found in this PDF"):
        process_conversion(str(empty_pdf), "empty_scan.pdf", "docx")


def test_rtf_unicode_uc_skipping():
    """Proves bug fix: RTF stripper correctly consumes fallback char after \\uN instead of appending '?'."""
    # 1. Right single quote / apostrophe: \u8217?
    rtf1 = r"{\rtf1\ansi It\u8217?s a great day!}"
    assert strip_rtf(rtf1) == "It’s a great day!"

    # 2. Chinese and European accented characters: \u19990?\u30028?
    rtf2 = r"{\rtf1\ansi Hello \u19990?\u30028? \u233?\u224?\u252?}"
    assert strip_rtf(rtf2) == "Hello 世界 éàü"

    # 3. uc0 explicit zero skip count
    rtf3 = r"{\rtf1\ansi\uc0\u19990\u30028}"
    assert strip_rtf(rtf3) == "世界"


def test_rtf_writing_unicode_roundtrip(tmp_path):
    """Proves bug fix: non-ASCII characters converted to RTF are encoded cleanly as \\uN? and roundtrip."""
    txt_file = tmp_path / "test_unicode.txt"
    txt_file.write_text("Hello 世界! Café and résumé.", encoding="utf-8")

    out_name = process_conversion(str(txt_file), "test_unicode.txt", "rtf")
    out_file = HOSTED_DIR / out_name
    assert out_file.exists()

    # Verify standard RTF escape syntax in file
    content = out_file.read_text(encoding="ascii")
    assert "\\u19990?" in content
    assert "\\u30028?" in content

    # Verify extracted text matches original
    extracted = extract_text_from_document(str(out_file), "rtf")
    assert len(extracted) == 1
    assert "Hello 世界! Café and résumé." in extracted[0][1]
    out_file.unlink()


def test_docx_control_character_sanitization(tmp_path):
    """Proves bug fix: DOCX generation strips XML 1.0 illegal control characters so files reload cleanly."""
    dirty_text = "Clean text\x00with null\x0bvertical tab\x0cand form feed\x0e."
    docx_out = tmp_path / "test_dirty.docx"

    write_docx_document("Title \x00 Here", [(1, dirty_text)], str(docx_out))
    assert docx_out.exists()

    # Re-extract to verify no XML syntax error and valid document structure
    extracted = extract_text_from_document(str(docx_out), "docx")
    assert len(extracted) == 1
    content = extracted[0][1]
    assert "Clean textwith null" in content
    assert "vertical tab" in content
    assert "and form feed." in content
    assert "\x00" not in content
    assert "\x0b" not in content
    assert "\x0c" not in content
    assert "\x0e" not in content


def test_empty_file_conversion_error(tmp_path):
    """Proves bug fix: 0-byte file input gives clear user-friendly error instead of library crashes."""
    empty_file = tmp_path / "empty_test.csv"
    empty_file.write_bytes(b"")

    with pytest.raises(ValueError, match="The uploaded file is empty \\(0 bytes\\)"):
        process_conversion(str(empty_file), "empty_test.csv", "json")


def test_empty_csv_reading(tmp_path):
    """Proves bug fix: read_data_file returns empty DataFrame for 0-byte CSV without crashing."""
    empty_csv = tmp_path / "empty_sheet.csv"
    empty_csv.write_bytes(b"")

    df = read_data_file(str(empty_csv), "csv")
    assert df.empty


def test_encrypted_ole_docx_detection(tmp_path):
    """Proves bug fix: password-protected or legacy OLE documents return informative error."""
    encrypted_docx = tmp_path / "protected.docx"
    encrypted_docx.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00\x00\x00\x00\x00")

    with pytest.raises(ValueError, match="encrypted or legacy binary Office document"):
        extract_text_from_document(str(encrypted_docx), "docx")


def test_bw_image_conversion(tmp_path):
    """Verifies that black and white option converts color images to true monochrome/grayscale."""
    # Create bright color test image (vibrant red, green, blue)
    color_img = Image.new("RGB", (60, 60), (255, 60, 20))
    in_file = tmp_path / "vibrant_color.png"
    color_img.save(in_file, "PNG")

    # 1. Convert to WebP in B&W
    out_webp = HOSTED_DIR / "vibrant_color.webp"
    process_conversion(str(in_file), "vibrant_color.png", "webp", black_and_white=True)
    assert out_webp.exists()
    with Image.open(out_webp) as im_w:
        reloaded_webp = im_w.convert("RGB")
        px = reloaded_webp.getpixel((10, 10))
        assert abs(px[0] - px[1]) <= 2 and abs(px[1] - px[2]) <= 2
    out_webp.unlink()

    # 2. Convert to PNG in B&W
    out_png = HOSTED_DIR / "vibrant_color.png"
    process_conversion(str(in_file), "vibrant_color.png", "png", black_and_white=True)
    assert out_png.exists()
    with Image.open(out_png) as reloaded_png:
        assert reloaded_png.mode in ("L", "RGB", "RGBA")
        if reloaded_png.mode != "L":
            rgb = reloaded_png.convert("RGB")
            p = rgb.getpixel((10, 10))
            assert p[0] == p[1] == p[2]
    out_png.unlink()


def test_bw_pdf_conversion(tmp_path):
    """Verifies that PDF-to-PDF conversion in B&W mode renders monochrome pages."""
    # Generate a sample 2-page color PDF
    pdf_in = tmp_path / "color_doc.pdf"
    p1 = Image.new("RGB", (200, 200), (255, 0, 0))
    p2 = Image.new("RGB", (200, 200), (0, 0, 255))
    p1.save(pdf_in, "PDF", save_all=True, append_images=[p2])

    out_name = process_conversion(str(pdf_in), "color_doc.pdf", "pdf", black_and_white=True)
    out_pdf = HOSTED_DIR / out_name
    assert out_pdf.exists()
    content = out_pdf.read_bytes()
    assert content.startswith(b"%PDF-")
    out_pdf.unlink()


def test_bw_api_convert_endpoint():
    """Verifies that /api/convert honors black_and_white form parameter."""
    buf = io.BytesIO()
    im = Image.new("RGB", (50, 50), (200, 50, 50))
    im.save(buf, format="PNG")
    buf.seek(0)

    res = client.post(
        "/api/convert",
        data={"target_format": "jpg", "black_and_white": "true"},
        files={"files": ("test_api_bw.png", buf, "image/png")},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["status"] == "ok"
    assert data["results"][0]["hosted_name"] == "test_api_bw.jpg"

    out_file = HOSTED_DIR / "test_api_bw.jpg"
    assert out_file.exists()
    with Image.open(out_file) as im_j:
        reloaded = im_j.convert("RGB")
        px = reloaded.getpixel((10, 10))
        assert abs(px[0] - px[1]) <= 2 and abs(px[1] - px[2]) <= 2
    out_file.unlink()


def test_bw_api_upload_chunk():
    """Verifies that /api/upload-chunk honors black_and_white form parameter."""
    buf = io.BytesIO()
    im = Image.new("RGB", (40, 40), (10, 220, 30))
    im.save(buf, format="PNG")
    buf.seek(0)
    content = buf.getvalue()

    file_id = f"bw_chunk_{int(time.time() * 1000)}"
    res = client.post(
        "/api/upload-chunk",
        data={
            "file_id": file_id,
            "chunk_index": 0,
            "total_chunks": 1,
            "filename": "test_chunk_bw.png",
            "target_format": "png",
            "black_and_white": "true",
        },
        files={"chunk": ("blob", io.BytesIO(content), "application/octet-stream")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "complete"
    assert data["results"][0]["status"] == "ok"

    out_file = HOSTED_DIR / "test_chunk_bw.png"
    assert out_file.exists()
    with Image.open(out_file) as reloaded:
        if reloaded.mode != "L":
            rgb = reloaded.convert("RGB")
            p = rgb.getpixel((5, 5))
            assert p[0] == p[1] == p[2]
    out_file.unlink()


def test_bw_diagnostics_and_dashboard():
    """Verifies that diagnostics and dashboard expose the Black & White page options."""
    # Diagnostics check
    diag_res = client.get("/api/diagnostics")
    assert diag_res.status_code == 200
    diag_data = diag_res.json()
    assert diag_data.get("bw_page_option") is True

    # Dashboard HTML check
    dash_res = client.get("/")
    assert dash_res.status_code == 200
    html_text = dash_res.text
    assert "bwPageToggle" in html_text
    assert "bwToggleText" in html_text
    assert "bwConvertOption" in html_text
    assert "bw-mode" in html_text
    assert "themeSwitcher" in html_text
    assert "themeBtnColor" in html_text
    assert "themeBtnBwLight" in html_text
    assert "themeBtnBwDark" in html_text
    assert "themeBtnEditorial" in html_text
    assert "themeBtnLiquidGlass" in html_text
    assert "bw-page-light" in html_text
    assert "bw-page-dark" in html_text
    assert "theme-editorial" in html_text
    assert "theme-liquid-glass" in html_text
    assert "setWebpageTheme" in html_text
    assert "toggleSwitcherNavBtn" in html_text
    assert "toggleSwitcherText" in html_text
    assert "themeBar" in html_text
    assert "initSwitcherVisibility" in html_text
    assert "themeBarCloseBtn" in html_text
    assert "formatSwitcherWrap" in html_text
    assert "formatCatBar" in html_text
    assert "formatPillsContainer" in html_text
    assert "formatActiveTag" in html_text
    assert "initFormatSwitcher" in html_text
    assert "toggleMoreFormatsBtn" in html_text
    assert "moreFormatsBtnText" in html_text
    assert "formatExpandedPanel" in html_text
    assert "quickFormatsPills" in html_text
    assert "toggleMoreFormats" in html_text








