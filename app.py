import os
import io
import shutil
import gc
from pathlib import Path
from datetime import datetime
import urllib.parse
import traceback
from contextlib import asynccontextmanager

from pydantic import BaseModel
import pandas as pd
from PIL import Image, ImageOps
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

CONVERSION_LOGS = []

def log_event(status: str, msg: str, details: dict = None):
    CONVERSION_LOGS.append({
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": status,
        "message": msg,
        "details": details or {}
    })
    if len(CONVERSION_LOGS) > 60:
        CONVERSION_LOGS.pop(0)

class ClientLogPayload(BaseModel):
    event: str = ""
    filename: str = ""
    status: str = ""
    detail: str = ""

# Setup directory paths relative to this script
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
HOSTED_DIR = STORAGE_DIR / "hosted"

# Ensure directories exist
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
HOSTED_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "bmp", "ico", "pdf", "heic", "heif"}
DATA_FORMATS = {"csv", "json", "xlsx", "html", "md", "txt"}

# Try importing pillow_heif for iPhone photo support
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except ImportError:
    HAS_HEIF = False

# Try importing pypdfium2 for high-fidelity PDF-to-image reading
try:
    import pypdfium2 as pdfium
    HAS_PDFIUM = True
except ImportError:
    HAS_PDFIUM = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure storage folders exist on startup
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    HOSTED_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Universal File Format Converter & File Host",
    description="Convert images, spreadsheets, and structured data, and host them with permanent shareable URLs.",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for mobile browsers and PWA web apps
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount /files to serve static hosted content
app.mount("/files", StaticFiles(directory=str(HOSTED_DIR)), name="files")


def read_data_file(input_path: str, ext: str) -> pd.DataFrame:
    """Reads structured data from supported formats into a pandas DataFrame."""
    ext = ext.lower().lstrip(".")
    if ext == "csv":
        return pd.read_csv(input_path)
    elif ext == "xlsx":
        return pd.read_excel(input_path)
    elif ext == "json":
        return pd.read_json(input_path)
    elif ext == "html":
        tables = pd.read_html(input_path)
        if not tables:
            raise ValueError("No table found in the HTML file.")
        return tables[0]
    elif ext == "md":
        with open(input_path, "r", encoding="utf-8") as f:
            lines = [line.strip().strip("|") for line in f if line.strip()]
        clean_lines = [line for line in lines if not all(c in "-:| " for c in line)]
        if clean_lines:
            return pd.read_csv(
                io.StringIO("\n".join(clean_lines)),
                sep=r"\s*\|\s*",
                engine="python",
            )
        raise ValueError("Could not parse table rows from the Markdown file.")
    elif ext == "txt":
        with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f if line.strip()]
        return pd.DataFrame({"line": range(1, len(lines) + 1), "text": lines})
    else:
        raise ValueError(f"Reading from .{ext} is not supported.")


def write_data_file(df: pd.DataFrame, target_ext: str, output_path: str):
    """Writes a pandas DataFrame to the desired output format."""
    target_ext = target_ext.lower().lstrip(".")
    if target_ext == "csv":
        df.to_csv(output_path, index=False)
    elif target_ext == "xlsx":
        df.to_excel(output_path, index=False)
    elif target_ext == "json":
        df.to_json(output_path, orient="records", indent=2)
    elif target_ext == "html":
        df.to_html(output_path, index=False, classes="table", border=0)
    elif target_ext == "md":
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(df.to_markdown(index=False))
    elif target_ext == "txt":
        with open(output_path, "w", encoding="utf-8") as f:
            for _, row in df.iterrows():
                f.write(str(row.values[0]) + "\n")
    else:
        raise ValueError(f"Writing to .{target_ext} is not supported.")


def convert_image(input_path: str, source_ext: str, target_ext: str, output_path: str):
    """Converts images between PNG, JPG, JPEG, WEBP, BMP, ICO, and PDF with strict memory bounds."""
    source_ext = source_ext.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")

    pdf_doc = None
    page = None
    bitmap = None
    img = None
    try:
        # If source is PDF, extract page 1 as PIL Image
        if source_ext == "pdf":
            if not HAS_PDFIUM:
                raise ValueError("PDF rendering requires pypdfium2.")
            pdf_doc = pdfium.PdfDocument(input_path)
            if len(pdf_doc) == 0:
                raise ValueError("PDF file is empty.")
            page = pdf_doc[0]
            # Use scale=1.0 (72-150 DPI) to prevent Linux OOM killer on 512MB RAM cloud container
            bitmap = page.render(scale=1.0)
            img = bitmap.to_pil()
        else:
            img = Image.open(input_path)
            try:
                # Correct phone camera orientation from EXIF metadata
                transposed = ImageOps.exif_transpose(img)
                if transposed is not None:
                    img = transposed
            except Exception:
                pass

        # Prevent 512MB RAM OOM crash on cloud containers: downscale ultra-high-res camera photos (>2560px)
        max_dim = 2560
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)

        if target_ext in ("jpg", "jpeg"):
            if img.mode in ("RGBA", "LA", "P", "CMYK"):
                img = img.convert("RGB")
            img.save(output_path, "JPEG", quality=92, optimize=True)
        elif target_ext == "png":
            img.save(output_path, "PNG", optimize=True)
        elif target_ext == "webp":
            img.save(output_path, "WEBP", quality=90)
        elif target_ext == "bmp":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(output_path, "BMP")
        elif target_ext == "ico":
            if img.size[0] > 256 or img.size[1] > 256:
                img = img.resize((256, 256), Image.Resampling.BILINEAR)
            if img.mode not in ("RGBA", "RGB"):
                img = img.convert("RGBA")
            img.save(
                output_path,
                format="ICO",
                sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
        elif target_ext == "pdf":
            if img.mode in ("RGBA", "LA", "P", "CMYK"):
                img = img.convert("RGB")
            img.save(output_path, "PDF", resolution=100.0)
        else:
            raise ValueError(f"Target format .{target_ext} is not a supported image format.")
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass
        if bitmap is not None:
            try:
                bitmap.close()
            except Exception:
                pass
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if pdf_doc is not None:
            try:
                pdf_doc.close()
            except Exception:
                pass
        gc.collect()


def process_conversion(input_path: str, orig_name: str, target_ext: str) -> str:
    """Dispatches file conversion based on format categories with memory safety."""
    base_name = Path(orig_name).stem
    source_ext = Path(orig_name).suffix.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")

    output_filename = f"{base_name}.{target_ext}"
    output_path = str(HOSTED_DIR / output_filename)

    # 1. PDF Direct pass-through (preserves all pages)
    if source_ext == "pdf" and target_ext == "pdf":
        shutil.copyfile(input_path, output_path)
        log_event("info", f"PDF direct pass-through hosted: {output_filename}")
        return output_filename

    # 2. PDF to Notes/Text (Markdown, Plain Text, HTML, CSV, JSON extraction)
    if source_ext == "pdf" and target_ext in ("md", "txt", "html", "csv", "json"):
        if not HAS_PDFIUM:
            raise ValueError("PDF text extraction requires pypdfium2.")
        
        pdf = None
        all_text = []
        try:
            pdf = pdfium.PdfDocument(input_path)
            num_pages = len(pdf)
            for page_idx in range(num_pages):
                page = None
                textpage = None
                try:
                    page = pdf[page_idx]
                    textpage = page.get_textpage()
                    page_text = textpage.get_text_range().strip()
                    if page_text:
                        all_text.append((page_idx + 1, page_text))
                except Exception:
                    continue
                finally:
                    if textpage is not None:
                        try:
                            textpage.close()
                        except Exception:
                            pass
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
        finally:
            if pdf is not None:
                try:
                    pdf.close()
                except Exception:
                    pass
            gc.collect()

        if not all_text:
            raise ValueError(
                "No extractable text found in this PDF (it may contain scanned image pages). "
                "Select 'PDF Document (.pdf)' to host it directly, or 'WebP/PNG' to convert as an image."
            )

        if target_ext == "md":
            lines = [f"# {base_name}\n"]
            for p_num, text in all_text:
                lines.append(f"\n## Page {p_num}\n\n{text}\n")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            log_event("info", f"PDF converted to Markdown notes: {output_filename} ({len(all_text)} pages)")
            return output_filename

        elif target_ext == "txt":
            lines = [f"{base_name}\n" + "=" * len(base_name)]
            for p_num, text in all_text:
                lines.append(f"\n--- Page {p_num} ---\n\n{text}\n")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            log_event("info", f"PDF converted to Plain Text: {output_filename} ({len(all_text)} pages)")
            return output_filename

        elif target_ext == "html":
            html_parts = [
                "<!DOCTYPE html><html><head><meta charset='utf-8'>",
                f"<title>{base_name}</title>",
                "<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:840px;margin:30px auto;padding:0 20px;line-height:1.6;color:#1e293b;background:#f8fafc;} h1{color:#0f172a;} .page{background:#ffffff;padding:24px;margin-bottom:24px;border-radius:10px;border:1px solid #e2e8f0;white-space:pre-wrap;box-shadow:0 1px 3px rgba(0,0,0,0.05);} .page-hdr{font-size:0.85rem;color:#64748b;font-weight:600;margin-bottom:8px;}</style></head><body>",
                f"<h1>{base_name}</h1>",
            ]
            for p_num, text in all_text:
                html_parts.append(f"<div class='page'><div class='page-hdr'>Page {p_num}</div>{text}</div>")
            html_parts.append("</body></html>")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(html_parts))
            log_event("info", f"PDF converted to HTML notes: {output_filename} ({len(all_text)} pages)")
            return output_filename

        elif target_ext in ("csv", "json"):
            records = [{"page": p_num, "content": text} for p_num, text in all_text]
            df = pd.DataFrame(records)
            write_data_file(df, target_ext, output_path)
            log_event("info", f"PDF converted to {target_ext.upper()}: {output_filename}")
            return output_filename

    # 3. Image Conversions
    if source_ext in IMAGE_FORMATS and target_ext in IMAGE_FORMATS:
        convert_image(input_path, source_ext, target_ext, output_path)
        log_event("info", f"Image converted: {orig_name} -> {output_filename}")
        return output_filename

    # 4. Data / Spreadsheet Conversions
    if source_ext in DATA_FORMATS and target_ext in DATA_FORMATS:
        df = read_data_file(input_path, source_ext)
        write_data_file(df, target_ext, output_path)
        log_event("info", f"Data file converted: {orig_name} -> {output_filename}")
        return output_filename

    raise ValueError(
        f"Cannot convert from .{source_ext} to .{target_ext}. Both must belong to either Images/Docs or Data/Spreadsheets."
    )


def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


@app.post("/api/convert")
async def convert_files(
    target_format: str = Form(...),
    files: list[UploadFile] = File(...),
):
    results = []
    for file in files:
        if not file.filename:
            continue
        raw_path = str(UPLOAD_DIR / file.filename)
        with open(raw_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            out_name = process_conversion(raw_path, file.filename, target_format)
            out_file = HOSTED_DIR / out_name
            size_bytes = out_file.stat().st_size if out_file.exists() else 0
            safe_url = f"/files/{urllib.parse.quote(out_name)}"
            results.append({
                "original": file.filename,
                "hosted_name": out_name,
                "url": safe_url,
                "size_formatted": format_file_size(size_bytes),
                "status": "ok",
            })
        except Exception as e:
            results.append({
                "original": file.filename,
                "error": str(e),
                "status": "error",
            })

    return {"results": results}


@app.post("/api/upload-chunk")
async def upload_chunk(
    file_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    target_format: str = Form(...),
    chunk: UploadFile = File(...),
):
    """Handles chunked file uploads (768KB chunks) to prevent mobile network timeouts."""
    safe_file_id = "".join(c for c in file_id if c.isalnum() or c in ("-", "_"))
    safe_filename = Path(filename).name
    temp_dir = UPLOAD_DIR / f"temp_{safe_file_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    chunk_path = temp_dir / f"chunk_{chunk_index:05d}.part"
    with open(chunk_path, "wb") as buffer:
        shutil.copyfileobj(chunk.file, buffer)

    if chunk_index + 1 == total_chunks:
        # All chunks received -> assemble final file
        final_path = UPLOAD_DIR / safe_filename
        with open(final_path, "wb") as outfile:
            for i in range(total_chunks):
                c_file = temp_dir / f"chunk_{i:05d}.part"
                if not c_file.exists():
                    log_event("error", f"Missing chunk {i} for {safe_filename}")
                    raise HTTPException(status_code=400, detail=f"Missing chunk {i}")
                with open(c_file, "rb") as infile:
                    shutil.copyfileobj(infile, outfile)

        # Clean up temporary chunk files
        shutil.rmtree(temp_dir, ignore_errors=True)

        try:
            log_event("info", f"Assembled file ready for conversion: {safe_filename} -> {target_format}")
            out_name = process_conversion(str(final_path), safe_filename, target_format)
            out_file = HOSTED_DIR / out_name
            size_bytes = out_file.stat().st_size if out_file.exists() else 0
            safe_url = f"/files/{urllib.parse.quote(out_name)}"
            log_event("info", f"Successfully converted and hosted: {out_name} ({size_bytes} bytes)")
            return {
                "status": "complete",
                "results": [{
                    "original": safe_filename,
                    "hosted_name": out_name,
                    "url": safe_url,
                    "size_formatted": format_file_size(size_bytes),
                    "status": "ok",
                }]
            }
        except Exception as e:
            tb = traceback.format_exc()
            log_event("error", f"Conversion exception for {safe_filename}: {str(e)}", {"trace": tb})
            return {
                "status": "complete",
                "results": [{
                    "original": safe_filename,
                    "error": str(e),
                    "status": "error",
                }]
            }

    return {
        "status": "chunk_received",
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
    }


@app.post("/api/client-log")
async def client_log(payload: ClientLogPayload):
    log_event("client", f"[{payload.event}] {payload.filename}: {payload.detail}", {"status": payload.status})
    return {"status": "ok"}


@app.get("/api/diagnostics")
async def get_diagnostics():
    return {
        "status": "healthy",
        "has_pdfium": HAS_PDFIUM,
        "has_heif": HAS_HEIF,
        "logs": CONVERSION_LOGS[-30:],
    }


@app.get("/api/files")
async def list_hosted_files():
    file_list = []
    if HOSTED_DIR.exists():
        for f in HOSTED_DIR.iterdir():
            if f.is_file() and not f.name.startswith("."):
                stat = f.stat()
                file_list.append({
                    "name": f.name,
                    "url": f"/files/{urllib.parse.quote(f.name)}",
                    "size_bytes": stat.st_size,
                    "size_formatted": format_file_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "ext": f.suffix.lower().lstrip("."),
                })
    # Sort files by newest modified first
    file_list.sort(key=lambda x: x["modified"], reverse=True)
    return file_list


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Universal Converter & File Host</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: #111827;
            --card-border: #1f293d;
            --accent-primary: #3b82f6;
            --accent-hover: #2563eb;
            --accent-glow: rgba(59, 130, 246, 0.25);
            --text-main: #f9fafb;
            --text-muted: #9ca3af;
            --text-dim: #6b7280;
            --success: #10b981;
            --danger: #ef4444;
            --radius-md: 10px;
            --radius-lg: 16px;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            min-height: 100vh;
            padding: 40px 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .container {
            width: 100%;
            max-width: 820px;
            display: flex;
            flex-direction: column;
            gap: 28px;
        }

        .header {
            text-align: center;
            margin-bottom: 8px;
        }

        .header-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(59, 130, 246, 0.12);
            color: #60a5fa;
            border: 1px solid rgba(59, 130, 246, 0.3);
            border-radius: 9999px;
            padding: 4px 14px;
            font-size: 0.8rem;
            font-weight: 600;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .header h1 {
            font-size: 2.2rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            background: linear-gradient(135deg, #ffffff 40%, #94a3b8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header p {
            color: var(--text-muted);
            margin-top: 6px;
            font-size: 0.95rem;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: var(--radius-lg);
            padding: 28px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
        }

        .card-title {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .badge-count {
            background: #1e293b;
            color: #38bdf8;
            font-size: 0.8rem;
            font-weight: 600;
            padding: 3px 10px;
            border-radius: 9999px;
            border: 1px solid #334155;
        }

        /* Drag and Drop Zone */
        .dropzone {
            border: 2px dashed #334155;
            border-radius: var(--radius-md);
            padding: 36px 20px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s ease;
            background: rgba(15, 23, 42, 0.4);
            position: relative;
        }

        .dropzone:hover, .dropzone.dragover {
            border-color: var(--accent-primary);
            background: rgba(59, 130, 246, 0.06);
            box-shadow: 0 0 20px var(--accent-glow);
        }

        .dropzone-icon {
            width: 48px;
            height: 48px;
            stroke: #60a5fa;
            margin-bottom: 12px;
        }

        .dropzone-text {
            font-weight: 600;
            font-size: 1rem;
            margin-bottom: 6px;
        }

        .dropzone-sub {
            font-size: 0.82rem;
            color: var(--text-dim);
        }

        #fileInput {
            display: none;
        }

        /* Staged file chips */
        .staged-files {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 14px;
        }

        .file-chip {
            background: #1e293b;
            border: 1px solid #334155;
            color: #cbd5e1;
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 0.82rem;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-family: 'JetBrains Mono', monospace;
        }

        .file-chip-remove {
            cursor: pointer;
            color: #94a3b8;
            font-weight: bold;
        }

        .file-chip-remove:hover {
            color: var(--danger);
        }

        .file-chip-badge {
            background: rgba(245, 158, 11, 0.18);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.35);
            font-size: 0.7rem;
            padding: 1px 6px;
            border-radius: 4px;
        }

        .mobile-opt-row {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 14px;
            font-size: 0.85rem;
            color: #94a3b8;
            background: rgba(15, 23, 42, 0.6);
            padding: 10px 14px;
            border-radius: var(--radius-md);
            border: 1px solid #1e293b;
        }

        .mobile-opt-row input[type="checkbox"] {
            accent-color: var(--accent-primary);
            width: 17px;
            height: 17px;
            cursor: pointer;
            flex-shrink: 0;
        }

        .progress-wrapper {
            display: none;
            margin-top: 16px;
        }

        .progress-bar-container {
            background: #1e293b;
            border-radius: 9999px;
            height: 8px;
            overflow: hidden;
            border: 1px solid #334155;
            position: relative;
        }

        .progress-bar-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, #3b82f6, #60a5fa);
            border-radius: 9999px;
            transition: width 0.15s ease-out;
        }

        .progress-status {
            font-size: 0.82rem;
            color: #93c5fd;
            margin-top: 6px;
            text-align: right;
            font-family: 'JetBrains Mono', monospace;
        }

        /* Form elements */
        .form-row {
            margin-top: 20px;
            display: flex;
            gap: 12px;
            flex-direction: column;
        }

        label {
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 4px;
            display: block;
        }

        select {
            width: 100%;
            padding: 13px 16px;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: var(--radius-md);
            color: var(--text-main);
            font-size: 0.95rem;
            font-family: inherit;
            cursor: pointer;
            outline: none;
            transition: border-color 0.2s;
        }

        select:focus {
            border-color: var(--accent-primary);
        }

        optgroup {
            background: #1e293b;
            color: #94a3b8;
            font-weight: 600;
        }

        option {
            background: #0f172a;
            color: #f8fafc;
            padding: 8px;
        }

        .btn-submit {
            width: 100%;
            padding: 14px;
            background: var(--accent-primary);
            color: white;
            border: none;
            border-radius: var(--radius-md);
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 4px 14px rgba(59, 130, 246, 0.4);
            margin-top: 8px;
        }

        .btn-submit:hover:not(:disabled) {
            background: var(--accent-hover);
            transform: translateY(-1px);
        }

        .btn-submit:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }

        /* Results / Status alert */
        .status-box {
            margin-top: 18px;
            padding: 14px 18px;
            border-radius: var(--radius-md);
            font-size: 0.9rem;
            display: none;
        }

        .status-box.success {
            display: block;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #34d399;
        }

        .status-box.error {
            display: block;
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #f87171;
        }

        .result-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            font-size: 0.85rem;
        }

        .result-item:last-child {
            border-bottom: none;
        }

        /* Hosted Files List */
        .files-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
        }

        .btn-refresh {
            background: #1e293b;
            border: 1px solid #334155;
            color: var(--text-muted);
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 0.8rem;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .btn-refresh:hover {
            color: var(--text-main);
            background: #334155;
        }

        .files-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .file-card {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: var(--radius-md);
            padding: 14px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            transition: border-color 0.2s;
        }

        .file-card:hover {
            border-color: #334155;
        }

        .file-info {
            display: flex;
            align-items: center;
            gap: 12px;
            overflow: hidden;
        }

        .file-badge {
            background: #1e293b;
            color: #38bdf8;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.72rem;
            font-weight: 700;
            text-transform: uppercase;
            padding: 4px 8px;
            border-radius: 6px;
            border: 1px solid #334155;
            flex-shrink: 0;
        }

        .file-name {
            font-weight: 500;
            font-size: 0.92rem;
            color: var(--text-main);
            text-decoration: none;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .file-name:hover {
            color: #60a5fa;
            text-decoration: underline;
        }

        .file-meta {
            font-size: 0.75rem;
            color: var(--text-dim);
            margin-top: 2px;
        }

        .file-actions {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
        }

        .btn-action {
            background: #1e293b;
            color: var(--text-muted);
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 7px 12px;
            font-size: 0.8rem;
            font-weight: 500;
            text-decoration: none;
            cursor: pointer;
            transition: all 0.15s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .btn-action:hover {
            background: #334155;
            color: var(--text-main);
        }

        .btn-action.download {
            background: rgba(59, 130, 246, 0.12);
            border-color: rgba(59, 130, 246, 0.35);
            color: #60a5fa;
        }

        .btn-action.download:hover {
            background: var(--accent-primary);
            color: white;
        }

        .empty-state {
            text-align: center;
            padding: 36px 20px;
            color: var(--text-dim);
            font-size: 0.9rem;
        }

        /* Spinner */
        .spinner {
            width: 18px;
            height: 18px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            display: none;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-badge">⚡ Instant Conversion & Permanent Hosting</div>
            <h1>Universal File Converter</h1>
            <p>Convert images, spreadsheets, and markdown tables. Hosted instantly at <code>/files</code>.</p>
        </div>

        <!-- Upload & Convert Card -->
        <div class="card">
            <div class="card-title">
                <span>Upload & Convert</span>
            </div>

            <form id="uploadForm">
                <!-- Drag and Drop Box -->
                <div class="dropzone" id="dropZone">
                    <svg class="dropzone-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="17 8 12 3 7 8"></polyline>
                        <line x1="12" y1="3" x2="12" y2="15"></line>
                    </svg>
                    <div class="dropzone-text">Drag & drop files here, or click to browse</div>
                    <div class="dropzone-sub">
                        Images: PNG, JPG, JPEG, WEBP, HEIC, BMP, ICO &bull; Docs & Notes: PDF, MD, TXT, HTML &bull; Data: CSV, JSON, XLSX
                    </div>
                    <input type="file" id="fileInput" multiple accept="image/*,.heic,.heif,.csv,.json,.xlsx,.html,.md,.txt,.pdf">
                </div>

                <!-- Staged files display -->
                <div class="staged-files" id="stagedFiles"></div>

                <!-- Target Format Selection -->
                <div class="form-row">
                    <div>
                        <label for="targetFormat">Select Target Format</label>
                        <select id="targetFormat" name="target_format" required>
                            <optgroup label="Documents & Notes">
                                <option value="pdf">Keep / Host as PDF (.pdf)</option>
                                <option value="md">Extract to Markdown Notes (.md)</option>
                                <option value="txt">Extract to Plain Text (.txt)</option>
                                <option value="html">Extract to HTML Notes (.html)</option>
                            </optgroup>
                            <optgroup label="Images">
                                <option value="webp">WebP Image (.webp)</option>
                                <option value="png">PNG Image (.png)</option>
                                <option value="jpg">JPEG Image (.jpg)</option>
                                <option value="ico">Windows Icon (.ico)</option>
                                <option value="bmp">Bitmap Image (.bmp)</option>
                            </optgroup>
                            <optgroup label="Data & Spreadsheets">
                                <option value="csv">CSV (.csv)</option>
                                <option value="json">JSON (.json)</option>
                                <option value="xlsx">Excel Spreadsheet (.xlsx)</option>
                            </optgroup>
                        </select>
                    </div>

                    <div class="mobile-opt-row">
                        <input type="checkbox" id="optImages" checked>
                        <label for="optImages" style="margin-bottom:0; cursor:pointer;">⚡ Fast Mobile Upload: Auto-optimize large photos for speed</label>
                    </div>

                    <button type="submit" class="btn-submit" id="submitBtn">
                        <div class="spinner" id="spinner"></div>
                        <span id="btnText">Convert & Host Files</span>
                    </button>

                    <div class="progress-wrapper" id="progressWrapper">
                        <div class="progress-bar-container">
                            <div class="progress-bar-fill" id="progressFill"></div>
                        </div>
                        <div class="progress-status" id="progressStatus">Uploading...</div>
                    </div>
                </div>
            </form>

            <div class="status-box" id="statusBox"></div>
        </div>

        <!-- Hosted Files Card -->
        <div class="card">
            <div class="files-header">
                <div class="card-title" style="margin-bottom:0;">
                    <span>Hosted Files</span>
                    <span class="badge-count" id="filesCount">0 files</span>
                </div>
                <button class="btn-refresh" id="refreshBtn">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="23 4 23 10 17 10"></polyline>
                        <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                    </svg>
                    Refresh
                </button>
            </div>

            <div class="files-list" id="filesList">
                <div class="empty-state">Loading hosted files...</div>
            </div>
        </div>
    </div>

    <script>
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const stagedContainer = document.getElementById('stagedFiles');
        const uploadForm = document.getElementById('uploadForm');
        const targetFormat = document.getElementById('targetFormat');
        const optImages = document.getElementById('optImages');
        const submitBtn = document.getElementById('submitBtn');
        const spinner = document.getElementById('spinner');
        const btnText = document.getElementById('btnText');
        const progressWrapper = document.getElementById('progressWrapper');
        const progressFill = document.getElementById('progressFill');
        const progressStatus = document.getElementById('progressStatus');
        const statusBox = document.getElementById('statusBox');
        const filesList = document.getElementById('filesList');
        const filesCount = document.getElementById('filesCount');
        const refreshBtn = document.getElementById('refreshBtn');

        let stagedFilesList = [];

        // Drag & Drop handlers
        dropZone.addEventListener('click', () => fileInput.click());

        ['dragenter', 'dragover'].forEach(name => {
            dropZone.addEventListener(name, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropZone.classList.add('dragover');
            });
        });

        ['dragleave', 'drop'].forEach(name => {
            dropZone.addEventListener(name, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropZone.classList.remove('dragover');
            });
        });

        dropZone.addEventListener('drop', (e) => {
            if (e.dataTransfer && e.dataTransfer.files) {
                addFiles(Array.from(e.dataTransfer.files));
            }
        });

        fileInput.addEventListener('change', () => {
            if (fileInput.files) {
                addFiles(Array.from(fileInput.files));
            }
        });

        function addFiles(newFiles) {
            for (const f of newFiles) {
                if (!stagedFilesList.some(item => item.name === f.name && item.size === f.size)) {
                    stagedFilesList.push(f);
                }
            }
            const hasPdf = stagedFilesList.some(f => f.name.toLowerCase().endsWith('.pdf'));
            const isImg = stagedFilesList.some(f => /\.(jpe?g|png|webp|heic|heif|bmp)$/i.test(f.name));
            if (hasPdf && !isImg) {
                targetFormat.value = 'pdf';
            } else if (isImg && !hasPdf) {
                targetFormat.value = 'webp';
            }
            renderStagedFiles();
        }

        function removeFile(index) {
            stagedFilesList.splice(index, 1);
            renderStagedFiles();
        }

        function renderStagedFiles() {
            stagedContainer.innerHTML = '';
            stagedFilesList.forEach((file, idx) => {
                const chip = document.createElement('div');
                chip.className = 'file-chip';
                const isLarge = file.size > 10 * 1024 * 1024;
                const sizeStr = file.size >= 1024 * 1024 
                    ? (file.size / (1024 * 1024)).toFixed(1) + ' MB'
                    : (file.size / 1024).toFixed(1) + ' KB';
                chip.innerHTML = `
                    <span>${file.name} (${sizeStr})</span>
                    ${isLarge ? '<span class="file-chip-badge" title="Large file">Large</span>' : ''}
                    <span class="file-chip-remove" onclick="removeFile(${idx})">&times;</span>
                `;
                stagedContainer.appendChild(chip);
            });
        }

        // Client-side photo downscaling for ultra-fast mobile upload
        async function optimizeImageForUpload(file) {
            const isImage = file.type.startsWith('image/') || /\.(jpe?g|png|webp|bmp)$/i.test(file.name);
            if (!isImage || file.size < 1.2 * 1024 * 1024) {
                return file;
            }

            return new Promise((resolve) => {
                const img = new Image();
                const url = URL.createObjectURL(file);
                img.onload = () => {
                    URL.revokeObjectURL(url);
                    const maxDim = 2560;
                    let width = img.width;
                    let height = img.height;

                    if (width > maxDim || height > maxDim) {
                        if (width > height) {
                            height = Math.round((height * maxDim) / width);
                            width = maxDim;
                        } else {
                            width = Math.round((width * maxDim) / height);
                            height = maxDim;
                        }
                    }

                    const canvas = document.createElement('canvas');
                    canvas.width = width;
                    canvas.height = height;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, width, height);

                    canvas.toBlob(
                        (blob) => {
                            if (blob && blob.size < file.size) {
                                const newName = file.name.replace(/\.[^/.]+$/, "") + ".jpg";
                                resolve(new File([blob], newName, { type: 'image/jpeg' }));
                            } else {
                                resolve(file);
                            }
                        },
                        'image/jpeg',
                        0.88
                    );
                };
                img.onerror = () => {
                    URL.revokeObjectURL(url);
                    resolve(file);
                };
                img.src = url;
            });
        }

        // Global store for failed files so users can tap "Retry" on mobile
        let lastFailedBatch = [];

        window.retrySingleFailed = async function(idx) {
            const item = lastFailedBatch[idx];
            if (!item || !item.file) return;
            stagedFilesList = [item.file];
            renderStagedFiles();
            uploadForm.requestSubmit();
        };

        // Single file upload via XMLHttpRequest with real-time progress (< 512KB)
        function uploadSingleFile(file, targetExt, onProgress) {
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                const formData = new FormData();
                formData.append('target_format', targetExt);
                formData.append('files', file, file.name);

                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable && onProgress) {
                        onProgress(e.loaded, e.total);
                    }
                };

                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            const data = JSON.parse(xhr.responseText);
                            resolve(data);
                        } catch (err) {
                            reject(new Error("Invalid server JSON response"));
                        }
                    } else if (xhr.status === 502 || xhr.status === 503) {
                        reject(new Error("SERVER_WAKING_UP"));
                    } else if (xhr.status === 413) {
                        reject(new Error("File exceeds cloud payload limit."));
                    } else {
                        let errMsg = "Server error " + xhr.status;
                        try {
                            const parsed = JSON.parse(xhr.responseText);
                            if (parsed.detail) errMsg = parsed.detail;
                        } catch (_) {}
                        reject(new Error(errMsg));
                    }
                };

                xhr.onerror = () => reject(new Error("NETWORK_DISCONNECTED"));
                xhr.ontimeout = () => reject(new Error("REQUEST_TIMEOUT"));
                xhr.timeout = 180000;

                xhr.open('POST', '/api/convert');
                xhr.send(formData);
            });
        }

        // Chunked file upload (768KB chunks) with auto-retry per chunk for resilient mobile uploads
        async function uploadFileChunked(file, targetExt, onProgress) {
            const CHUNK_SIZE = 768 * 1024; // 768 KB per chunk (optimal for mobile packet windows)
            const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
            const fileId = Date.now() + '_' + Math.random().toString(36).substring(2, 8);

            for (let chunkIdx = 0; chunkIdx < totalChunks; chunkIdx++) {
                const start = chunkIdx * CHUNK_SIZE;
                const end = Math.min(file.size, start + CHUNK_SIZE);
                const chunkBlob = file.slice(start, end);
                const isFinal = (chunkIdx + 1 === totalChunks);

                let attempt = 0;
                let chunkSuccess = false;
                let lastError = null;

                while (attempt < 4 && !chunkSuccess) {
                    attempt++;
                    try {
                        const res = await uploadSingleChunk(
                            fileId,
                            chunkIdx,
                            totalChunks,
                            file.name,
                            targetExt,
                            chunkBlob,
                            isFinal,
                            (loaded, _) => {
                                const bytesSentBefore = chunkIdx * CHUNK_SIZE;
                                const currentTotalSent = bytesSentBefore + loaded;
                                if (onProgress) {
                                    onProgress(currentTotalSent, file.size, chunkIdx + 1, totalChunks);
                                }
                            }
                        );
                        chunkSuccess = true;
                        if (isFinal) {
                            return res;
                        }
                    } catch (err) {
                        lastError = err;
                        // Fire background beacon
                        try {
                            fetch('/api/client-log', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    event: 'chunk_fail',
                                    filename: file.name,
                                    status: `Chunk ${chunkIdx + 1}/${totalChunks} attempt ${attempt}/4`,
                                    detail: err.message || String(err)
                                })
                            }).catch(() => {});
                        } catch (_) {}

                        if (attempt < 4) {
                            await new Promise(r => setTimeout(r, 1500 * attempt));
                        }
                    }
                }

                if (!chunkSuccess) {
                    throw new Error(`Upload paused at chunk ${chunkIdx + 1}/${totalChunks}: ${lastError ? lastError.message : 'Connection dropped'}`);
                }
            }
        }

        function uploadSingleChunk(fileId, chunkIdx, totalChunks, filename, targetExt, chunkBlob, isFinal, onProgress) {
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                const formData = new FormData();
                formData.append('file_id', fileId);
                formData.append('chunk_index', chunkIdx);
                formData.append('total_chunks', totalChunks);
                formData.append('filename', filename);
                formData.append('target_format', targetExt);
                formData.append('chunk', chunkBlob, 'blob');

                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable && onProgress) {
                        onProgress(e.loaded, e.total);
                    }
                };

                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            const data = JSON.parse(xhr.responseText);
                            resolve(data);
                        } catch (_) {
                            reject(new Error("Invalid server JSON response"));
                        }
                    } else if (xhr.status === 502 || xhr.status === 503) {
                        reject(new Error("SERVER_WAKING_UP"));
                    } else if (xhr.status === 413) {
                        reject(new Error("File payload exceeds server limits"));
                    } else {
                        let errMsg = "Server error " + xhr.status;
                        try {
                            const p = JSON.parse(xhr.responseText);
                            if (p.detail) errMsg = p.detail;
                        } catch (_) {}
                        reject(new Error(errMsg));
                    }
                };

                xhr.onerror = () => reject(new Error("Cellular connection interrupted"));
                xhr.ontimeout = () => reject(new Error("Upload request timed out"));
                // Allow up to 180s on the final chunk since conversion executes then
                xhr.timeout = isFinal ? 180000 : 50000;

                xhr.open('POST', '/api/upload-chunk');
                xhr.send(formData);
            });
        }

        // Form Submit Handler
        uploadForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (stagedFilesList.length === 0) {
                showStatus('Please select or drop at least one file to convert.', 'error');
                return;
            }

            const targetExt = targetFormat.value;
            const filesToProcess = [...stagedFilesList];
            const total = filesToProcess.length;

            submitBtn.disabled = true;
            spinner.style.display = 'inline-block';
            btnText.textContent = 'Processing...';
            statusBox.style.display = 'none';
            progressWrapper.style.display = 'block';
            progressFill.style.width = '0%';
            progressStatus.textContent = 'Preparing files...';

            let wakeLock = null;
            try {
                if ('wakeLock' in navigator) {
                    wakeLock = await navigator.wakeLock.request('screen');
                }
            } catch (_) {}

            const aggregatedResults = [];
            lastFailedBatch = [];

            try {
                for (let i = 0; i < total; i++) {
                    const originalFile = filesToProcess[i];
                    let fileToUpload = originalFile;

                    // Step 1: Client optimization if enabled for photos
                    if (optImages.checked) {
                        progressStatus.textContent = `[${i + 1}/${total}] Checking optimization: ${originalFile.name}...`;
                        fileToUpload = await optimizeImageForUpload(originalFile);
                    }

                    // Step 2: Chunk any file larger than 512 KB to avoid mobile connection drops
                    const isChunked = fileToUpload.size > 512 * 1024;
                    let attempt = 0;
                    let success = false;
                    let lastError = null;

                    while (attempt < 3 && !success) {
                        attempt++;
                        try {
                            progressStatus.textContent = `[${i + 1}/${total}] Uploading ${fileToUpload.name}...`;
                            let responseData;
                            if (isChunked) {
                                responseData = await uploadFileChunked(
                                    fileToUpload,
                                    targetExt,
                                    (loaded, totalBytes, chunkNum, totalChunks) => {
                                        const filePct = totalBytes > 0 ? loaded / totalBytes : 0;
                                        const overallPct = Math.round(((i + filePct) / total) * 100);
                                        progressFill.style.width = overallPct + '%';
                                        const loadedMb = (loaded / (1024 * 1024)).toFixed(1);
                                        const totalMb = (totalBytes / (1024 * 1024)).toFixed(1);
                                        progressStatus.textContent = `[${i + 1}/${total}] Uploading ${fileToUpload.name} — Chunk ${chunkNum}/${totalChunks} (${loadedMb}MB / ${totalMb}MB)...`;
                                    }
                                );
                            } else {
                                responseData = await uploadSingleFile(
                                    fileToUpload,
                                    targetExt,
                                    (loaded, totalBytes) => {
                                        const filePct = totalBytes > 0 ? loaded / totalBytes : 0;
                                        const overallPct = Math.round(((i + filePct) / total) * 100);
                                        progressFill.style.width = overallPct + '%';
                                        const loadedKb = (loaded / 1024).toFixed(0);
                                        const totalKb = (totalBytes / 1024).toFixed(0);
                                        progressStatus.textContent = `[${i + 1}/${total}] Uploading ${fileToUpload.name} (${loadedKb} KB / ${totalKb} KB)...`;
                                    }
                                );
                            }

                            progressStatus.textContent = `[${i + 1}/${total}] Finished converting ${fileToUpload.name}!`;
                            if (responseData.results && responseData.results.length > 0) {
                                aggregatedResults.push(...responseData.results);
                            }
                            success = true;
                        } catch (err) {
                            lastError = err;
                            if (err.message === "SERVER_WAKING_UP" && attempt < 3) {
                                progressStatus.textContent = `Cloud server is spinning up from sleep. Retrying in 6s (Attempt ${attempt}/3)...`;
                                await new Promise(r => setTimeout(r, 6000));
                            } else if ((err.message.includes("Cellular") || err.message === "NETWORK_DISCONNECTED") && attempt < 3) {
                                progressStatus.textContent = `Connection blip. Retrying in 2s (Attempt ${attempt}/3)...`;
                                await new Promise(r => setTimeout(r, 2000));
                            } else {
                                break;
                            }
                        }
                    }

                    if (!success) {
                        let msg = lastError ? lastError.message : "Upload interrupted";
                        if (msg === "NETWORK_DISCONNECTED" || msg === "Cellular connection interrupted") {
                            msg = "Connection interrupted over mobile data.";
                        } else if (msg === "REQUEST_TIMEOUT") {
                            msg = "Request timed out over mobile data.";
                        } else if (msg === "SERVER_WAKING_UP") {
                            msg = "Cloud server was spinning up. Tap Retry to process now.";
                        }
                        
                        const failEntry = {
                            original: originalFile.name,
                            error: msg,
                            status: "error",
                            file: originalFile
                        };
                        aggregatedResults.push(failEntry);
                        lastFailedBatch.push(failEntry);

                        try {
                            fetch('/api/client-log', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    event: 'upload_failed',
                                    filename: originalFile.name,
                                    status: 'final_failure',
                                    detail: msg
                                })
                            }).catch(() => {});
                        } catch (_) {}
                    }
                }

                progressFill.style.width = '100%';
                progressStatus.textContent = 'Done!';

                // Render Results
                let hasErrors = false;
                let html = '<div style="font-weight:600; margin-bottom:8px;">Conversion Results:</div>';
                let failIdx = 0;
                aggregatedResults.forEach(res => {
                    if (res.status === 'ok') {
                        html += `
                            <div class="result-item">
                                <span>&#10003; <strong>${res.original}</strong> &rarr; <a href="${res.url}" target="_blank" style="color:#60a5fa; text-decoration:underline;">${res.hosted_name}</a> (${res.size_formatted})</span>
                            </div>
                        `;
                    } else {
                        hasErrors = true;
                        const thisIdx = failIdx++;
                        html += `
                            <div class="result-item" style="color:#f87171; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                                <span>&#10007; <strong>${res.original}</strong>: ${res.error}</span>
                                <button type="button" onclick="retrySingleFailed(${thisIdx})" style="background:#2563eb; color:#fff; border:none; padding:4px 12px; border-radius:6px; font-size:0.8rem; cursor:pointer; font-weight:600; white-space:nowrap;">🔄 Retry</button>
                            </div>
                        `;
                    }
                });

                statusBox.innerHTML = html;
                statusBox.className = hasErrors ? 'status-box error' : 'status-box success';
                statusBox.style.display = 'block';

                // Clear staged files and update hosted list
                stagedFilesList = [];
                fileInput.value = '';
                renderStagedFiles();
                loadHostedFiles();
            } finally {
                if (wakeLock) {
                    try { await wakeLock.release(); } catch (_) {}
                    wakeLock = null;
                }
                submitBtn.disabled = false;
                spinner.style.display = 'none';
                btnText.textContent = 'Convert & Host Files';
                setTimeout(() => {
                    progressWrapper.style.display = 'none';
                    progressFill.style.width = '0%';
                }, 3000);
            }
        });

        function showStatus(message, type) {
            statusBox.textContent = message;
            statusBox.className = 'status-box ' + type;
            statusBox.style.display = 'block';
        }

        // Fetch & render hosted files list
        async function loadHostedFiles() {
            try {
                const response = await fetch('/api/files');
                const files = await response.json();

                filesCount.textContent = `${files.length} file${files.length === 1 ? '' : 's'}`;

                if (files.length === 0) {
                    filesList.innerHTML = '<div class="empty-state">No files hosted yet. Upload and convert your first file above!</div>';
                    return;
                }

                filesList.innerHTML = files.map(file => {
                    const fullUrl = window.location.origin + file.url;
                    return `
                        <div class="file-card">
                            <div class="file-info">
                                <span class="file-badge">.${file.ext}</span>
                                <div>
                                    <a href="${file.url}" target="_blank" class="file-name" title="${file.name}">${file.name}</a>
                                    <div class="file-meta">${file.size_formatted} &bull; ${file.modified}</div>
                                </div>
                            </div>
                            <div class="file-actions">
                                <button class="btn-action" onclick="copyFileLink('${fullUrl}', this)">
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                                    </svg>
                                    <span>Copy Link</span>
                                </button>
                                <a href="${file.url}" download="${file.name}" class="btn-action download">
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                                        <polyline points="7 10 12 15 17 10"></polyline>
                                        <line x1="12" y1="15" x2="12" y2="3"></line>
                                    </svg>
                                    <span>Download</span>
                                </a>
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                filesList.innerHTML = '<div class="empty-state" style="color:#ef4444;">Failed to load hosted files.</div>';
            }
        }

        // Copy link helper
        function copyFileLink(url, btn) {
            navigator.clipboard.writeText(url).then(() => {
                const span = btn.querySelector('span');
                const origText = span.textContent;
                span.textContent = 'Copied!';
                btn.style.borderColor = '#10b981';
                btn.style.color = '#10b981';
                setTimeout(() => {
                    span.textContent = origText;
                    btn.style.borderColor = '';
                    btn.style.color = '';
                }, 2000);
            }).catch(() => {
                alert('Copied link: ' + url);
            });
        }

        refreshBtn.addEventListener('click', loadHostedFiles);
        loadHostedFiles();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

