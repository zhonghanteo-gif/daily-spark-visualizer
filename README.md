# Universal File Format Converter & File Host

A high-performance, single-file FastAPI web application for converting images, spreadsheets, and structured documents, with automatic file hosting and permanent shareable URLs.

## Features

- **Automatic Folder Setup**: Creates `storage/uploads` and `storage/hosted` automatically on startup.
- **Image & Document Conversions** (via Pillow & pypdfium2):
  - Supported: `PNG`, `JPG`, `JPEG`, `WEBP`, `BMP`, `ICO`, `PDF`
  - Automatic RGBA-to-RGB conversion for alpha-incompatible formats (JPEG, PDF).
  - High-resolution multi-size Windows icon generation (`.ico`).
- **Data & Spreadsheet Conversions** (via Pandas, OpenPyXL, and Tabulate):
  - Supported: `CSV`, `JSON`, `XLSX`, `HTML`, `Markdown` (`.md`)
  - Parses and converts tables across all formats seamlessly.
- **Permanent File Hosting**:
  - All converted files are mounted under `/files/<filename>`.
- **Modern Dark-Mode Dashboard**:
  - Interactive drag-and-drop file upload zone.
  - Multi-file concurrent conversion.
  - Live hosted file manager with one-click **Copy Link** and direct **Download** buttons.

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

## API Endpoints

- `GET /`: Modern dark-mode web dashboard.
- `GET /api/files`: Returns JSON list of all hosted files with metadata (sizes, timestamps, direct links).
- `POST /api/convert`: Accepts multipart form upload with `files` list and `target_format` string.
- `GET /files/{filename}`: Static file server providing permanent access to converted files.
