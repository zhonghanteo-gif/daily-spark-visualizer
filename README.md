# Universal File Format Converter & File Host

A high-performance, single-file FastAPI web application for converting images, spreadsheets, and structured documents, with automatic file hosting, chunked uploads, and permanent shareable URLs.

## Features

- **Automatic Folder Setup**: Creates `storage/uploads` and `storage/hosted` automatically on startup and cleans up orphan uploads.
- **Image Conversions** (via Pillow, pillow-heif & pypdfium2):
  - Supported: `PNG`, `JPG`, `JPEG`, `WEBP`, `BMP`, `ICO`, `GIF`, `HEIC`, `HEIF`, `PDF`
  - Solid white compositing for transparent PNGs/alpha images when converting to JPG/BMP/PDF.
  - Automatic EXIF orientation transpose for mobile phone camera captures.
  - Multi-resolution Windows icon generation (`.ico`).
  - High-res downscaling (>2560px) and memory bounds to prevent OOM on 512MB RAM cloud containers.
- **Document & Note Conversions** (via python-docx, pypdfium2, and standard library):
  - Supported: `PDF`, `DOCX`, `DOC`, `RTF`, `TXT`, `MD`, `HTML`
  - PDF to Markdown / Text / HTML / CSV / JSON notes extraction.
  - Multi-page document to clean paginated PDF rendering.
  - Full HTML text extraction ignoring scripts and styles.
  - OpenXML DOCX archive fallback with XML escaping.
- **Data & Spreadsheet Conversions** (via Pandas, OpenPyXL, and Tabulate):
  - Supported: `CSV`, `JSON`, `XLSX`, `XLS`, `HTML`, `Markdown` (`.md`), `TXT`
  - Multi-column tab-delimited formatting for structured text exports.
  - Parses and converts tables across all data formats seamlessly.
- **Resumable & Chunked Uploads (Mobile Shield)**:
  - 768KB chunked uploads via `/api/upload-chunk` to eliminate mobile network timeouts.
- **Permanent File Hosting**:
  - Serves files under `/files/<filename>` with RFC 5987 UTF-8 encoded filename headers, MIME types, and inline preview or download.
  - Safe file deletion via `DELETE /api/files/<filename>`.
- **Modern Dark-Mode Dashboard**:
  - Direct Action Pickers: **Photo Gallery**, **Camera Capture**, and **Document Browser**.
  - Interactive drag-and-drop file upload zone with live image thumbnails.
  - Filter bar (All, Images, Docs, Data), search bar, and View Mode toggle (List view vs Grid / Thumbnail Gallery view).
  - Diagnostics Drawer showing server health, detected conversion engines, and live logs.

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run Application
```bash
python app.py
```
Or on Windows, double-click `run.bat`.

### 3. Open in Browser
Navigate to:
```
http://localhost:8000
```

## Running Tests
```bash
pytest tests/test_converter.py -v
```

## API Endpoints

- `GET /`: Modern dark-mode web dashboard.
- `GET /api/files`: Returns JSON list of all hosted files with metadata (sizes, timestamps, direct links, preview type).
- `POST /api/convert`: Accepts multipart form upload with `files` list and `target_format` string.
- `POST /api/upload-chunk`: Resumable / chunked file upload endpoint for 768KB chunks.
- `GET /api/diagnostics`: System health status, engine capabilities (pdfium, heif, docx), and recent logs.
- `POST /api/client-log`: Endpoint for client-side diagnostic and error telemetry.
- `GET /files/{filename}`: File server providing inline preview and `?download=1` attachment disposition.
- `DELETE /api/files/{filename}`: Permanently deletes a hosted file.
