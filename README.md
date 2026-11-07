# Universal File Format Converter & ZeroBlur Engine Studio

A high-performance FastAPI web application for converting images, spreadsheets, structured documents, and mastering social video with the **ZeroBlur Engine**, featuring automatic permanent file hosting, mobile chunked uploads, and interactive multi-theme dashboards.

## Features

- **ZeroBlur Engine Studio & Video Mastering** (`/zeroblur` & `/formats`):
  - **Live Optical Comparator**: Real-time 60 FPS canvas shader comparing platform compression blur against the ZeroBlur master with split wipe slider and 1×, 2×, 3× zoom.
  - **Dual-Mode Video Mastering**:
    - **`⚡ Cloud Master`**: Server-side 2-pass FFmpeg mastering (`/api/v1/optimize`) with Lanczos sub-pixel chroma scaling, 5×5 unsharp mask pre-compensation, and CFR lock.
    - **`Browser Master`**: Instant in-browser stream mastering and export via HTML5 `MediaRecorder`.
  - **Social Media Bypass Presets**:
    - TikTok HD (1080×1920, 60fps CFR, 9.5 Mbps)
    - WhatsApp Status Safe (720×1280, strict <14.5MB ceiling)
    - WhatsApp HD (1080×1920 dynamic bitrate budgeter)
  - **Dynamic FFmpeg Script Generator**: Generates 2-pass libx264 scripts with one-click copy.
  - **Multi-Platform Code Repository**: Production reference code for Python, FastAPI, Flutter Dart, and React Native.
- **5 Distinct Display Styles & Themes**:
  - **Modern Dark**: Sleek obsidian and emerald studio styling.
  - **Editorial**: Warm ivory newspaper style with serif typography (`Newsreader`, `Playfair Display`).
  - **Soft Liquid Glass**: Frosted translucent cards with ambient background orbs.
  - **B&W Light & Dark**: Clean high-contrast monochrome palettes.
  - Synchronized across pages via `localStorage` with a hideable navigation bar.
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
- **Modern Dashboard & Studio UI**:
  - Direct Action Pickers: **Photo Gallery**, **Camera Capture**, and **Document Browser**.
  - Interactive drag-and-drop file upload zone with live image thumbnails.
  - Filter bar (All, Images, Docs, Data, Video), search bar, and View Mode toggle (List view vs Grid / Thumbnail Gallery view).
  - Diagnostics Drawer showing server health, detected conversion engines (`pdfium`, `heif`, `docx`, `ffmpeg`), and live logs.

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
- Converter Dashboard: `http://localhost:8000/`
- ZeroBlur Engine Studio: `http://localhost:8000/zeroblur`

## Running Tests
```bash
pytest tests/test_converter.py -v
```

## API Endpoints

- `GET /`: Modern dashboard with theme switcher and compact format selection.
- `GET /zeroblur`: Interactive ZeroBlur Engine Studio for TikTok & WhatsApp video mastering (alias: `GET /formats`).
- `POST /api/v1/optimize`: ZeroBlur server-side video mastering endpoint (Lanczos + 5×5 unsharp + CFR lock via FFmpeg).
- `GET /api/files`: Returns JSON list of all hosted files with metadata (sizes, timestamps, direct links, preview type).
- `POST /api/convert`: Accepts multipart form upload with `files` list and `target_format` string.
- `POST /api/upload-chunk`: Resumable / chunked file upload endpoint for 768KB chunks.
- `GET /api/diagnostics`: System health status, engine capabilities (pdfium, heif, docx, ffmpeg), and recent logs.
- `POST /api/client-log`: Endpoint for client-side diagnostic and error telemetry.
- `GET /files/{filename}`: File server providing inline preview and `?download=1` attachment disposition.
- `DELETE /api/files/{filename}`: Permanently deletes a hosted file.
