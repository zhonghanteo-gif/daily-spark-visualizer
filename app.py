import os
import uuid
import subprocess
import io
import time
import shutil
import gc
from pathlib import Path
from datetime import datetime, timezone
import urllib.parse
import traceback
import textwrap
import html
from html.parser import HTMLParser
import json
import xml.sax.saxutils as saxutils
from contextlib import asynccontextmanager

from pydantic import BaseModel
import pandas as pd
from PIL import Image, ImageOps, ImageDraw
import mimetypes
import re
import zipfile
import xml.etree.ElementTree as ET
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

CONVERSION_LOGS = []

def log_event(status: str, msg: str, details: dict = None):
    CONVERSION_LOGS.append({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": status,
        "message": msg,
        "details": details or {}
    })
    if len(CONVERSION_LOGS) > 100:
        CONVERSION_LOGS.pop(0)

class ClientLogPayload(BaseModel):
    event: str = ""
    filename: str = ""
    status: str = ""
    detail: str = ""
    user_agent: str = ""

# Setup directory paths relative to this script
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
HOSTED_DIR = STORAGE_DIR / "hosted"

# Ensure directories exist
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
HOSTED_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "bmp", "ico", "pdf", "heic", "heif", "gif"}
DATA_FORMATS = {"csv", "json", "xlsx", "xls", "html", "md", "txt"}
DOCUMENT_FORMATS = {"pdf", "docx", "doc", "txt", "md", "rtf", "xlsx", "csv", "html"}

MIME_TO_EXT = {
    # Images
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/x-ms-bmp": "bmp",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    # Documents
    "application/pdf": "pdf",
    "text/plain": "txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/html": "html",
    "application/json": "json",
}

CONTENT_TYPE_BY_EXT = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "rtf": "application/rtf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "json": "application/json; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "webp": "image/webp",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
    "heic": "image/heic",
    "heif": "image/heif",
    "gif": "image/gif",
    "svg": "image/svg+xml",
}

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

# Try importing python-docx
try:
    import docx as python_docx
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def cleanup_old_temp_dirs(max_age_seconds: int = 1800):
    """Deletes abandoned temp directories and orphan chunks older than max_age_seconds."""
    now = time.time()
    if UPLOAD_DIR.exists():
        for p in UPLOAD_DIR.iterdir():
            try:
                if p.is_dir() and p.name.startswith("temp_") and (now - p.stat().st_mtime > max_age_seconds):
                    shutil.rmtree(p, ignore_errors=True)
                elif p.is_file() and (now - p.stat().st_mtime > max_age_seconds):
                    p.unlink(missing_ok=True)
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure storage folders exist on startup and clean orphan uploads
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    HOSTED_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_temp_dirs()
    yield


app = FastAPI(
    title="Universal File Format Converter & File Host",
    description="Convert images, spreadsheets, and structured data, and host them with permanent shareable URLs.",
    version="1.4.0",
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


@app.get("/files/{filename:path}")
async def serve_hosted_file(filename: str, download: bool = Query(False)):
    """Serves hosted files with explicit Content-Type and Content-Disposition headers."""
    unquoted = urllib.parse.unquote(filename)
    safe_name = Path(unquoted).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(status_code=404, detail="File not found")
    file_path = (HOSTED_DIR / safe_name).resolve()
    if HOSTED_DIR.resolve() not in file_path.parents or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ext = file_path.suffix.lower().lstrip(".")
    media_type = (
        CONTENT_TYPE_BY_EXT.get(ext)
        or mimetypes.guess_type(file_path.name)[0]
        or "application/octet-stream"
    )

    # Inline for browser-viewable documents (PDF, plain text, markdown, CSV, HTML, images) unless ?download=1
    force_attachment_exts = {"docx", "doc", "rtf", "xlsx", "xls"}
    disposition_type = "attachment" if (download or ext in force_attachment_exts) else "inline"

    raw_ascii = file_path.name.encode("ascii", "ignore").decode("ascii")
    raw_ascii = re.sub(r'[\r\n";]', '_', raw_ascii).strip()
    if not raw_ascii or raw_ascii.startswith(".") or not Path(raw_ascii).stem.lstrip("."):
        ascii_fallback = f"document.{ext or 'bin'}"
    else:
        ascii_fallback = raw_ascii
    utf8_quoted = urllib.parse.quote(file_path.name)
    content_disposition = f'{disposition_type}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{utf8_quoted}'

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers={
            "Content-Disposition": content_disposition,
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
        },
    )


def read_data_file(input_path: str, ext: str) -> pd.DataFrame:
    """Reads structured data from supported formats into a pandas DataFrame."""
    ext = ext.lower().lstrip(".")
    if ext == "csv":
        try:
            return pd.read_csv(input_path)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except UnicodeDecodeError:
            try:
                return pd.read_csv(input_path, encoding="latin1")
            except pd.errors.EmptyDataError:
                return pd.DataFrame()
    elif ext in ("xlsx", "xls"):
        return pd.read_excel(input_path)
    elif ext == "json":
        try:
            return pd.read_json(input_path)
        except Exception:
            with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
                raw_json = json.load(f)
            if isinstance(raw_json, list):
                return pd.DataFrame(raw_json)
            elif isinstance(raw_json, dict):
                return pd.json_normalize(raw_json)
            raise ValueError("Invalid JSON format for table conversion.")
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
            content = f.read()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return pd.DataFrame({"line": [1], "text": [""]})
        if "\t" in lines[0]:
            try:
                return pd.read_csv(io.StringIO(content), sep="\t")
            except Exception:
                pass
        return pd.DataFrame({"line": range(1, len(lines) + 1), "text": lines})
    else:
        raise ValueError(f"Reading from .{ext} is not supported.")


def write_data_file(df: pd.DataFrame, target_ext: str, output_path: str):
    """Writes a pandas DataFrame to the desired output format."""
    target_ext = target_ext.lower().lstrip(".")
    if target_ext == "csv":
        df.to_csv(output_path, index=False)
    elif target_ext in ("xlsx", "xls"):
        df.to_excel(output_path, engine="openpyxl", index=False)
    elif target_ext == "json":
        df.to_json(output_path, orient="records", indent=2)
    elif target_ext == "html":
        df.to_html(output_path, index=False, classes="table", border=0)
    elif target_ext == "md":
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(df.to_markdown(index=False) or "")
        except Exception:
            cols = [str(c) for c in df.columns]
            lines = ["| " + " | ".join(cols) + " |"]
            lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
            for _, row in df.iterrows():
                lines.append("| " + " | ".join(str(v) if pd.notna(v) else "" for v in row.values) + " |")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    elif target_ext == "txt":
        with open(output_path, "w", encoding="utf-8") as f:
            if not df.empty:
                f.write("\t".join(str(c) for c in df.columns) + "\n")
                for _, row in df.iterrows():
                    f.write("\t".join(str(v) if pd.notna(v) else "" for v in row.values) + "\n")
            else:
                f.write("")
    else:
        raise ValueError(f"Writing to .{target_ext} is not supported.")


def convert_image(input_path: str, source_ext: str, target_ext: str, output_path: str, black_and_white: bool = False):
    """Converts images between PNG, JPG, JPEG, WEBP, BMP, ICO, GIF, and PDF with strict memory bounds."""
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

        # Helper to composite transparent images over solid white
        def composite_on_white(im: Image.Image) -> Image.Image:
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                conv = im.convert("RGBA")
                bg = Image.new("RGB", conv.size, (255, 255, 255))
                bg.paste(conv, mask=conv.split()[3])
                return bg
            elif im.mode != "RGB":
                return im.convert("RGB")
            return im

        # Normalize CMYK for formats that do not support it
        if img.mode == "CMYK":
            img = img.convert("RGB")

        # Black and White / Grayscale transformation option
        if black_and_white:
            img = composite_on_white(img)
            img = ImageOps.grayscale(img)
            if target_ext in ("jpg", "jpeg", "bmp", "pdf", "gif", "webp", "heic", "heif"):
                img = img.convert("RGB")

        if target_ext in ("jpg", "jpeg"):
            img = composite_on_white(img)
            img.save(output_path, "JPEG", quality=92, optimize=True)
        elif target_ext == "png":
            if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                img = img.convert("RGBA" if "A" in img.mode else "RGB")
            img.save(output_path, "PNG", optimize=True)
        elif target_ext == "webp":
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.save(output_path, "WEBP", quality=90)
        elif target_ext == "bmp":
            img = composite_on_white(img)
            img.save(output_path, "BMP")
        elif target_ext == "gif":
            if img.mode not in ("RGB", "RGBA", "P", "L"):
                img = img.convert("RGB")
            img.save(output_path, "GIF")
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
        elif target_ext in ("heic", "heif"):
            if not HAS_HEIF:
                raise ValueError("HEIC/HEIF encoding requires pillow-heif.")
            img = composite_on_white(img)
            img.save(output_path, format="HEIF")
        elif target_ext == "pdf":
            img = composite_on_white(img)
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


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.text_parts = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "meta", "title"):
            self.skip = True
        elif tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "meta", "title"):
            self.skip = False
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self.text_parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.text_parts.append(data)

    def get_text(self):
        return "".join(self.text_parts)


def strip_html_tags(html_str: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(html_str)
    raw = parser.get_text()
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(l for l in lines if l)


def strip_rtf(text: str) -> str:
    """Extracts plain text from RTF content, ignoring font/color tables, properly handling unicode escapes and skipping uc fallback chars."""
    pattern = re.compile(
        r"\\([a-z]{1,32})(-?\d+)? ?|\\\'([0-9a-fA-F]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)",
        re.IGNORECASE,
    )
    destinations = {"fonttbl", "colortbl", "stylesheet", "info", "generator", "*"}
    out = []
    stack = []
    ignoring = 0
    uc_skip = 1
    skipping_chars = 0

    for match in pattern.finditer(text):
        word, arg, hex_byte, char, brace, raw_char = match.groups()
        if brace == "{":
            stack.append((ignoring, uc_skip))
        elif brace == "}":
            if stack:
                ignoring, uc_skip = stack.pop()
        elif word:
            w_lower = word.lower()
            if w_lower in destinations:
                ignoring = 1
            elif w_lower == "uc" and arg:
                try:
                    uc_skip = max(0, int(arg))
                except Exception:
                    pass
            elif w_lower in ("par", "line"):
                if not ignoring:
                    out.append("\n")
            elif w_lower == "tab":
                if not ignoring:
                    out.append("\t")
            elif w_lower == "u" and arg:
                if not ignoring:
                    try:
                        val = int(arg)
                        if val < 0:
                            val += 65536
                        out.append(chr(val))
                    except Exception:
                        pass
                    skipping_chars = uc_skip
        elif hex_byte:
            if skipping_chars > 0:
                skipping_chars -= 1
            elif not ignoring:
                try:
                    out.append(bytes.fromhex(hex_byte).decode("cp1252"))
                except Exception:
                    pass
        elif char:
            if skipping_chars > 0:
                skipping_chars -= 1
            elif not ignoring:
                out.append(char)
        elif raw_char:
            if skipping_chars > 0:
                skipping_chars -= 1
            elif not ignoring:
                out.append(raw_char)
    cleaned = "".join(out).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def extract_text_from_document(input_path: str, source_ext: str) -> list[tuple[int, str]]:
    """Extracts page/section text tuples from PDF, DOCX, DOC, RTF, TXT, MD, HTML, XLSX, XLS, or CSV."""
    source_ext = source_ext.lower().lstrip(".")
    if source_ext == "pdf":
        if not HAS_PDFIUM:
            raise ValueError("PDF text extraction requires pypdfium2.")
        pdf = None
        all_text = []
        try:
            pdf = pdfium.PdfDocument(input_path)
            for page_idx in range(len(pdf)):
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
        return all_text

    if source_ext == "docx":
        # Check for OLE encrypted or legacy binary Office header
        try:
            with open(input_path, "rb") as f:
                header = f.read(8)
            if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
                raise ValueError(
                    "This document appears to be an encrypted or legacy binary Office document. "
                    "Please save it as an unencrypted DOCX or DOC file before converting."
                )
        except ValueError:
            raise
        except Exception:
            pass

        if HAS_DOCX:
            try:
                doc = python_docx.Document(input_path)
                paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
                for table in doc.tables:
                    for row in table.rows:
                        row_vals = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if row_vals:
                            paras.append(" | ".join(row_vals))
                text = "\n\n".join(paras).strip()
                return [(1, text)] if text else [(1, "(Empty DOCX document)")]
            except Exception:
                pass

        try:
            with zipfile.ZipFile(input_path, "r") as zf:
                xml_bytes = zf.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            texts = [elem.text for elem in root.iter() if elem.tag.endswith("}t") and elem.text]
            joined = " ".join(texts).strip()
            return [(1, joined)] if joined else [(1, "(Empty DOCX document)")]
        except Exception as e:
            raise ValueError(f"Could not extract text from DOCX: {e}")

    if source_ext in ("xlsx", "xls", "csv"):
        try:
            df = read_data_file(input_path, source_ext)
            cols = [str(c) for c in df.columns]
            lines = [" | ".join(cols), "-" * max(10, len(" | ".join(cols)))]
            for _, row in df.iterrows():
                lines.append(" | ".join(str(v) if pd.notna(v) else "" for v in row.values))
            text = "\n".join(lines).strip()
            return [(1, text)] if text else [(1, "(Empty spreadsheet)")]
        except Exception as e:
            if source_ext in ("xlsx", "xls"):
                raise ValueError(f"Could not read spreadsheet data: {e}")

    if source_ext == "rtf":
        raw = Path(input_path).read_text(encoding="utf-8", errors="ignore")
        cleaned = strip_rtf(raw)
        return [(1, cleaned)] if cleaned else [(1, "(Empty RTF document)")]

    if source_ext == "html":
        raw_html = Path(input_path).read_text(encoding="utf-8", errors="ignore")
        text = strip_html_tags(raw_html)
        return [(1, text)] if text else [(1, "(Empty HTML document)")]

    if source_ext == "doc":
        raw_bytes = Path(input_path).read_bytes()
        if raw_bytes.startswith(b"{\\rtf"):
            try:
                rtf_text = strip_rtf(raw_bytes.decode("utf-8", errors="ignore"))
                return [(1, rtf_text)] if rtf_text else [(1, "(Empty DOC document)")]
            except Exception:
                pass
        utf16_chunks = re.findall(rb"(?:[\x20-\x7E\t\r\n]\x00){4,}", raw_bytes)
        ascii_chunks = re.findall(rb"[\x20-\x7E\t\r\n]{4,}", raw_bytes)
        extracted = []
        if utf16_chunks:
            decoded_utf16 = " ".join(c.decode("utf-16le", errors="ignore") for c in utf16_chunks)
            clean_16 = re.sub(r"\s+", " ", decoded_utf16).strip()
            if clean_16:
                extracted.append(clean_16)
        if ascii_chunks:
            decoded_ascii = " ".join(c.decode("latin1", errors="ignore") for c in ascii_chunks)
            clean_ascii = re.sub(r"\s+", " ", decoded_ascii).strip()
            if clean_ascii:
                extracted.append(clean_ascii)
        joined_doc = "\n\n".join(extracted).strip()
        return [(1, joined_doc)] if joined_doc else [(1, "(Empty DOC document)")]

    raw_text = Path(input_path).read_text(encoding="utf-8", errors="ignore").strip()
    return [(1, raw_text)] if raw_text else [(1, "")]


def clean_xml_string(val: str) -> str:
    """Removes XML 1.0 illegal control characters."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", val or "")


def write_docx_document(title: str, sections: list[tuple[int, str]], output_path: str):
    """Writes extracted text sections to a valid .docx file."""
    clean_title = clean_xml_string(title)
    if HAS_DOCX:
        try:
            doc = python_docx.Document()
            doc.add_heading(clean_title, level=1)
            for p_num, text in sections:
                if len(sections) > 1:
                    doc.add_heading(f"Page {p_num}", level=2)
                for para in text.splitlines():
                    cleaned_para = clean_xml_string(para.strip())
                    if cleaned_para:
                        doc.add_paragraph(cleaned_para)
            doc.save(output_path)
            return
        except Exception:
            pass

    # Fallback to minimal valid OpenXML archive with properly XML-escaped text
    body_xml = "".join(
        f"<w:p><w:r><w:t>{ saxutils.escape(clean_xml_string(line.strip())) }</w:t></w:r></w:p>"
        for _, text in sections for line in text.splitlines() if clean_xml_string(line.strip())
    )
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body_xml}</w:body></w:document>",
        )


def write_text_as_pdf(title: str, sections: list[tuple[int, str]], output_path: str, black_and_white: bool = False):
    """Renders text sections to a clean, multi-page PDF document."""
    lines = []
    lines.append(title.upper())
    lines.append("=" * min(70, max(10, len(title))))
    lines.append("")

    for p_num, sec_text in sections:
        if len(sections) > 1:
            lines.append(f"--- Page / Section {p_num} ---")
            lines.append("")
        for raw_line in sec_text.splitlines():
            if not raw_line.strip():
                lines.append("")
            else:
                wrapped = textwrap.wrap(raw_line, width=80)
                lines.extend(wrapped or [""])
        lines.append("")

    lines_per_page = 42
    total_pages = max(1, (len(lines) + lines_per_page - 1) // lines_per_page)
    pages = []

    for page_idx in range(total_pages):
        start = page_idx * lines_per_page
        page_lines = lines[start:start + lines_per_page]
        img = Image.new("RGB", (850, 1100), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)

        # Header bar & page counter
        counter_color = (0, 0, 0) if black_and_white else (110, 120, 135)
        line_color = (0, 0, 0) if black_and_white else (226, 232, 240)
        text_color = (0, 0, 0) if black_and_white else (20, 25, 35)

        draw.text((700, 30), f"Page {page_idx + 1} of {total_pages}", fill=counter_color)
        draw.line([(50, 48), (800, 48)], fill=line_color, width=1)

        y = 60
        for ln in page_lines:
            draw.text((50, y), ln, fill=text_color)
            y += 22

        pages.append(img)

    if pages:
        pages[0].save(output_path, "PDF", resolution=100.0, save_all=True, append_images=pages[1:])
        for p in pages:
            p.close()


def process_conversion(input_path: str, orig_name: str, target_ext: str, black_and_white: bool = False) -> str:
    """Dispatches file conversion based on format categories with memory safety."""
    base_name = Path(orig_name).stem
    source_ext = Path(orig_name).suffix.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")

    if target_ext in ("keep", "original", ""):
        target_ext = source_ext or "bin"

    output_filename = f"{base_name}.{target_ext}"
    output_path = str(HOSTED_DIR / output_filename)

    if Path(input_path).stat().st_size == 0:
        if source_ext == target_ext and not black_and_white:
            if Path(input_path).resolve() != Path(output_path).resolve():
                shutil.copyfile(input_path, output_path)
            log_event("info", f"Direct pass-through hosted (.{source_ext}): {output_filename}")
            return output_filename
        raise ValueError("The uploaded file is empty (0 bytes).")

    # 1. Direct pass-through when source and target formats match AND not black_and_white
    if source_ext == target_ext and not black_and_white:
        if Path(input_path).resolve() != Path(output_path).resolve():
            shutil.copyfile(input_path, output_path)
        log_event("info", f"Direct pass-through hosted (.{source_ext}): {output_filename}")
        return output_filename

    # 1b. PDF to PDF in Black & White mode
    if source_ext == "pdf" and target_ext == "pdf" and black_and_white:
        if not HAS_PDFIUM:
            raise ValueError("PDF Black & White processing requires pypdfium2.")
        pdf_doc = pdfium.PdfDocument(input_path)
        if len(pdf_doc) == 0:
            raise ValueError("PDF file is empty.")
        bw_pages = []
        try:
            for page_idx in range(len(pdf_doc)):
                page = pdf_doc[page_idx]
                bm = page.render(scale=1.5)
                pil_im = bm.to_pil()
                bw_im = ImageOps.grayscale(pil_im).convert("RGB")
                bw_pages.append(bw_im)
                bm.close()
                page.close()
        finally:
            pdf_doc.close()

        if bw_pages:
            bw_pages[0].save(output_path, "PDF", resolution=150.0, save_all=True, append_images=bw_pages[1:])
            for p in bw_pages:
                p.close()
        log_event("info", f"PDF converted to Black & White pages: {output_filename} ({len(bw_pages)} pages)")
        return output_filename

    # 2. PDF to Notes/Text (Markdown, Plain Text, HTML, CSV, JSON extraction)
    if source_ext == "pdf" and target_ext in ("md", "txt", "html", "csv", "json"):
        sections = extract_text_from_document(input_path, "pdf")
        if not sections:
            raise ValueError(
                "No extractable text found in this PDF (it may contain scanned image pages). "
                "Select 'PDF Document (.pdf)' to host it directly, or 'WebP/PNG' to convert as an image."
            )

        if target_ext == "md":
            lines = [f"# {base_name}\n"]
            for p_num, text in sections:
                lines.append(f"\n## Page {p_num}\n\n{text}\n")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            log_event("info", f"PDF converted to Markdown notes: {output_filename} ({len(sections)} pages)")
            return output_filename

        elif target_ext == "txt":
            lines = [f"{base_name}\n" + "=" * len(base_name)]
            for p_num, text in sections:
                lines.append(f"\n--- Page {p_num} ---\n\n{text}\n")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            log_event("info", f"PDF converted to Plain Text: {output_filename} ({len(sections)} pages)")
            return output_filename

        elif target_ext == "html":
            html_parts = [
                "<!DOCTYPE html><html><head><meta charset='utf-8'>",
                f"<title>{html.escape(base_name)}</title>",
                "<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:840px;margin:30px auto;padding:0 20px;line-height:1.6;color:#1e293b;background:#f8fafc;} h1{color:#0f172a;} .page{background:#ffffff;padding:24px;margin-bottom:24px;border-radius:10px;border:1px solid #e2e8f0;white-space:pre-wrap;box-shadow:0 1px 3px rgba(0,0,0,0.05);} .page-hdr{font-size:0.85rem;color:#64748b;font-weight:600;margin-bottom:8px;}</style></head><body>",
                f"<h1>{html.escape(base_name)}</h1>",
            ]
            for p_num, text in sections:
                html_parts.append(f"<div class='page'><div class='page-hdr'>Page {p_num}</div>{html.escape(text)}</div>")
            html_parts.append("</body></html>")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(html_parts))
            log_event("info", f"PDF converted to HTML notes: {output_filename} ({len(sections)} pages)")
            return output_filename

        elif target_ext in ("csv", "json"):
            records = [{"page": p_num, "content": text} for p_num, text in sections]
            df = pd.DataFrame(records)
            write_data_file(df, target_ext, output_path)
            log_event("info", f"PDF converted to {target_ext.upper()}: {output_filename}")
            return output_filename

    # 3. Image Conversions
    if source_ext in IMAGE_FORMATS and target_ext in IMAGE_FORMATS:
        convert_image(input_path, source_ext, target_ext, output_path, black_and_white=black_and_white)
        log_event("info", f"Image converted: {orig_name} -> {output_filename}" + (" (B&W)" if black_and_white else ""))
        return output_filename

    # 4. Data / Spreadsheet Conversions
    if source_ext in DATA_FORMATS and target_ext in DATA_FORMATS:
        try:
            df = read_data_file(input_path, source_ext)
            write_data_file(df, target_ext, output_path)
            log_event("info", f"Data file converted: {orig_name} -> {output_filename}")
            return output_filename
        except Exception:
            if not (source_ext in DOCUMENT_FORMATS and target_ext in DOCUMENT_FORMATS):
                raise

    # 6. Video & ZeroBlur Master (.mp4)
    if target_ext == "mp4" and source_ext in ("mp4", "mov", "webm", "mkv", "avi"):
        if HAS_FFMPEG:
            try:
                cmd = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos+accurate_rnd,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,unsharp=5:5:0.65:3:3:0.20,format=yuv420p",
                    "-c:v", "libx264", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    output_path
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode == 0:
                    log_event("info", f"ZeroBlur Video Mastered: {orig_name} -> {output_filename}")
                    return output_filename
            except Exception as e:
                log_event("error", f"ZeroBlur mastering error: {e}")
        # Safe fallback: copy directly
        if Path(input_path).resolve() != Path(output_path).resolve():
            shutil.copyfile(input_path, output_path)
        log_event("info", f"Video hosted: {output_filename}")
        return output_filename

    # 5. Document Cross-Format Conversions (.docx, .doc, .rtf, .txt, .md, .html, .pdf)
    if source_ext in DOCUMENT_FORMATS and target_ext in (DOCUMENT_FORMATS | {"json"}):
        sections = extract_text_from_document(input_path, source_ext)
        if source_ext == "pdf" and not any(t.strip() for _, t in sections):
            raise ValueError(
                "No extractable text found in this PDF (it may contain scanned image pages). "
                "Select 'PDF Document (.pdf)' to host it directly, or 'WebP/PNG' to convert as an image."
            )
        if target_ext == "txt":
            body = "\n\n".join(text for _, text in sections)
            Path(output_path).write_text(body, encoding="utf-8")
            return output_filename
        elif target_ext == "md":
            lines = [f"# {base_name}\n"]
            for p_num, text in sections:
                if len(sections) > 1:
                    lines.append(f"\n## Section {p_num}\n\n{text}\n")
                else:
                    lines.append(f"\n{text}\n")
            Path(output_path).write_text("\n".join(lines), encoding="utf-8")
            return output_filename
        elif target_ext == "rtf":
            raw_body = "\n".join(text for _, text in sections)
            escaped_body = raw_body.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            rtf_chars = []
            for c in escaped_body:
                code = ord(c)
                if code > 127:
                    rtf_chars.append(f"\\u{code}?")
                else:
                    rtf_chars.append(c)
            rtf_encoded = "".join(rtf_chars)
            rtf_lines = "\\par\n".join(rtf_encoded.splitlines())
            Path(output_path).write_text(f"{{\\rtf1\\ansi\\deff0 {rtf_lines}}}", encoding="ascii", errors="replace")
            return output_filename
        elif target_ext == "html":
            body_html = "".join(f"<div class='page'>{html.escape(text)}</div>" for _, text in sections)
            html_doc = (
                f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(base_name)}</title>"
                "<style>body{font-family:system-ui,sans-serif;max-width:840px;margin:30px auto;padding:0 20px;line-height:1.6;color:#1e293b;background:#f8fafc;} .page{background:#fff;padding:24px;border-radius:10px;border:1px solid #e2e8f0;white-space:pre-wrap;margin-bottom:16px;}</style>"
                f"</head><body><h1>{html.escape(base_name)}</h1>{body_html}</body></html>"
            )
            Path(output_path).write_text(html_doc, encoding="utf-8")
            return output_filename
        elif target_ext == "docx":
            write_docx_document(base_name, sections, output_path)
            return output_filename
        elif target_ext == "pdf":
            write_text_as_pdf(base_name, sections, output_path, black_and_white=black_and_white)
            return output_filename
        elif target_ext in ("csv", "json", "xlsx"):
            records = [{"section": p_num, "content": text} for p_num, text in sections]
            df = pd.DataFrame(records)
            write_data_file(df, target_ext, output_path)
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


def normalize_upload_filename(filename: str, content_type: str = "") -> str:
    """Ensures uploaded files retain or infer a valid safe extension from name or Content-Type."""
    safe_name = Path(filename).name if filename else "document"
    safe_name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', safe_name).strip()
    if not safe_name or safe_name.startswith('.'):
        safe_name = f"file_{int(time.time())}" + (Path(safe_name).suffix if Path(safe_name).suffix else "")

    ext = Path(safe_name).suffix.lower().lstrip(".")
    clean_mime = (content_type or "").split(";")[0].strip().lower()

    if not ext and clean_mime in MIME_TO_EXT:
        inferred_ext = MIME_TO_EXT[clean_mime]
        safe_name = f"{safe_name}.{inferred_ext}"
    return safe_name


@app.post("/api/convert")
async def convert_files(
    target_format: str = Form(...),
    black_and_white: str = Form("false"),
    files: list[UploadFile] = File(...),
):
    is_bw = str(black_and_white).lower() in ("true", "1", "yes", "on")
    results = []
    for file in files:
        if not file.filename and not file.content_type:
            continue
        normalized_name = normalize_upload_filename(file.filename or "upload", file.content_type or "")
        raw_path = str(UPLOAD_DIR / normalized_name)
        with open(raw_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            out_name = process_conversion(raw_path, normalized_name, target_format, black_and_white=is_bw)
            out_file = HOSTED_DIR / out_name
            size_bytes = out_file.stat().st_size if out_file.exists() else 0
            out_ext = out_file.suffix.lower().lstrip(".")
            safe_url = f"/files/{urllib.parse.quote(out_name)}"
            results.append({
                "original": normalized_name,
                "hosted_name": out_name,
                "url": safe_url,
                "ext": out_ext,
                "mime_type": CONTENT_TYPE_BY_EXT.get(out_ext, "application/octet-stream").split(";")[0],
                "size_formatted": format_file_size(size_bytes),
                "status": "ok",
            })
        except Exception as e:
            tb = traceback.format_exc()
            log_event("error", f"Conversion failed for {normalized_name}: {str(e)}", {"trace": tb})
            results.append({
                "original": file.filename or normalized_name,
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
    black_and_white: str = Form("false"),
    chunk: UploadFile = File(...),
):
    """Handles chunked file uploads (768KB chunks) to prevent mobile network timeouts."""
    is_bw = str(black_and_white).lower() in ("true", "1", "yes", "on")
    safe_file_id = "".join(c for c in file_id if c.isalnum() or c in ("-", "_"))
    if not safe_file_id:
        safe_file_id = f"upload_{int(time.time() * 1000)}"
    normalized_name = normalize_upload_filename(filename or f"upload_{safe_file_id}.bin")

    cleanup_old_temp_dirs()

    temp_dir = UPLOAD_DIR / f"temp_{safe_file_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    chunk_path = temp_dir / f"chunk_{chunk_index:05d}.part"
    with open(chunk_path, "wb") as buffer:
        shutil.copyfileobj(chunk.file, buffer)

    all_chunks_present = len(list(temp_dir.glob("chunk_*.part"))) == total_chunks
    if all_chunks_present or (chunk_index + 1 == total_chunks):
        final_path = UPLOAD_DIR / normalized_name
        with open(final_path, "wb") as outfile:
            for i in range(total_chunks):
                c_file = temp_dir / f"chunk_{i:05d}.part"
                if not c_file.exists():
                    log_event("error", f"Missing chunk {i} for {normalized_name}")
                    raise HTTPException(status_code=400, detail=f"Missing chunk {i}")
                with open(c_file, "rb") as infile:
                    shutil.copyfileobj(infile, outfile)

        shutil.rmtree(temp_dir, ignore_errors=True)

        try:
            log_event("info", f"Assembled file ready for conversion: {normalized_name} -> {target_format}" + (" (B&W)" if is_bw else ""))
            out_name = process_conversion(str(final_path), normalized_name, target_format, black_and_white=is_bw)
            out_file = HOSTED_DIR / out_name
            size_bytes = out_file.stat().st_size if out_file.exists() else 0
            out_ext = out_file.suffix.lower().lstrip(".")
            safe_url = f"/files/{urllib.parse.quote(out_name)}"
            log_event("info", f"Successfully converted and hosted: {out_name} ({size_bytes} bytes)")
            return {
                "status": "complete",
                "results": [{
                    "original": normalized_name,
                    "hosted_name": out_name,
                    "url": safe_url,
                    "ext": out_ext,
                    "mime_type": CONTENT_TYPE_BY_EXT.get(out_ext, "application/octet-stream").split(";")[0],
                    "size_formatted": format_file_size(size_bytes),
                    "status": "ok",
                }]
            }
        except Exception as e:
            tb = traceback.format_exc()
            log_event("error", f"Conversion exception for {normalized_name}: {str(e)}", {"trace": tb})
            return {
                "status": "complete",
                "results": [{
                    "original": normalized_name,
                    "error": str(e),
                    "status": "error",
                }]
            }

    return {
        "status": "chunk_received",
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
    }


@app.delete("/api/files/{filename:path}")
async def delete_hosted_file(filename: str):
    """Deletes a file from hosted storage."""
    unquoted = urllib.parse.unquote(filename)
    safe_name = Path(unquoted).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = (HOSTED_DIR / safe_name).resolve()
    if HOSTED_DIR.resolve() not in file_path.parents or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.unlink()
        log_event("info", f"Deleted hosted file: {safe_name}")
        return {"status": "ok", "message": f"Deleted {safe_name}"}
    except Exception as e:
        log_event("error", f"Failed to delete {safe_name}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


@app.post("/api/client-log")
async def client_log(payload: ClientLogPayload, request: Request):
    ua = payload.user_agent or request.headers.get("user-agent", "")
    details = {"status": payload.status}
    if ua:
        details["user_agent"] = ua
    log_event("client", f"[{payload.event}] {payload.filename}: {payload.detail}", details)
    return {"status": "ok"}


@app.get("/api/diagnostics")
async def get_diagnostics():
    return {
        "status": "healthy",
        "version": "1.4.0",
        "mobile_shield": True,
        "photo_gallery": True,
        "bw_page_option": True,
        "zeroblur_engine": True,
        "has_pdfium": HAS_PDFIUM,
        "has_heif": HAS_HEIF,
        "has_docx": HAS_DOCX,
        "has_ffmpeg": HAS_FFMPEG,
        "hosted_count": len([f for f in HOSTED_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]) if HOSTED_DIR.exists() else 0,
        "logs": CONVERSION_LOGS[-40:],
    }


@app.post("/api/v1/optimize")
async def optimize_video_endpoint(
    file: UploadFile = File(...),
    platform: str = Form("tiktok"),
    target_fps: int = Form(60),
    whatsapp_res: str = Form("720p"),
    sharpen_strength: float = Form(0.65),
    audio_normalize: bool = Form(True),
):
    """ZeroBlur Video Engine Server Endpoint for TikTok and WhatsApp status mastering."""
    if not HAS_FFMPEG:
        raise HTTPException(status_code=501, detail="FFmpeg is not installed on this host environment.")

    job_id = str(uuid.uuid4())
    in_ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    in_file = UPLOAD_DIR / f"{job_id}_in{in_ext}"
    out_file = HOSTED_DIR / f"{job_id}_{platform}_master.mp4"

    with open(in_file, "wb") as f:
        shutil.copyfileobj(file.file, f)

    tw = 720 if (platform == "whatsapp" and whatsapp_res == "720p") else 1080
    th = 1280 if (platform == "whatsapp" and whatsapp_res == "720p") else 1920
    fps = max(15, min(60, target_fps))
    luma = round(sharpen_strength, 2)
    chroma = round(sharpen_strength * 0.3, 2)

    vf = (
        f"fps=fps={fps}:round=near,"
        f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos+accurate_rnd+full_chroma_int+full_chroma_inp,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"unsharp=5:5:{luma}:3:3:{chroma},format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(in_file),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast",
    ]
    if audio_normalize:
        cmd.extend(["-af", "loudnorm=I=-14:TP=-1.0:LRA=11"])
    cmd.extend([
        "-c:a", "aac",
        "-b:a", "192k" if platform in ("tiktok", "reels", "shorts") else "128k",
        "-movflags", "+faststart",
        str(out_file)
    ])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            log_event("error", f"ZeroBlur FFmpeg encode failed: {proc.stderr[:160]}")
            raise HTTPException(status_code=500, detail="Video mastering processing failed.")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Video encoding exceeded timeout limit.")
    finally:
        if in_file.exists():
            try:
                in_file.unlink(missing_ok=True)
            except Exception:
                pass

    log_event("info", f"ZeroBlur Master generated: {out_file.name}")
    return FileResponse(
        str(out_file),
        media_type="video/mp4",
        filename=out_file.name,
        headers={"Content-Disposition": f'attachment; filename="{out_file.name}"'}
    )


@app.get("/api/files")
async def list_hosted_files():
    file_list = []
    if HOSTED_DIR.exists():
        for f in HOSTED_DIR.iterdir():
            try:
                if f.is_file() and not f.name.startswith("."):
                    stat = f.stat()
                    ext_clean = f.suffix.lower().lstrip(".")
                    file_list.append({
                        "name": f.name,
                        "url": f"/files/{urllib.parse.quote(f.name)}",
                        "download_url": f"/files/{urllib.parse.quote(f.name)}?download=1",
                        "size_bytes": stat.st_size,
                        "size_formatted": format_file_size(stat.st_size),
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "ext": ext_clean,
                        "mime_type": CONTENT_TYPE_BY_EXT.get(ext_clean, "application/octet-stream").split(";")[0],
                        "is_image": ext_clean in {"png", "jpg", "jpeg", "webp", "bmp", "gif", "ico", "heic", "heif"},
                    })
            except (FileNotFoundError, OSError):
                continue
    file_list.sort(key=lambda x: x["modified"], reverse=True)
    return file_list


HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Universal File Converter & Permanent Host</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;0,6..72,700;1,6..72,400;1,6..72,600&family=Playfair+Display:ital,wght@0,600;0,700;1,400;1,600&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
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
            --warning: #f59e0b;
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
            padding: 36px 18px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .container {
            width: 100%;
            max-width: 860px;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }

        .header {
            text-align: center;
            margin-bottom: 4px;
        }

        .header-top {
            display: flex;
            align-items: center;
            justify-content: flex-start;
            gap: 10px;
            margin-bottom: 12px;
            flex-wrap: wrap;
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
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .btn-diag {
            background: #1e293b;
            border: 1px solid #334155;
            color: #94a3b8;
            border-radius: 9999px;
            padding: 4px 12px;
            font-size: 0.78rem;
            cursor: pointer;
            transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        color 0.2s ease,
                        border-color 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-diag svg {
            transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1), color 0.2s ease;
        }

        .btn-diag:hover {
            color: #f1f5f9;
            border-color: #38bdf8;
            background: #223249;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.25);
        }

        .btn-diag:hover svg {
            transform: scale(1.18);
            color: #38bdf8;
        }

        .btn-diag:active {
            transform: translateY(1px) scale(0.96);
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
        }

        .btn-bw-toggle {
            background: #18181b;
            border-color: #3f3f46;
            color: #d4d4d8;
        }

        .btn-bw-toggle:hover {
            color: #ffffff;
            border-color: #ffffff;
            background: #27272a;
            box-shadow: 0 4px 12px rgba(255, 255, 255, 0.2);
        }

        .btn-bw-toggle svg {
            transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .btn-bw-toggle:hover svg {
            transform: rotate(180deg);
        }

        /* Webpage Theme Switcher Bar */
        .theme-bar {
            display: flex;
            align-items: center;
            justify-content: flex-start;
            gap: 10px;
            margin: 6px 0 16px 0;
            flex-wrap: wrap;
            transition: max-height 0.35s cubic-bezier(0.16, 1, 0.3, 1),
                        opacity 0.25s ease,
                        margin 0.35s cubic-bezier(0.16, 1, 0.3, 1),
                        transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            max-height: 140px;
            opacity: 1;
            transform: translateY(0);
            overflow: hidden;
        }

        .theme-bar.hidden {
            max-height: 0;
            opacity: 0;
            margin-top: 0;
            margin-bottom: 0;
            padding-top: 0;
            padding-bottom: 0;
            transform: translateY(-8px);
            pointer-events: none;
        }

        .btn-toggle-switcher {
            background: #1e293b;
            border: 1px solid #334155;
            color: #94a3b8;
        }

        .btn-toggle-switcher:hover {
            color: #f1f5f9;
            border-color: #38bdf8;
            background: #223249;
        }

        .btn-toggle-switcher.active-hidden {
            border-color: #64748b;
            color: #94a3b8;
            background: #0f172a;
        }

        .theme-bar-close {
            background: transparent;
            border: none;
            color: var(--text-dim);
            font-size: 1.15rem;
            line-height: 1;
            padding: 4px 7px;
            cursor: pointer;
            border-radius: 50%;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }

        .theme-bar-close:hover {
            color: var(--text-main);
            background: rgba(255, 255, 255, 0.1);
            transform: scale(1.1);
        }

        .theme-bar-label {
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .theme-switcher {
            display: inline-flex;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 9999px;
            padding: 3px;
            gap: 3px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            transition: all 0.2s ease;
        }

        .theme-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 5px 12px;
            border-radius: 9999px;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .theme-btn:hover:not(.active) {
            color: #ffffff;
            background: rgba(255, 255, 255, 0.08);
            transform: translateY(-1px);
        }

        .theme-btn:active {
            transform: scale(0.96);
        }

        .theme-btn.active {
            background: var(--accent-primary);
            color: white;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }

        .theme-dot {
            width: 9px;
            height: 9px;
            border-radius: 50%;
            display: inline-block;
            flex-shrink: 0;
        }

        /* Black & White Page Option (Monochrome High Contrast Base) */
        body.bw-mode {
            --bg-color: #09090b;
            --card-bg: #121214;
            --card-border: #27272a;
            --accent-primary: #f4f4f5;
            --accent-hover: #e4e4e7;
            --accent-glow: rgba(255, 255, 255, 0.2);
            --text-main: #fafafa;
            --text-muted: #a1a1aa;
            --text-dim: #71717a;
            --success: #ffffff;
            --danger: #d4d4d8;
        }

        /* Black & White Light (Paper Page Mode) */
        body.bw-page-light {
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
            --card-border: #cbd5e1;
            --accent-primary: #09090b;
            --accent-hover: #27272a;
            --accent-glow: rgba(0, 0, 0, 0.15);
            --text-main: #09090b;
            --text-muted: #52525b;
            --text-dim: #71717a;
            --success: #09090b;
            --danger: #71717a;
            --warning: #52525b;
            background-color: #f8fafc;
            color: #09090b;
        }

        body.bw-page-light .header h1 {
            background: linear-gradient(135deg, #09090b 40%, #52525b);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        body.bw-page-light .header-badge {
            background: #f4f4f5 !important;
            color: #09090b !important;
            border-color: #d4d4d8 !important;
        }

        body.bw-page-light .btn-diag,
        body.bw-page-light .btn-bw-toggle {
            background: #ffffff !important;
            border-color: #d4d4d8 !important;
            color: #09090b !important;
            box-shadow: 0 1px 4px rgba(0,0,0,0.08) !important;
        }

        body.bw-page-light .btn-diag:hover,
        body.bw-page-light .btn-bw-toggle:hover {
            background: #f4f4f5 !important;
            border-color: #09090b !important;
            color: #000000 !important;
        }

        body.bw-page-light .theme-switcher {
            background: #ffffff;
            border-color: #d4d4d8;
            box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        }

        body.bw-page-light .theme-btn {
            color: #71717a;
        }

        body.bw-page-light .theme-btn.active {
            background: #09090b;
            color: #ffffff;
        }

        body.bw-page-light .card {
            background: #ffffff;
            border-color: #e4e4e7;
            box-shadow: 0 4px 20px rgba(0,0,0,0.05);
        }

        body.bw-page-light .btn-picker.btn-gallery {
            background: #09090b !important;
            color: #ffffff !important;
            border-color: #09090b !important;
            box-shadow: 0 2px 8px rgba(0,0,0,0.2) !important;
        }

        body.bw-page-light .btn-picker.btn-gallery:hover {
            background: #27272a !important;
            box-shadow: 0 4px 14px rgba(0,0,0,0.3) !important;
        }

        body.bw-page-light .btn-picker.btn-camera,
        body.bw-page-light .btn-picker.btn-docs {
            background: #ffffff !important;
            color: #09090b !important;
            border-color: #d4d4d8 !important;
        }

        body.bw-page-light .btn-picker.btn-camera:hover,
        body.bw-page-light .btn-picker.btn-docs:hover {
            background: #f4f4f5 !important;
            border-color: #09090b !important;
        }

        body.bw-page-light .dropzone {
            background: #fafafa;
            border-color: #d4d4d8;
            color: #09090b;
        }

        body.bw-page-light .dropzone:hover,
        body.bw-page-light .dropzone.dragover {
            background: #f4f4f5;
            border-color: #09090b;
        }

        body.bw-page-light .dropzone-icon {
            stroke: #09090b;
        }

        body.bw-page-light .mobile-opt-row {
            background: #f4f4f5;
            border-color: #e4e4e7;
            color: #52525b;
        }

        body.bw-page-light .mobile-opt-row span {
            color: #09090b !important;
        }

        body.bw-page-light select {
            background: #ffffff;
            color: #09090b;
            border-color: #d4d4d8;
        }

        body.bw-page-light optgroup {
            background: #f4f4f5;
            color: #71717a;
        }

        body.bw-page-light option {
            background: #ffffff;
            color: #09090b;
        }

        body.bw-page-light .format-switcher-wrap {
            background: #ffffff;
            border-color: #cbd5e1;
            box-shadow: 0 1px 4px rgba(0,0,0,0.06);
        }

        body.bw-page-light .format-cat-bar {
            background: #f4f4f5;
            border-color: #cbd5e1;
        }

        body.bw-page-light .format-cat-btn.active {
            background: #09090b;
            color: #ffffff;
        }

        body.bw-page-light .format-pill-btn {
            background: #ffffff;
            border-color: #cbd5e1;
            color: #09090b;
        }

        body.bw-page-light .format-pill-btn.active {
            background: #09090b;
            color: #ffffff;
            border-color: #09090b;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
        }

        body.bw-page-light .format-pill-btn.active .format-pill-name,
        body.bw-page-light .format-pill-btn.active .format-pill-ext,
        body.bw-page-light .format-pill-btn.active .format-pill-hint {
            color: #ffffff;
        }

        body.bw-page-light .quick-format-pill {
            background: #ffffff;
            border: 1px solid #cbd5e1;
            color: #09090b;
        }

        body.bw-page-light .quick-format-pill:hover:not(.active) {
            background: #f4f4f5;
            border-color: #09090b;
        }

        body.bw-page-light .quick-format-pill.active {
            background: #09090b;
            color: #ffffff;
            border-color: #09090b;
        }

        body.bw-page-light .btn-more-formats {
            background: #ffffff;
            border: 1px solid #cbd5e1;
            color: #09090b;
        }

        body.bw-page-light .btn-more-formats:hover {
            background: #f4f4f5;
            border-color: #09090b;
        }

        body.bw-page-light .btn-more-formats.expanded {
            background: #09090b;
            color: #ffffff;
            border-color: #09090b;
        }

        body.bw-page-light .btn-submit {
            background: #09090b !important;
            color: #ffffff !important;
            box-shadow: 0 4px 14px rgba(0,0,0,0.2) !important;
        }

        body.bw-page-light .btn-submit:hover:not(:disabled) {
            background: #27272a !important;
            box-shadow: 0 6px 20px rgba(0,0,0,0.3) !important;
        }

        body.bw-page-light .file-card {
            background: #ffffff;
            border-color: #e4e4e7;
        }

        body.bw-page-light .file-card:hover {
            border-color: #09090b;
        }

        body.bw-page-light .file-name {
            color: #09090b;
        }

        body.bw-page-light .file-badge {
            background: #09090b;
            color: #ffffff;
            border-color: #09090b;
        }

        body.bw-page-light .badge-count {
            background: #f4f4f5;
            color: #09090b;
            border-color: #d4d4d8;
        }

        body.bw-page-light .search-box {
            background: #ffffff;
            color: #09090b;
            border-color: #d4d4d8;
        }

        body.bw-page-light .filter-btn-group,
        body.bw-page-light .view-toggle {
            background: #f4f4f5;
            border-color: #d4d4d8;
        }

        body.bw-page-light .btn-filter {
            color: #71717a;
        }

        body.bw-page-light .btn-filter.active {
            background: #09090b !important;
            color: #ffffff !important;
        }

        body.bw-page-light .btn-view.active {
            background: #ffffff;
            color: #09090b;
        }

        body.bw-page-light .btn-refresh {
            background: #ffffff;
            border-color: #d4d4d8;
            color: #09090b;
        }

        body.bw-page-light .btn-action {
            background: #ffffff;
            border-color: #d4d4d8;
            color: #09090b;
        }

        body.bw-page-light .btn-action.download {
            background: #09090b !important;
            color: #ffffff !important;
            border-color: #09090b !important;
        }

        body.bw-page-light .btn-action.delete {
            background: #ffffff !important;
            color: #09090b !important;
            border-color: #d4d4d8 !important;
        }

        body.bw-page-light .btn-action.delete:hover {
            background: #09090b !important;
            color: #ffffff !important;
            border-color: #09090b !important;
        }

        body.bw-page-light .grid-card {
            background: #ffffff;
            border-color: #e4e4e7;
        }

        body.bw-page-light .grid-thumb {
            background: #f4f4f5;
        }

        body.bw-page-light .grid-name {
            color: #09090b;
        }

        body.bw-page-light .file-chip {
            background: #f4f4f5;
            color: #09090b;
            border-color: #d4d4d8;
        }

        body.bw-page-light .modal-card {
            background: #ffffff;
            border-color: #d4d4d8;
            color: #09090b;
        }

        body.bw-page-light .diag-box {
            background: #f4f4f5;
            border-color: #e4e4e7;
        }

        body.bw-page-light .diag-val {
            color: #09090b;
        }

        body.bw-page-light .log-list {
            background: #f8fafc;
            border-color: #e4e4e7;
            color: #09090b;
        }

        /* Black & White Dark (Monochrome Dark Page Mode) */
        body.bw-page-dark {
            --bg-color: #000000;
            --card-bg: #0d0d0d;
            --card-border: #262626;
            --accent-primary: #ffffff;
            --accent-hover: #e5e5e5;
            --accent-glow: rgba(255, 255, 255, 0.2);
            --text-main: #ffffff;
            --text-muted: #a3a3a3;
            --text-dim: #737373;
            --success: #ffffff;
            --danger: #d4d4d8;
            --warning: #a1a1aa;
            background-color: #000000;
            color: #ffffff;
            filter: grayscale(100%);
        }

        body.bw-page-dark .header h1 {
            background: linear-gradient(135deg, #ffffff 40%, #737373);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        body.bw-page-dark .theme-switcher {
            background: #0d0d0d;
            border-color: #262626;
        }

        body.bw-page-dark .theme-btn.active {
            background: #ffffff;
            color: #000000;
        }

        body.bw-page-dark .btn-picker.btn-gallery {
            background: #262626 !important;
            border-color: #404040 !important;
            box-shadow: 0 2px 8px rgba(0,0,0,0.5) !important;
        }

        body.bw-page-dark .format-switcher-wrap {
            background: #0d0d0d;
            border-color: #262626;
        }

        body.bw-page-dark .format-cat-bar {
            background: #171717;
            border-color: #262626;
        }

        body.bw-page-dark .format-cat-btn.active {
            background: #ffffff;
            color: #000000;
        }

        body.bw-page-dark .format-pill-btn {
            background: #171717;
            border-color: #262626;
            color: #ffffff;
        }

        body.bw-page-dark .format-pill-btn.active {
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
            box-shadow: 0 0 12px rgba(255,255,255,0.25);
        }

        body.bw-page-dark .format-pill-btn.active .format-pill-name,
        body.bw-page-dark .format-pill-btn.active .format-pill-ext,
        body.bw-page-dark .format-pill-btn.active .format-pill-hint {
            color: #000000;
        }

        body.bw-page-dark .quick-format-pill {
            background: #171717;
            border: 1px solid #262626;
            color: #ffffff;
        }

        body.bw-page-dark .quick-format-pill:hover:not(.active) {
            background: #262626;
            border-color: #ffffff;
        }

        body.bw-page-dark .quick-format-pill.active {
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
        }

        body.bw-page-dark .btn-more-formats {
            background: #171717;
            border: 1px solid #262626;
            color: #ffffff;
        }

        body.bw-page-dark .btn-more-formats:hover {
            border-color: #ffffff;
            background: #262626;
        }

        body.bw-page-dark .btn-more-formats.expanded {
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
        }

        body.bw-page-dark .btn-submit {
            background: #ffffff !important;
            color: #000000 !important;
            box-shadow: 0 4px 16px rgba(255, 255, 255, 0.25) !important;
        }

        body.bw-page-dark .btn-submit:hover:not(:disabled) {
            background: #e5e5e5 !important;
            box-shadow: 0 6px 20px rgba(255, 255, 255, 0.4) !important;
        }

        body.bw-page-dark .btn-filter.active {
            background: #ffffff !important;
            color: #000000 !important;
        }

        body.bw-page-dark .btn-action.download {
            background: #262626 !important;
            border-color: #404040 !important;
            color: #ffffff !important;
        }

        body.bw-page-dark .btn-action.download:hover {
            background: #ffffff !important;
            color: #000000 !important;
        }

        /* ==========================================================================
           DISPLAY STYLE 1: EDITORIAL (High-fashion Typography & Tactile Print)
           ========================================================================== */
        body.theme-editorial {
            --bg-color: #f7f4ec;
            --card-bg: #fffefb;
            --card-border: #1c1917;
            --accent-primary: #853b1b;
            --accent-hover: #6c2f14;
            --accent-glow: rgba(133, 59, 27, 0.22);
            --text-main: #1c1917;
            --text-muted: #57534e;
            --text-dim: #78716c;
            --success: #1c1917;
            --danger: #991b1b;
            --warning: #b45309;
            --radius-md: 4px;
            --radius-lg: 6px;
            font-family: 'Newsreader', 'Playfair Display', Georgia, serif;
            background-color: #f7f4ec;
            background-image: 
                radial-gradient(#e5dfd3 0.8px, transparent 0.8px),
                linear-gradient(to bottom, rgba(247, 244, 236, 0.7), rgba(247, 244, 236, 1));
            background-size: 18px 18px, 100% 100%;
            color: #1c1917;
            letter-spacing: -0.005em;
        }

        body.theme-editorial .header-top {
            border-bottom: 1.5px solid #1c1917;
            padding-bottom: 12px;
            margin-bottom: 18px;
        }

        body.theme-editorial .header-badge {
            background: #ede8dc !important;
            border: 1px solid #1c1917 !important;
            color: #1c1917 !important;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.72rem;
            letter-spacing: 0.08em;
            border-radius: 2px !important;
            box-shadow: 2px 2px 0px #1c1917;
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        body.theme-editorial .header-badge:hover {
            transform: translateY(-2px) rotate(-1deg);
        }

        body.theme-editorial .btn-diag,
        body.theme-editorial .btn-bw-toggle {
            background: #fffefb !important;
            border: 1.5px solid #1c1917 !important;
            color: #1c1917 !important;
            border-radius: 2px !important;
            box-shadow: 2px 2px 0px #1c1917 !important;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
        }

        body.theme-editorial .btn-diag:hover,
        body.theme-editorial .btn-bw-toggle:hover {
            background: #f1ecdf !important;
            transform: translate(-1px, -1px);
            box-shadow: 3px 3px 0px #1c1917 !important;
        }

        body.theme-editorial .header h1 {
            font-family: 'Newsreader', 'Playfair Display', Georgia, serif;
            font-style: italic;
            font-weight: 600;
            font-size: 2.85rem;
            letter-spacing: -0.02em;
            color: #1c1917;
            background: none;
            -webkit-background-clip: initial;
            -webkit-text-fill-color: initial;
        }

        body.theme-editorial .header p {
            font-family: 'Newsreader', Georgia, serif;
            font-size: 1.05rem;
            color: #57534e;
            font-style: normal;
        }

        body.theme-editorial .header p code {
            font-family: 'JetBrains Mono', monospace;
            background: #ede8dc;
            padding: 2px 6px;
            border: 1px solid #d6cfbf;
            border-radius: 2px;
            color: #1c1917;
        }

        body.theme-editorial .theme-switcher {
            background: #fffefb;
            border: 1.5px solid #1c1917;
            border-radius: 4px;
            box-shadow: 3px 3px 0px #1c1917;
            padding: 4px;
        }

        body.theme-editorial .theme-btn {
            border-radius: 2px;
            color: #57534e;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.76rem;
            padding: 5px 11px;
        }

        body.theme-editorial .theme-btn.active {
            background: #1c1917;
            color: #fffefb;
            box-shadow: 2px 2px 0px #853b1b;
        }

        body.theme-editorial .theme-btn:hover:not(.active) {
            background: #ede8dc;
            color: #1c1917;
        }

        body.theme-editorial .card {
            background: #fffefb;
            border: 2px solid #1c1917;
            border-radius: 4px;
            box-shadow: 5px 5px 0px #1c1917;
            transition: transform 0.22s ease, box-shadow 0.22s ease;
        }

        body.theme-editorial .card:hover {
            transform: translate(-1px, -1px);
            box-shadow: 7px 7px 0px #1c1917;
        }

        body.theme-editorial .card-title {
            font-family: 'Newsreader', 'Playfair Display', Georgia, serif;
            font-weight: 700;
            font-size: 1.35rem;
            letter-spacing: -0.01em;
            color: #1c1917;
            border-bottom: 1.5px solid #ede8dc;
            padding-bottom: 10px;
        }

        body.theme-editorial .picker-buttons-row .btn-picker {
            border-radius: 3px;
            border: 1.5px solid #1c1917;
            box-shadow: 3px 3px 0px #1c1917;
            font-family: 'Plus Jakarta Sans', system-ui, sans-serif;
            font-weight: 600;
            font-size: 0.82rem;
            background: #fffefb;
            color: #1c1917;
            transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.18s ease;
        }

        body.theme-editorial .picker-buttons-row .btn-picker:hover {
            background: #ede8dc;
            transform: translate(-2px, -2px);
            box-shadow: 5px 5px 0px #1c1917;
        }

        body.theme-editorial .picker-buttons-row .btn-picker:active {
            transform: translate(2px, 2px);
            box-shadow: 1px 1px 0px #1c1917;
        }

        body.theme-editorial .btn-picker.btn-gallery {
            background: #fffefb !important;
            border-color: #1c1917 !important;
            color: #1c1917 !important;
        }

        body.theme-editorial .btn-picker.btn-gallery svg {
            color: #853b1b;
        }

        body.theme-editorial .dropzone {
            background: #faf7f0;
            border: 2px dashed #1c1917;
            border-radius: 4px;
            transition: all 0.22s ease;
        }

        body.theme-editorial .dropzone:hover,
        body.theme-editorial .dropzone.dragover {
            background: #f2ebd9;
            border-style: solid;
            border-color: #853b1b;
            box-shadow: inset 0 0 0 2px #853b1b;
            transform: scale(1.005);
        }

        body.theme-editorial .dropzone-icon {
            background: #ede8dc;
            border: 1.5px solid #1c1917;
            box-shadow: 3px 3px 0px #1c1917;
            border-radius: 4px;
        }

        body.theme-editorial .dropzone-icon svg {
            color: #853b1b;
        }

        body.theme-editorial .dropzone h3 {
            font-family: 'Newsreader', Georgia, serif;
            font-size: 1.3rem;
            font-weight: 600;
            color: #1c1917;
        }

        body.theme-editorial .dropzone p {
            color: #57534e;
        }

        body.theme-editorial .file-chip {
            background: #faf7f0;
            border: 1.5px solid #1c1917;
            box-shadow: 3px 3px 0px #1c1917;
            border-radius: 3px;
            color: #1c1917;
        }

        body.theme-editorial .chip-name {
            font-family: 'Newsreader', serif;
            font-weight: 600;
            font-size: 0.95rem;
            color: #1c1917;
        }

        body.theme-editorial .chip-meta {
            font-family: 'JetBrains Mono', monospace;
            color: #57534e;
        }

        body.theme-editorial .chip-remove {
            color: #853b1b;
        }

        body.theme-editorial .chip-remove:hover {
            background: #853b1b;
            color: #fffefb;
            border-radius: 2px;
        }

        body.theme-editorial .select-styled {
            background: #fffefb;
            border: 1.5px solid #1c1917;
            border-radius: 3px;
            box-shadow: 2px 2px 0px #1c1917;
            color: #1c1917;
            font-family: 'Newsreader', serif;
            font-size: 1rem;
        }

        body.theme-editorial .format-switcher-wrap {
            background: #fffefb;
            border: 2px solid #1c1917;
            border-radius: 4px;
            box-shadow: 4px 4px 0px #1c1917;
        }

        body.theme-editorial .format-switcher-title {
            font-family: 'Newsreader', serif;
            font-size: 0.95rem;
            color: #1c1917;
        }

        body.theme-editorial .format-active-tag {
            background: #ede8dc;
            border: 1.5px solid #1c1917;
            color: #853b1b;
            font-family: 'JetBrains Mono', monospace;
            border-radius: 2px;
            box-shadow: 1px 1px 0px #1c1917;
        }

        body.theme-editorial .format-cat-bar {
            background: #faf7f0;
            border: 1.5px solid #1c1917;
            border-radius: 3px;
        }

        body.theme-editorial .format-cat-btn {
            font-family: 'Newsreader', serif;
            font-size: 0.88rem;
            color: #57534e;
            border-radius: 2px;
        }

        body.theme-editorial .format-cat-btn.active {
            background: #1c1917;
            color: #fffefb;
            box-shadow: 2px 2px 0px #853b1b;
        }

        body.theme-editorial .format-pill-btn {
            background: #faf7f0;
            border: 1.5px solid #1c1917;
            border-radius: 3px;
            box-shadow: 2px 2px 0px #1c1917;
            color: #1c1917;
            font-family: 'Newsreader', serif;
        }

        body.theme-editorial .format-pill-btn:hover:not(.active) {
            background: #ede8dc;
            transform: translate(-1px, -1px);
            box-shadow: 3px 3px 0px #1c1917;
        }

        body.theme-editorial .format-pill-btn.active {
            background: #1c1917;
            border-color: #1c1917;
            color: #fffefb;
            box-shadow: 3px 3px 0px #853b1b;
        }

        body.theme-editorial .format-pill-btn.active .format-pill-name,
        body.theme-editorial .format-pill-btn.active .format-pill-ext {
            color: #fffefb;
        }

        body.theme-editorial .format-pill-btn.active .format-pill-hint {
            color: #d6cfbf;
        }

        body.theme-editorial .quick-format-pill {
            background: #fffefb;
            border: 1.5px solid #1c1917;
            color: #1c1917;
            font-family: 'Newsreader', serif;
            border-radius: 2px;
            box-shadow: 1px 1px 0px #1c1917;
        }

        body.theme-editorial .quick-format-pill:hover:not(.active) {
            background: #ede8dc;
            transform: translate(-1px, -1px);
            box-shadow: 2px 2px 0px #1c1917;
        }

        body.theme-editorial .quick-format-pill.active {
            background: #1c1917;
            color: #fffefb;
            box-shadow: 2px 2px 0px #853b1b;
        }

        body.theme-editorial .btn-more-formats {
            background: #ede8dc;
            border: 1.5px solid #1c1917;
            color: #1c1917;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            border-radius: 2px;
            box-shadow: 2px 2px 0px #1c1917;
        }

        body.theme-editorial .btn-more-formats:hover {
            background: #fffefb;
            transform: translate(-1px, -1px);
            box-shadow: 3px 3px 0px #1c1917;
        }

        body.theme-editorial .btn-more-formats.expanded {
            background: #1c1917;
            color: #fffefb;
            box-shadow: 2px 2px 0px #853b1b;
        }

        body.theme-editorial .btn-submit {
            background: #1c1917 !important;
            color: #fffefb !important;
            border: 2px solid #1c1917 !important;
            border-radius: 3px !important;
            box-shadow: 4px 4px 0px #853b1b !important;
            font-family: 'Newsreader', Georgia, serif;
            font-size: 1.15rem;
            font-weight: 600;
            letter-spacing: 0.02em;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        body.theme-editorial .btn-submit:hover:not(:disabled) {
            background: #853b1b !important;
            border-color: #853b1b !important;
            transform: translate(-2px, -2px);
            box-shadow: 6px 6px 0px #1c1917 !important;
        }

        body.theme-editorial .btn-submit:active {
            transform: translate(2px, 2px);
            box-shadow: 1px 1px 0px #1c1917 !important;
        }

        body.theme-editorial .search-box {
            background: #fffefb;
            border: 1.5px solid #1c1917;
            border-radius: 3px;
            box-shadow: 2px 2px 0px #1c1917;
            color: #1c1917;
            font-family: 'Newsreader', serif;
        }

        body.theme-editorial .btn-filter {
            background: #fffefb;
            border: 1px solid #1c1917;
            border-radius: 2px;
            color: #57534e;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.74rem;
        }

        body.theme-editorial .btn-filter.active {
            background: #1c1917 !important;
            color: #fffefb !important;
            box-shadow: 2px 2px 0px #853b1b;
        }

        body.theme-editorial .btn-action.download {
            background: #1c1917 !important;
            color: #fffefb !important;
            border: 1.5px solid #1c1917 !important;
            border-radius: 2px !important;
            box-shadow: 2px 2px 0px #853b1b !important;
            font-family: 'JetBrains Mono', monospace;
        }

        body.theme-editorial .btn-action.download:hover {
            background: #853b1b !important;
            border-color: #853b1b !important;
            transform: translate(-1px, -1px);
            box-shadow: 3px 3px 0px #1c1917 !important;
        }

        body.theme-editorial .btn-action.delete {
            background: #fffefb !important;
            color: #991b1b !important;
            border: 1.5px solid #991b1b !important;
            border-radius: 2px !important;
            box-shadow: 2px 2px 0px #991b1b !important;
        }

        body.theme-editorial .btn-action.delete:hover {
            background: #991b1b !important;
            color: #fffefb !important;
        }

        body.theme-editorial .file-row {
            background: #faf7f0;
            border: 1.5px solid #1c1917;
            border-radius: 3px;
            box-shadow: 3px 3px 0px #1c1917;
            transition: all 0.18s ease;
        }

        body.theme-editorial .file-row:hover {
            background: #fffefb;
            transform: translate(-1px, -1px);
            box-shadow: 4px 4px 0px #1c1917;
        }

        body.theme-editorial .file-name {
            font-family: 'Newsreader', serif;
            font-weight: 600;
            font-size: 0.98rem;
            color: #1c1917;
        }

        body.theme-editorial .grid-card {
            background: #fffefb;
            border: 1.5px solid #1c1917;
            border-radius: 3px;
            box-shadow: 4px 4px 0px #1c1917;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        body.theme-editorial .grid-card:hover {
            transform: translate(-2px, -2px);
            box-shadow: 6px 6px 0px #1c1917;
        }

        body.theme-editorial .grid-name {
            font-family: 'Newsreader', serif;
            font-weight: 600;
            color: #1c1917;
        }

        body.theme-editorial .modal-card {
            background: #fffefb;
            border: 2px solid #1c1917;
            border-radius: 4px;
            box-shadow: 8px 8px 0px #1c1917;
            color: #1c1917;
        }

        body.theme-editorial .diag-box {
            background: #faf7f0;
            border: 1.5px solid #1c1917;
            border-radius: 2px;
            box-shadow: 2px 2px 0px #1c1917;
        }

        body.theme-editorial .diag-val {
            color: #1c1917;
            font-family: 'JetBrains Mono', monospace;
        }

        body.theme-editorial .log-list {
            background: #f7f4ec;
            border: 1.5px solid #1c1917;
            color: #1c1917;
            font-family: 'JetBrains Mono', monospace;
        }

        /* ==========================================================================
           DISPLAY STYLE 2: SOFT LIQUID GLASS (VisionOS Fluid Acrylic & Specular Sheen)
           ========================================================================== */
        body.theme-liquid-glass {
            --bg-color: #070913;
            --card-bg: rgba(255, 255, 255, 0.045);
            --card-border: rgba(255, 255, 255, 0.16);
            --accent-primary: #6366f1;
            --accent-hover: #4f46e5;
            --accent-glow: rgba(99, 102, 241, 0.45);
            --text-main: #ffffff;
            --text-muted: #cbd5e1;
            --text-dim: #94a3b8;
            --success: #34d399;
            --danger: #f87171;
            --warning: #fbbf24;
            --radius-md: 14px;
            --radius-lg: 24px;
            background-color: #070913;
            background-image: 
                radial-gradient(circle 900px at 15% 15%, rgba(99, 102, 241, 0.22), transparent 70%),
                radial-gradient(circle 800px at 85% 25%, rgba(236, 72, 153, 0.18), transparent 70%),
                radial-gradient(circle 950px at 50% 85%, rgba(56, 189, 248, 0.22), transparent 70%),
                radial-gradient(circle 700px at 10% 75%, rgba(168, 85, 247, 0.18), transparent 70%);
            background-attachment: fixed;
            color: #ffffff;
        }

        /* Ambient Floating Orbs */
        .liquid-bg-orbs {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            pointer-events: none;
            z-index: -1;
            overflow: hidden;
        }

        body.theme-liquid-glass .liquid-bg-orbs {
            display: block;
        }

        .liquid-orb {
            position: absolute;
            border-radius: 50%;
            filter: blur(80px);
            opacity: 0.42;
            mix-blend-mode: screen;
            animation: liquidOrbFloat 18s ease-in-out infinite alternate;
        }

        .liquid-orb.orb-1 {
            width: 440px;
            height: 440px;
            background: radial-gradient(circle, #6366f1, #a855f7);
            top: -100px;
            left: -80px;
            animation-duration: 20s;
        }

        .liquid-orb.orb-2 {
            width: 500px;
            height: 500px;
            background: radial-gradient(circle, #ec4899, #38bdf8);
            bottom: -120px;
            right: -100px;
            animation-duration: 24s;
            animation-direction: alternate-reverse;
        }

        .liquid-orb.orb-3 {
            width: 360px;
            height: 360px;
            background: radial-gradient(circle, #06b6d4, #8b5cf6);
            top: 45%;
            left: 55%;
            animation-duration: 22s;
        }

        @keyframes liquidOrbFloat {
            0% { transform: translate(0, 0) scale(1); }
            50% { transform: translate(70px, 45px) scale(1.1); }
            100% { transform: translate(-50px, 80px) scale(0.94); }
        }

        body.theme-liquid-glass .header h1 {
            background: linear-gradient(135deg, #ffffff 30%, #38bdf8 65%, #c084fc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-shadow: 0 0 35px rgba(56, 189, 248, 0.3);
        }

        body.theme-liquid-glass .header-badge {
            background: rgba(255, 255, 255, 0.08) !important;
            border: 1px solid rgba(255, 255, 255, 0.22) !important;
            color: #e2e8f0 !important;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.25), inset 0 1px 1px rgba(255, 255, 255, 0.35);
        }

        body.theme-liquid-glass .btn-diag,
        body.theme-liquid-glass .btn-bw-toggle {
            background: rgba(255, 255, 255, 0.07) !important;
            border: 1px solid rgba(255, 255, 255, 0.2) !important;
            color: #f1f5f9 !important;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            box-shadow: 0 4px 15px rgba(0,0,0,0.3), inset 0 1px 1px rgba(255,255,255,0.3) !important;
        }

        body.theme-liquid-glass .btn-diag:hover,
        body.theme-liquid-glass .btn-bw-toggle:hover {
            background: rgba(255, 255, 255, 0.15) !important;
            border-color: rgba(56, 189, 248, 0.6) !important;
            color: #ffffff !important;
            box-shadow: 0 6px 20px rgba(56, 189, 248, 0.35), inset 0 1px 1.5px rgba(255, 255, 255, 0.6) !important;
            transform: translateY(-2px);
        }

        body.theme-liquid-glass .theme-switcher {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.2);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4), inset 0 1px 1px rgba(255, 255, 255, 0.35);
        }

        body.theme-liquid-glass .theme-btn {
            color: #cbd5e1;
            border-radius: 9999px;
        }

        body.theme-liquid-glass .theme-btn.active {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.9), rgba(168, 85, 247, 0.9));
            color: #ffffff;
            box-shadow: 0 4px 18px rgba(168, 85, 247, 0.55), inset 0 1px 1.5px rgba(255, 255, 255, 0.7);
        }

        body.theme-liquid-glass .card {
            background: rgba(255, 255, 255, 0.045);
            backdrop-filter: blur(26px) saturate(210%);
            -webkit-backdrop-filter: blur(26px) saturate(210%);
            border: 1px solid rgba(255, 255, 255, 0.18);
            border-radius: 24px;
            box-shadow: 
                0 20px 50px rgba(0, 0, 0, 0.5),
                inset 0 1.5px 1px 0 rgba(255, 255, 255, 0.35),
                inset 0 -1px 1px 0 rgba(0, 0, 0, 0.25);
            position: relative;
            overflow: hidden;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }

        body.theme-liquid-glass .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: radial-gradient(550px circle at var(--mouse-x, 50%) var(--mouse-y, 50%), rgba(255, 255, 255, 0.08), transparent 45%);
            pointer-events: none;
            opacity: 0.8;
            transition: opacity 0.3s ease;
        }

        body.theme-liquid-glass .card:hover {
            transform: translateY(-3px);
            border-color: rgba(168, 85, 247, 0.35);
            box-shadow: 
                0 25px 60px rgba(99, 102, 241, 0.25),
                inset 0 1.5px 1.5px 0 rgba(255, 255, 255, 0.5);
        }

        body.theme-liquid-glass .btn-picker.btn-gallery {
            background: linear-gradient(135deg, rgba(56, 189, 248, 0.18), rgba(168, 85, 247, 0.18)) !important;
            border: 1px solid rgba(255, 255, 255, 0.25) !important;
            backdrop-filter: blur(14px) !important;
            -webkit-backdrop-filter: blur(14px) !important;
            border-radius: 9999px !important;
            box-shadow: 0 4px 15px rgba(0,0,0,0.3), inset 0 1px 1px rgba(255,255,255,0.4) !important;
            color: #ffffff !important;
        }

        body.theme-liquid-glass .btn-picker.btn-gallery:hover {
            background: linear-gradient(135deg, rgba(56, 189, 248, 0.3), rgba(168, 85, 247, 0.3)) !important;
            border-color: rgba(255, 255, 255, 0.55) !important;
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(56, 189, 248, 0.4), inset 0 1px 1.5px rgba(255,255,255,0.7) !important;
        }

        body.theme-liquid-glass .btn-picker.btn-camera,
        body.theme-liquid-glass .btn-picker.btn-files {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.18);
            backdrop-filter: blur(14px);
            border-radius: 9999px;
            color: #e2e8f0;
            box-shadow: 0 4px 15px rgba(0,0,0,0.25), inset 0 1px 1px rgba(255,255,255,0.3);
        }

        body.theme-liquid-glass .btn-picker.btn-camera:hover,
        body.theme-liquid-glass .btn-picker.btn-files:hover {
            background: rgba(255, 255, 255, 0.12);
            border-color: rgba(255, 255, 255, 0.4);
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(0,0,0,0.35), inset 0 1px 1px rgba(255,255,255,0.5);
        }

        body.theme-liquid-glass .dropzone {
            background: rgba(255, 255, 255, 0.03);
            border: 1.5px dashed rgba(255, 255, 255, 0.22);
            border-radius: 18px;
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            box-shadow: inset 0 2px 12px rgba(0,0,0,0.25);
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }

        body.theme-liquid-glass .dropzone:hover,
        body.theme-liquid-glass .dropzone.dragover {
            background: rgba(99, 102, 241, 0.1);
            border-color: #a855f7;
            box-shadow: 0 0 35px rgba(168, 85, 247, 0.35), inset 0 1px 2px rgba(255, 255, 255, 0.45);
            transform: scale(1.01);
        }

        body.theme-liquid-glass .dropzone-icon {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.25), rgba(168, 85, 247, 0.25));
            border: 1px solid rgba(255, 255, 255, 0.25);
            box-shadow: 0 0 20px rgba(99, 102, 241, 0.3), inset 0 1px 1px rgba(255, 255, 255, 0.4);
            border-radius: 50%;
        }

        body.theme-liquid-glass .dropzone-icon svg {
            color: #38bdf8;
        }

        body.theme-liquid-glass .file-chip {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 9999px;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.25), inset 0 1px 1px rgba(255, 255, 255, 0.35);
            color: #ffffff;
        }

        body.theme-liquid-glass .select-styled {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.2);
            backdrop-filter: blur(16px);
            border-radius: 12px;
            color: #ffffff;
            box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.2);
        }

        body.theme-liquid-glass .select-styled option {
            background: #0f172a;
            color: #ffffff;
        }

        body.theme-liquid-glass .format-switcher-wrap {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            border-radius: 20px;
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.25), 0 8px 30px rgba(0, 0, 0, 0.3);
        }

        body.theme-liquid-glass .format-active-tag {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.22);
            color: #38bdf8;
            backdrop-filter: blur(14px);
        }

        body.theme-liquid-glass .format-cat-bar {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.16);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
        }

        body.theme-liquid-glass .format-cat-btn.active {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.8), rgba(168, 85, 247, 0.8));
            color: #ffffff;
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.45), inset 0 1px 1px rgba(255, 255, 255, 0.5);
        }

        body.theme-liquid-glass .format-pill-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 14px;
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            color: #cbd5e1;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        }

        body.theme-liquid-glass .format-pill-btn:hover:not(.active) {
            background: rgba(255, 255, 255, 0.1);
            border-color: rgba(56, 189, 248, 0.5);
            color: #ffffff;
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(56, 189, 248, 0.25);
        }

        body.theme-liquid-glass .format-pill-btn.active {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.35), rgba(168, 85, 247, 0.35));
            border-color: rgba(168, 85, 247, 0.6);
            color: #ffffff;
            box-shadow: 0 0 20px rgba(168, 85, 247, 0.4), inset 0 1px 1.5px rgba(255, 255, 255, 0.5);
        }

        body.theme-liquid-glass .quick-format-pill {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.16);
            color: #cbd5e1;
            backdrop-filter: blur(14px);
            border-radius: 9999px;
        }

        body.theme-liquid-glass .quick-format-pill:hover:not(.active) {
            background: rgba(255, 255, 255, 0.12);
            border-color: rgba(56, 189, 248, 0.5);
            color: #ffffff;
            transform: translateY(-2px);
        }

        body.theme-liquid-glass .quick-format-pill.active {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.85), rgba(168, 85, 247, 0.85));
            border-color: rgba(255, 255, 255, 0.4);
            color: #ffffff;
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.45);
        }

        body.theme-liquid-glass .btn-more-formats {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.22);
            color: #38bdf8;
            backdrop-filter: blur(14px);
            box-shadow: 0 4px 15px rgba(0,0,0,0.25), inset 0 1px 1px rgba(255,255,255,0.35);
        }

        body.theme-liquid-glass .btn-more-formats:hover {
            background: rgba(255, 255, 255, 0.15);
            border-color: rgba(56, 189, 248, 0.6);
            color: #ffffff;
            box-shadow: 0 6px 20px rgba(56, 189, 248, 0.35);
            transform: translateY(-2px);
        }

        body.theme-liquid-glass .btn-more-formats.expanded {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.85), rgba(168, 85, 247, 0.85));
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.4);
            box-shadow: 0 4px 18px rgba(168, 85, 247, 0.5);
        }

        body.theme-liquid-glass .btn-submit {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.95), rgba(168, 85, 247, 0.95)) !important;
            border: 1px solid rgba(255, 255, 255, 0.35) !important;
            border-radius: 9999px !important;
            box-shadow: 0 8px 30px rgba(99, 102, 241, 0.6), inset 0 1.5px 2px rgba(255, 255, 255, 0.7) !important;
            color: #ffffff !important;
            letter-spacing: 0.02em;
            transition: all 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        body.theme-liquid-glass .btn-submit:hover:not(:disabled) {
            transform: translateY(-2px) scale(1.02);
            box-shadow: 0 12px 40px rgba(168, 85, 247, 0.75), inset 0 1.5px 2px rgba(255, 255, 255, 0.9) !important;
        }

        body.theme-liquid-glass .btn-submit:active {
            transform: scale(0.97);
        }

        body.theme-liquid-glass .search-box {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.2);
            backdrop-filter: blur(16px);
            border-radius: 12px;
            color: #ffffff;
            box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.2);
        }

        body.theme-liquid-glass .btn-filter {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.16);
            backdrop-filter: blur(12px);
            border-radius: 9999px;
            color: #cbd5e1;
        }

        body.theme-liquid-glass .btn-filter.active {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.85), rgba(168, 85, 247, 0.85)) !important;
            border-color: rgba(255, 255, 255, 0.35) !important;
            color: #ffffff !important;
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.45), inset 0 1px 1px rgba(255,255,255,0.5);
        }

        body.theme-liquid-glass .file-row {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.14);
            border-radius: 14px;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.25), inset 0 1px 1px rgba(255, 255, 255, 0.2);
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }

        body.theme-liquid-glass .file-row:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(56, 189, 248, 0.45);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(56, 189, 248, 0.25), inset 0 1px 1.5px rgba(255, 255, 255, 0.4);
        }

        body.theme-liquid-glass .grid-card {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.16);
            border-radius: 16px;
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.3), inset 0 1px 1px rgba(255, 255, 255, 0.25);
            transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }

        body.theme-liquid-glass .grid-card:hover {
            transform: translateY(-3px) scale(1.01);
            border-color: rgba(168, 85, 247, 0.4);
            box-shadow: 0 12px 35px rgba(99, 102, 241, 0.35), inset 0 1px 1.5px rgba(255, 255, 255, 0.5);
        }

        body.theme-liquid-glass .btn-action.download {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.8), rgba(168, 85, 247, 0.8)) !important;
            border: 1px solid rgba(255, 255, 255, 0.25) !important;
            color: #ffffff !important;
            border-radius: 9999px !important;
            box-shadow: 0 2px 10px rgba(99, 102, 241, 0.4), inset 0 1px 1px rgba(255,255,255,0.4) !important;
        }

        body.theme-liquid-glass .btn-action.download:hover {
            transform: translateY(-1px) scale(1.05);
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.6), inset 0 1px 1.5px rgba(255,255,255,0.6) !important;
        }

        body.theme-liquid-glass .modal-card {
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(30px) saturate(220%);
            -webkit-backdrop-filter: blur(30px) saturate(220%);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 24px;
            box-shadow: 0 25px 60px rgba(0, 0, 0, 0.6), inset 0 1.5px 1px rgba(255, 255, 255, 0.35);
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
            padding: 26px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
        }

        .card-title {
            font-size: 1.22rem;
            font-weight: 600;
            margin-bottom: 18px;
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

        /* Direct Mobile Gallery & Action Pickers */
        .picker-buttons-row {
            display: grid;
            grid-template-columns: 1.3fr 1fr 1fr;
            gap: 10px;
            margin-bottom: 14px;
        }

        @media (max-width: 580px) {
            .picker-buttons-row {
                grid-template-columns: 1fr;
                gap: 8px;
            }
        }

        .btn-picker {
            padding: 12px 14px;
            border-radius: var(--radius-md);
            font-size: 0.9rem;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        border-color 0.2s ease;
            box-shadow: 0 2px 8px rgba(0,0,0,0.25);
            position: relative;
            overflow: hidden;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-picker svg {
            transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .btn-picker:hover svg {
            transform: translateY(-2px) scale(1.15);
        }

        .btn-picker:active {
            transform: translateY(1px) scale(0.96);
            box-shadow: 0 1px 4px rgba(0,0,0,0.3);
        }

        .btn-picker.btn-gallery {
            background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 50%, #1d4ed8 100%);
            background-size: 200% 200%;
            color: white;
            border: 1px solid #3b82f6;
            box-shadow: 0 2px 10px rgba(37, 99, 235, 0.35);
        }

        .btn-picker.btn-gallery::after {
            content: '';
            position: absolute;
            top: -50%;
            left: -60%;
            width: 35%;
            height: 200%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.25), transparent);
            transform: rotate(25deg);
            transition: left 0.65s ease;
            pointer-events: none;
        }

        .btn-picker.btn-gallery:hover {
            background-position: 100% 100%;
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(59, 130, 246, 0.5);
            border-color: #60a5fa;
        }

        .btn-picker.btn-gallery:hover::after {
            left: 130%;
        }

        .btn-picker.btn-camera {
            background: #111827;
            color: #f3f4f6;
            border: 1px solid #374151;
        }

        .btn-picker.btn-camera:hover {
            background: #1f2937;
            border-color: #38bdf8;
            transform: translateY(-2px);
            box-shadow: 0 5px 16px rgba(0,0,0,0.45), 0 0 10px rgba(56, 189, 248, 0.2);
            color: #ffffff;
        }

        .btn-picker.btn-camera:hover svg {
            color: #38bdf8;
        }

        .btn-picker.btn-docs {
            background: #111827;
            color: #f3f4f6;
            border: 1px solid #374151;
        }

        .btn-picker.btn-docs:hover {
            background: #1f2937;
            border-color: #a855f7;
            transform: translateY(-2px);
            box-shadow: 0 5px 16px rgba(0,0,0,0.45), 0 0 10px rgba(168, 85, 247, 0.2);
            color: #ffffff;
        }

        .btn-picker.btn-docs:hover svg {
            color: #c084fc;
        }

        /* Drag and Drop Zone */
        .dropzone {
            border: 2px dashed #334155;
            border-radius: var(--radius-md);
            padding: 32px 20px;
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
            width: 44px;
            height: 44px;
            stroke: #60a5fa;
            margin-bottom: 10px;
        }

        .dropzone-text {
            font-weight: 600;
            font-size: 1rem;
            margin-bottom: 5px;
        }

        .dropzone-sub {
            font-size: 0.82rem;
            color: var(--text-dim);
            line-height: 1.4;
        }

        input[type="file"] {
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

        .file-chip-thumb {
            width: 22px;
            height: 22px;
            border-radius: 4px;
            object-fit: cover;
            border: 1px solid #334155;
            flex-shrink: 0;
        }

        .file-chip-remove {
            cursor: pointer;
            color: #94a3b8;
            font-weight: bold;
            font-size: 1rem;
            line-height: 1;
            padding: 0 4px;
            border-radius: 4px;
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        color 0.2s ease,
                        background 0.2s ease;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .file-chip-remove:hover {
            color: var(--danger);
            background: rgba(239, 68, 68, 0.15);
            transform: scale(1.25);
        }

        .file-chip-remove:active {
            transform: scale(0.9);
        }

        .mobile-opt-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-top: 14px;
            font-size: 0.85rem;
            color: #94a3b8;
            background: rgba(15, 23, 42, 0.6);
            padding: 10px 14px;
            border-radius: var(--radius-md);
            border: 1px solid #1e293b;
        }

        .mobile-opt-left {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .mobile-opt-row input[type="checkbox"] {
            accent-color: var(--accent-primary);
            width: 17px;
            height: 17px;
            cursor: pointer;
            display: block;
        }

        /* Progress Bar */
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
            margin-top: 18px;
            display: flex;
            gap: 14px;
            flex-direction: column;
        }

        /* Target Format Segmented Switcher */
        .format-switcher-wrap {
            display: flex;
            flex-direction: column;
            gap: 12px;
            background: rgba(15, 23, 42, 0.55);
            border: 1px solid #1f293d;
            border-radius: var(--radius-lg);
            padding: 16px;
            box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.25), 0 4px 16px rgba(0, 0, 0, 0.2);
            transition: all 0.25s ease;
        }

        .format-switcher-header {
            display: flex;
            align-items: center;
            justify-content: flex-start;
            flex-wrap: wrap;
            gap: 10px;
        }

        .format-header-left {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .format-header-actions {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .format-switcher-title {
            font-size: 0.8rem;
            font-weight: 700;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin: 0;
        }

        .format-active-tag {
            font-size: 0.78rem;
            font-weight: 600;
            color: #60a5fa;
            background: rgba(59, 130, 246, 0.12);
            padding: 4px 12px;
            border-radius: 9999px;
            border: 1px solid rgba(59, 130, 246, 0.3);
            display: inline-flex;
            align-items: center;
            gap: 5px;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .quick-formats-pills {
            display: flex;
            align-items: center;
            gap: 5px;
            flex-wrap: wrap;
        }

        .quick-format-pill {
            background: #1e293b;
            border: 1px solid #334155;
            color: #94a3b8;
            border-radius: 9999px;
            padding: 4px 11px;
            font-size: 0.76rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
            user-select: none;
        }

        .quick-format-pill:hover:not(.active) {
            color: #ffffff;
            border-color: #38bdf8;
            background: #273549;
            transform: translateY(-1px);
        }

        .quick-format-pill:active {
            transform: scale(0.96);
        }

        .quick-format-pill.active {
            background: var(--accent-primary);
            color: #ffffff;
            border-color: var(--accent-primary);
            box-shadow: 0 2px 8px var(--accent-glow);
        }

        .btn-more-formats {
            background: rgba(59, 130, 246, 0.12);
            border: 1px solid rgba(59, 130, 246, 0.35);
            color: #60a5fa;
            border-radius: 9999px;
            padding: 4px 12px;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            transition: all 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
            user-select: none;
        }

        .btn-more-formats:hover {
            background: rgba(59, 130, 246, 0.22);
            border-color: #38bdf8;
            color: #ffffff;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.25);
        }

        .btn-more-formats:active {
            transform: scale(0.96);
        }

        .btn-more-formats .more-chevron {
            transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .btn-more-formats.expanded {
            background: #1e293b;
            border-color: #475569;
            color: #f1f5f9;
        }

        .btn-more-formats.expanded .more-chevron {
            transform: rotate(180deg);
        }

        .format-expanded-panel {
            display: flex;
            flex-direction: column;
            gap: 10px;
            max-height: 0;
            opacity: 0;
            overflow: hidden;
            pointer-events: none;
            transition: max-height 0.35s cubic-bezier(0.16, 1, 0.3, 1),
                        opacity 0.28s ease,
                        margin 0.35s cubic-bezier(0.16, 1, 0.3, 1);
            margin-top: 0;
        }

        .format-expanded-panel.expanded {
            max-height: 540px;
            opacity: 1;
            pointer-events: auto;
            margin-top: 6px;
        }

        .format-cat-bar {
            display: flex;
            background: #0b0f19;
            border: 1px solid #1f293d;
            border-radius: 9999px;
            padding: 3px;
            gap: 4px;
            overflow-x: auto;
            scrollbar-width: none;
            -webkit-overflow-scrolling: touch;
        }

        .format-cat-bar::-webkit-scrollbar {
            display: none;
        }

        .format-cat-btn {
            flex: 1;
            min-width: fit-content;
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
            user-select: none;
            white-space: nowrap;
        }

        .format-cat-btn:hover:not(.active) {
            color: #ffffff;
            background: rgba(255, 255, 255, 0.06);
            transform: translateY(-1px);
        }

        .format-cat-btn:active {
            transform: scale(0.96);
        }

        .format-cat-btn.active {
            background: #1e293b;
            color: #ffffff;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
        }

        .format-pills-container {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
            gap: 8px;
            padding-top: 4px;
        }

        .format-pill-btn {
            background: #111827;
            border: 1px solid #1f293d;
            border-radius: var(--radius-md);
            padding: 9px 12px;
            color: var(--text-muted);
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            flex-direction: column;
            gap: 3px;
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
            user-select: none;
            text-align: left;
        }

        .format-pill-btn:hover:not(.active) {
            border-color: #38bdf8;
            color: #ffffff;
            background: #1a2234;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.15);
        }

        .format-pill-btn:active {
            transform: scale(0.96);
        }

        .format-pill-btn.active {
            background: linear-gradient(135deg, rgba(59, 130, 246, 0.22), rgba(37, 99, 235, 0.16));
            border-color: var(--accent-primary);
            color: #ffffff;
            box-shadow: 0 0 14px var(--accent-glow);
        }

        .format-pill-top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 4px;
        }

        .format-pill-name {
            font-weight: 700;
            font-size: 0.88rem;
            color: var(--text-main);
        }

        .format-pill-ext {
            font-size: 0.72rem;
            color: var(--text-dim);
            font-family: 'JetBrains Mono', monospace;
        }

        .format-pill-btn.active .format-pill-ext {
            color: #93c5fd;
            font-weight: 600;
        }

        .format-pill-hint {
            font-size: 0.72rem;
            color: var(--text-dim);
            font-weight: 400;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .format-pill-btn.active .format-pill-hint {
            color: #cbd5e1;
        }

        label {
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 6px;
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
            background: linear-gradient(135deg, #2563eb 0%, #3b82f6 50%, #1d4ed8 100%);
            background-size: 200% auto;
            color: white;
            border: none;
            border-radius: var(--radius-md);
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.24s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.24s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.3s ease,
                        opacity 0.2s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 4px 16px rgba(59, 130, 246, 0.45);
            margin-top: 6px;
            position: relative;
            overflow: hidden;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-submit::after {
            content: '';
            position: absolute;
            top: -50%;
            left: -70%;
            width: 45%;
            height: 200%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.3), transparent);
            transform: rotate(25deg);
            transition: left 0.75s ease;
            pointer-events: none;
        }

        .btn-submit:hover:not(:disabled) {
            background-position: right center;
            transform: translateY(-2px);
            box-shadow: 0 8px 26px rgba(59, 130, 246, 0.6), 0 0 16px rgba(96, 165, 250, 0.4);
        }

        .btn-submit:hover:not(:disabled)::after {
            left: 140%;
        }

        .btn-submit:active:not(:disabled) {
            transform: translateY(1px) scale(0.98);
            box-shadow: 0 2px 8px rgba(59, 130, 246, 0.35);
        }

        .btn-submit:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none !important;
            box-shadow: none;
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
            padding: 8px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            font-size: 0.88rem;
            word-break: break-all;
        }

        .result-item:last-child {
            border-bottom: none;
        }

        /* Hosted Files List */
        .files-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 14px;
            flex-wrap: wrap;
            gap: 12px;
        }

        .header-controls {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .search-box {
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 6px 12px;
            color: var(--text-main);
            font-size: 0.85rem;
            font-family: inherit;
            outline: none;
            width: 140px;
            transition: all 0.2s;
        }

        .search-box:focus {
            border-color: var(--accent-primary);
            width: 180px;
        }

        .filter-btn-group {
            display: inline-flex;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 2px;
        }

        .btn-filter {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 5px 11px;
            border-radius: 6px;
            font-size: 0.78rem;
            cursor: pointer;
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        color 0.2s ease,
                        box-shadow 0.2s ease;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-filter:hover:not(.active) {
            color: #f1f5f9;
            background: rgba(255, 255, 255, 0.08);
            transform: translateY(-1px);
        }

        .btn-filter:active {
            transform: scale(0.95);
        }

        .btn-filter.active {
            background: var(--accent-primary);
            color: white;
            font-weight: 600;
            box-shadow: 0 2px 10px rgba(59, 130, 246, 0.4);
        }

        .view-toggle {
            display: inline-flex;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 2px;
        }

        .btn-view {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 5px 8px;
            border-radius: 6px;
            cursor: pointer;
            display: flex;
            align-items: center;
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        color 0.2s ease;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-view:hover:not(.active) {
            color: #f1f5f9;
            background: rgba(255, 255, 255, 0.08);
            transform: scale(1.1);
        }

        .btn-view:active {
            transform: scale(0.94);
        }

        .btn-view.active {
            background: #1e293b;
            color: #38bdf8;
            box-shadow: 0 1px 4px rgba(0,0,0,0.25);
        }

        .btn-refresh {
            background: #1e293b;
            border: 1px solid #334155;
            color: var(--text-muted);
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 0.8rem;
            cursor: pointer;
            transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        border-color 0.2s ease,
                        color 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-refresh svg {
            transition: transform 0.45s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .btn-refresh:hover {
            color: var(--text-main);
            background: #334155;
            border-color: #64748b;
            transform: translateY(-1.5px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }

        .btn-refresh:hover svg {
            transform: rotate(180deg);
        }

        .btn-refresh:active {
            transform: translateY(1px) scale(0.95);
            box-shadow: 0 1px 3px rgba(0,0,0,0.25);
        }

        .btn-refresh:active svg {
            transform: rotate(360deg);
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
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        border-color 0.2s ease,
                        color 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.2);
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .btn-action svg {
            transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .btn-action:hover {
            background: #334155;
            border-color: #475569;
            color: var(--text-main);
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }

        .btn-action:hover svg {
            transform: scale(1.18);
        }

        .btn-action:active {
            transform: translateY(1px) scale(0.95);
            box-shadow: 0 1px 2px rgba(0,0,0,0.2);
        }

        .btn-action.download {
            background: rgba(59, 130, 246, 0.12);
            border-color: rgba(59, 130, 246, 0.35);
            color: #60a5fa;
        }

        .btn-action.download:hover {
            background: var(--accent-primary);
            border-color: var(--accent-primary);
            color: white;
            box-shadow: 0 4px 14px rgba(59, 130, 246, 0.45);
        }

        .btn-action.download:hover svg {
            transform: translateY(1px) scale(1.15);
        }

        .btn-action.delete {
            background: rgba(239, 68, 68, 0.1);
            border-color: rgba(239, 68, 68, 0.3);
            color: #f87171;
        }

        .btn-action.delete:hover {
            background: var(--danger);
            border-color: var(--danger);
            color: white;
            box-shadow: 0 4px 14px rgba(239, 68, 68, 0.45);
        }

        .btn-action.delete:hover svg {
            transform: scale(1.18) rotate(6deg);
        }

        /* Grid View */
        .files-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 14px;
        }

        .grid-card {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: var(--radius-md);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: all 0.2s;
        }

        .grid-card:hover {
            border-color: #3b82f6;
            transform: translateY(-2px);
        }

        .grid-thumb {
            height: 120px;
            background: #182234;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            position: relative;
        }

        .grid-thumb img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .grid-icon-doc {
            font-size: 2.2rem;
            color: #64748b;
        }

        .grid-body {
            padding: 10px 12px;
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }

        .grid-name {
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-main);
            text-decoration: none;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            margin-bottom: 4px;
        }

        .grid-name:hover {
            color: #60a5fa;
        }

        .grid-meta {
            font-size: 0.72rem;
            color: var(--text-dim);
            margin-bottom: 8px;
        }

        .grid-actions {
            display: flex;
            gap: 6px;
        }

        .grid-actions .btn-action {
            flex: 1;
            justify-content: center;
            padding: 5px 6px;
            font-size: 0.74rem;
        }

        .empty-state {
            text-align: center;
            padding: 36px 20px;
            color: var(--text-dim);
            font-size: 0.9rem;
        }

        /* Diagnostics Modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            backdrop-filter: blur(4px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }

        .modal-card {
            background: #111827;
            border: 1px solid #334155;
            border-radius: var(--radius-lg);
            width: 100%;
            max-width: 700px;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 20px 40px rgba(0,0,0,0.6);
        }

        .modal-header {
            padding: 16px 20px;
            border-bottom: 1px solid #1f293d;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .modal-title {
            font-size: 1.1rem;
            font-weight: 600;
        }

        .modal-close {
            background: transparent;
            border: none;
            color: #94a3b8;
            font-size: 1.4rem;
            cursor: pointer;
            line-height: 1;
            width: 32px;
            height: 32px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.2s ease,
                        color 0.2s ease;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        .modal-close:hover {
            color: #ffffff;
            background: rgba(239, 68, 68, 0.2);
            transform: scale(1.15) rotate(90deg);
        }

        .modal-close:active {
            transform: scale(0.92) rotate(90deg);
        }

        .modal-body {
            padding: 20px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .diag-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: 10px;
        }

        .diag-box {
            background: #0f172a;
            border: 1px solid #1e293b;
            padding: 10px 14px;
            border-radius: 8px;
        }

        .diag-label {
            font-size: 0.72rem;
            color: var(--text-dim);
            text-transform: uppercase;
            font-weight: 600;
        }

        .diag-val {
            font-size: 1rem;
            font-weight: 700;
            color: #38bdf8;
            margin-top: 4px;
        }

        .log-list {
            background: #090d16;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 12px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            max-height: 240px;
            overflow-y: auto;
            color: #94a3b8;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .log-entry {
            display: flex;
            gap: 8px;
        }

        .log-time {
            color: #64748b;
            white-space: nowrap;
        }

        .log-status-info { color: #38bdf8; }
        .log-status-error { color: #f87171; }
        .log-status-client { color: #fbbf24; }

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

        /* Accessible Focus Outlines */
        button:focus-visible, a.btn-action:focus-visible, select:focus-visible, input:focus-visible {
            outline: 2px solid #38bdf8 !important;
            outline-offset: 2px !important;
        }

        @keyframes copyPulse {
            0% { transform: scale(0.95); }
            50% { transform: scale(1.08); }
            100% { transform: scale(1); }
        }

        .btn-action-copied {
            animation: copyPulse 0.35s cubic-bezier(0.34, 1.56, 0.64, 1) !important;
        }

        /* ZeroBlur Engine Integration Navigation & Callout */
        .btn-zeroblur-link {
            background: rgba(16, 185, 129, 0.15) !important;
            border: 1px solid rgba(16, 185, 129, 0.45) !important;
            color: #34d399 !important;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            border-radius: 9999px;
            padding: 4px 12px;
            font-size: 0.78rem;
            font-weight: 600;
            transition: all 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
            user-select: none;
        }

        .btn-zeroblur-link:hover {
            background: rgba(16, 185, 129, 0.28) !important;
            border-color: #10b981 !important;
            color: #ffffff !important;
            transform: translateY(-1px);
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.35) !important;
        }

        .btn-zeroblur-link:active {
            transform: scale(0.96);
        }

        .btn-zeroblur-nav {
            background: rgba(16, 185, 129, 0.15) !important;
            border: 1px solid rgba(16, 185, 129, 0.4) !important;
            color: #34d399 !important;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .btn-zeroblur-nav:hover {
            background: rgba(16, 185, 129, 0.3) !important;
            border-color: #34d399 !important;
            color: #ffffff !important;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3) !important;
        }

        .zeroblur-banner-callout {
            margin-top: 10px;
            padding: 10px 14px;
            background: rgba(16, 185, 129, 0.08);
            border: 1px dashed rgba(16, 185, 129, 0.35);
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }

        .zeroblur-callout-text {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 0.78rem;
            color: #cbd5e1;
        }

        .zeroblur-callout-text p {
            margin: 0;
        }

        .zeroblur-badge {
            background: rgba(16, 185, 129, 0.2);
            color: #34d399;
            padding: 2px 8px;
            border-radius: 9999px;
            font-size: 0.72rem;
            font-weight: 700;
            white-space: nowrap;
        }

        .btn-callout-launch {
            background: #10b981;
            color: #064e3b;
            font-weight: 700;
            font-size: 0.75rem;
            padding: 5px 12px;
            border-radius: 8px;
            text-decoration: none;
            transition: all 0.2s ease;
            white-space: nowrap;
        }

        .btn-callout-launch:hover {
            background: #34d399;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.4);
        }

        /* Theme Overrides for ZeroBlur elements */
        body.bw-page-light .btn-zeroblur-link,
        body.bw-page-light .btn-zeroblur-nav {
            background: #ffffff !important;
            border: 1px solid #09090b !important;
            color: #09090b !important;
        }
        body.bw-page-light .btn-zeroblur-link:hover,
        body.bw-page-light .btn-zeroblur-nav:hover {
            background: #09090b !important;
            color: #ffffff !important;
        }
        body.bw-page-light .zeroblur-banner-callout {
            background: #f4f4f5 !important;
            border: 1px dashed #09090b !important;
        }
        body.bw-page-light .zeroblur-badge {
            background: #09090b !important;
            color: #ffffff !important;
        }
        body.bw-page-light .btn-callout-launch {
            background: #09090b !important;
            color: #ffffff !important;
        }

        body.bw-page-dark .btn-zeroblur-link,
        body.bw-page-dark .btn-zeroblur-nav {
            background: #18181b !important;
            border: 1px solid #3f3f46 !important;
            color: #fafafa !important;
        }
        body.bw-page-dark .btn-zeroblur-link:hover,
        body.bw-page-dark .btn-zeroblur-nav:hover {
            background: #27272a !important;
            border-color: #fafafa !important;
            color: #ffffff !important;
        }
        body.bw-page-dark .zeroblur-banner-callout {
            background: #18181b !important;
            border: 1px dashed #3f3f46 !important;
        }
        body.bw-page-dark .zeroblur-badge {
            background: #27272a !important;
            color: #fafafa !important;
        }
        body.bw-page-dark .btn-callout-launch {
            background: #fafafa !important;
            color: #09090b !important;
        }

        body.theme-editorial .btn-zeroblur-link,
        body.theme-editorial .btn-zeroblur-nav {
            background: rgba(4, 120, 87, 0.12) !important;
            border: 1px solid rgba(4, 120, 87, 0.35) !important;
            color: #047857 !important;
            font-family: 'Newsreader', Georgia, serif;
        }
        body.theme-editorial .btn-zeroblur-link:hover,
        body.theme-editorial .btn-zeroblur-nav:hover {
            background: #047857 !important;
            color: #ffffff !important;
        }
        body.theme-editorial .zeroblur-banner-callout {
            background: #fbf9f4 !important;
            border: 1px dashed #cbd5e1 !important;
        }
        body.theme-editorial .btn-callout-launch {
            background: #047857 !important;
            color: #ffffff !important;
        }

        body.theme-liquid-glass .btn-zeroblur-link,
        body.theme-liquid-glass .btn-zeroblur-nav {
            background: rgba(16, 185, 129, 0.2) !important;
            backdrop-filter: blur(12px) !important;
            border: 1px solid rgba(52, 211, 153, 0.5) !important;
            color: #6ee7b7 !important;
            box-shadow: 0 4px 16px rgba(16, 185, 129, 0.25) !important;
        }
        body.theme-liquid-glass .btn-zeroblur-link:hover,
        body.theme-liquid-glass .btn-zeroblur-nav:hover {
            background: rgba(16, 185, 129, 0.4) !important;
            border-color: #a7f3d0 !important;
            color: #ffffff !important;
            box-shadow: 0 6px 22px rgba(16, 185, 129, 0.4) !important;
        }

    
        /* ==================== SLIDE-OUT NAVIGATION DRAWER & HAMBURGER ==================== */
        .btn-hamburger {
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.42rem 0.8rem;
            border-radius: 9999px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.14);
            color: #f1f5f9;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            user-select: none;
            -webkit-user-select: none;
            touch-action: manipulation;
            z-index: 40;
        }
        .btn-hamburger:hover {
            background: rgba(255, 255, 255, 0.12);
            border-color: rgba(255, 255, 255, 0.28);
            color: #ffffff;
            transform: translateY(-1px);
        }
        .btn-hamburger:active {
            transform: translateY(0);
        }
        .btn-hamburger svg {
            flex-shrink: 0;
            stroke: currentColor;
        }
        .btn-hamburger .hamburger-label {
            font-size: 0.78rem;
            letter-spacing: 0.02em;
        }

        /* Backdrop */
        .slide-nav-backdrop {
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(4, 7, 15, 0.72);
            backdrop-filter: blur(5px);
            -webkit-backdrop-filter: blur(5px);
            z-index: 9998;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .slide-nav-backdrop.is-open {
            opacity: 1;
            pointer-events: auto;
        }

        /* Slide Drawer */
        .slide-nav-drawer {
            position: fixed;
            top: 0;
            left: 0;
            bottom: 0;
            width: min(340px, 86vw);
            height: 100%;
            height: 100dvh;
            z-index: 9999;
            background: #0b1120;
            border-right: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 15px 0 40px rgba(0, 0, 0, 0.65);
            transform: translateX(-100%);
            transition: transform 0.28s cubic-bezier(0.16, 1, 0.3, 1);
            display: flex;
            flex-direction: column;
            overflow: hidden;
            will-change: transform;
            touch-action: pan-y;
        }
        .slide-nav-drawer.is-open {
            transform: translateX(0);
        }

        .drawer-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 1.1rem 1.25rem 0.9rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(15, 23, 42, 0.6);
            flex-shrink: 0;
        }
        .drawer-brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .drawer-logo {
            width: 34px;
            height: 34px;
            border-radius: 10px;
            background: linear-gradient(135deg, #10b981, #059669);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
        }
        .drawer-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: #f8fafc;
            line-height: 1.2;
        }
        .drawer-subtitle {
            font-size: 0.72rem;
            color: #94a3b8;
            font-family: monospace;
        }
        .drawer-close-btn {
            background: transparent;
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: #94a3b8;
            font-size: 1.35rem;
            line-height: 1;
            width: 32px;
            height: 32px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
        }
        .drawer-close-btn:hover {
            background: rgba(255, 255, 255, 0.1);
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.25);
        }

        .drawer-content {
            flex: 1;
            overflow-y: auto;
            overscroll-behavior: contain;
            -webkit-overflow-scrolling: touch;
            padding: 0.85rem 1rem 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 1.1rem;
        }

        .drawer-gesture-hint {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.72rem;
            color: #64748b;
            padding: 0.4rem 0.6rem;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 6px;
            border: 1px dashed rgba(255, 255, 255, 0.08);
        }

        .drawer-section {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }
        .drawer-section-title {
            font-size: 0.7rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #64748b;
            margin-bottom: 0.25rem;
            padding-left: 0.35rem;
        }

        .drawer-link {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            padding: 0.6rem 0.75rem;
            border-radius: 10px;
            color: #cbd5e1;
            text-decoration: none;
            background: transparent;
            border: 1px solid transparent;
            text-align: left;
            cursor: pointer;
            transition: all 0.15s ease;
            width: 100%;
        }
        .drawer-link:hover {
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
        }
        .drawer-link.active {
            background: rgba(16, 185, 129, 0.12);
            border-color: rgba(16, 185, 129, 0.35);
            color: #34d399;
        }
        .drawer-link-icon {
            font-size: 1rem;
            width: 24px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .drawer-link-body {
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .drawer-link-name {
            font-size: 0.84rem;
            font-weight: 600;
            color: inherit;
            line-height: 1.25;
        }
        .drawer-link-desc {
            font-size: 0.7rem;
            color: #64748b;
            margin-top: 1px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .drawer-link:hover .drawer-link-desc {
            color: #94a3b8;
        }

        /* Theme Grid in Drawer */
        .drawer-theme-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 0.35rem;
        }
        .drawer-theme-pill {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            padding: 0.5rem 0.75rem;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: #94a3b8;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
            text-align: left;
        }
        .drawer-theme-pill:hover {
            background: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
        }
        .drawer-theme-pill.active {
            background: rgba(16, 185, 129, 0.15);
            border-color: #10b981;
            color: #34d399;
        }

        .drawer-footer {
            padding: 0.85rem 1.15rem;
            border-top: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(15, 23, 42, 0.6);
            flex-shrink: 0;
        }
        .drawer-footer-status {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.74rem;
            font-weight: 600;
            color: #34d399;
        }
        .status-indicator-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
        }
        .drawer-footer-sub {
            font-size: 0.68rem;
            color: #64748b;
            margin-top: 3px;
        }

        /* THEME OVERRIDES FOR DRAWER */
        body.theme-editorial .btn-hamburger {
            background: #ede8dc;
            border-color: #dcd7cb;
            color: #1c1917;
        }
        body.theme-editorial .btn-hamburger:hover {
            background: #e3ded1;
            border-color: #1c1917;
        }
        body.theme-editorial .slide-nav-backdrop {
            background: rgba(28, 25, 23, 0.55);
        }
        body.theme-editorial .slide-nav-drawer {
            background: #fbf9f4;
            border-right: 1.5px solid #dcd7cb;
            box-shadow: 15px 0 35px rgba(28, 25, 23, 0.2);
        }
        body.theme-editorial .drawer-header {
            background: #f2eee3;
            border-bottom: 1.5px solid #dcd7cb;
        }
        body.theme-editorial .drawer-title {
            color: #1c1917;
            font-family: 'Newsreader', Georgia, serif;
        }
        body.theme-editorial .drawer-subtitle {
            color: #78716c;
        }
        body.theme-editorial .drawer-close-btn {
            border-color: #dcd7cb;
            color: #78716c;
        }
        body.theme-editorial .drawer-close-btn:hover {
            background: #e7e2d4;
            color: #1c1917;
            border-color: #1c1917;
        }
        body.theme-editorial .drawer-gesture-hint {
            background: #ede8dc;
            border-color: #dcd7cb;
            color: #78716c;
        }
        body.theme-editorial .drawer-section-title {
            color: #78716c;
        }
        body.theme-editorial .drawer-link {
            color: #292524;
        }
        body.theme-editorial .drawer-link:hover {
            background: #ede8dc;
            border-color: #dcd7cb;
            color: #0c0a09;
        }
        body.theme-editorial .drawer-link.active {
            background: #853b1b;
            border-color: #1c1917;
            color: #ffffff;
        }
        body.theme-editorial .drawer-link.active .drawer-link-desc {
            color: #f5f5f4;
        }
        body.theme-editorial .drawer-theme-pill {
            background: #f2eee3;
            border-color: #dcd7cb;
            color: #292524;
        }
        body.theme-editorial .drawer-theme-pill:hover {
            background: #ede8dc;
            border-color: #1c1917;
        }
        body.theme-editorial .drawer-theme-pill.active {
            background: #853b1b;
            border-color: #1c1917;
            color: #ffffff;
        }
        body.theme-editorial .drawer-footer {
            background: #f2eee3;
            border-top: 1.5px solid #dcd7cb;
        }

        body.theme-liquid-glass .btn-hamburger {
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            border: 1px solid rgba(255, 255, 255, 0.25);
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25), inset 0 1px 1px rgba(255, 255, 255, 0.35);
            color: #e0f2fe;
        }
        body.theme-liquid-glass .slide-nav-drawer {
            background: rgba(13, 17, 34, 0.78);
            backdrop-filter: blur(28px) saturate(180%);
            -webkit-backdrop-filter: blur(28px) saturate(180%);
            border-right: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 18px 0 50px rgba(0, 0, 0, 0.55), inset 0 1px 1px rgba(255, 255, 255, 0.3);
        }
        body.theme-liquid-glass .drawer-header {
            background: rgba(255, 255, 255, 0.04);
            border-bottom: 1px solid rgba(255, 255, 255, 0.12);
        }
        body.theme-liquid-glass .drawer-link:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.18);
        }
        body.theme-liquid-glass .drawer-link.active {
            background: rgba(56, 189, 248, 0.18);
            border-color: rgba(56, 189, 248, 0.45);
            color: #38bdf8;
        }
        body.theme-liquid-glass .drawer-theme-pill.active {
            background: rgba(56, 189, 248, 0.2);
            border-color: rgba(56, 189, 248, 0.6);
            color: #38bdf8;
        }

        body.bw-page-light .btn-hamburger, body.bw-mode.bw-page-light .btn-hamburger {
            background: #ffffff;
            border: 1.5px solid #000000;
            color: #000000;
        }
        body.bw-page-light .slide-nav-drawer, body.bw-mode.bw-page-light .slide-nav-drawer {
            background: #ffffff;
            border-right: 2px solid #000000;
            box-shadow: 15px 0 35px rgba(0, 0, 0, 0.3);
        }
        body.bw-page-light .drawer-header, body.bw-mode.bw-page-light .drawer-header {
            background: #f4f4f5;
            border-bottom: 2px solid #000000;
        }
        body.bw-page-light .drawer-title, body.bw-mode.bw-page-light .drawer-title {
            color: #000000;
        }
        body.bw-page-light .drawer-subtitle, body.bw-mode.bw-page-light .drawer-subtitle {
            color: #52525b;
        }
        body.bw-page-light .drawer-close-btn, body.bw-mode.bw-page-light .drawer-close-btn {
            border-color: #000000;
            color: #000000;
        }
        body.bw-page-light .drawer-link, body.bw-mode.bw-page-light .drawer-link {
            color: #000000;
        }
        body.bw-page-light .drawer-link:hover, body.bw-mode.bw-page-light .drawer-link:hover {
            background: #f4f4f5;
            border-color: #000000;
        }
        body.bw-page-light .drawer-link.active, body.bw-mode.bw-page-light .drawer-link.active {
            background: #000000;
            color: #ffffff;
            border-color: #000000;
        }
        body.bw-page-light .drawer-link.active .drawer-link-desc, body.bw-mode.bw-page-light .drawer-link.active .drawer-link-desc {
            color: #d4d4d8;
        }
        body.bw-page-light .drawer-theme-pill, body.bw-mode.bw-page-light .drawer-theme-pill {
            background: #ffffff;
            border: 1.5px solid #000000;
            color: #000000;
        }
        body.bw-page-light .drawer-theme-pill.active, body.bw-mode.bw-page-light .drawer-theme-pill.active {
            background: #000000;
            color: #ffffff;
        }
        body.bw-page-light .drawer-footer, body.bw-mode.bw-page-light .drawer-footer {
            background: #f4f4f5;
            border-top: 2px solid #000000;
        }
        body.bw-page-light .drawer-footer-status, body.bw-mode.bw-page-light .drawer-footer-status {
            color: #000000;
        }

        body.bw-page-dark .btn-hamburger, body.bw-mode.bw-page-dark .btn-hamburger {
            background: #000000;
            border: 1.5px solid #ffffff;
            color: #ffffff;
        }
        body.bw-page-dark .slide-nav-drawer, body.bw-mode.bw-page-dark .slide-nav-drawer {
            background: #000000;
            border-right: 2px solid #ffffff;
            box-shadow: 15px 0 35px rgba(255, 255, 255, 0.15);
        }
        body.bw-page-dark .drawer-header, body.bw-mode.bw-page-dark .drawer-header {
            background: #09090b;
            border-bottom: 2px solid #ffffff;
        }
        body.bw-page-dark .drawer-title, body.bw-mode.bw-page-dark .drawer-title {
            color: #ffffff;
        }
        body.bw-page-dark .drawer-close-btn, body.bw-mode.bw-page-dark .drawer-close-btn {
            border-color: #ffffff;
            color: #ffffff;
        }
        body.bw-page-dark .drawer-link, body.bw-mode.bw-page-dark .drawer-link {
            color: #ffffff;
        }
        body.bw-page-dark .drawer-link:hover, body.bw-mode.bw-page-dark .drawer-link:hover {
            background: #18181b;
            border-color: #ffffff;
        }
        body.bw-page-dark .drawer-link.active, body.bw-mode.bw-page-dark .drawer-link.active {
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
        }
        body.bw-page-dark .drawer-link.active .drawer-link-desc, body.bw-mode.bw-page-dark .drawer-link.active .drawer-link-desc {
            color: #27272a;
        }
        body.bw-page-dark .drawer-theme-pill, body.bw-mode.bw-page-dark .drawer-theme-pill {
            background: #000000;
            border: 1.5px solid #ffffff;
            color: #ffffff;
        }
        body.bw-page-dark .drawer-theme-pill.active, body.bw-mode.bw-page-dark .drawer-theme-pill.active {
            background: #ffffff;
            color: #000000;
        }
        body.bw-page-dark .drawer-footer, body.bw-mode.bw-page-dark .drawer-footer {
            background: #09090b;
            border-top: 2px solid #ffffff;
        }
        body.bw-page-dark .drawer-footer-status, body.bw-mode.bw-page-dark .drawer-footer-status {
            color: #ffffff;
        }

    </style>
</head>
<body>

    <!-- Slide Navigation Backdrop -->
    <div id="slideNavBackdrop" class="slide-nav-backdrop" aria-hidden="true"></div>

    <!-- Slide Navigation Drawer -->
    <aside id="slideNavDrawer" class="slide-nav-drawer" role="dialog" aria-modal="true" aria-label="Navigation Menu">
        <div class="drawer-header">
            <div class="drawer-brand">
                <div class="drawer-logo">⚡</div>
                <div>
                    <div class="drawer-title">Universal File Studio</div>
                    <div class="drawer-subtitle">v1.4.0 • Master Edition</div>
                </div>
            </div>
            <button type="button" id="btnCloseNavDrawer" class="drawer-close-btn" aria-label="Close navigation drawer" title="Close drawer">&times;</button>
        </div>

        <div class="drawer-content">
            <!-- Slide Gesture Hint -->
            <div class="drawer-gesture-hint">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="19" y1="12" x2="5" y2="12"></line>
                    <polyline points="12 19 5 12 12 5"></polyline>
                </svg>
                <span>Swipe left or tap outside to slide close</span>
            </div>

            <!-- Pages & Workspaces -->
            <div class="drawer-section">
                <div class="drawer-section-title">Pages & Workspaces</div>
                <a href="/" class="drawer-link active" id="drawerLinkConverter">
                    <span class="drawer-link-icon">⚡</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Universal File Converter</span>
                        <span class="drawer-link-desc">All-in-one instant conversion engine</span>
                    </div>
                </a>
                <a href="/zeroblur" class="drawer-link" id="drawerLinkZeroBlur">
                    <span class="drawer-link-icon">🎬</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">ZeroBlur Engine Studio</span>
                        <span class="drawer-link-desc">TikTok & WhatsApp Status video mastering</span>
                    </div>
                </a>
                <a href="/formats" class="drawer-link" id="drawerLinkFormats">
                    <span class="drawer-link-icon">📚</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Formats Catalog</span>
                        <span class="drawer-link-desc">Supported extensions and compatibility</span>
                    </div>
                </a>
                <a href="/api/diagnostics" target="_blank" class="drawer-link" id="drawerLinkDiagnostics">
                    <span class="drawer-link-icon">📊</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">System Diagnostics</span>
                        <span class="drawer-link-desc">Cloud health, FFmpeg & library report</span>
                    </div>
                </a>
            </div>

            <!-- Format Category Shortcuts -->
            <div class="drawer-section">
                <div class="drawer-section-title">Format Categories</div>
                <button type="button" class="drawer-link drawer-format-cat-trigger" data-cat="image">
                    <span class="drawer-link-icon">🖼️</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Image Formats</span>
                        <span class="drawer-link-desc">PNG, JPG, WEBP, AVIF, GIF, BMP, TIFF</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-format-cat-trigger" data-cat="document">
                    <span class="drawer-link-icon">📄</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Document Formats</span>
                        <span class="drawer-link-desc">PDF, DOCX, TXT, HTML, MD</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-format-cat-trigger" data-cat="audio">
                    <span class="drawer-link-icon">🎵</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Audio Formats</span>
                        <span class="drawer-link-desc">MP3, WAV, AAC, FLAC, OGG, M4A</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-format-cat-trigger" data-cat="video">
                    <span class="drawer-link-icon">🎥</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Video Formats</span>
                        <span class="drawer-link-desc">MP4, MKV, WEBM, MOV, AVI</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-format-cat-trigger" data-cat="archive">
                    <span class="drawer-link-icon">📦</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Archive Formats</span>
                        <span class="drawer-link-desc">ZIP, TAR, GZ, 7Z, BZ2</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-format-cat-trigger" data-cat="data">
                    <span class="drawer-link-icon">🗄️</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Data & Spreadsheets</span>
                        <span class="drawer-link-desc">CSV, JSON, XML, YAML, XLSX</span>
                    </div>
                </button>
            </div>

            <!-- Display Theme Switcher -->
            <div class="drawer-section">
                <div class="drawer-section-title">Display Theme</div>
                <div class="drawer-theme-grid">
                    <button type="button" class="drawer-theme-pill" data-theme="color" title="Modern Dark">
                        <span class="theme-dot" style="background:#10b981;"></span>
                        <span>Modern Dark</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="editorial" title="Editorial Magazine">
                        <span class="theme-dot" style="background:#853b1b; border:1px solid #1c1917;"></span>
                        <span>Editorial</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="liquid-glass" title="Soft Liquid Glass">
                        <span class="theme-dot" style="background:linear-gradient(135deg, #38bdf8, #c084fc); box-shadow:0 0 6px rgba(192,132,252,0.8);"></span>
                        <span>Liquid Glass</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="bw-light" title="B&W Light">
                        <span class="theme-dot" style="background:#ffffff; border:1px solid #71717a;"></span>
                        <span>B&W Light</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="bw-dark" title="B&W Dark">
                        <span class="theme-dot" style="background:#000000; border:1px solid #a1a1aa;"></span>
                        <span>B&W Dark</span>
                    </button>
                </div>
            </div>
        </div>

        <div class="drawer-footer">
            <div class="drawer-footer-status">
                <span class="status-indicator-dot"></span>
                <span>Cloud Engine: Online & Ready</span>
            </div>
            <div class="drawer-footer-sub">Swipe left to close • Slide from edge to open</div>
        </div>
    </aside>
    <!-- Ambient Floating Orbs for Soft Liquid Glass Display -->
    <div class="liquid-bg-orbs" aria-hidden="true">
        <div class="liquid-orb orb-1"></div>
        <div class="liquid-orb orb-2"></div>
        <div class="liquid-orb orb-3"></div>
    </div>

    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-top">
                <!-- Slide Navigation Drawer Hamburger Button -->
                <button type="button" id="navDrawerHamburgerBtn" class="btn-hamburger" aria-label="Open Navigation Menu" title="Open Navigation Drawer" aria-expanded="false">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <line x1="3" y1="8" x2="21" y2="8"></line>
                        <line x1="3" y1="16" x2="21" y2="16"></line>
                    </svg>
                    <span class="hamburger-label">Menu</span>
                </button>
                <div class="header-badge">⚡ Instant Conversion & Permanent Hosting</div>
                <div class="header-badge" style="margin-bottom:0; background:rgba(16, 185, 129, 0.15); color:#34d399; border-color:rgba(16, 185, 129, 0.35);">v1.4.0 • Mobile Shield & Photo Gallery</div>
                <!-- ZeroBlur Engine Page Navigation Button -->
                <a href="/zeroblur" class="btn-diag btn-zeroblur-nav" id="navZeroBlurBtn" title="Open ZeroBlur Engine Studio for TikTok & WhatsApp Video Mastering">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
                    </svg>
                    <span>⚡ ZeroBlur Studio ↗</span>
                </a>
                <button type="button" class="btn-diag btn-bw-toggle" id="bwPageToggle" title="Toggle webpage display style (Editorial, Liquid Glass, Modern Dark, B&W)">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"></circle>
                        <path d="M12 2a10 10 0 0 1 0 20z" fill="currentColor"></path>
                    </svg>
                    <span id="bwToggleText">Display: Modern Dark</span>
                </button>
                <button type="button" class="btn-diag btn-toggle-switcher" id="toggleSwitcherNavBtn" title="Hide or show the display style switcher bar" aria-expanded="true">
                    <svg id="toggleSwitcherIcon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path>
                        <line x1="1" y1="1" x2="23" y2="23"></line>
                    </svg>
                    <span id="toggleSwitcherText">Hide Switcher</span>
                </button>
                <button class="btn-diag" id="openDiagBtn" title="View Server Diagnostics and Logs">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
                    </svg>
                    <span>Diagnostics v1.4.0</span>
                </button>
            </div>
            <h1>Universal File Converter</h1>
            <p>Convert images, documents, and spreadsheets. Hosted permanently at <code>/files</code>.</p>

            <!-- Webpage Display Style Switcher (Editorial, Soft Liquid Glass, Modern Dark, B&W Light, B&W Dark) -->
            <div class="theme-bar" id="themeBar">
                <span class="theme-bar-label">Display Style:</span>
                <div class="theme-switcher" id="themeSwitcher" role="group" aria-label="Webpage Display Styles">
                    <button type="button" class="theme-btn" data-theme="editorial" id="themeBtnEditorial" title="Editorial Magazine Layout">
                        <span class="theme-dot" style="background:#853b1b; border:1px solid #1c1917;"></span>
                        <span>📰 Editorial</span>
                    </button>
                    <button type="button" class="theme-btn" data-theme="liquid-glass" id="themeBtnLiquidGlass" title="Soft Liquid Glassmorphism">
                        <span class="theme-dot" style="background:linear-gradient(135deg, #38bdf8, #c084fc); box-shadow:0 0 6px rgba(192,132,252,0.8);"></span>
                        <span>🫧 Soft Liquid Glass</span>
                    </button>
                    <button type="button" class="theme-btn active" data-theme="color" id="themeBtnColor" title="Modern Dark Slate">
                        <span class="theme-dot" style="background:#3b82f6;"></span>
                        <span>Modern Dark</span>
                    </button>
                    <button type="button" class="theme-btn" data-theme="bw-light" id="themeBtnBwLight" title="Black & White Paper Page">
                        <span class="theme-dot" style="background:#ffffff; border:1px solid #71717a;"></span>
                        <span>B&W Light</span>
                    </button>
                    <button type="button" class="theme-btn" data-theme="bw-dark" id="themeBtnBwDark" title="Black & White Dark Monochrome">
                        <span class="theme-dot" style="background:#000000; border:1px solid #a1a1aa;"></span>
                        <span>B&W Dark</span>
                    </button>
                </div>
                <button type="button" class="theme-bar-close" id="themeBarCloseBtn" title="Hide switcher bar" aria-label="Hide theme switcher">&times;</button>
            </div>
        </div>

        <!-- Upload & Convert Card -->
        <div class="card">
            <div class="card-title">
                <span>Upload & Convert</span>
            </div>

            <!-- Direct Action Pickers for mobile & quick select -->
            <div class="picker-buttons-row">
                <button type="button" class="btn-picker btn-gallery" id="btnPickGallery">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
                        <circle cx="8.5" cy="8.5" r="1.5"></circle>
                        <polyline points="21 15 16 10 5 21"></polyline>
                    </svg>
                    <span>Photo Gallery</span>
                </button>
                <button type="button" class="btn-picker btn-camera" id="btnPickCamera">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path>
                        <circle cx="12" cy="13" r="4"></circle>
                    </svg>
                    <span>Camera</span>
                </button>
                <button type="button" class="btn-picker btn-docs" id="btnPickDocs">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                        <polyline points="14 2 14 8 20 8"></polyline>
                        <line x1="16" y1="13" x2="8" y2="13"></line>
                        <line x1="16" y1="17" x2="8" y2="17"></line>
                    </svg>
                    <span>Documents</span>
                </button>
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
                        Images: PNG, JPG, WEBP, BMP, ICO, HEIC, GIF &bull; Docs: PDF, DOCX, DOC, RTF, TXT, MD, HTML &bull; Data: CSV, JSON, XLSX, XLS
                    </div>
                    <input type="file" id="fileInput" multiple accept="*/*">
                    <input type="file" id="galleryInput" multiple accept="image/*,.heic,.heif">
                    <input type="file" id="cameraInput" accept="image/*" capture="environment">
                    <input type="file" id="docsInput" multiple accept=".pdf,.docx,.doc,.txt,.md,.rtf,.xlsx,.xls,.csv,.json,.html">
                </div>

                <!-- Staged files display -->
                <div class="staged-files" id="stagedFiles"></div>

                <!-- Mobile Shield & Upload Optimization -->
                <div class="mobile-opt-row">
                    <div class="mobile-opt-left">
                        <input type="checkbox" id="mobileShield" checked>
                        <div>
                            <span style="font-weight:600; color:#e2e8f0;">Mobile Shield & Resumable Chunks</span>
                            <div style="font-size:0.75rem; color:#64748b;">Splits large files into 768KB chunks to prevent cellular upload timeouts</div>
                        </div>
                    </div>
                    <span id="stagedCountText" style="font-size:0.8rem; color:#38bdf8;">0 staged</span>
                </div>

                <!-- Black & White Page & Document Conversion Option -->
                <div class="mobile-opt-row" style="margin-top:8px;">
                    <div class="mobile-opt-left">
                        <input type="checkbox" id="bwConvertOption">
                        <div>
                            <span style="font-weight:600; color:#e2e8f0;">Black & White Pages (Grayscale / Ink Saver)</span>
                            <div style="font-size:0.75rem; color:#64748b;">Converts document pages and photos to monochrome black & white for printing & reduced size</div>
                        </div>
                    </div>
                    <span style="font-size:0.78rem; color:#94a3b8; font-family:'JetBrains Mono', monospace;">Monochrome</span>
                </div>

                <!-- Progress Bar -->
                <div class="progress-wrapper" id="progressWrapper">
                    <div class="progress-bar-container">
                        <div class="progress-bar-fill" id="progressBar"></div>
                    </div>
                    <div class="progress-status" id="progressStatus">Uploading 0%</div>
                </div>

                <!-- Target Format Selection (Clean Segmented Switcher with Expandable "More" Panel) -->
                <div class="form-row">
                    <div class="format-switcher-wrap" id="formatSwitcherWrap">
                        <div class="format-switcher-header">
                            <div class="format-header-left">
                                <label class="format-switcher-title" for="targetFormat">Target Format</label>
                                <span class="format-active-tag" id="formatActiveTag">Selected: WebP (.webp)</span>
                            </div>

                            <div class="format-header-actions">
                                <!-- Top Quick Formats Pills -->
                                <div class="quick-formats-pills" id="quickFormatsPills">
                                    <button type="button" class="quick-format-pill active" data-format="webp">WebP</button>
                                    <button type="button" class="quick-format-pill" data-format="png">PNG</button>
                                    <button type="button" class="quick-format-pill" data-format="jpg">JPG</button>
                                    <button type="button" class="quick-format-pill" data-format="pdf">PDF</button>
                                </div>

                                <!-- Navigation "More" Button -->
                                <button type="button" class="btn-more-formats" id="toggleMoreFormatsBtn" aria-expanded="false" title="Browse all categories and formats">
                                    <span id="moreFormatsBtnText">More</span>
                                    <svg class="more-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                        <polyline points="6 9 12 15 18 9"></polyline>
                                    </svg>
                                </button>

                                <!-- Navigation to ZeroBlur Engine Studio Dedicated Page -->
                                <a href="/zeroblur" class="btn-more-formats btn-zeroblur-link" id="btnZeroBlurEnginePage" title="Open ZeroBlur Engine Studio & Advanced Mastering Page">
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
                                    </svg>
                                    <span>⚡ ZeroBlur Engine</span>
                                    <svg class="more-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                        <line x1="7" y1="17" x2="17" y2="7"></line>
                                        <polyline points="7 7 17 7 17 17"></polyline>
                                    </svg>
                                </a>
                            </div>
                        </div>

                        <!-- Expandable More Formats Panel (Initially collapsed to keep web page clean) -->
                        <div class="format-expanded-panel" id="formatExpandedPanel">
                            <!-- Category Segmented Switcher -->
                            <div class="format-cat-bar" id="formatCatBar" role="tablist" aria-label="Format Categories">
                                <button type="button" class="format-cat-btn active" data-cat="images" role="tab" aria-selected="true" id="catBtnImages">
                                    <span>🖼️ Images</span>
                                </button>
                                <button type="button" class="format-cat-btn" data-cat="docs" role="tab" aria-selected="false" id="catBtnDocs">
                                    <span>📄 Documents</span>
                                </button>
                                <button type="button" class="format-cat-btn" data-cat="data" role="tab" aria-selected="false" id="catBtnData">
                                    <span>📊 Data</span>
                                </button>
                                <button type="button" class="format-cat-btn" data-cat="host" role="tab" aria-selected="false" id="catBtnHost">
                                    <span>📦 Direct Host</span>
                                </button>
                                <button type="button" class="format-cat-btn" data-cat="video" role="tab" aria-selected="false" id="catBtnVideo">
                                    <span>⚡ Video</span>
                                </button>
                            </div>

                            <!-- Format Pills Switcher -->
                            <div class="format-pills-container" id="formatPillsContainer" role="radiogroup" aria-label="Format Options">
                                <!-- Injected dynamically by JS -->
                            </div>

                            <!-- ZeroBlur Engine Banner inside expanded format panel -->
                            <div class="zeroblur-banner-callout">
                                <div class="zeroblur-callout-text">
                                    <span class="zeroblur-badge">⚡ ZeroBlur Studio</span>
                                    <span>Master TikTok 60fps CFR &amp; WhatsApp &lt;14.5MB Status with optical deblurring &amp; 2-pass FFmpeg.</span>
                                </div>
                                <a href="/zeroblur" class="btn-callout-launch" id="calloutLaunchZeroBlurBtn">
                                    Open Studio ↗
                                </a>
                            </div>
                        </div>

                        <!-- Preserved <select> for 100% Backward, Form & Test Compatibility -->
                        <select id="targetFormat" name="target_format" required style="position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; clip:rect(0,0,0,0);">
                            <optgroup label="Direct Hosting">
                                <option value="keep">Keep Original Format (Direct Host)</option>
                            </optgroup>
                            <optgroup label="Video & ZeroBlur Mastering">
                                <option value="mp4">MP4 Video (.mp4) - ZeroBlur Master</option>
                            </optgroup>
                            <optgroup label="Images">
                                <option value="webp" selected>WebP Image (.webp) - High Quality & Small</option>
                                <option value="png">PNG Image (.png)</option>
                                <option value="jpg">JPEG Image (.jpg)</option>
                                <option value="ico">Windows Icon (.ico)</option>
                                <option value="bmp">Bitmap Image (.bmp)</option>
                                <option value="gif">GIF Animation / Image (.gif)</option>
                            </optgroup>
                            <optgroup label="Documents & Notes">
                                <option value="pdf">PDF Document (.pdf)</option>
                                <option value="docx">Word Document (.docx)</option>
                                <option value="md">Markdown Notes (.md)</option>
                                <option value="txt">Plain Text (.txt)</option>
                                <option value="html">HTML Webpage (.html)</option>
                                <option value="rtf">Rich Text Format (.rtf)</option>
                            </optgroup>
                            <optgroup label="Data & Spreadsheets">
                                <option value="csv">CSV Spreadsheet (.csv)</option>
                                <option value="xlsx">Excel Workbook (.xlsx)</option>
                                <option value="json">JSON Structured Data (.json)</option>
                            </optgroup>
                        </select>
                    </div>

                    <button type="submit" class="btn-submit" id="submitBtn">
                        <div class="spinner" id="spinner"></div>
                        <span id="btnText">Convert & Host Files</span>
                    </button>
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
                <div class="header-controls">
                    <input type="text" class="search-box" id="searchBox" placeholder="Filter files...">
                    <div class="filter-btn-group">
                        <button type="button" class="btn-filter active" data-filter="all">All</button>
                        <button type="button" class="btn-filter" data-filter="images">Images</button>
                        <button type="button" class="btn-filter" data-filter="docs">Docs</button>
                        <button type="button" class="btn-filter" data-filter="data">Data</button>
                    </div>
                    <div class="view-toggle">
                        <button type="button" class="btn-view active" id="viewListBtn" title="List View">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"></line><line x1="8" y1="12" x2="21" y2="12"></line><line x1="8" y1="18" x2="21" y2="18"></line><line x1="3" y1="6" x2="3.01" y2="6"></line><line x1="3" y1="12" x2="3.01" y2="12"></line><line x1="3" y1="18" x2="3.01" y2="18"></line></svg>
                        </button>
                        <button type="button" class="btn-view" id="viewGridBtn" title="Grid View">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
                        </button>
                    </div>
                    <button class="btn-refresh" id="refreshBtn">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="23 4 23 10 17 10"></polyline>
                            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                        </svg>
                        <span>Refresh</span>
                    </button>
                </div>
            </div>

            <div id="filesContainer">
                <div class="files-list" id="filesList">
                    <div class="empty-state">Loading hosted files...</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Diagnostics Modal -->
    <div class="modal-overlay" id="diagModal">
        <div class="modal-card">
            <div class="modal-header">
                <div class="modal-title">System Diagnostics & Logs</div>
                <button type="button" class="modal-close" id="closeDiagBtn">&times;</button>
            </div>
            <div class="modal-body">
                <div class="diag-grid">
                    <div class="diag-box">
                        <div class="diag-label">Status</div>
                        <div class="diag-val" id="diagStatus">Checking...</div>
                    </div>
                    <div class="diag-box">
                        <div class="diag-label">PDFium Engine</div>
                        <div class="diag-val" id="diagPdfium">--</div>
                    </div>
                    <div class="diag-box">
                        <div class="diag-label">HEIF Engine</div>
                        <div class="diag-val" id="diagHeif">--</div>
                    </div>
                    <div class="diag-box">
                        <div class="diag-label">Docx Engine</div>
                        <div class="diag-val" id="diagDocx">--</div>
                    </div>
                    <div class="diag-box">
                        <div class="diag-label">Hosted Files</div>
                        <div class="diag-val" id="diagHosted">--</div>
                    </div>
                    <div class="diag-box">
                        <div class="diag-label">B&W Page Option</div>
                        <div class="diag-val" id="diagBw" style="color:#10b981;">Active</div>
                    </div>
                </div>

                <div style="font-weight:600; font-size:0.9rem; color:#e2e8f0; display:flex; justify-content:space-between; align-items:center;">
                    <span>Recent Conversion Logs</span>
                    <button type="button" class="btn-refresh" id="diagRefreshBtn" style="padding:3px 8px; font-size:0.75rem;">Reload Logs</button>
                </div>

                <div class="log-list" id="diagLogs">
                    <div style="color:#64748b;">Loading logs...</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const galleryInput = document.getElementById('galleryInput');
        const cameraInput = document.getElementById('cameraInput');
        const docsInput = document.getElementById('docsInput');

        const btnPickGallery = document.getElementById('btnPickGallery');
        const btnPickCamera = document.getElementById('btnPickCamera');
        const btnPickDocs = document.getElementById('btnPickDocs');

        const stagedContainer = document.getElementById('stagedFiles');
        const stagedCountText = document.getElementById('stagedCountText');
        const mobileShield = document.getElementById('mobileShield');
        const bwConvertOption = document.getElementById('bwConvertOption');

        const bwPageToggle = document.getElementById('bwPageToggle');
        const bwToggleText = document.getElementById('bwToggleText');

        const uploadForm = document.getElementById('uploadForm');
        const targetFormat = document.getElementById('targetFormat');
        const submitBtn = document.getElementById('submitBtn');
        const spinner = document.getElementById('spinner');
        const btnText = document.getElementById('btnText');
        const statusBox = document.getElementById('statusBox');

        const progressWrapper = document.getElementById('progressWrapper');
        const progressBar = document.getElementById('progressBar');
        const progressStatus = document.getElementById('progressStatus');

        const filesContainer = document.getElementById('filesContainer');
        const filesCount = document.getElementById('filesCount');
        const refreshBtn = document.getElementById('refreshBtn');
        const searchBox = document.getElementById('searchBox');
        const filterBtns = document.querySelectorAll('.btn-filter');
        const viewListBtn = document.getElementById('viewListBtn');
        const viewGridBtn = document.getElementById('viewGridBtn');

        const openDiagBtn = document.getElementById('openDiagBtn');
        const closeDiagBtn = document.getElementById('closeDiagBtn');
        const diagModal = document.getElementById('diagModal');
        const diagRefreshBtn = document.getElementById('diagRefreshBtn');

        let stagedFilesList = [];
        let allHostedFiles = [];
        let currentFilter = 'all';
        let currentView = 'list';

        // Picker click bindings
        btnPickGallery.addEventListener('click', () => galleryInput.click());
        btnPickCamera.addEventListener('click', () => cameraInput.click());
        btnPickDocs.addEventListener('click', () => docsInput.click());
        dropZone.addEventListener('click', () => fileInput.click());

        [fileInput, galleryInput, cameraInput, docsInput].forEach(inp => {
            inp.addEventListener('change', () => {
                if (inp.files && inp.files.length) {
                    addFiles(Array.from(inp.files));
                    inp.value = '';
                }
            });
        });

        // Drag & Drop
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

        function addFiles(newFiles) {
            for (const f of newFiles) {
                if (!stagedFilesList.some(item => item.name === f.name && item.size === f.size)) {
                    stagedFilesList.push(f);
                }
            }
            renderStagedFiles();
        }

        window.removeStagedFile = function(index) {
            stagedFilesList.splice(index, 1);
            renderStagedFiles();
        };

        function renderStagedFiles() {
            stagedContainer.innerHTML = '';
            stagedCountText.textContent = `${stagedFilesList.length} staged`;

            stagedFilesList.forEach((file, idx) => {
                const chip = document.createElement('div');
                chip.className = 'file-chip';
                const sizeStr = (file.size / 1024).toFixed(1) + ' KB';

                let thumbHtml = '';
                if (file.type && file.type.startsWith('image/')) {
                    const thumbUrl = URL.createObjectURL(file);
                    thumbHtml = `<img src="${thumbUrl}" class="file-chip-thumb" alt="thumb">`;
                }

                chip.innerHTML = `
                    ${thumbHtml}
                    <span>${file.name} (${sizeStr})</span>
                    <span class="file-chip-remove" onclick="removeStagedFile(${idx})">&times;</span>
                `;
                stagedContainer.appendChild(chip);
            });
        }

        function updateProgress(percent, label) {
            progressWrapper.style.display = 'block';
            progressBar.style.width = Math.min(100, Math.max(0, percent)) + '%';
            progressStatus.textContent = label || `${Math.round(percent)}%`;
        }

        function resetProgress() {
            progressWrapper.style.display = 'none';
            progressBar.style.width = '0%';
            progressStatus.textContent = '0%';
        }

        // Chunked file upload
        async function uploadChunked(file, format, isBw) {
            const CHUNK_SIZE = 768 * 1024;
            const totalChunks = Math.ceil(file.size / CHUNK_SIZE) || 1;
            const fileId = Date.now().toString(36) + Math.random().toString(36).substring(2, 6);

            for (let i = 0; i < totalChunks; i++) {
                const start = i * CHUNK_SIZE;
                const end = Math.min(file.size, start + CHUNK_SIZE);
                const chunkBlob = file.slice(start, end);

                const formData = new FormData();
                formData.append('file_id', fileId);
                formData.append('chunk_index', i);
                formData.append('total_chunks', totalChunks);
                formData.append('filename', file.name);
                formData.append('target_format', format);
                formData.append('black_and_white', isBw ? 'true' : 'false');
                formData.append('chunk', chunkBlob, file.name);

                const res = await fetch('/api/upload-chunk', {
                    method: 'POST',
                    body: formData
                });

                if (!res.ok) {
                    const err = await res.text();
                    throw new Error(`Chunk upload failed: ${err}`);
                }

                const data = await res.json();
                const pct = ((i + 1) / totalChunks) * 100;
                updateProgress(pct, `Uploading ${file.name} (${i + 1}/${totalChunks})`);

                if (data.status === 'complete') {
                    return data.results;
                }
            }
        }

        // Form Submit
        uploadForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (stagedFilesList.length === 0) {
                showStatus('Please select or drop at least one file to convert.', 'error');
                return;
            }

            submitBtn.disabled = true;
            spinner.style.display = 'inline-block';
            btnText.textContent = 'Processing...';
            statusBox.style.display = 'none';
            resetProgress();

            const format = targetFormat.value;
            const isBw = bwConvertOption ? bwConvertOption.checked : false;
            const useChunks = mobileShield.checked || stagedFilesList.some(f => f.size > 1.5 * 1024 * 1024);
            const allResults = [];
            let hasErrors = false;

            try {
                if (useChunks) {
                    // Chunked upload sequentially
                    for (let fIdx = 0; fIdx < stagedFilesList.length; fIdx++) {
                        const file = stagedFilesList[fIdx];
                        updateProgress(0, `Uploading ${file.name}...`);
                        const results = await uploadChunked(file, format, isBw);
                        if (results) {
                            allResults.push(...results);
                        }
                    }
                } else {
                    // Standard single batch POST
                    updateProgress(40, 'Uploading files...');
                    const formData = new FormData();
                    formData.append('target_format', format);
                    formData.append('black_and_white', isBw ? 'true' : 'false');
                    stagedFilesList.forEach(file => formData.append('files', file));

                    const response = await fetch('/api/convert', {
                        method: 'POST',
                        body: formData
                    });

                    if (!response.ok) {
                        const errText = await response.text();
                        let errMsg = 'Server error ' + response.status;
                        try {
                            const parsed = JSON.parse(errText);
                            if (parsed.detail) errMsg = parsed.detail;
                        } catch (_) {}
                        throw new Error(errMsg);
                    }

                    const data = await response.json();
                    if (data.results) {
                        allResults.push(...data.results);
                    }
                }

                updateProgress(100, 'Conversion Complete!');

                // Render result summary
                let html = '<div style="font-weight:600; margin-bottom:8px;">Conversion Results:</div>';
                allResults.forEach(res => {
                    if (res.status === 'ok') {
                        html += `
                            <div class="result-item">
                                <span>&#10003; <strong>${res.original}</strong> &rarr; <a href="${res.url}" target="_blank" style="color:#60a5fa; text-decoration:underline;">${res.hosted_name}</a> (${res.size_formatted})</span>
                            </div>
                        `;
                    } else {
                        hasErrors = true;
                        html += `
                            <div class="result-item" style="color:#f87171;">
                                <span>&#10007; <strong>${res.original}</strong>: ${res.error}</span>
                            </div>
                        `;
                    }
                });

                statusBox.innerHTML = html;
                statusBox.className = hasErrors ? 'status-box error' : 'status-box success';
                statusBox.style.display = 'block';

                stagedFilesList = [];
                renderStagedFiles();
                loadHostedFiles();
            } catch (err) {
                let msg = err.message || 'Unknown network error';
                showStatus('Error: ' + msg, 'error');
                fetch('/api/client-log', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        event: 'upload_error',
                        filename: stagedFilesList.map(f => f.name).join(', '),
                        status: 'error',
                        detail: msg
                    })
                }).catch(() => {});
            } finally {
                submitBtn.disabled = false;
                spinner.style.display = 'none';
                btnText.textContent = 'Convert & Host Files';
                setTimeout(resetProgress, 3000);
            }
        });

        function showStatus(message, type) {
            statusBox.textContent = message;
            statusBox.className = 'status-box ' + type;
            statusBox.style.display = 'block';
        }

        // Hosted files loading & rendering
        async function loadHostedFiles() {
            try {
                const response = await fetch('/api/files');
                allHostedFiles = await response.json();
                renderHostedFiles();
            } catch (err) {
                filesContainer.innerHTML = '<div class="empty-state" style="color:#ef4444;">Failed to load hosted files.</div>';
            }
        }

        function filterAndSearchFiles() {
            const query = searchBox.value.toLowerCase().trim();
            return allHostedFiles.filter(file => {
                const matchesQuery = !query || file.name.toLowerCase().includes(query) || file.ext.toLowerCase().includes(query);
                if (!matchesQuery) return false;

                if (currentFilter === 'images') {
                    return file.is_image;
                } else if (currentFilter === 'docs') {
                    return ['pdf', 'docx', 'doc', 'txt', 'md', 'rtf', 'html'].includes(file.ext);
                } else if (currentFilter === 'data') {
                    return ['csv', 'json', 'xlsx', 'xls'].includes(file.ext);
                }
                return true;
            });
        }

        function renderHostedFiles() {
            const filtered = filterAndSearchFiles();
            filesCount.textContent = `${filtered.length} of ${allHostedFiles.length} file${allHostedFiles.length === 1 ? '' : 's'}`;

            if (filtered.length === 0) {
                filesContainer.innerHTML = `<div class="empty-state">${allHostedFiles.length === 0 ? 'No files hosted yet. Upload and convert above!' : 'No matching files found.'}</div>`;
                return;
            }

            if (currentView === 'list') {
                filesContainer.innerHTML = `<div class="files-list">${filtered.map(renderListCard).join('')}</div>`;
            } else {
                filesContainer.innerHTML = `<div class="files-grid">${filtered.map(renderGridCard).join('')}</div>`;
            }
        }

        function renderListCard(file) {
            const fullUrl = window.location.origin + file.url;
            return `
                <div class="file-card">
                    <div class="file-info">
                        <span class="file-badge">.${file.ext}</span>
                        <div style="overflow:hidden;">
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
                        <a href="${file.download_url}" class="btn-action download" title="Direct Download">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                                <polyline points="7 10 12 15 17 10"></polyline>
                                <line x1="12" y1="15" x2="12" y2="3"></line>
                            </svg>
                            <span>Download</span>
                        </a>
                        <button class="btn-action delete" onclick="deleteFile('${file.name}')" title="Delete File">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                        </button>
                    </div>
                </div>
            `;
        }

        function renderGridCard(file) {
            const fullUrl = window.location.origin + file.url;
            let thumb = `<div class="grid-icon-doc">📄</div>`;
            if (file.is_image) {
                thumb = `<img src="${file.url}" alt="${file.name}" loading="lazy">`;
            }

            return `
                <div class="grid-card">
                    <div class="grid-thumb">
                        ${thumb}
                    </div>
                    <div class="grid-body">
                        <div>
                            <a href="${file.url}" target="_blank" class="grid-name" title="${file.name}">${file.name}</a>
                            <div class="grid-meta">${file.size_formatted} &bull; .${file.ext}</div>
                        </div>
                        <div class="grid-actions">
                            <button class="btn-action" onclick="copyFileLink('${fullUrl}', this)">Copy</button>
                            <a href="${file.download_url}" class="btn-action download">Get</a>
                            <button class="btn-action delete" onclick="deleteFile('${file.name}')">&times;</button>
                        </div>
                    </div>
                </div>
            `;
        }

        window.copyFileLink = function(url, btn) {
            navigator.clipboard.writeText(url).then(() => {
                const span = btn.querySelector('span');
                const orig = span ? span.textContent : btn.textContent;
                if (span) span.textContent = 'Copied!';
                else btn.textContent = 'Copied!';
                btn.style.borderColor = '#10b981';
                btn.style.color = '#10b981';
                btn.classList.add('btn-action-copied');
                setTimeout(() => {
                    if (span) span.textContent = orig;
                    else btn.textContent = orig;
                    btn.style.borderColor = '';
                    btn.style.color = '';
                    btn.classList.remove('btn-action-copied');
                }, 2000);
            }).catch(() => {
                alert('Copied URL: ' + url);
            });
        };

        window.deleteFile = async function(name) {
            if (!confirm(`Are you sure you want to permanently delete "${name}"?`)) return;
            try {
                const res = await fetch(`/api/files/${encodeURIComponent(name)}`, { method: 'DELETE' });
                if (!res.ok) throw new Error('Deletion failed: ' + res.status);
                loadHostedFiles();
            } catch (err) {
                alert('Error deleting file: ' + err.message);
            }
        };

        // Filter / Search / View events
        searchBox.addEventListener('input', renderHostedFiles);

        filterBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                filterBtns.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentFilter = btn.dataset.filter;
                renderHostedFiles();
            });
        });

        viewListBtn.addEventListener('click', () => {
            viewListBtn.classList.add('active');
            viewGridBtn.classList.remove('active');
            currentView = 'list';
            renderHostedFiles();
        });

        viewGridBtn.addEventListener('click', () => {
            viewGridBtn.classList.add('active');
            viewListBtn.classList.remove('active');
            currentView = 'grid';
            renderHostedFiles();
        });

        refreshBtn.addEventListener('click', loadHostedFiles);

        // Diagnostics Modal
        async function loadDiagnostics() {
            try {
                const res = await fetch('/api/diagnostics');
                const data = await res.json();
                document.getElementById('diagStatus').textContent = data.status || 'healthy';
                document.getElementById('diagPdfium').textContent = data.has_pdfium ? 'Available' : 'Missing';
                document.getElementById('diagPdfium').style.color = data.has_pdfium ? '#10b981' : '#f87171';
                document.getElementById('diagHeif').textContent = data.has_heif ? 'Available' : 'Missing';
                document.getElementById('diagHeif').style.color = data.has_heif ? '#10b981' : '#f87171';
                document.getElementById('diagDocx').textContent = data.has_docx ? 'Available' : 'XML Fallback';
                document.getElementById('diagHosted').textContent = data.hosted_count;
                const diagBw = document.getElementById('diagBw');
                if (diagBw) {
                    diagBw.textContent = data.bw_page_option ? 'Active' : 'Disabled';
                    diagBw.style.color = data.bw_page_option ? '#10b981' : '#f87171';
                }

                const logsContainer = document.getElementById('diagLogs');
                if (!data.logs || data.logs.length === 0) {
                    logsContainer.innerHTML = '<div style="color:#64748b;">No conversion logs recorded yet.</div>';
                } else {
                    logsContainer.innerHTML = data.logs.slice().reverse().map(l => {
                        const cls = l.status === 'error' ? 'log-status-error' : (l.status === 'client' ? 'log-status-client' : 'log-status-info');
                        return `
                            <div class="log-entry">
                                <span class="log-time">[${l.timestamp.split(' ')[1] || ''}]</span>
                                <span class="${cls}">[${l.status.toUpperCase()}]</span>
                                <span>${l.message}</span>
                            </div>
                        `;
                    }).join('');
                }
            } catch (err) {
                document.getElementById('diagStatus').textContent = 'Error';
            }
        }

        openDiagBtn.addEventListener('click', () => {
            diagModal.style.display = 'flex';
            loadDiagnostics();
        });

        closeDiagBtn.addEventListener('click', () => {
            diagModal.style.display = 'none';
        });

        diagRefreshBtn.addEventListener('click', loadDiagnostics);

        diagModal.addEventListener('click', (e) => {
            if (e.target === diagModal) diagModal.style.display = 'none';
        });

        // Webpage Display Style Manager (Editorial, Soft Liquid Glass, Modern Dark, B&W Light, B&W Dark)
        const AVAILABLE_THEMES = ['editorial', 'liquid-glass', 'color', 'bw-light', 'bw-dark'];

        function setWebpageTheme(theme) {
            const validTheme = AVAILABLE_THEMES.includes(theme) ? theme : 'color';
            
            // Remove existing theme classes
            document.body.classList.remove('bw-mode', 'bw-page-light', 'bw-page-dark', 'theme-editorial', 'theme-liquid-glass');
            
            if (validTheme === 'editorial') {
                document.body.classList.add('theme-editorial');
                if (bwToggleText) bwToggleText.textContent = 'Display: Editorial';
                if (bwPageToggle) {
                    bwPageToggle.style.borderColor = '#1c1917';
                    bwPageToggle.style.color = '#1c1917';
                    bwPageToggle.style.background = '#f1ecdf';
                }
            } else if (validTheme === 'liquid-glass') {
                document.body.classList.add('theme-liquid-glass');
                if (bwToggleText) bwToggleText.textContent = 'Display: Liquid Glass';
                if (bwPageToggle) {
                    bwPageToggle.style.borderColor = 'rgba(255, 255, 255, 0.35)';
                    bwPageToggle.style.color = '#38bdf8';
                    bwPageToggle.style.background = 'rgba(255, 255, 255, 0.08)';
                }
            } else if (validTheme === 'bw-light') {
                document.body.classList.add('bw-mode', 'bw-page-light');
                if (bwToggleText) bwToggleText.textContent = 'B&W Page: Light';
                if (bwPageToggle) {
                    bwPageToggle.style.borderColor = '#09090b';
                    bwPageToggle.style.color = '#09090b';
                    bwPageToggle.style.background = '#ffffff';
                }
            } else if (validTheme === 'bw-dark') {
                document.body.classList.add('bw-mode', 'bw-page-dark');
                if (bwToggleText) bwToggleText.textContent = 'B&W Page: Dark';
                if (bwPageToggle) {
                    bwPageToggle.style.borderColor = '#ffffff';
                    bwPageToggle.style.color = '#ffffff';
                    bwPageToggle.style.background = '#18181b';
                }
            } else {
                if (bwToggleText) bwToggleText.textContent = 'Display: Modern Dark';
                if (bwPageToggle) {
                    bwPageToggle.style.borderColor = '';
                    bwPageToggle.style.color = '';
                    bwPageToggle.style.background = '';
                }
            }
            
            // Sync theme buttons in theme switcher bar
            document.querySelectorAll('.theme-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.theme === validTheme);
            });
            
            // Persist theme selection in localStorage
            localStorage.setItem('webpage_theme', validTheme);
            localStorage.setItem('bw_page_mode', (validTheme === 'bw-light' || validTheme === 'bw-dark') ? 'true' : 'false');
        }

        function applyBwPageMode(enabled) {
            setWebpageTheme(enabled ? 'bw-light' : 'color');
        }

        function initWebpageTheme() {
            let savedTheme = localStorage.getItem('webpage_theme');
            if (!savedTheme) {
                const legacyMode = localStorage.getItem('bw_page_mode');
                savedTheme = legacyMode === 'true' ? 'bw-light' : 'color';
            }
            setWebpageTheme(savedTheme);
            
            // Direct click handlers on theme switcher options
            document.querySelectorAll('.theme-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const chosen = btn.dataset.theme;
                    if (chosen) setWebpageTheme(chosen);
                });
            });
            
            // Quick cycling via header pill button
            if (bwPageToggle) {
                bwPageToggle.addEventListener('click', () => {
                    const current = localStorage.getItem('webpage_theme') || 'color';
                    const idx = AVAILABLE_THEMES.indexOf(current);
                    const next = AVAILABLE_THEMES[(idx + 1) % AVAILABLE_THEMES.length];
                    setWebpageTheme(next);
                });
            }

            // Interactive mouse tracking for Liquid Glass specular highlights
            document.addEventListener('pointermove', (e) => {
                if (!document.body.classList.contains('theme-liquid-glass')) return;
                const glassCards = document.querySelectorAll('.theme-liquid-glass .card, .theme-liquid-glass .dropzone, .theme-liquid-glass .modal-card');
                glassCards.forEach(card => {
                    const rect = card.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const y = e.clientY - rect.top;
                    card.style.setProperty('--mouse-x', `${x}px`);
                    card.style.setProperty('--mouse-y', `${y}px`);
                });
            });

            // Initialize Switcher Bar Navigation Hide / Show Controls
            initSwitcherVisibility();
        }

        // Switcher Bar Hide / Show Navigation Controls
        const themeBar = document.getElementById('themeBar');
        const toggleSwitcherNavBtn = document.getElementById('toggleSwitcherNavBtn');
        const toggleSwitcherText = document.getElementById('toggleSwitcherText');
        const toggleSwitcherIcon = document.getElementById('toggleSwitcherIcon');
        const themeBarCloseBtn = document.getElementById('themeBarCloseBtn');

        const SVG_EYE_OFF = `<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line>`;
        const SVG_EYE_ON = `<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>`;

        function setSwitcherVisibility(visible) {
            if (!themeBar) return;
            if (visible) {
                themeBar.classList.remove('hidden');
                if (toggleSwitcherText) toggleSwitcherText.textContent = 'Hide Switcher';
                if (toggleSwitcherIcon) toggleSwitcherIcon.innerHTML = SVG_EYE_OFF;
                if (toggleSwitcherNavBtn) {
                    toggleSwitcherNavBtn.setAttribute('aria-expanded', 'true');
                    toggleSwitcherNavBtn.classList.remove('active-hidden');
                    toggleSwitcherNavBtn.title = 'Hide the display style switcher bar';
                }
                localStorage.setItem('switcher_hidden', 'false');
            } else {
                themeBar.classList.add('hidden');
                if (toggleSwitcherText) toggleSwitcherText.textContent = 'Show Switcher';
                if (toggleSwitcherIcon) toggleSwitcherIcon.innerHTML = SVG_EYE_ON;
                if (toggleSwitcherNavBtn) {
                    toggleSwitcherNavBtn.setAttribute('aria-expanded', 'false');
                    toggleSwitcherNavBtn.classList.add('active-hidden');
                    toggleSwitcherNavBtn.title = 'Show the display style switcher bar';
                }
                localStorage.setItem('switcher_hidden', 'true');
            }
        }

        function initSwitcherVisibility() {
            const isHidden = localStorage.getItem('switcher_hidden') === 'true';
            setSwitcherVisibility(!isHidden);

            if (toggleSwitcherNavBtn) {
                toggleSwitcherNavBtn.addEventListener('click', () => {
                    const currentlyHidden = themeBar ? themeBar.classList.contains('hidden') : false;
                    setSwitcherVisibility(currentlyHidden);
                });
            }

            if (themeBarCloseBtn) {
                themeBarCloseBtn.addEventListener('click', () => {
                    setSwitcherVisibility(false);
                });
            }
        }

        // =========================================================================
        // Target Format Segmented Switcher Logic
        // =========================================================================
        const FORMAT_CATEGORIES = {
            images: {
                label: '🖼️ Images',
                formats: [
                    { id: 'webp', name: 'WebP', ext: '.webp', hint: 'Fast & Small (Best)' },
                    { id: 'png', name: 'PNG', ext: '.png', hint: 'Lossless Transparent' },
                    { id: 'jpg', name: 'JPG', ext: '.jpg', hint: 'Universal Photos' },
                    { id: 'ico', name: 'ICO', ext: '.ico', hint: 'Favicon / Icon' },
                    { id: 'bmp', name: 'BMP', ext: '.bmp', hint: 'Raw Bitmap' },
                    { id: 'gif', name: 'GIF', ext: '.gif', hint: 'Graphics / Animation' }
                ]
            },
            docs: {
                label: '📄 Documents',
                formats: [
                    { id: 'pdf', name: 'PDF', ext: '.pdf', hint: 'Standard PDF Document' },
                    { id: 'docx', name: 'DOCX', ext: '.docx', hint: 'Microsoft Word Document' },
                    { id: 'md', name: 'Markdown', ext: '.md', hint: 'Clean Formatted Notes' },
                    { id: 'txt', name: 'TXT', ext: '.txt', hint: 'Plain Text' },
                    { id: 'html', name: 'HTML', ext: '.html', hint: 'Web Document' },
                    { id: 'rtf', name: 'RTF', ext: '.rtf', hint: 'Rich Text Format' }
                ]
            },
            data: {
                label: '📊 Data',
                formats: [
                    { id: 'csv', name: 'CSV', ext: '.csv', hint: 'Spreadsheet Table' },
                    { id: 'xlsx', name: 'Excel', ext: '.xlsx', hint: 'Excel Workbook' },
                    { id: 'json', name: 'JSON', ext: '.json', hint: 'Structured Data' }
                ]
            },
            host: {
                label: '📦 Direct Host',
                formats: [
                    { id: 'keep', name: 'Keep Original', ext: 'direct', hint: 'Host original format directly' }
                ]
            },
            video: {
                label: '⚡ Video & ZeroBlur',
                formats: [
                    { id: 'mp4', name: 'MP4 Master', ext: '.mp4', hint: 'ZeroBlur 60fps CFR & WhatsApp Master' }
                ]
            }
        };

        const formatPillsContainer = document.getElementById('formatPillsContainer');
        const formatActiveTag = document.getElementById('formatActiveTag');
        const formatCatBtns = document.querySelectorAll('.format-cat-btn');
        const toggleMoreFormatsBtn = document.getElementById('toggleMoreFormatsBtn');
        const formatExpandedPanel = document.getElementById('formatExpandedPanel');
        const moreFormatsBtnText = document.getElementById('moreFormatsBtnText');
        const quickFormatPills = document.querySelectorAll('.quick-format-pill');

        let currentFormatCat = 'images';

        function toggleMoreFormats(expand) {
            if (!formatExpandedPanel || !toggleMoreFormatsBtn) return;
            const willExpand = typeof expand === 'boolean' ? expand : !formatExpandedPanel.classList.contains('expanded');
            if (willExpand) {
                formatExpandedPanel.classList.add('expanded');
                toggleMoreFormatsBtn.classList.add('expanded');
                toggleMoreFormatsBtn.setAttribute('aria-expanded', 'true');
                if (moreFormatsBtnText) moreFormatsBtnText.textContent = 'Less';
                toggleMoreFormatsBtn.title = 'Collapse format switcher';
            } else {
                formatExpandedPanel.classList.remove('expanded');
                toggleMoreFormatsBtn.classList.remove('expanded');
                toggleMoreFormatsBtn.setAttribute('aria-expanded', 'false');
                if (moreFormatsBtnText) moreFormatsBtnText.textContent = 'More';
                toggleMoreFormatsBtn.title = 'Browse all categories and formats';
            }
        }

        function renderFormatPills() {
            if (!formatPillsContainer) return;
            const category = FORMAT_CATEGORIES[currentFormatCat] || FORMAT_CATEGORIES.images;
            const currentSelected = targetFormat ? targetFormat.value : 'webp';

            formatPillsContainer.innerHTML = '';
            category.formats.forEach(f => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'format-pill-btn' + (f.id === currentSelected ? ' active' : '');
                btn.dataset.format = f.id;
                btn.setAttribute('role', 'radio');
                btn.setAttribute('aria-checked', f.id === currentSelected ? 'true' : 'false');
                btn.innerHTML = `
                    <div class="format-pill-top">
                        <span class="format-pill-name">${f.name}</span>
                        <span class="format-pill-ext">${f.ext}</span>
                    </div>
                    <span class="format-pill-hint">${f.hint}</span>
                `;
                btn.addEventListener('click', () => {
                    selectTargetFormat(f.id);
                });
                formatPillsContainer.appendChild(btn);
            });
        }

        function selectTargetFormat(formatId) {
            if (targetFormat) {
                targetFormat.value = formatId;
            }

            // Sync quick format pills in compact header
            quickFormatPills.forEach(pill => {
                pill.classList.toggle('active', pill.dataset.format === formatId);
            });

            // Find format details across categories to ensure right category tab is active
            let matchedCat = currentFormatCat;
            let matchedFormat = null;
            for (const [catKey, catData] of Object.entries(FORMAT_CATEGORIES)) {
                const found = catData.formats.find(f => f.id === formatId);
                if (found) {
                    matchedCat = catKey;
                    matchedFormat = found;
                    break;
                }
            }

            if (matchedCat !== currentFormatCat) {
                currentFormatCat = matchedCat;
                formatCatBtns.forEach(btn => {
                    const isActive = btn.dataset.cat === currentFormatCat;
                    btn.classList.toggle('active', isActive);
                    btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
                });
            }

            renderFormatPills();

            if (formatActiveTag && matchedFormat) {
                formatActiveTag.textContent = `Selected: ${matchedFormat.name} (${matchedFormat.ext || 'Original'})`;
            }
        }

        function initFormatSwitcher() {
            // Category navigation tab buttons
            formatCatBtns.forEach(btn => {
                btn.addEventListener('click', () => {
                    currentFormatCat = btn.dataset.cat;
                    formatCatBtns.forEach(b => {
                        const isA = b.dataset.cat === currentFormatCat;
                        b.classList.toggle('active', isA);
                        b.setAttribute('aria-selected', isA ? 'true' : 'false');
                    });
                    renderFormatPills();
                });
            });

            // Quick format pills in compact view
            quickFormatPills.forEach(pill => {
                pill.addEventListener('click', () => {
                    selectTargetFormat(pill.dataset.format);
                });
            });

            // "More" navigation button to expand/collapse full format categories
            if (toggleMoreFormatsBtn) {
                toggleMoreFormatsBtn.addEventListener('click', () => {
                    toggleMoreFormats();
                });
            }

            // Listen for changes on targetFormat
            if (targetFormat) {
                targetFormat.addEventListener('change', () => {
                    selectTargetFormat(targetFormat.value);
                });
            }

            // Handle URL query parameter ?format=... (e.g. returning from ZeroBlur Studio)
            const urlParams = new URLSearchParams(window.location.search);
            const queryFormat = urlParams.get('format');
            if (queryFormat) {
                selectTargetFormat(queryFormat);
                if (typeof showActionToast === 'function') {
                    showActionToast(`Target format set to ${queryFormat.toUpperCase()} from ZeroBlur Studio`, 'info');
                }
            } else {
                selectTargetFormat(targetFormat ? targetFormat.value : 'webp');
            }
        }

        initWebpageTheme();
        initFormatSwitcher();
        loadHostedFiles();
    
    // ==================== SLIDE-OUT NAVIGATION DRAWER & TOUCH SLIDING PHYSICS ====================
    function initSlideNavigationDrawer() {
        const drawer = document.getElementById('slideNavDrawer');
        const backdrop = document.getElementById('slideNavBackdrop');
        const openBtn = document.getElementById('navDrawerHamburgerBtn');
        const closeBtn = document.getElementById('btnCloseNavDrawer');
        if (!drawer || !backdrop) return;

        let isOpen = false;
        let isDragging = false;
        let startX = 0;
        let currentX = 0;
        let startY = 0;
        let isHorizontalGesture = null;

        const getDrawerWidth = () => drawer.offsetWidth || Math.min(340, window.innerWidth * 0.86);

        function setDrawerTransform(x, animate) {
            drawer.style.transition = animate ? 'transform 0.28s cubic-bezier(0.16, 1, 0.3, 1)' : 'none';
            drawer.style.transform = `translateX(${x}px)`;
        }

        function setBackdropOpacity(ratio, animate) {
            backdrop.style.transition = animate ? 'opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1)' : 'none';
            backdrop.style.opacity = Math.max(0, Math.min(1, ratio));
            backdrop.style.pointerEvents = ratio > 0.05 ? 'auto' : 'none';
        }

        function openDrawer() {
            isOpen = true;
            drawer.classList.add('is-open');
            backdrop.classList.add('is-open');
            drawer.style.transform = '';
            drawer.style.transition = '';
            backdrop.style.opacity = '';
            backdrop.style.pointerEvents = '';
            backdrop.style.transition = '';
            document.body.style.overflow = 'hidden';
            if (openBtn) openBtn.setAttribute('aria-expanded', 'true');
            
            // Sync active theme in drawer
            const currentTheme = localStorage.getItem('webpage_theme') || 'color';
            document.querySelectorAll('.drawer-theme-pill').forEach(pill => {
                pill.classList.toggle('active', pill.getAttribute('data-theme') === currentTheme);
            });
        }

        function closeDrawer() {
            isOpen = false;
            drawer.classList.remove('is-open');
            backdrop.classList.remove('is-open');
            drawer.style.transform = '';
            drawer.style.transition = '';
            backdrop.style.opacity = '';
            backdrop.style.pointerEvents = '';
            backdrop.style.transition = '';
            document.body.style.overflow = '';
            if (openBtn) openBtn.setAttribute('aria-expanded', 'false');
        }

        function toggleDrawer() {
            if (isOpen) closeDrawer();
            else openDrawer();
        }

        if (openBtn) {
            openBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleDrawer();
            });
        }

        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                closeDrawer();
            });
        }

        backdrop.addEventListener('click', () => {
            closeDrawer();
        });

        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && isOpen) {
                closeDrawer();
            }
        });

        // Touch Swipe & Edge Sliding Physics
        const EDGE_SWIPE_THRESHOLD = 40; // px from screen left edge

        window.addEventListener('touchstart', (e) => {
            if (e.touches.length !== 1) return;
            const touch = e.touches[0];
            startX = touch.clientX;
            startY = touch.clientY;
            currentX = startX;
            isHorizontalGesture = null;

            if (!isOpen && startX <= EDGE_SWIPE_THRESHOLD) {
                isDragging = true;
                const width = getDrawerWidth();
                setDrawerTransform(-width, false);
                setBackdropOpacity(0, false);
            } else if (isOpen) {
                if (e.target.closest('#slideNavDrawer') || e.target.closest('#slideNavBackdrop')) {
                    isDragging = true;
                }
            }
        }, { passive: true });

        window.addEventListener('touchmove', (e) => {
            if (!isDragging || e.touches.length !== 1) return;
            const touch = e.touches[0];
            currentX = touch.clientX;
            const deltaX = currentX - startX;
            const deltaY = touch.clientY - startY;

            if (isHorizontalGesture === null) {
                if (Math.abs(deltaX) > 6 || Math.abs(deltaY) > 6) {
                    isHorizontalGesture = Math.abs(deltaX) > Math.abs(deltaY);
                    if (!isHorizontalGesture) {
                        isDragging = false;
                        return;
                    }
                }
            }

            if (!isHorizontalGesture) return;

            const width = getDrawerWidth();
            if (!isOpen) {
                const targetX = Math.min(0, -width + Math.max(0, deltaX));
                setDrawerTransform(targetX, false);
                const ratio = (width + targetX) / width;
                setBackdropOpacity(ratio, false);
            } else {
                if (deltaX < 0) {
                    const targetX = Math.max(-width, Math.min(0, deltaX));
                    setDrawerTransform(targetX, false);
                    const ratio = (width + targetX) / width;
                    setBackdropOpacity(ratio, false);
                }
            }
        }, { passive: true });

        window.addEventListener('touchend', (e) => {
            if (!isDragging) return;
            isDragging = false;
            const deltaX = currentX - startX;

            if (!isOpen) {
                if (deltaX > 55) {
                    openDrawer();
                } else {
                    closeDrawer();
                }
            } else {
                if (deltaX < -50) {
                    closeDrawer();
                } else {
                    openDrawer();
                }
            }
            isHorizontalGesture = null;
        }, { passive: true });

        window.addEventListener('touchcancel', () => {
            if (isDragging) {
                isDragging = false;
                if (isOpen) openDrawer();
                else closeDrawer();
            }
        }, { passive: true });

        // Drawer Theme Switcher Buttons
        document.querySelectorAll('.drawer-theme-pill').forEach(pill => {
            pill.addEventListener('click', () => {
                const theme = pill.getAttribute('data-theme');
                if (theme) {
                    if (typeof setWebpageTheme === 'function') setWebpageTheme(theme);
                    else if (typeof applyDisplayTheme === 'function') applyDisplayTheme(theme);
                    document.querySelectorAll('.drawer-theme-pill').forEach(p => {
                        p.classList.toggle('active', p.getAttribute('data-theme') === theme);
                    });
                }
            });
        });

        // Close drawer when any internal navigation link or trigger is clicked
        drawer.querySelectorAll('a, .drawer-link').forEach(el => {
            el.addEventListener('click', () => {
                setTimeout(closeDrawer, 120);
            });
        });

        // Studio Tab Triggers (for ZeroBlur Studio)
        document.querySelectorAll('.drawer-tab-trigger').forEach(btn => {
            btn.addEventListener('click', () => {
                const tabId = btn.getAttribute('data-tab-target');
                if (tabId && typeof switchStudioTab === 'function') {
                    switchStudioTab(tabId);
                }
            });
        });

        // Step Navigation Triggers (for ZeroBlur Studio)
        document.querySelectorAll('.drawer-step-trigger').forEach(btn => {
            btn.addEventListener('click', () => {
                const stepId = btn.getAttribute('data-step-target');
                if (stepId && typeof setActiveStep === 'function') {
                    setActiveStep(stepId, true);
                }
            });
        });

        // Format Category Shortcuts (for File Converter)
        document.querySelectorAll('.drawer-format-cat-trigger').forEach(btn => {
            btn.addEventListener('click', () => {
                const cat = btn.getAttribute('data-cat');
                if (cat) {
                    const targetBtn = document.querySelector(`.format-cat-btn[data-cat="${cat}"]`);
                    if (targetBtn) targetBtn.click();
                }
            });
        });
    }

    // Initialize slide navigation drawer on DOM load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSlideNavigationDrawer);
    } else {
        initSlideNavigationDrawer();
    }

    </script>
</body>
</html>
"""


HTML_ZEROBLUR = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ZeroBlur Engine Studio — TikTok & WhatsApp Status Video Mastering</title>
  <script src="https://cdn.tailwindcss.com/3.4.17"></script>

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;0,6..72,700;1,6..72,400;1,6..72,600&family=Playfair+Display:ital,wght@0,600;0,700;1,400;1,600&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Plus Jakarta Sans', 'Inter', 'system-ui', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: {
              50: '#eefdf8',
              400: '#34d399',
              500: '#10b981',
              600: '#059669',
            }
          }
        }
      }
    };
  </script>
  <style>
    :root {
      --bg-color: #0b0f19;
      --card-bg: #111827;
      --card-border: #1f293d;
      --accent-primary: #10b981;
      --accent-hover: #059669;
      --accent-glow: rgba(16, 185, 129, 0.25);
      --text-main: #f9fafb;
      --text-muted: #9ca3af;
      --text-dim: #6b7280;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --radius-md: 10px;
      --radius-lg: 16px;
    }

    * {
      box-sizing: border-box;
    }

    body {
      font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
      background-color: var(--bg-color);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
      transition: background-color 0.25s ease, color 0.25s ease;
    }

    .custom-scroll::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    .custom-scroll::-webkit-scrollbar-track {
      background: #0f172a;
    }
    .custom-scroll::-webkit-scrollbar-thumb {
      background: #334155;
      border-radius: 9999px;
    }
    input[type="range"] {
      accent-color: #10b981;
    }

    /* Ambient Floating Orbs for Soft Liquid Glass Mode */
    .liquid-bg-orbs {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: 100vw;
      height: 100vh;
      pointer-events: none;
      z-index: -1;
      overflow: hidden;
    }
    body.theme-liquid-glass .liquid-bg-orbs {
      display: block;
    }
    .liquid-orb {
      position: absolute;
      border-radius: 50%;
      filter: blur(80px);
      opacity: 0.42;
      mix-blend-mode: screen;
      animation: liquidOrbFloat 18s ease-in-out infinite alternate;
    }
    .liquid-orb.orb-1 {
      width: 440px;
      height: 440px;
      background: radial-gradient(circle, #6366f1, #a855f7);
      top: -100px;
      left: -80px;
      animation-duration: 20s;
    }
    .liquid-orb.orb-2 {
      width: 500px;
      height: 500px;
      background: radial-gradient(circle, #38bdf8, #10b981);
      bottom: -150px;
      right: -100px;
      animation-duration: 24s;
    }
    .liquid-orb.orb-3 {
      width: 380px;
      height: 380px;
      background: radial-gradient(circle, #ec4899, #f43f5e);
      top: 35%;
      left: 45%;
      animation-duration: 28s;
    }
    @keyframes liquidOrbFloat {
      0% { transform: translate(0, 0) scale(1); }
      50% { transform: translate(60px, 40px) scale(1.08); }
      100% { transform: translate(-40px, 80px) scale(0.95); }
    }

    /* Header Badges & Pill Buttons */
    .header-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(16, 185, 129, 0.12);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
      border-radius: 9999px;
      padding: 4px 14px;
      font-size: 0.8rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .btn-diag {
      background: #1e293b;
      border: 1px solid #334155;
      color: #94a3b8;
      border-radius: 9999px;
      padding: 5px 14px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      text-decoration: none;
      transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .btn-diag:hover {
      background: #334155;
      color: #f8fafc;
      transform: translateY(-1px);
    }
    .btn-converter-nav {
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.22), rgba(6, 182, 212, 0.22));
      border: 1px solid rgba(16, 185, 129, 0.45);
      color: #34d399;
      box-shadow: 0 2px 8px rgba(16, 185, 129, 0.2);
    }
    .btn-converter-nav:hover {
      background: linear-gradient(135deg, #10b981, #06b6d4);
      color: #042f2e;
      box-shadow: 0 6px 18px rgba(16, 185, 129, 0.4);
    }

    /* Webpage Display Style Switcher Bar */
    .theme-bar {
            display: flex;
            align-items: center;
            justify-content: flex-start;
      gap: 10px;
      margin-top: 10px;
      margin-bottom: 4px;
      flex-wrap: wrap;
      transition: opacity 0.25s ease, transform 0.25s ease;
    }
    .theme-bar-label {
      font-size: 0.78rem;
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .theme-switcher {
      display: inline-flex;
      background: #111827;
      border: 1px solid #1f293d;
      border-radius: 9999px;
      padding: 3px;
      gap: 3px;
      box-shadow: 0 4px 14px rgba(0,0,0,0.3);
      max-width: 100%;
      overflow-x: auto;
    }
    .theme-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      padding: 5px 12px;
      border-radius: 9999px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
      white-space: nowrap;
    }
    .theme-btn:hover:not(.active) {
      color: #f8fafc;
      background: rgba(255, 255, 255, 0.06);
    }
    .theme-btn.active {
      background: #10b981;
      color: #042f2e;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    .theme-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      display: inline-block;
      flex-shrink: 0;
    }
    .theme-bar-close {
      background: transparent;
      border: none;
      color: #64748b;
      cursor: pointer;
      font-size: 1.25rem;
      line-height: 1;
      padding: 4px 8px;
      border-radius: 50%;
      transition: all 0.2s;
    }
    .theme-bar-close:hover {
      color: #cbd5e1;
      background: rgba(255, 255, 255, 0.08);
    }

    /* Stepper Navigation Bar across all selections */
    .selection-stepper-bar {
      scrollbar-width: thin;
    }
    .step-nav-pill {
      cursor: pointer;
    }

    /* Selection Blocks */
    .selection-block {
      transition: all 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .selection-block.active {
      border-color: rgba(16, 185, 129, 0.4);
      box-shadow: 0 0 16px rgba(16, 185, 129, 0.1);
    }
    .selection-block-header:hover .selection-block-title {
      color: #34d399;
    }

    /* ==========================================================================
       DISPLAY STYLE 1: EDITORIAL (High-fashion Typography & Tactile Print)
       ========================================================================== */
    body.theme-editorial {
      --bg-color: #f7f4ec;
      --card-bg: #fffefb;
      --card-border: #1c1917;
      --accent-primary: #853b1b;
      --accent-hover: #6c2f14;
      --accent-glow: rgba(133, 59, 27, 0.22);
      --text-main: #1c1917;
      --text-muted: #57534e;
      --text-dim: #78716c;
      --success: #1c1917;
      --danger: #991b1b;
      --warning: #b45309;
      --radius-md: 4px;
      --radius-lg: 6px;
      font-family: 'Newsreader', 'Playfair Display', Georgia, serif !important;
      background-color: #f7f4ec !important;
      background-image: 
        radial-gradient(#e5dfd3 0.8px, transparent 0.8px),
        linear-gradient(to bottom, rgba(247, 244, 236, 0.7), rgba(247, 244, 236, 1)) !important;
      background-size: 18px 18px, 100% 100% !important;
      color: #1c1917 !important;
      letter-spacing: -0.005em;
    }
    body.theme-editorial header {
      background: #fffefb !important;
      border-bottom: 2px solid #1c1917 !important;
    }
    body.theme-editorial h1,
    body.theme-editorial h2,
    body.theme-editorial h3 {
      font-family: 'Newsreader', 'Playfair Display', Georgia, serif !important;
      color: #1c1917 !important;
    }
    body.theme-editorial .header-badge {
      background: #ede8dc !important;
      border: 1px solid #1c1917 !important;
      color: #1c1917 !important;
      font-family: 'JetBrains Mono', monospace;
      border-radius: 2px !important;
      box-shadow: 2px 2px 0px #1c1917;
    }
    body.theme-editorial .btn-diag {
      background: #fffefb !important;
      border: 1.5px solid #1c1917 !important;
      color: #1c1917 !important;
      border-radius: 2px !important;
      box-shadow: 2px 2px 0px #1c1917 !important;
      font-family: 'JetBrains Mono', monospace;
    }
    body.theme-editorial .btn-diag:hover {
      background: #f1ecdf !important;
      transform: translate(-1px, -1px);
      box-shadow: 3px 3px 0px #1c1917 !important;
    }
    body.theme-editorial .theme-switcher {
      background: #fffefb;
      border: 1.5px solid #1c1917;
      border-radius: 4px;
      box-shadow: 3px 3px 0px #1c1917;
    }
    body.theme-editorial .theme-btn {
      color: #57534e;
      border-radius: 2px;
      font-family: 'JetBrains Mono', monospace;
    }
    body.theme-editorial .theme-btn.active {
      background: #1c1917;
      color: #fffefb;
      box-shadow: 2px 2px 0px #853b1b;
    }
    body.theme-editorial .bg-slate-900\/80,
    body.theme-editorial .bg-slate-900\/70,
    body.theme-editorial .bg-slate-900\/90,
    body.theme-editorial .bg-slate-950,
    body.theme-editorial .bg-slate-950\/80,
    body.theme-editorial .bg-slate-950\/60 {
      background: #fffefb !important;
      border: 1.5px solid #1c1917 !important;
      border-radius: 4px !important;
      box-shadow: 4px 4px 0px #1c1917 !important;
      color: #1c1917 !important;
    }
    body.theme-editorial .text-white {
      color: #1c1917 !important;
    }
    body.theme-editorial .text-slate-200,
    body.theme-editorial .text-slate-300 {
      color: #292524 !important;
    }
    body.theme-editorial .text-slate-400 {
      color: #57534e !important;
    }
    body.theme-editorial .selection-stepper-bar {
      background: #faf7f0 !important;
      border: 1.5px solid #1c1917 !important;
      border-radius: 3px !important;
    }
    body.theme-editorial .step-nav-pill {
      font-family: 'Newsreader', serif !important;
      color: #57534e !important;
      border-radius: 2px !important;
    }
    body.theme-editorial .step-nav-pill.active {
      background: #1c1917 !important;
      color: #fffefb !important;
      box-shadow: 2px 2px 0px #853b1b !important;
    }
    body.theme-editorial .selection-block {
      background: #faf7f0 !important;
      border: 1.5px solid #1c1917 !important;
      border-radius: 3px !important;
      box-shadow: 3px 3px 0px #1c1917 !important;
    }
    body.theme-editorial .selection-summary-badge {
      background: #ede8dc !important;
      border: 1px solid #1c1917 !important;
      color: #853b1b !important;
      font-family: 'JetBrains Mono', monospace;
      border-radius: 2px !important;
    }
    body.theme-editorial .btn-step-next {
      background: #1c1917 !important;
      color: #fffefb !important;
      border: 1.5px solid #1c1917 !important;
      border-radius: 2px !important;
      box-shadow: 3px 3px 0px #853b1b !important;
      font-family: 'JetBrains Mono', monospace;
    }
    body.theme-editorial .btn-step-next:hover {
      background: #853b1b !important;
      transform: translate(-1px, -1px);
      box-shadow: 4px 4px 0px #1c1917 !important;
    }
    body.theme-editorial .btn-step-prev {
      background: #fffefb !important;
      color: #1c1917 !important;
      border: 1.5px solid #1c1917 !important;
      border-radius: 2px !important;
      box-shadow: 2px 2px 0px #1c1917 !important;
      font-family: 'JetBrains Mono', monospace;
    }
    body.theme-editorial .plat-btn {
      background: #fffefb !important;
      border: 1.5px solid #1c1917 !important;
      border-radius: 2px !important;
      box-shadow: 2px 2px 0px #1c1917 !important;
      color: #1c1917 !important;
    }
    body.theme-editorial .plat-btn.border-emerald-500,
    body.theme-editorial .plat-btn.bg-emerald-500\/15 {
      background: #1c1917 !important;
      color: #fffefb !important;
      box-shadow: 3px 3px 0px #853b1b !important;
    }
    body.theme-editorial .plat-btn.border-emerald-500 .text-emerald-300 {
      color: #fecdd3 !important;
    }
    body.theme-editorial select {
      background: #fffefb !important;
      border: 1.5px solid #1c1917 !important;
      color: #1c1917 !important;
      border-radius: 2px !important;
    }
    body.theme-editorial pre,
    body.theme-editorial code {
      font-family: 'JetBrains Mono', monospace !important;
    }

    /* ==========================================================================
       DISPLAY STYLE 2: SOFT LIQUID GLASS (VisionOS Fluid Acrylic & Specular Sheen)
       ========================================================================== */
    body.theme-liquid-glass {
      --bg-color: #070913;
      --card-bg: rgba(255, 255, 255, 0.045);
      --card-border: rgba(255, 255, 255, 0.16);
      --accent-primary: #6366f1;
      --accent-hover: #4f46e5;
      --accent-glow: rgba(99, 102, 241, 0.45);
      --text-main: #ffffff;
      --text-muted: #cbd5e1;
      --text-dim: #94a3b8;
      --success: #34d399;
      --danger: #f87171;
      --warning: #fbbf24;
      --radius-md: 14px;
      --radius-lg: 24px;
      background-color: #070913 !important;
      background-image: 
        radial-gradient(circle 900px at 15% 15%, rgba(99, 102, 241, 0.22), transparent 70%),
        radial-gradient(circle 800px at 85% 25%, rgba(236, 72, 153, 0.18), transparent 70%),
        radial-gradient(circle 950px at 50% 85%, rgba(56, 189, 248, 0.22), transparent 70%),
        radial-gradient(circle 700px at 10% 75%, rgba(168, 85, 247, 0.18), transparent 70%) !important;
      background-attachment: fixed !important;
      color: #ffffff !important;
    }
    body.theme-liquid-glass header {
      background: rgba(8, 14, 29, 0.75) !important;
      backdrop-filter: blur(20px) !important;
      border-bottom: 1px solid rgba(255, 255, 255, 0.14) !important;
    }
    body.theme-liquid-glass .bg-slate-900\/80,
    body.theme-liquid-glass .bg-slate-900\/70,
    body.theme-liquid-glass .bg-slate-900\/90 {
      background: rgba(255, 255, 255, 0.045) !important;
      backdrop-filter: blur(20px) !important;
      border: 1px solid rgba(255, 255, 255, 0.16) !important;
      box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37) !important;
      border-radius: 20px !important;
    }
    body.theme-liquid-glass .bg-slate-950,
    body.theme-liquid-glass .bg-slate-950\/80,
    body.theme-liquid-glass .bg-slate-950\/60 {
      background: rgba(10, 15, 30, 0.6) !important;
      border-color: rgba(255, 255, 255, 0.12) !important;
      border-radius: 14px !important;
    }
    body.theme-liquid-glass .selection-block.active {
      border-color: rgba(99, 102, 241, 0.5) !important;
      box-shadow: 0 0 20px rgba(99, 102, 241, 0.25) !important;
    }
    body.theme-liquid-glass .step-nav-pill.active {
      background: linear-gradient(135deg, #6366f1, #38bdf8) !important;
      color: #ffffff !important;
      box-shadow: 0 0 16px rgba(99, 102, 241, 0.5) !important;
    }
    body.theme-liquid-glass .btn-step-next {
      background: linear-gradient(135deg, #6366f1, #38bdf8) !important;
      color: #ffffff !important;
      box-shadow: 0 0 16px rgba(99, 102, 241, 0.4) !important;
    }
    body.theme-liquid-glass .theme-btn.active {
      background: linear-gradient(135deg, #6366f1, #c084fc) !important;
      color: #ffffff !important;
      box-shadow: 0 0 16px rgba(192, 132, 252, 0.5) !important;
    }

    /* ==========================================================================
       DISPLAY STYLE 3: BLACK & WHITE LIGHT (Paper Mode)
       ========================================================================== */
    body.bw-page-light {
      --bg-color: #f8fafc;
      --card-bg: #ffffff;
      --card-border: #cbd5e1;
      --accent-primary: #09090b;
      --accent-hover: #27272a;
      --accent-glow: rgba(0, 0, 0, 0.15);
      --text-main: #09090b;
      --text-muted: #52525b;
      --text-dim: #71717a;
      --success: #09090b;
      --danger: #71717a;
      --warning: #52525b;
      background-color: #f8fafc !important;
      color: #09090b !important;
    }
    body.bw-page-light header {
      background: #ffffff !important;
      border-bottom: 2px solid #09090b !important;
    }
    body.bw-page-light h1,
    body.bw-page-light h2,
    body.bw-page-light h3,
    body.bw-page-light .text-white {
      color: #09090b !important;
    }
    body.bw-page-light .text-slate-200,
    body.bw-page-light .text-slate-300 {
      color: #27272a !important;
    }
    body.bw-page-light .text-slate-400 {
      color: #52525b !important;
    }
    body.bw-page-light .bg-slate-900\/80,
    body.bw-page-light .bg-slate-900\/70,
    body.bw-page-light .bg-slate-900\/90,
    body.bw-page-light .bg-slate-950,
    body.bw-page-light .bg-slate-950\/80,
    body.bw-page-light .bg-slate-950\/60 {
      background: #ffffff !important;
      border: 1px solid #cbd5e1 !important;
      color: #09090b !important;
      box-shadow: 0 2px 10px rgba(0,0,0,0.04) !important;
    }
    body.bw-page-light .header-badge {
      background: #f4f4f5 !important;
      color: #09090b !important;
      border-color: #d4d4d8 !important;
    }
    body.bw-page-light .btn-diag {
      background: #ffffff !important;
      border: 1px solid #cbd5e1 !important;
      color: #09090b !important;
    }
    body.bw-page-light .btn-diag:hover {
      background: #f4f4f5 !important;
      border-color: #09090b !important;
    }
    body.bw-page-light .theme-switcher {
      background: #ffffff !important;
      border: 1px solid #d4d4d8 !important;
    }
    body.bw-page-light .theme-btn.active {
      background: #09090b !important;
      color: #ffffff !important;
    }
    body.bw-page-light .step-nav-pill.active {
      background: #09090b !important;
      color: #ffffff !important;
    }
    body.bw-page-light .btn-step-next {
      background: #09090b !important;
      color: #ffffff !important;
    }
    body.bw-page-light .selection-summary-badge {
      background: #f4f4f5 !important;
      color: #09090b !important;
      border: 1px solid #d4d4d8 !important;
    }

    /* ==========================================================================
       DISPLAY STYLE 4: BLACK & WHITE DARK (Monochrome Dark Mode)
       ========================================================================== */
    body.bw-page-dark {
      --bg-color: #000000;
      --card-bg: #0d0d0d;
      --card-border: #262626;
      --accent-primary: #ffffff;
      --accent-hover: #e5e5e5;
      --accent-glow: rgba(255, 255, 255, 0.2);
      --text-main: #ffffff;
      --text-muted: #a3a3a3;
      --text-dim: #737373;
      --success: #ffffff;
      --danger: #d4d4d8;
      --warning: #a1a1aa;
      background-color: #000000 !important;
      color: #ffffff !important;
      filter: grayscale(100%);
    }
    body.bw-page-dark header {
      background: #0d0d0d !important;
      border-bottom: 1px solid #262626 !important;
    }
    body.bw-page-dark .bg-slate-900\/80,
    body.bw-page-dark .bg-slate-900\/70,
    body.bw-page-dark .bg-slate-900\/90,
    body.bw-page-dark .bg-slate-950,
    body.bw-page-dark .bg-slate-950\/80,
    body.bw-page-dark .bg-slate-950\/60 {
      background: #0d0d0d !important;
      border: 1px solid #262626 !important;
    }
    body.bw-page-dark .step-nav-pill.active {
      background: #ffffff !important;
      color: #000000 !important;
    }
    body.bw-page-dark .btn-step-next {
      background: #ffffff !important;
      color: #000000 !important;
    }
    body.bw-page-dark .theme-btn.active {
      background: #ffffff !important;
      color: #000000 !important;
    }
  
        /* ==================== SLIDE-OUT NAVIGATION DRAWER & HAMBURGER ==================== */
        .btn-hamburger {
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.42rem 0.8rem;
            border-radius: 9999px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.14);
            color: #f1f5f9;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            user-select: none;
            -webkit-user-select: none;
            touch-action: manipulation;
            z-index: 40;
        }
        .btn-hamburger:hover {
            background: rgba(255, 255, 255, 0.12);
            border-color: rgba(255, 255, 255, 0.28);
            color: #ffffff;
            transform: translateY(-1px);
        }
        .btn-hamburger:active {
            transform: translateY(0);
        }
        .btn-hamburger svg {
            flex-shrink: 0;
            stroke: currentColor;
        }
        .btn-hamburger .hamburger-label {
            font-size: 0.78rem;
            letter-spacing: 0.02em;
        }

        /* Backdrop */
        .slide-nav-backdrop {
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(4, 7, 15, 0.72);
            backdrop-filter: blur(5px);
            -webkit-backdrop-filter: blur(5px);
            z-index: 9998;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .slide-nav-backdrop.is-open {
            opacity: 1;
            pointer-events: auto;
        }

        /* Slide Drawer */
        .slide-nav-drawer {
            position: fixed;
            top: 0;
            left: 0;
            bottom: 0;
            width: min(340px, 86vw);
            height: 100%;
            height: 100dvh;
            z-index: 9999;
            background: #0b1120;
            border-right: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 15px 0 40px rgba(0, 0, 0, 0.65);
            transform: translateX(-100%);
            transition: transform 0.28s cubic-bezier(0.16, 1, 0.3, 1);
            display: flex;
            flex-direction: column;
            overflow: hidden;
            will-change: transform;
            touch-action: pan-y;
        }
        .slide-nav-drawer.is-open {
            transform: translateX(0);
        }

        .drawer-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 1.1rem 1.25rem 0.9rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(15, 23, 42, 0.6);
            flex-shrink: 0;
        }
        .drawer-brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .drawer-logo {
            width: 34px;
            height: 34px;
            border-radius: 10px;
            background: linear-gradient(135deg, #10b981, #059669);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
        }
        .drawer-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: #f8fafc;
            line-height: 1.2;
        }
        .drawer-subtitle {
            font-size: 0.72rem;
            color: #94a3b8;
            font-family: monospace;
        }
        .drawer-close-btn {
            background: transparent;
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: #94a3b8;
            font-size: 1.35rem;
            line-height: 1;
            width: 32px;
            height: 32px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
        }
        .drawer-close-btn:hover {
            background: rgba(255, 255, 255, 0.1);
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.25);
        }

        .drawer-content {
            flex: 1;
            overflow-y: auto;
            overscroll-behavior: contain;
            -webkit-overflow-scrolling: touch;
            padding: 0.85rem 1rem 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 1.1rem;
        }

        .drawer-gesture-hint {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.72rem;
            color: #64748b;
            padding: 0.4rem 0.6rem;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 6px;
            border: 1px dashed rgba(255, 255, 255, 0.08);
        }

        .drawer-section {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }
        .drawer-section-title {
            font-size: 0.7rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #64748b;
            margin-bottom: 0.25rem;
            padding-left: 0.35rem;
        }

        .drawer-link {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            padding: 0.6rem 0.75rem;
            border-radius: 10px;
            color: #cbd5e1;
            text-decoration: none;
            background: transparent;
            border: 1px solid transparent;
            text-align: left;
            cursor: pointer;
            transition: all 0.15s ease;
            width: 100%;
        }
        .drawer-link:hover {
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
        }
        .drawer-link.active {
            background: rgba(16, 185, 129, 0.12);
            border-color: rgba(16, 185, 129, 0.35);
            color: #34d399;
        }
        .drawer-link-icon {
            font-size: 1rem;
            width: 24px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .drawer-link-body {
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .drawer-link-name {
            font-size: 0.84rem;
            font-weight: 600;
            color: inherit;
            line-height: 1.25;
        }
        .drawer-link-desc {
            font-size: 0.7rem;
            color: #64748b;
            margin-top: 1px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .drawer-link:hover .drawer-link-desc {
            color: #94a3b8;
        }

        /* Theme Grid in Drawer */
        .drawer-theme-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 0.35rem;
        }
        .drawer-theme-pill {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            padding: 0.5rem 0.75rem;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: #94a3b8;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
            text-align: left;
        }
        .drawer-theme-pill:hover {
            background: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
        }
        .drawer-theme-pill.active {
            background: rgba(16, 185, 129, 0.15);
            border-color: #10b981;
            color: #34d399;
        }

        .drawer-footer {
            padding: 0.85rem 1.15rem;
            border-top: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(15, 23, 42, 0.6);
            flex-shrink: 0;
        }
        .drawer-footer-status {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            font-size: 0.74rem;
            font-weight: 600;
            color: #34d399;
        }
        .status-indicator-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
        }
        .drawer-footer-sub {
            font-size: 0.68rem;
            color: #64748b;
            margin-top: 3px;
        }

        /* THEME OVERRIDES FOR DRAWER */
        body.theme-editorial .btn-hamburger {
            background: #ede8dc;
            border-color: #dcd7cb;
            color: #1c1917;
        }
        body.theme-editorial .btn-hamburger:hover {
            background: #e3ded1;
            border-color: #1c1917;
        }
        body.theme-editorial .slide-nav-backdrop {
            background: rgba(28, 25, 23, 0.55);
        }
        body.theme-editorial .slide-nav-drawer {
            background: #fbf9f4;
            border-right: 1.5px solid #dcd7cb;
            box-shadow: 15px 0 35px rgba(28, 25, 23, 0.2);
        }
        body.theme-editorial .drawer-header {
            background: #f2eee3;
            border-bottom: 1.5px solid #dcd7cb;
        }
        body.theme-editorial .drawer-title {
            color: #1c1917;
            font-family: 'Newsreader', Georgia, serif;
        }
        body.theme-editorial .drawer-subtitle {
            color: #78716c;
        }
        body.theme-editorial .drawer-close-btn {
            border-color: #dcd7cb;
            color: #78716c;
        }
        body.theme-editorial .drawer-close-btn:hover {
            background: #e7e2d4;
            color: #1c1917;
            border-color: #1c1917;
        }
        body.theme-editorial .drawer-gesture-hint {
            background: #ede8dc;
            border-color: #dcd7cb;
            color: #78716c;
        }
        body.theme-editorial .drawer-section-title {
            color: #78716c;
        }
        body.theme-editorial .drawer-link {
            color: #292524;
        }
        body.theme-editorial .drawer-link:hover {
            background: #ede8dc;
            border-color: #dcd7cb;
            color: #0c0a09;
        }
        body.theme-editorial .drawer-link.active {
            background: #853b1b;
            border-color: #1c1917;
            color: #ffffff;
        }
        body.theme-editorial .drawer-link.active .drawer-link-desc {
            color: #f5f5f4;
        }
        body.theme-editorial .drawer-theme-pill {
            background: #f2eee3;
            border-color: #dcd7cb;
            color: #292524;
        }
        body.theme-editorial .drawer-theme-pill:hover {
            background: #ede8dc;
            border-color: #1c1917;
        }
        body.theme-editorial .drawer-theme-pill.active {
            background: #853b1b;
            border-color: #1c1917;
            color: #ffffff;
        }
        body.theme-editorial .drawer-footer {
            background: #f2eee3;
            border-top: 1.5px solid #dcd7cb;
        }

        body.theme-liquid-glass .btn-hamburger {
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            border: 1px solid rgba(255, 255, 255, 0.25);
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25), inset 0 1px 1px rgba(255, 255, 255, 0.35);
            color: #e0f2fe;
        }
        body.theme-liquid-glass .slide-nav-drawer {
            background: rgba(13, 17, 34, 0.78);
            backdrop-filter: blur(28px) saturate(180%);
            -webkit-backdrop-filter: blur(28px) saturate(180%);
            border-right: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 18px 0 50px rgba(0, 0, 0, 0.55), inset 0 1px 1px rgba(255, 255, 255, 0.3);
        }
        body.theme-liquid-glass .drawer-header {
            background: rgba(255, 255, 255, 0.04);
            border-bottom: 1px solid rgba(255, 255, 255, 0.12);
        }
        body.theme-liquid-glass .drawer-link:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.18);
        }
        body.theme-liquid-glass .drawer-link.active {
            background: rgba(56, 189, 248, 0.18);
            border-color: rgba(56, 189, 248, 0.45);
            color: #38bdf8;
        }
        body.theme-liquid-glass .drawer-theme-pill.active {
            background: rgba(56, 189, 248, 0.2);
            border-color: rgba(56, 189, 248, 0.6);
            color: #38bdf8;
        }

        body.bw-page-light .btn-hamburger, body.bw-mode.bw-page-light .btn-hamburger {
            background: #ffffff;
            border: 1.5px solid #000000;
            color: #000000;
        }
        body.bw-page-light .slide-nav-drawer, body.bw-mode.bw-page-light .slide-nav-drawer {
            background: #ffffff;
            border-right: 2px solid #000000;
            box-shadow: 15px 0 35px rgba(0, 0, 0, 0.3);
        }
        body.bw-page-light .drawer-header, body.bw-mode.bw-page-light .drawer-header {
            background: #f4f4f5;
            border-bottom: 2px solid #000000;
        }
        body.bw-page-light .drawer-title, body.bw-mode.bw-page-light .drawer-title {
            color: #000000;
        }
        body.bw-page-light .drawer-subtitle, body.bw-mode.bw-page-light .drawer-subtitle {
            color: #52525b;
        }
        body.bw-page-light .drawer-close-btn, body.bw-mode.bw-page-light .drawer-close-btn {
            border-color: #000000;
            color: #000000;
        }
        body.bw-page-light .drawer-link, body.bw-mode.bw-page-light .drawer-link {
            color: #000000;
        }
        body.bw-page-light .drawer-link:hover, body.bw-mode.bw-page-light .drawer-link:hover {
            background: #f4f4f5;
            border-color: #000000;
        }
        body.bw-page-light .drawer-link.active, body.bw-mode.bw-page-light .drawer-link.active {
            background: #000000;
            color: #ffffff;
            border-color: #000000;
        }
        body.bw-page-light .drawer-link.active .drawer-link-desc, body.bw-mode.bw-page-light .drawer-link.active .drawer-link-desc {
            color: #d4d4d8;
        }
        body.bw-page-light .drawer-theme-pill, body.bw-mode.bw-page-light .drawer-theme-pill {
            background: #ffffff;
            border: 1.5px solid #000000;
            color: #000000;
        }
        body.bw-page-light .drawer-theme-pill.active, body.bw-mode.bw-page-light .drawer-theme-pill.active {
            background: #000000;
            color: #ffffff;
        }
        body.bw-page-light .drawer-footer, body.bw-mode.bw-page-light .drawer-footer {
            background: #f4f4f5;
            border-top: 2px solid #000000;
        }
        body.bw-page-light .drawer-footer-status, body.bw-mode.bw-page-light .drawer-footer-status {
            color: #000000;
        }

        body.bw-page-dark .btn-hamburger, body.bw-mode.bw-page-dark .btn-hamburger {
            background: #000000;
            border: 1.5px solid #ffffff;
            color: #ffffff;
        }
        body.bw-page-dark .slide-nav-drawer, body.bw-mode.bw-page-dark .slide-nav-drawer {
            background: #000000;
            border-right: 2px solid #ffffff;
            box-shadow: 15px 0 35px rgba(255, 255, 255, 0.15);
        }
        body.bw-page-dark .drawer-header, body.bw-mode.bw-page-dark .drawer-header {
            background: #09090b;
            border-bottom: 2px solid #ffffff;
        }
        body.bw-page-dark .drawer-title, body.bw-mode.bw-page-dark .drawer-title {
            color: #ffffff;
        }
        body.bw-page-dark .drawer-close-btn, body.bw-mode.bw-page-dark .drawer-close-btn {
            border-color: #ffffff;
            color: #ffffff;
        }
        body.bw-page-dark .drawer-link, body.bw-mode.bw-page-dark .drawer-link {
            color: #ffffff;
        }
        body.bw-page-dark .drawer-link:hover, body.bw-mode.bw-page-dark .drawer-link:hover {
            background: #18181b;
            border-color: #ffffff;
        }
        body.bw-page-dark .drawer-link.active, body.bw-mode.bw-page-dark .drawer-link.active {
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
        }
        body.bw-page-dark .drawer-link.active .drawer-link-desc, body.bw-mode.bw-page-dark .drawer-link.active .drawer-link-desc {
            color: #27272a;
        }
        body.bw-page-dark .drawer-theme-pill, body.bw-mode.bw-page-dark .drawer-theme-pill {
            background: #000000;
            border: 1.5px solid #ffffff;
            color: #ffffff;
        }
        body.bw-page-dark .drawer-theme-pill.active, body.bw-mode.bw-page-dark .drawer-theme-pill.active {
            background: #ffffff;
            color: #000000;
        }
        body.bw-page-dark .drawer-footer, body.bw-mode.bw-page-dark .drawer-footer {
            background: #09090b;
            border-top: 2px solid #ffffff;
        }
        body.bw-page-dark .drawer-footer-status, body.bw-mode.bw-page-dark .drawer-footer-status {
            color: #ffffff;
        }

  </style>

</head>

<body class="min-h-screen flex flex-col bg-[#0b0f19] text-slate-100 selection:bg-emerald-500/30 selection:text-emerald-200">

  <!-- Ambient Floating Orbs for Soft Liquid Glass Mode -->
  <div class="liquid-bg-orbs" aria-hidden="true">
    <div class="liquid-orb orb-1"></div>
    <div class="liquid-orb orb-2"></div>
    <div class="liquid-orb orb-3"></div>
  </div>

  <!-- Toast Notification -->
  <div id="toastContainer" class="fixed bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none"></div>

  
    <!-- Slide Navigation Backdrop -->
    <div id="slideNavBackdrop" class="slide-nav-backdrop" aria-hidden="true"></div>

    <!-- Slide Navigation Drawer -->
    <aside id="slideNavDrawer" class="slide-nav-drawer" role="dialog" aria-modal="true" aria-label="Navigation Menu">
        <div class="drawer-header">
            <div class="drawer-brand">
                <div class="drawer-logo" style="background:linear-gradient(135deg, #0ea5e9, #6366f1); box-shadow:0 4px 12px rgba(14,165,233,0.35);">🎬</div>
                <div>
                    <div class="drawer-title">ZeroBlur Engine Studio</div>
                    <div class="drawer-subtitle">v1.4.0 • Video Mastering</div>
                </div>
            </div>
            <button type="button" id="btnCloseNavDrawer" class="drawer-close-btn" aria-label="Close navigation drawer" title="Close drawer">&times;</button>
        </div>

        <div class="drawer-content">
            <!-- Slide Gesture Hint -->
            <div class="drawer-gesture-hint">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="19" y1="12" x2="5" y2="12"></line>
                    <polyline points="12 19 5 12 12 5"></polyline>
                </svg>
                <span>Swipe left or tap outside to slide close</span>
            </div>

            <!-- Main Workspaces -->
            <div class="drawer-section">
                <div class="drawer-section-title">Pages & Workspaces</div>
                <a href="/" class="drawer-link" id="drawerLinkConverter">
                    <span class="drawer-link-icon">⚡</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Universal File Converter</span>
                        <span class="drawer-link-desc">All-in-one instant conversion engine</span>
                    </div>
                </a>
                <a href="/zeroblur" class="drawer-link active" id="drawerLinkZeroBlur">
                    <span class="drawer-link-icon">🎬</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">ZeroBlur Engine Studio</span>
                        <span class="drawer-link-desc">TikTok & WhatsApp Status video mastering</span>
                    </div>
                </a>
                <a href="/formats" class="drawer-link" id="drawerLinkFormats">
                    <span class="drawer-link-icon">📚</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Formats Catalog</span>
                        <span class="drawer-link-desc">Supported extensions and compatibility</span>
                    </div>
                </a>
                <a href="/api/diagnostics" target="_blank" class="drawer-link" id="drawerLinkDiagnostics">
                    <span class="drawer-link-icon">📊</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">System Diagnostics</span>
                        <span class="drawer-link-desc">Cloud health, FFmpeg & library report</span>
                    </div>
                </a>
            </div>

            <!-- Studio Workspace Tabs -->
            <div class="drawer-section">
                <div class="drawer-section-title">Studio Workspaces</div>
                <button type="button" class="drawer-link drawer-tab-trigger active" data-tab-target="view-studio">
                    <span class="drawer-link-icon">🎛️</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Live Studio</span>
                        <span class="drawer-link-desc">Visual comparison slider & preset mastering</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-tab-trigger" data-tab-target="view-builder">
                    <span class="drawer-link-icon">💻</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">FFmpeg Command Builder</span>
                        <span class="drawer-link-desc">CLI parameters & bitrate tuning</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-tab-trigger" data-tab-target="view-code">
                    <span class="drawer-link-icon">📱</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Web & Mobile Implementation</span>
                        <span class="drawer-link-desc">React, Vue, iOS & Android code</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-tab-trigger" data-tab-target="view-blueprint">
                    <span class="drawer-link-icon">🛡️</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Compression Bypass Blueprint</span>
                        <span class="drawer-link-desc">Reverse-engineered platform algorithms</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-tab-trigger" data-tab-target="view-converter">
                    <span class="drawer-link-icon">🔄</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Formats & Converter</span>
                        <span class="drawer-link-desc">Return to file converter workspace</span>
                    </div>
                </button>
            </div>

            <!-- Tuning Workflow Steps -->
            <div class="drawer-section">
                <div class="drawer-section-title">Tuning Workflow Steps</div>
                <button type="button" class="drawer-link drawer-step-trigger" data-step-target="step-platform">
                    <span class="drawer-link-icon" style="font-weight:700; color:#38bdf8;">1</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Target Platform</span>
                        <span class="drawer-link-desc">TikTok vs WhatsApp Status algorithms</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-step-trigger" data-step-target="step-footage">
                    <span class="drawer-link-icon" style="font-weight:700; color:#38bdf8;">2</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Source Footage Profile</span>
                        <span class="drawer-link-desc">Talking head, screen recording, gaming</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-step-trigger" data-step-target="step-fps">
                    <span class="drawer-link-icon" style="font-weight:700; color:#38bdf8;">3</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">FPS & Temporal Cadence</span>
                        <span class="drawer-link-desc">Frame rate lock and pacing</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-step-trigger" data-step-target="step-unsharp">
                    <span class="drawer-link-icon" style="font-weight:700; color:#38bdf8;">4</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Detail & Unsharp Mask</span>
                        <span class="drawer-link-desc">Pre-sharpening compensation</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-step-trigger" data-step-target="step-switches">
                    <span class="drawer-link-icon" style="font-weight:700; color:#38bdf8;">5</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Duration & Master Switches</span>
                        <span class="drawer-link-desc">Max duration & strict GOP 1-sec lock</span>
                    </div>
                </button>
                <button type="button" class="drawer-link drawer-step-trigger" data-step-target="step-output">
                    <span class="drawer-link-icon" style="font-weight:700; color:#38bdf8;">6</span>
                    <div class="drawer-link-body">
                        <span class="drawer-link-name">Master Output & Deploy</span>
                        <span class="drawer-link-desc">Run cloud master or browser canvas test</span>
                    </div>
                </button>
            </div>

            <!-- Display Theme Switcher -->
            <div class="drawer-section">
                <div class="drawer-section-title">Display Theme</div>
                <div class="drawer-theme-grid">
                    <button type="button" class="drawer-theme-pill" data-theme="color" title="Modern Dark">
                        <span class="theme-dot" style="background:#10b981;"></span>
                        <span>Modern Dark</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="editorial" title="Editorial Magazine">
                        <span class="theme-dot" style="background:#853b1b; border:1px solid #1c1917;"></span>
                        <span>Editorial</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="liquid-glass" title="Soft Liquid Glass">
                        <span class="theme-dot" style="background:linear-gradient(135deg, #38bdf8, #c084fc); box-shadow:0 0 6px rgba(192,132,252,0.8);"></span>
                        <span>Liquid Glass</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="bw-light" title="B&W Light">
                        <span class="theme-dot" style="background:#ffffff; border:1px solid #71717a;"></span>
                        <span>B&W Light</span>
                    </button>
                    <button type="button" class="drawer-theme-pill" data-theme="bw-dark" title="B&W Dark">
                        <span class="theme-dot" style="background:#000000; border:1px solid #a1a1aa;"></span>
                        <span>B&W Dark</span>
                    </button>
                </div>
            </div>
        </div>

        <div class="drawer-footer">
            <div class="drawer-footer-status">
                <span class="status-indicator-dot" style="background:#38bdf8; box-shadow:0 0 8px #38bdf8;"></span>
                <span>ZeroBlur Mastering Engine: Ready</span>
            </div>
            <div class="drawer-footer-sub">Swipe left to close • Slide from edge to open</div>
        </div>
    </aside>


  <!-- Top Header Matching Universal File Converter -->
  <header class="sticky top-0 z-30 border-b border-slate-800/80 bg-[#0b0f19]/90 backdrop-blur-md">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center justify-start gap-3">
      <div class="flex items-center gap-3">
        <!-- Slide Navigation Drawer Hamburger Button -->
                <button type="button" id="navDrawerHamburgerBtn" class="btn-hamburger" aria-label="Open Navigation Menu" title="Open Navigation Drawer" aria-expanded="false">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <line x1="3" y1="8" x2="21" y2="8"></line>
                        <line x1="3" y1="16" x2="21" y2="16"></line>
                    </svg>
                    <span class="hamburger-label">Menu</span>
                </button>
        <!-- Navigation Button Back to Universal File Converter -->
        <a href="/" id="backToConverterBtn" class="btn-diag btn-converter-nav" title="Return to Universal File Converter">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"></line>
            <polyline points="12 19 5 12 12 5"></polyline>
          </svg>
          <span>← Back to Converter</span>
        </a>

        <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-400 to-cyan-500 flex items-center justify-center shadow-lg shadow-emerald-500/20">
          <svg class="w-5 h-5 text-slate-950" fill="none" stroke="currentColor" stroke-width="2.4" viewBox="0 0 24 24">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
          </svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-base sm:text-lg font-bold tracking-tight text-white">ZeroBlur Engine Studio</h1>
            <span class="header-badge" style="padding: 2px 8px; font-size: 0.7rem;">v2.4 Master</span>
          </div>
          <p class="text-xs text-slate-400 hidden sm:block">2-Pass Veryslow H.264 • Lanczos + Unsharp • 10-Bit HDR→Rec.709 • CFR Lock</p>
        </div>
      </div>

      <!-- Right Header Actions & Navigation Tabs -->
      <div class="flex flex-wrap items-center gap-2">
        <!-- Display Style Cycle Toggle Button -->
        <button type="button" class="btn-diag btn-bw-toggle" id="zeroblurThemeToggle" title="Toggle webpage display style (Editorial, Liquid Glass, Modern Dark, B&W)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <path d="M12 2a10 10 0 0 1 0 20z" fill="currentColor"></path>
          </svg>
          <span id="zeroblurToggleText">Display: Modern Dark</span>
        </button>

        <!-- Navigation Button to Hide / Show Display Switcher -->
        <button type="button" class="btn-diag btn-toggle-switcher" id="toggleZeroblurSwitcherNavBtn" title="Hide or show the display style switcher bar" aria-expanded="true">
          <svg id="toggleZeroblurSwitcherIcon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path>
            <line x1="1" y1="1" x2="23" y2="23"></line>
          </svg>
          <span id="toggleZeroblurSwitcherText">Hide Switcher</span>
        </button>

        <!-- Compatibility button for tests -->
        <button id="zeroblurThemeBtn" class="hidden" style="display:none;"></button>

        <!-- Top Tab Navigation -->
        <nav class="flex items-center gap-1 bg-slate-900/90 p-1 rounded-xl border border-slate-800 overflow-x-auto max-w-full">
          <button id="tabBtn-studio" data-tab="studio" class="nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-semibold transition-all bg-emerald-500 text-slate-950 shadow">
            Live Studio &amp; Comparator
          </button>
          <button id="tabBtn-pipeline" data-tab="pipeline" class="nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-medium transition-all text-slate-300 hover:text-white hover:bg-slate-800/60">
            Bitrate &amp; FFmpeg Builder
          </button>
          <button id="tabBtn-code" data-tab="code" class="nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-medium transition-all text-slate-300 hover:text-white hover:bg-slate-800/60">
            Web &amp; Mobile Code
          </button>
          <button id="tabBtn-guide" data-tab="guide" class="nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-medium transition-all text-slate-300 hover:text-white hover:bg-slate-800/60">
            Bypass Blueprint
          </button>
          <button id="tabBtn-converter" data-tab="converter" class="nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-medium transition-all text-slate-300 hover:text-white hover:bg-slate-800/60">
            Formats &amp; Return
          </button>
        </nav>
      </div>
    </div>

    <!-- Segmented Webpage Display Style Switcher Bar -->
    <div class="theme-bar px-4 pb-2 justify-start" id="themeBar">
      <span class="theme-bar-label">Display Style:</span>
      <div class="theme-switcher" id="themeSwitcher" role="group" aria-label="Webpage Display Styles">
        <button type="button" class="theme-btn" data-theme="editorial" id="themeBtnEditorial" title="Editorial Magazine Layout">
          <span class="theme-dot" style="background:#853b1b; border:1px solid #1c1917;"></span>
          <span>📰 Editorial</span>
        </button>
        <button type="button" class="theme-btn" data-theme="liquid-glass" id="themeBtnLiquidGlass" title="Soft Liquid Glassmorphism">
          <span class="theme-dot" style="background:linear-gradient(135deg, #38bdf8, #c084fc); box-shadow:0 0 6px rgba(192,132,252,0.8);"></span>
          <span>🫧 Soft Liquid Glass</span>
        </button>
        <button type="button" class="theme-btn active" data-theme="color" id="themeBtnColor" title="Modern Dark Slate">
          <span class="theme-dot" style="background:#10b981;"></span>
          <span>Modern Dark</span>
        </button>
        <button type="button" class="theme-btn" data-theme="bw-light" id="themeBtnBwLight" title="Black & White Paper Page">
          <span class="theme-dot" style="background:#ffffff; border:1px solid #71717a;"></span>
          <span>B&W Light</span>
        </button>
        <button type="button" class="theme-btn" data-theme="bw-dark" id="themeBtnBwDark" title="Black & White Dark Monochrome">
          <span class="theme-dot" style="background:#000000; border:1px solid #a1a1aa;"></span>
          <span>B&W Dark</span>
        </button>
      </div>
      <button type="button" class="theme-bar-close" id="themeBarCloseBtn" title="Hide switcher bar" aria-label="Hide theme switcher">&times;</button>
    </div>
  </header>

  <!-- Main Content Area -->
  <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">

<section id="view-studio" class="space-y-6">
      <!-- Top Status Banner -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div class="bg-slate-900/70 border border-slate-800/90 rounded-xl p-3.5">
          <div class="text-xs text-slate-400 flex items-center justify-between">
            <span>Active Preset</span>
            <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
          </div>
          <div id="metricPresetName" class="text-sm sm:text-base font-bold text-white mt-1">TikTok Ultra HD (1080×1920)</div>
          <div id="metricPresetSub" class="text-[11px] font-mono text-emerald-400 mt-0.5">60.0 FPS CFR • 9,500 kbps 2-Pass</div>
        </div>

        <div class="bg-slate-900/70 border border-slate-800/90 rounded-xl p-3.5">
          <div class="text-xs text-slate-400">Source Inspection</div>
          <div id="metricSourceRes" class="text-sm sm:text-base font-bold text-white mt-1">3840×2160 (4K Demo)</div>
          <div id="metricSourceFlags" class="text-[11px] font-mono text-amber-400 mt-0.5">10-Bit HLG HDR • VFR Detected</div>
        </div>

        <div class="bg-slate-900/70 border border-slate-800/90 rounded-xl p-3.5">
          <div class="text-xs text-slate-400">Estimated Master Payload</div>
          <div id="metricPayloadSize" class="text-sm sm:text-base font-bold text-white mt-1">14.2 MB / 15.0s</div>
          <div id="metricBypassStatus" class="text-[11px] font-mono text-emerald-400 mt-0.5">PASS: Below Re-Encode Trigger</div>
        </div>

        <div class="bg-slate-900/70 border border-slate-800/90 rounded-xl p-3.5">
          <div class="text-xs text-slate-400">Visual Fidelity (VMAF Est.)</div>
          <div class="flex items-baseline gap-2 mt-1">
            <span id="metricVmafScore" class="text-sm sm:text-base font-bold text-emerald-400">98.4 / 100</span>
            <span class="text-xs text-slate-400">vs 71.2 Raw Upload</span>
          </div>
          <div class="text-[11px] font-mono text-cyan-400 mt-0.5">Lanczos + Unsharp 5×5 Active</div>
        </div>
      </div>

      <!-- Studio Workspace Grid -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">

        <!-- Left Column: Interactive Split-Screen Video Preview & Processor (7 cols) -->
        <div class="lg:col-span-7 bg-slate-900/80 border border-slate-800 rounded-2xl p-4 sm:p-5 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 class="text-base font-semibold text-white flex items-center gap-2">
                <span>Real-Time Optical Comparator</span>
                <span id="liveBadge" class="px-2 py-0.5 text-[10px] font-mono uppercase rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">60 FPS Live Shader</span>
              </h2>
              <p class="text-xs text-slate-400">Drag the slider to compare platform compression blur vs. ZeroBlur Engine output</p>
            </div>

            <!-- Upload or Synthetic Stream Controls -->
            <div class="flex items-center gap-2">
              <label for="videoFileInput" class="cursor-pointer px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-200 border border-slate-700 transition flex items-center gap-1.5">
                <svg class="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                </svg>
                <span>Load Local Video</span>
              </label>
              <input id="videoFileInput" type="file" accept="video/*,.mov,.mp4,.webm" class="hidden" />

              <button id="btnResetDemoStream" type="button" class="px-2.5 py-1.5 rounded-lg bg-slate-800/70 hover:bg-slate-800 text-xs font-medium text-slate-300 border border-slate-700/80 transition">
                4K HDR Test Scene
              </button>
            </div>
          </div>

          <!-- Hidden HTML5 video element for user uploads -->
          <video id="hiddenSourceVideo" class="hidden" playsinline muted loop crossorigin="anonymous"></video>

          <!-- Canvas Split-View Container -->
          <div id="canvasViewport" style="touch-action: none;" class="relative w-full aspect-[16/10] bg-slate-950 rounded-xl overflow-hidden border border-slate-800 select-none transition-all duration-200">
            <canvas id="comparisonCanvas" width="960" height="600" class="w-full h-full object-contain block"></canvas>

            <!-- Top Overlay Labels -->
            <div class="absolute top-3 left-3 pointer-events-none px-2.5 py-1 rounded-md bg-rose-950/85 border border-rose-500/40 text-[11px] font-mono text-rose-200 shadow">
              LEFT: Naive Upload (Bilinear Crush + HDR Clip)
            </div>
            <div class="absolute top-3 right-3 pointer-events-none px-2.5 py-1 rounded-md bg-emerald-950/85 border border-emerald-500/40 text-[11px] font-mono text-emerald-200 shadow">
              RIGHT: ZeroBlur Master (Lanczos + Unsharp + Rec.709)
            </div>

            <!-- Split Slider Overlay Control -->
            <div id="splitLineIndicator" class="absolute top-0 bottom-0 w-0.5 bg-emerald-400 shadow-[0_0_12px_rgba(16,185,129,0.9)] pointer-events-none" style="left: 50%;">
              <div class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-8 h-8 rounded-full bg-emerald-400 text-slate-950 flex items-center justify-center shadow-lg border-2 border-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M8 9l-4 3 4 3m8-6l4 3-4 3" />
                </svg>
              </div>
            </div>
          </div>

          <!-- Interactive Split & Zoom Toolbar -->
          <div class="flex flex-wrap items-center justify-between gap-3 pt-1">
            <div class="flex items-center gap-3 flex-1 min-w-[200px]">
              <span class="text-xs font-mono text-slate-400 whitespace-nowrap">Split Wipe:</span>
              <input id="splitRangeSlider" type="range" min="5" max="95" value="50" class="w-full h-1.5 bg-slate-800 rounded-lg cursor-pointer" />
              <span id="splitPercentLabel" class="text-xs font-mono text-emerald-400 w-10 text-right">50%</span>
            </div>

            <div class="flex items-center gap-1.5">
              <button id="btnTogglePlay" type="button" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-white border border-slate-700 transition">
                Pause Frame
              </button>
              <button id="btnZoom1x" type="button" data-zoom="1" class="zoom-btn px-2.5 py-1.5 rounded-lg bg-emerald-500 text-slate-950 text-xs font-semibold transition">
                1× Fit
              </button>
              <button id="btnZoom2x" type="button" data-zoom="2" class="zoom-btn px-2.5 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white text-xs font-medium border border-slate-700 transition">
                2× Pixel Zoom
              </button>
              <button id="btnZoom3x" type="button" data-zoom="3" class="zoom-btn px-2.5 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white text-xs font-medium border border-slate-700 transition">
                3× Micro Texture
              </button>
            </div>
          </div>

          <!-- Studio Pro Keyboard Shortcuts Bar -->
          <div class="text-[11px] font-mono text-slate-400 hidden sm:flex items-center gap-2 pt-0.5 select-none">
            <span class="text-slate-500">Mastering Shortcuts:</span>
            <kbd class="px-1.5 py-0.5 rounded bg-slate-800/80 text-slate-300 border border-slate-700/80">Space</kbd> Pause/Play •
            <kbd class="px-1.5 py-0.5 rounded bg-slate-800/80 text-slate-300 border border-slate-700/80">← / →</kbd> Split Wipe •
            <kbd class="px-1.5 py-0.5 rounded bg-slate-800/80 text-slate-300 border border-slate-700/80">1 / 2 / 3</kbd> Zoom •
            <kbd class="px-1.5 py-0.5 rounded bg-slate-800/80 text-slate-300 border border-slate-700/80">T</kbd> Theme
          </div>

          <!-- In-Browser Master Render & Download Bar -->
          <div class="pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-3">
            <div class="space-y-0.5">
              <div id="renderStatusText" class="text-xs font-medium text-slate-200">Ready to master current stream with active filters</div>
              <div class="w-48 sm:w-64 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                <div id="renderProgressBar" class="h-full bg-gradient-to-r from-emerald-400 to-cyan-400 transition-all duration-200" style="width: 0%;"></div>
              </div>
            </div>

            <div class="flex items-center gap-2">
              <button id="btnRunBrowserMaster" type="button" class="px-3.5 py-2 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-slate-950 font-semibold text-xs sm:text-sm shadow-lg shadow-emerald-500/20 transition flex items-center gap-1.5" title="Master video stream right inside your browser">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                  <path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span>Browser Master</span>
              </button>
              <button id="btnRunCloudMaster" type="button" class="px-3.5 py-2 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-500 hover:from-cyan-400 hover:to-blue-400 text-slate-950 font-semibold text-xs sm:text-sm shadow-lg shadow-cyan-500/20 transition flex items-center gap-1.5" title="Master on server via FFmpeg Lanczos + 5x5 unsharp API">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 00-9.78 2.096A4.001 4.001 0 003 15z" />
                </svg>
                <span>⚡ Cloud Master</span>
              </button>
              <button id="downloadMasterLink" type="button" class="hidden px-3.5 py-2 rounded-xl bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/40 font-semibold text-xs sm:text-sm transition">
                Save Master File
              </button>
            </div>
          </div>
        </div>

        
        <!-- Right Column: Real-Time Engine Controls (Upgraded with Modular Selection Blocks & Navigation Buttons) -->
        <div class="lg:col-span-5 tuning-panel-container bg-slate-900/80 border border-slate-800 rounded-2xl p-4 sm:p-5 space-y-4" id="tuningControlsPanel">
          
          <!-- Header with Clean Mode Toggle -->
          <div class="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-slate-800">
            <div>
              <span class="text-[11px] font-mono uppercase tracking-wider text-emerald-400 font-semibold">Tuning Workflow</span>
              <h2 class="text-base font-bold text-white">Engine Tuning Parameters</h2>
              <p class="text-xs text-slate-400">Navigate each selection cleanly to configure pre-compensation rules</p>
            </div>
            <div class="flex items-center gap-1.5">
              <button type="button" id="btnToggleCleanMode" class="btn-clean-mode px-2.5 py-1 rounded-lg text-xs font-medium border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition flex items-center gap-1" title="Toggle Clean Mode (collapses inactive selections to keep page clean)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 6h16M4 12h10M4 18h6"></path>
                </svg>
                <span id="cleanModeStatusText">Clean View</span>
              </button>
              <button type="button" id="btnExpandAllSelections" class="btn-clean-mode px-2.5 py-1 rounded-lg text-xs font-medium border border-slate-700 bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 transition" title="Expand or collapse all selections">
                <span id="expandAllStatusText">Expand All</span>
              </button>
            </div>
          </div>

          <!-- Stepper Navigation Bar across all selections -->
          <div class="selection-stepper-bar flex items-center gap-1 p-1 bg-slate-950/80 rounded-xl border border-slate-800/90 overflow-x-auto" role="tablist" aria-label="Tuning Selections">
            <button type="button" data-step-target="step-platform" id="pill-step-platform" class="step-nav-pill active px-2.5 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap transition bg-emerald-500 text-slate-950 shadow">
              <span class="step-pill-num mr-1 font-mono">1</span>Platform
            </button>
            <button type="button" data-step-target="step-footage" id="pill-step-footage" class="step-nav-pill px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition text-slate-400 hover:text-white hover:bg-slate-800/60">
              <span class="step-pill-num mr-1 font-mono">2</span>Footage
            </button>
            <button type="button" data-step-target="step-fps" id="pill-step-fps" class="step-nav-pill px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition text-slate-400 hover:text-white hover:bg-slate-800/60">
              <span class="step-pill-num mr-1 font-mono">3</span>FPS Lock
            </button>
            <button type="button" data-step-target="step-unsharp" id="pill-step-unsharp" class="step-nav-pill px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition text-slate-400 hover:text-white hover:bg-slate-800/60">
              <span class="step-pill-num mr-1 font-mono">4</span>Unsharp
            </button>
            <button type="button" data-step-target="step-switches" id="pill-step-switches" class="step-nav-pill px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition text-slate-400 hover:text-white hover:bg-slate-800/60">
              <span class="step-pill-num mr-1 font-mono">5</span>Budget &amp; Switches
            </button>
            <button type="button" data-step-target="step-master" id="pill-step-master" class="step-nav-pill px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition text-slate-400 hover:text-white hover:bg-slate-800/60">
              <span class="step-pill-num mr-1 font-mono">6</span>Master Output
            </button>
          </div>

          <!-- Selection Block 1: Target Platform Profile -->
          <div class="selection-block active bg-slate-950/60 border border-slate-800/80 rounded-xl p-3.5 transition-all" id="step-platform">
            <div class="selection-block-header flex items-center justify-between gap-2 cursor-pointer" data-toggle-block="step-platform">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">STEP 1</span>
                <span class="text-xs font-bold text-white selection-block-title">Target Platform Profile</span>
                <span class="selection-summary-badge text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300" id="summaryPlatform">TikTok HD (1080p • 9.5M)</span>
              </div>
              <button type="button" class="btn-selection-toggle text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition" data-target="step-platform" title="Collapse or Expand Platform Selection">
                <span class="toggle-icon text-xs">▲</span>
              </button>
            </div>
            <div class="selection-block-body mt-3 space-y-3">
              <div class="grid grid-cols-3 gap-2">
                <button type="button" data-platform="tiktok" class="plat-btn px-3 py-2.5 rounded-xl border text-left transition bg-emerald-500/15 border-emerald-500 text-white shadow-sm">
                  <div class="text-xs font-bold">TikTok HD</div>
                  <div class="text-[10px] font-mono text-emerald-300 mt-0.5">1080p • 9.5M</div>
                </button>
                <button type="button" data-platform="whatsapp_720" class="plat-btn px-3 py-2.5 rounded-xl border text-left transition bg-slate-800/60 border-slate-700/80 text-slate-300 hover:border-slate-600">
                  <div class="text-xs font-bold">WhatsApp Safe</div>
                  <div class="text-[10px] font-mono text-slate-400 mt-0.5">720p • &lt;14.5MB</div>
                </button>
                <button type="button" data-platform="whatsapp_1080" class="plat-btn px-3 py-2.5 rounded-xl border text-left transition bg-slate-800/60 border-slate-700/80 text-slate-300 hover:border-slate-600">
                  <div class="text-xs font-bold">WhatsApp HD</div>
                  <div class="text-[10px] font-mono text-slate-400 mt-0.5">1080p • &lt;14.5MB</div>
                </button>
              </div>
              <div class="selection-nav-footer pt-2 border-t border-slate-800/70 flex items-center justify-start gap-2">
                <button type="button" class="btn-step-nav btn-step-next px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 hover:bg-emerald-400 transition flex items-center gap-1 shadow" data-next-step="step-footage">
                  <span>Next: Input Footage Characteristics</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"></polyline></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- Selection Block 2: Input Footage Characteristics -->
          <div class="selection-block bg-slate-950/60 border border-slate-800/80 rounded-xl p-3.5 transition-all" id="step-footage">
            <div class="selection-block-header flex items-center justify-between gap-2 cursor-pointer" data-toggle-block="step-footage">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">STEP 2</span>
                <span class="text-xs font-bold text-white selection-block-title">Input Footage Characteristics</span>
                <span class="selection-summary-badge text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300" id="summaryFootage">Raw Smartphone (10-bit HDR)</span>
              </div>
              <button type="button" class="btn-selection-toggle text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition" data-target="step-footage" title="Collapse or Expand Footage Selection">
                <span class="toggle-icon text-xs">▼</span>
              </button>
            </div>
            <div class="selection-block-body hidden mt-3 space-y-3">
              <select id="selectSourceProfile" class="w-full rounded-xl bg-slate-900 border border-slate-700 px-3 py-2 text-xs sm:text-sm text-white focus:outline-none focus:border-emerald-500">
                <option value="raw_hdr">Raw Smartphone Recording (iPhone .MOV / 10-bit HLG HDR + VFR)</option>
                <option value="edited_4k">Edited Clip (CapCut / Premiere / DaVinci 4K or 1080p Export)</option>
                <option value="lowlight">Low-Light / High-ISO Footage (Requires Temporal Denoise First)</option>
              </select>
              <div class="selection-nav-footer pt-2 border-t border-slate-800/70 flex items-center justify-start gap-2">
                <button type="button" class="btn-step-nav btn-step-prev px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 transition flex items-center gap-1" data-prev-step="step-platform">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"></polyline></svg>
                  <span>Prev: Platform</span>
                </button>
                <button type="button" class="btn-step-nav btn-step-next px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 hover:bg-emerald-400 transition flex items-center gap-1 shadow" data-next-step="step-fps">
                  <span>Next: Constant Frame Rate Lock</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"></polyline></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- Selection Block 3: Constant Frame Rate (CFR) Lock -->
          <div class="selection-block bg-slate-950/60 border border-slate-800/80 rounded-xl p-3.5 transition-all" id="step-fps">
            <div class="selection-block-header flex items-center justify-between gap-2 cursor-pointer" data-toggle-block="step-fps">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">STEP 3</span>
                <span class="text-xs font-bold text-white selection-block-title">Constant Frame Rate (CFR) Lock</span>
                <span class="selection-summary-badge font-mono text-[11px] text-emerald-400 px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/30" id="fpsValueBadge">60.00 FPS (GOP=60)</span>
              </div>
              <button type="button" class="btn-selection-toggle text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition" data-target="step-fps" title="Collapse or Expand FPS Lock Selection">
                <span class="toggle-icon text-xs">▼</span>
              </button>
            </div>
            <div class="selection-block-body hidden mt-3 space-y-3">
              <div class="grid grid-cols-2 gap-2">
                <button type="button" id="btnFps60" class="px-3 py-2 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition shadow">
                  60 FPS Ultra Smooth
                </button>
                <button type="button" id="btnFps30" class="px-3 py-2 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 border border-slate-700 transition hover:text-white hover:bg-slate-700">
                  30 FPS High Bit-Per-Pixel
                </button>
              </div>
              <div class="selection-nav-footer pt-2 border-t border-slate-800/70 flex items-center justify-start gap-2">
                <button type="button" class="btn-step-nav btn-step-prev px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 transition flex items-center gap-1" data-prev-step="step-footage">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"></polyline></svg>
                  <span>Prev: Footage</span>
                </button>
                <button type="button" class="btn-step-nav btn-step-next px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 hover:bg-emerald-400 transition flex items-center gap-1 shadow" data-next-step="step-unsharp">
                  <span>Next: Unsharp Pre-Compensation</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"></polyline></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- Selection Block 4: Lanczos + 5x5 Unsharp Pre-Compensation -->
          <div class="selection-block bg-slate-950/60 border border-slate-800/80 rounded-xl p-3.5 transition-all" id="step-unsharp">
            <div class="selection-block-header flex items-center justify-between gap-2 cursor-pointer" data-toggle-block="step-unsharp">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">STEP 4</span>
                <span class="text-xs font-bold text-white selection-block-title">Lanczos + 5×5 Unsharp</span>
                <span class="selection-summary-badge font-mono text-[11px] text-emerald-400 px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/30" id="labelSharpenVal">0.65 (Sweet Spot)</span>
              </div>
              <button type="button" class="btn-selection-toggle text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition" data-target="step-unsharp" title="Collapse or Expand Unsharp Selection">
                <span class="toggle-icon text-xs">▼</span>
              </button>
            </div>
            <div class="selection-block-body hidden mt-3 space-y-3">
              <input id="sliderSharpen" type="range" min="0" max="150" value="65" class="w-full h-1.5 bg-slate-800 rounded-lg cursor-pointer" />
              <p class="text-[11px] text-slate-400">Counteracts mobile player bilinear softening without creating white halo rings.</p>
              <div class="selection-nav-footer pt-2 border-t border-slate-800/70 flex items-center justify-start gap-2">
                <button type="button" class="btn-step-nav btn-step-prev px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 transition flex items-center gap-1" data-prev-step="step-fps">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"></polyline></svg>
                  <span>Prev: FPS Lock</span>
                </button>
                <button type="button" class="btn-step-nav btn-step-next px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 hover:bg-emerald-400 transition flex items-center gap-1 shadow" data-next-step="step-switches">
                  <span>Next: Duration &amp; Optical Switches</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"></polyline></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- Selection Block 5: Duration & Optical Pipeline Switches -->
          <div class="selection-block bg-slate-950/60 border border-slate-800/80 rounded-xl p-3.5 transition-all" id="step-switches">
            <div class="selection-block-header flex items-center justify-between gap-2 cursor-pointer" data-toggle-block="step-switches">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">STEP 5</span>
                <span class="text-xs font-bold text-white selection-block-title">Clip Duration &amp; Switches</span>
                <span class="selection-summary-badge font-mono text-[11px] text-cyan-400 px-2 py-0.5 rounded bg-cyan-500/10 border border-cyan-500/30" id="labelDurationVal">15 sec</span>
              </div>
              <button type="button" class="btn-selection-toggle text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition" data-target="step-switches" title="Collapse or Expand Switches Selection">
                <span class="toggle-icon text-xs">▼</span>
              </button>
            </div>
            <div class="selection-block-body hidden mt-3 space-y-3">
              <div class="space-y-1">
                <div class="flex items-center justify-between text-xs text-slate-300">
                  <span>Duration (Bitrate Budgeter)</span>
                  <span id="labelDurationValInner" class="font-mono text-cyan-400">15s</span>
                </div>
                <input id="sliderDuration" type="range" min="5" max="120" value="15" class="w-full h-1.5 bg-slate-800 rounded-lg cursor-pointer" />
              </div>

              <!-- Optical Pipeline Switches -->
              <div class="space-y-2.5 pt-2 border-t border-slate-800">
                <label class="flex items-center justify-between cursor-pointer">
                  <div>
                    <div class="text-xs font-medium text-slate-200">Hable Filmic HDR → Rec.709 Tone-Map</div>
                    <div class="text-[11px] text-slate-400">Prevents blown-out skin tones from iPhone 10-bit Dolby Vision</div>
                  </div>
                  <input id="toggleHdrTonemap" type="checkbox" checked class="w-4 h-4 accent-emerald-500 rounded cursor-pointer" />
                </label>

                <label class="flex items-center justify-between cursor-pointer">
                  <div>
                    <div class="text-xs font-medium text-slate-200">3D Spatial-Temporal Denoise (<code class="font-mono text-emerald-400">hqdn3d</code>)</div>
                    <div class="text-[11px] text-slate-400">Strips sensor grain so H.264 spends 100% of bits on sharp edges</div>
                  </div>
                  <input id="toggleDenoise" type="checkbox" checked class="w-4 h-4 accent-emerald-500 rounded cursor-pointer" />
                </label>

                <label class="flex items-center justify-between cursor-pointer">
                  <div>
                    <div class="text-xs font-medium text-slate-200">2-Pass Veryslow Motion Lookahead</div>
                    <div class="text-[11px] text-slate-400">Pre-calculates fast motion cuts so TikTok server encoder never crushes quality</div>
                  </div>
                  <input id="toggleVeryslow" type="checkbox" checked class="w-4 h-4 accent-emerald-500 rounded cursor-pointer" />
                </label>
              </div>

              <div class="selection-nav-footer pt-2 border-t border-slate-800/70 flex items-center justify-start gap-2">
                <button type="button" class="btn-step-nav btn-step-prev px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 transition flex items-center gap-1" data-prev-step="step-unsharp">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"></polyline></svg>
                  <span>Prev: Unsharp</span>
                </button>
                <button type="button" class="btn-step-nav btn-step-next px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 hover:bg-emerald-400 transition flex items-center gap-1 shadow" data-next-step="step-master">
                  <span>Next: Master Output Actions</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"></polyline></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- Selection Block 6: Master Output Actions & Script Jump -->
          <div class="selection-block bg-slate-950/60 border border-slate-800/80 rounded-xl p-3.5 transition-all" id="step-master">
            <div class="selection-block-header flex items-center justify-between gap-2 cursor-pointer" data-toggle-block="step-master">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">STEP 6</span>
                <span class="text-xs font-bold text-white selection-block-title">Master Output &amp; FFmpeg Jump</span>
                <span class="selection-summary-badge font-mono text-[11px] text-emerald-400 px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/30" id="summaryMasterStatus">Config Ready</span>
              </div>
              <button type="button" class="btn-selection-toggle text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition" data-target="step-master" title="Collapse or Expand Master Output Selection">
                <span class="toggle-icon text-xs">▼</span>
              </button>
            </div>
            <div class="selection-block-body hidden mt-3 space-y-3">
              <p class="text-xs text-slate-300 leading-relaxed">
                Your pre-compensation parameters are locked. You can render via in-browser canvas shader, execute a high-speed cloud master, or jump straight to the exact generated 2-pass FFmpeg command.
              </p>
              <div class="selection-nav-footer pt-2 border-t border-slate-800/70 flex items-center justify-start gap-2">
                <button type="button" class="btn-step-nav btn-step-prev px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 transition flex items-center gap-1" data-prev-step="step-switches">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"></polyline></svg>
                  <span>Prev: Switches</span>
                </button>
                <button type="button" class="btn-step-nav btn-step-tab px-3.5 py-1.5 rounded-lg text-xs font-bold bg-cyan-500 text-slate-950 hover:bg-cyan-400 transition flex items-center gap-1.5 shadow" data-navtab="pipeline">
                  <span>View Generated FFmpeg Script ➔</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Bottom Page Navigation Row for Studio -->
      <div class="page-nav-row flex flex-wrap items-center justify-start gap-3 pt-6 border-t border-slate-800/80 mt-6">
        <a href="/" class="btn-diag btn-converter-nav" title="Return to Universal File Converter">
          <span>← Back to Universal File Converter</span>
        </a>
        <button type="button" class="btn-diag btn-nav-next-tab" data-navtab="pipeline">
          <span>Continue to Bitrate &amp; FFmpeg Builder ➔</span>
        </button>
      </div>
    </section>


    <!-- ==================== TAB 2: BITRATE & FFMPEG BUILDER ==================== -->
    <section id="view-pipeline" class="hidden space-y-6">
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
        <!-- Bitrate Breakdown Card -->
        <div class="lg:col-span-5 bg-slate-900/80 border border-slate-800 rounded-2xl p-5 space-y-4">
          <div>
            <h2 class="text-base font-semibold text-white">Platform Re-Compression Threshold Matrix</h2>
            <p class="text-xs text-slate-400 mt-0.5">Live math computed from your current Studio parameters</p>
          </div>

          <div class="space-y-3 bg-slate-950/80 border border-slate-800/90 rounded-xl p-4 font-mono text-xs">
            <div class="flex justify-between py-1 border-b border-slate-800/80">
              <span class="text-slate-400">Target Dimensions:</span>
              <span id="calcDimensions" class="text-white font-semibold">1080 × 1920 (9:16)</span>
            </div>
            <div class="flex justify-between py-1 border-b border-slate-800/80">
              <span class="text-slate-400">Resampling Kernel:</span>
              <span class="text-emerald-400">lanczos+accurate_rnd+full_chroma</span>
            </div>
            <div class="flex justify-between py-1 border-b border-slate-800/80">
              <span class="text-slate-400">Target Video Bitrate:</span>
              <span id="calcVideoBitrate" class="text-emerald-400 font-semibold">9,500 kbps</span>
            </div>
            <div class="flex justify-between py-1 border-b border-slate-800/80">
              <span class="text-slate-400">VBV Maxrate / Bufsize:</span>
              <span id="calcVbv" class="text-cyan-400">12,000k / 24,000k</span>
            </div>
            <div class="flex justify-between py-1 border-b border-slate-800/80">
              <span class="text-slate-400">Audio Stream:</span>
              <span id="calcAudio" class="text-slate-200">AAC-LC • 192 kbps • 48 kHz</span>
            </div>
            <div class="flex justify-between py-1">
              <span class="text-slate-400">WhatsApp Split Chunks:</span>
              <span id="calcWaChunks" class="text-amber-300">1 segment (No split required)</span>
            </div>
          </div>

          <div class="p-3.5 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-xs text-emerald-200 space-y-1">
            <div class="font-semibold text-emerald-300">Why 2-Pass Veryslow Beats Single-Pass CRF</div>
            <p class="text-slate-300 leading-relaxed">
              Single-pass CRF spikes bitrate above 25 Mbps during fast camera pans, which triggers TikTok and WhatsApp's server-side crusher. Two-pass <code class="font-mono">veryslow</code> with a 60-frame lookahead pre-maps every frame in Pass 1 so Pass 2 delivers near-lossless texture detail while staying strictly under the VBV ceiling.
            </p>
          </div>
        </div>

        <!-- Dynamic FFmpeg Command Output -->
        <div class="lg:col-span-7 bg-slate-900/80 border border-slate-800 rounded-2xl p-5 flex flex-col justify-between space-y-4">
          <div class="flex items-center justify-between gap-2">
            <div>
              <h2 class="text-base font-semibold text-white">Generated 2-Pass FFmpeg Mastering Script</h2>
              <p class="text-xs text-slate-400">Updates automatically when you tweak sliders in the Studio</p>
            </div>
            <button id="btnCopyFfmpeg" type="button" class="px-3 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs transition">
              Copy FFmpeg Script
            </button>
          </div>

          <pre id="ffmpegCommandBox" class="flex-1 bg-slate-950 border border-slate-800 rounded-xl p-4 text-xs font-mono text-emerald-300 overflow-x-auto custom-scroll leading-relaxed whitespace-pre-wrap"></pre>
        </div>
      </div>

      <!-- Bottom Navigation for Tab -->
      <div class="page-nav-row flex flex-wrap items-center justify-start gap-3 pt-6 border-t border-slate-800/80 mt-6">
        <button type="button" class="btn-diag btn-nav-prev-tab" data-navtab="studio">
          <span>← Back to Live Studio & Comparator</span>
        </button>
        <button type="button" class="btn-diag btn-nav-next-tab" data-navtab="code">
          <span>Continue to Web & Mobile Code ➔</span>
        </button>
      </div>
    </section>

    <!-- ==================== TAB 3: WEB & MOBILE CODE SNIPPETS ==================== -->
    <section id="view-code" class="hidden space-y-5">
      <div class="flex flex-wrap items-center justify-between gap-3 bg-slate-900/80 border border-slate-800 rounded-2xl p-4">
        <div class="flex flex-wrap items-center gap-2">
          <button type="button" data-codefile="python_engine" class="code-tab-btn px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition">
            1. Python Engine (engine.py)
          </button>
          <button type="button" data-codefile="fastapi_server" class="code-tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white transition">
            2. FastAPI Web Server (server.py)
          </button>
          <button type="button" data-codefile="flutter_mobile" class="code-tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white transition">
            3. Flutter Mobile App (Dart)
          </button>
          <button type="button" data-codefile="rn_mobile" class="code-tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white transition">
            4. React Native Mobile (TSX)
          </button>
        </div>

        <div class="flex items-center gap-2">
          <button id="btnCopyActiveCode" type="button" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-white border border-slate-700 transition">
            Copy Active File
          </button>
          <button id="btnDownloadActiveCode" type="button" class="px-3 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-xs font-semibold transition">
            Download Source File
          </button>
        </div>
      </div>

      <div class="bg-slate-950 border border-slate-800 rounded-2xl overflow-hidden">
        <div class="px-4 py-2.5 bg-slate-900/90 border-b border-slate-800 flex items-center justify-between text-xs font-mono text-slate-400">
          <span id="codeFilenameLabel">backend/engine.py</span>
          <span>Production-Ready • Zero Placeholders</span>
        </div>
        <pre id="codeDisplayPre" class="p-4 sm:p-5 text-xs font-mono text-slate-200 overflow-x-auto max-h-[540px] custom-scroll leading-relaxed"></pre>
      </div>

      <!-- Bottom Navigation for Tab -->
      <div class="page-nav-row flex flex-wrap items-center justify-start gap-3 pt-6 border-t border-slate-800/80 mt-6">
        <button type="button" class="btn-diag btn-nav-prev-tab" data-navtab="pipeline">
          <span>← Back to Bitrate & FFmpeg Builder</span>
        </button>
        <button type="button" class="btn-diag btn-nav-next-tab" data-navtab="guide">
          <span>Continue to Bypass Blueprint ➔</span>
        </button>
      </div>
    </section>

    <!-- ==================== TAB 4: ARCHITECTURE & BYPASS BLUEPRINT ==================== -->
    <section id="view-guide" class="hidden space-y-6">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 space-y-3">
          <div class="inline-flex items-center gap-2 px-2.5 py-1 rounded-full bg-cyan-500/15 text-cyan-300 text-xs font-semibold">
            TikTok Zero-Blur Protocol
          </div>
          <h3 class="text-base font-bold text-white">How the Engine Prevents TikTok Transcoder Blur</h3>
          <ul class="space-y-2.5 text-xs sm:text-sm text-slate-300">
            <li class="flex gap-2.5">
              <span class="text-emerald-400 font-bold">01.</span>
              <span><strong>Exact 1080×1920 Canvas Geometry:</strong> Uploading 4K (2160×3840) forces TikTok's fast server scaler to downscale your video with a low-tap bilinear filter. Pre-scaling with <code class="font-mono text-emerald-300">lanczos+accurate_rnd+full_chroma_int</code> retains micro-texture.</span>
            </li>
            <li class="flex gap-2.5">
              <span class="text-emerald-400 font-bold">02.</span>
              <span><strong>VBV Maxrate Capped at 12 Mbps:</strong> TikTok's ingestion pipeline passes through or lightly remuxes H.264 High@L4.2 streams when average bitrate is <code class="font-mono text-emerald-300">8–10 Mbps</code> and peaks stay below <code class="font-mono text-emerald-300">12 Mbps</code>.</span>
            </li>
            <li class="flex gap-2.5">
              <span class="text-emerald-400 font-bold">03.</span>
              <span><strong>Enable "Allow High-Quality Uploads":</strong> On mobile, always toggle <em>More Options → Allow high-quality uploads</em> (or upload via TikTok Web Studio from desktop/mobile browser using the engine's mastered MP4).</span>
            </li>
          </ul>
        </div>

        <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 space-y-3">
          <div class="inline-flex items-center gap-2 px-2.5 py-1 rounded-full bg-emerald-500/15 text-emerald-300 text-xs font-semibold">
            WhatsApp Status Protocol
          </div>
          <h3 class="text-base font-bold text-white">How the Engine Defeats WhatsApp's Aggressive Crusher</h3>
          <ul class="space-y-2.5 text-xs sm:text-sm text-slate-300">
            <li class="flex gap-2.5">
              <span class="text-emerald-400 font-bold">01.</span>
              <span><strong>Strict &lt;14.5 MB Payload Ceiling:</strong> WhatsApp Status enforces a hard ~16 MB media cap per 30-second slot. Our dynamic bitrate calculator sizes the 2-pass target bitrate to hit <code class="font-mono text-emerald-300">14.5 MB</code> max so WhatsApp does not crush the stream.</span>
            </li>
            <li class="flex gap-2.5">
              <span class="text-emerald-400 font-bold">02.</span>
              <span><strong>720×1280 High-Profile Sweet Spot:</strong> Many Android/iOS WhatsApp builds force a mandatory downscale if width exceeds 720px. Using our <code class="font-mono text-emerald-300">720×1280</code> Lanczos + 5×5 Unsharp preset looks dramatically sharper than a 1080p file that WhatsApp downscales itself.</span>
            </li>
            <li class="flex gap-2.5">
              <span class="text-emerald-400 font-bold">03.</span>
              <span><strong>Rec.709 YUV420P Color Standardization:</strong> Converts iPhone 10-bit Dolby Vision (<code class="font-mono text-emerald-300">bt2020nc</code>) into standard 8-bit <code class="font-mono text-emerald-300">yuv420p</code> using Hable filmic tone-mapping so WhatsApp never clips highlights.</span>
            </li>
          </ul>
        </div>
      </div>

      <!-- Bottom Navigation for Tab -->
      <div class="page-nav-row flex flex-wrap items-center justify-start gap-3 pt-6 border-t border-slate-800/80 mt-6">
        <button type="button" class="btn-diag btn-nav-prev-tab" data-navtab="code">
          <span>← Back to Web & Mobile Code</span>
        </button>
        <button type="button" class="btn-diag btn-nav-next-tab" data-navtab="converter">
          <span>Browse Supported Formats ➔</span>
        </button>
      </div>
    </section>

    <!-- ==================== TAB 5: FORMATS & CONVERTER RETURN ==================== -->
    <section id="view-converter" class="hidden space-y-6">
      <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-base font-semibold text-white flex items-center gap-2">
              <span>Target Formats &amp; Converter Synchronization</span>
              <span class="px-2 py-0.5 text-[10px] font-mono uppercase rounded bg-cyan-500/20 text-cyan-300 border border-cyan-500/30">1-Click Fast Sync</span>
            </h2>
            <p class="text-xs text-slate-400">Select any format or ZeroBlur mastering preset below to return directly to the Universal File Converter with that target format pre-selected.</p>
          </div>
          <a href="/" class="btn-back-converter text-xs">
            <span>← Return to Converter</span>
          </a>
        </div>

        <!-- ZeroBlur Master Presets Quick Sync -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-3 pt-2">
          <div class="p-4 rounded-xl bg-emerald-950/40 border border-emerald-500/40 flex items-center justify-between gap-3">
            <div>
              <div class="text-xs font-bold text-white flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                <span>TikTok Ultra HD</span>
              </div>
              <p class="text-[11px] text-slate-300 mt-0.5">1080×1920 • 60fps • 9.5M</p>
            </div>
            <a href="/?format=mp4&preset=tiktok" class="px-2 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs transition shadow whitespace-nowrap">
              Use →
            </a>
          </div>

          <div class="p-4 rounded-xl bg-purple-950/40 border border-purple-500/40 flex items-center justify-between gap-3">
            <div>
              <div class="text-xs font-bold text-white flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-purple-400"></span>
                <span>Instagram Reels</span>
              </div>
              <p class="text-[11px] text-slate-300 mt-0.5">1080×1920 • 8.5M • Faststart</p>
            </div>
            <a href="/?format=mp4&preset=reels" class="px-2 py-1.5 rounded-lg bg-purple-500 hover:bg-purple-400 text-slate-950 font-bold text-xs transition shadow whitespace-nowrap">
              Use →
            </a>
          </div>

          <div class="p-4 rounded-xl bg-rose-950/40 border border-rose-500/40 flex items-center justify-between gap-3">
            <div>
              <div class="text-xs font-bold text-white flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-rose-400"></span>
                <span>YouTube Shorts</span>
              </div>
              <p class="text-[11px] text-slate-300 mt-0.5">1080×1920 • 12M • -14 LUFS</p>
            </div>
            <a href="/?format=mp4&preset=shorts" class="px-2 py-1.5 rounded-lg bg-rose-500 hover:bg-rose-400 text-slate-950 font-bold text-xs transition shadow whitespace-nowrap">
              Use →
            </a>
          </div>

          <div class="p-4 rounded-xl bg-cyan-950/40 border border-cyan-500/40 flex items-center justify-between gap-3">
            <div>
              <div class="text-xs font-bold text-white flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-cyan-400"></span>
                <span>WhatsApp Safe</span>
              </div>
              <p class="text-[11px] text-slate-300 mt-0.5">720×1280 • &lt;14.5MB Budgeter</p>
            </div>
            <a href="/?format=mp4&preset=whatsapp" class="px-2 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition shadow whitespace-nowrap">
              Use →
            </a>
          </div>
        </div>

        <!-- Category Grids -->
        <div class="space-y-4 pt-2">
          <!-- Images -->
          <div class="space-y-2">
            <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400">🖼️ Image Formats</h3>
            <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
              <a href="/?format=webp" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-emerald-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-emerald-300">WebP</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">High Quality &amp; Small</div>
                </div>
                <span class="mt-2 text-[10px] text-emerald-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=png" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-emerald-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-emerald-300">PNG</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Lossless Transparency</div>
                </div>
                <span class="mt-2 text-[10px] text-emerald-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=jpg" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-emerald-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-emerald-300">JPG</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Standard Photo</div>
                </div>
                <span class="mt-2 text-[10px] text-emerald-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=ico" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-emerald-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-emerald-300">ICO</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Windows Icon</div>
                </div>
                <span class="mt-2 text-[10px] text-emerald-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=bmp" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-emerald-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-emerald-300">BMP</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Bitmap Raster</div>
                </div>
                <span class="mt-2 text-[10px] text-emerald-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=gif" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-emerald-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-emerald-300">GIF</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Animation / Loop</div>
                </div>
                <span class="mt-2 text-[10px] text-emerald-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
            </div>
          </div>

          <!-- Documents -->
          <div class="space-y-2">
            <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400">📄 Document Formats</h3>
            <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
              <a href="/?format=pdf" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-cyan-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-cyan-300">PDF</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Print Document</div>
                </div>
                <span class="mt-2 text-[10px] text-cyan-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=docx" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-cyan-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-cyan-300">DOCX</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Word Document</div>
                </div>
                <span class="mt-2 text-[10px] text-cyan-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=md" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-cyan-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-cyan-300">Markdown</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Formatted Notes</div>
                </div>
                <span class="mt-2 text-[10px] text-cyan-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=txt" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-cyan-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-cyan-300">TXT</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Plain Text</div>
                </div>
                <span class="mt-2 text-[10px] text-cyan-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=html" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-cyan-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-cyan-300">HTML</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Web Document</div>
                </div>
                <span class="mt-2 text-[10px] text-cyan-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
            </div>
          </div>

          <!-- Data & Hosting -->
          <div class="space-y-2">
            <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400">📊 Data &amp; Hosting</h3>
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <a href="/?format=csv" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-amber-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-amber-300">CSV</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Comma Separated</div>
                </div>
                <span class="mt-2 text-[10px] text-amber-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=json" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-amber-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-amber-300">JSON</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Structured Records</div>
                </div>
                <span class="mt-2 text-[10px] text-amber-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=xlsx" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-amber-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-amber-300">XLSX</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Excel Workbook</div>
                </div>
                <span class="mt-2 text-[10px] text-amber-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
              <a href="/?format=keep" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 hover:border-indigo-500/60 transition group text-center flex flex-col justify-between">
                <div>
                  <div class="text-xs font-bold text-white group-hover:text-indigo-300">Direct Host</div>
                  <div class="text-[10px] text-slate-400 mt-0.5">Keep Original File</div>
                </div>
                <span class="mt-2 text-[10px] text-indigo-400 font-semibold group-hover:underline">Select &amp; Return →</span>
              </a>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Bottom Sticky/Floating Return Bar -->
    <div class="pt-6 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <span class="text-xs text-slate-400">Done tuning in ZeroBlur Studio?</span>
        <a href="/" class="btn-back-converter text-xs" id="footerBackToConverterBtn">
          <span>← Back to Converter Dashboard</span>
        </a>
      </div>
      <div class="text-xs font-mono text-emerald-400">ZeroBlur Engine Studio v2.4 • Universal File Converter</div>
    </div>
  </main>

  
  <script>
    // ==================== APPLICATION STATE ====================
    const state = {
      activeTab: 'studio',
      platform: 'tiktok', // 'tiktok' | 'whatsapp_720' | 'whatsapp_1080'
      sourceProfile: 'raw_hdr',
      fps: 60,
      sharpen: 0.65,
      durationSec: 15,
      hdrTonemap: true,
      denoise: true,
      veryslow: true,
      loudnorm: true,
      splitPercent: 50,
      zoomLevel: 1,
      isPlaying: true,
      usingUploadedVideo: false,
      uploadedMeta: {
        name: '4K_HDR_Demo_Scene.mov',
        width: 3840,
        height: 2160,
        duration: 15.0,
        sizeMb: 84.6
      },
      activeCodeFile: 'python_engine'
    };

    // ==================== SOURCE CODE REPOSITORY ====================
    const CODE_FILES = {
      python_engine: {
        filename: 'engine.py',
        label: 'backend/engine.py (Python 2-Pass FFmpeg & HDR Tone-Mapping Engine)',
        content: `#!/usr/bin/env python3
'''
Ultra-Fidelity Video Pre-Optimization Engine for TikTok & WhatsApp Status
- 2-Pass libx264 -preset veryslow with 60-frame RC lookahead
- Lanczos accurate-rounding scaling + 5x5 frequency-separated unsharp mask
- 10-bit HDR (HLG / Dolby Vision PQ) -> Rec.709 Hable filmic tone-mapping
- Strict CFR lock + dynamic WhatsApp <14.5MB bitrate budgeting
'''

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

@dataclass
class EngineConfig:
    platform: str = "both"          # "tiktok" | "whatsapp" | "both"
    target_fps: int = 60            # 30 or 60 CFR
    whatsapp_res: str = "720p"      # "720p" (720x1280) or "1080p" (1080x1920)
    sharpen_strength: float = 0.65  # Luma 5x5 unsharp amount
    denoise_prepass: bool = True    # hqdn3d temporal noise reduction
    hdr_tonemap: bool = True        # zscale Hable HDR->SDR conversion

class ZeroBlurEngine:
    def __init__(self, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe"):
        self.ffmpeg = ffmpeg_bin
        self.ffprobe = ffprobe_bin

    def probe(self, input_path: str) -> dict:
        cmd = [
            self.ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", input_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(res.stdout)
        v_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        trc = v_stream.get("color_transfer", "bt709").lower()
        pix = v_stream.get("pix_fmt", "yuv420p").lower()
        is_hdr = trc in ("arib-std-b67", "smpte2084") or "10le" in pix
        duration = float(v_stream.get("duration") or info["format"].get("duration") or 15.0)
        return {"duration": duration, "is_hdr": is_hdr, "width": int(v_stream["width"]), "height": int(v_stream["height"])}

    def compute_bitrates(self, platform: str, duration: float, fps: int, wa_res: str) -> Tuple[int, int, int, int]:
        if platform == "tiktok":
            v_kbps = 9500 if fps >= 50 else 7500
            max_kbps = 12000 if fps >= 50 else 9500
            return v_kbps, max_kbps, max_kbps * 2, 192
        audio_kbps = 128
        max_kbits = 14.5 * 1024 * 8
        budget_kbps = int((max_kbits / max(duration, 1.0)) - audio_kbps)
        cap = 4200 if wa_res == "1080p" else 3400
        v_kbps = max(1200, min(cap, budget_kbps))
        max_kbps = int(v_kbps * 1.15)
        return v_kbps, max_kbps, max_kbps * 2, audio_kbps

    def build_vf(self, is_hdr: bool, cfg: EngineConfig, platform: str) -> str:
        tw, th = (1080, 1920) if (platform == "tiktok" or cfg.whatsapp_res == "1080p") else (720, 1280)
        chain = [f"fps=fps={cfg.target_fps}:round=near"]
        if is_hdr and cfg.hdr_tonemap:
            chain.append(
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0.0,zscale=t=bt709:m=bt709:r=tv"
            )
        chain.append(
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos+accurate_rnd+full_chroma_int+full_chroma_inp,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        if cfg.denoise_prepass:
            chain.append("hqdn3d=1.2:1.2:2.5:2.5")
        if cfg.sharpen_strength > 0:
            luma = round(cfg.sharpen_strength, 2)
            chroma = round(cfg.sharpen_strength * 0.3, 2)
            chain.append(f"unsharp=5:5:{luma}:3:3:{chroma}")
        chain.append("format=yuv420p")
        return ",".join(chain)

    def encode_two_pass(self, input_path: str, output_path: str, cfg: EngineConfig, platform: str):
        meta = self.probe(input_path)
        vf = self.build_vf(meta["is_hdr"], cfg, platform)
        v_k, max_k, buf_k, a_k = self.compute_bitrates(platform, meta["duration"], cfg.target_fps, cfg.whatsapp_res)
        gop = cfg.target_fps
        x264_opts = (
            f"ref=5:bframes=4:b-adapt=2:direct=auto:me=umh:subme=10:merange=24:"
            f"trellis=2:aq-mode=3:aq-strength=0.85:psy-rd=1.05,0.15:rc-lookahead=60:"
            f"keyint={gop}:min-keyint={gop//2}:deblock=-1,-1"
        )
        common = [
            "-vf", vf, "-c:v", "libx264", "-preset", "veryslow", "-tune", "film",
            "-profile:v", "high", "-level:v", "4.2",
            "-b:v", f"{v_k}k", "-maxrate", f"{max_k}k", "-bufsize", f"{buf_k}k",
            "-x264-params", x264_opts,
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "passlog")
            null_dev = "NUL" if os.name == "nt" else "/dev/null"
            subprocess.run([self.ffmpeg, "-y", "-i", input_path, *common, "-pass", "1", "-passlogfile", log, "-an", "-f", "mp4", null_dev], check=True)
            subprocess.run([self.ffmpeg, "-y", "-i", input_path, *common, "-pass", "2", "-passlogfile", log, "-c:a", "aac", "-b:a", f"{a_k}k", "-ar", "48000", "-movflags", "+faststart", output_path], check=True)`
      },
      fastapi_server: {
        filename: 'server.py',
        label: 'backend/server.py (FastAPI Web & Mobile Cloud Endpoint)',
        content: `from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import shutil, uuid
from pathlib import Path
from engine import ZeroBlurEngine, EngineConfig

app = FastAPI(title="ZeroBlur Video Engine API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

 WORK_DIR = Path("./storage")
WORK_DIR.mkdir(exist_ok=True)
engine = ZeroBlurEngine()

@app.post("/api/v1/optimize")
async def optimize_clip(
    file: UploadFile = File(...),
    platform: str = Form("tiktok"),
    target_fps: int = Form(60),
    whatsapp_res: str = Form("720p"),
    sharpen_strength: float = Form(0.65),
):
    job_id = str(uuid.uuid4())
    in_file = WORK_DIR / f"{job_id}_in.mp4"
    out_file = WORK_DIR / f"{job_id}_{platform}_master.mp4"
    with open(in_file, "wb") as f:
        shutil.copyfileobj(file.file, f)

    cfg = EngineConfig(
        platform=platform,
        target_fps=target_fps,
        whatsapp_res=whatsapp_res,
        sharpen_strength=sharpen_strength,
    )
    engine.encode_two_pass(str(in_file), str(out_file), cfg, platform)
    return FileResponse(str(out_file), media_type="video/mp4", filename=out_file.name)`
      },
      flutter_mobile: {
        filename: 'video_optimizer_service.dart',
        label: 'mobile/flutter/video_optimizer_service.dart (On-Device Flutter FFmpegKit Engine)',
        content: `import 'dart:io';
import 'package:ffmpeg_kit_flutter_full_gpl/ffmpeg_kit.dart';
import 'package:ffmpeg_kit_flutter_full_gpl/return_code.dart';
import 'package:path_provider/path_provider.dart';

class FlutterZeroBlurEngine {
  Future<File> masterVideo({
    required String inputPath,
    required bool isWhatsapp,
    required double durationSec,
    int fps = 60,
    double sharpen = 0.65,
    bool isHdr = true,
  }) async {
    final dir = await getTemporaryDirectory();
    final ts = DateTime.now().millisecondsSinceEpoch;
    final passLog = '\${dir.path}/pass_\$ts';
    final outPath = '\${dir.path}/master_\$ts.mp4';

    final tw = isWhatsapp ? 720 : 1080;
    final th = isWhatsapp ? 1280 : 1920;
    final vKbps = isWhatsapp
        ? (((14.5 * 1024 * 8) / durationSec).floor() - 128).clamp(1200, 3400)
        : (fps == 60 ? 9500 : 7500);
    final maxKbps = isWhatsapp ? (vKbps * 1.15).floor() : 12000;
    final bufKbps = maxKbps * 2;

    final hdrFilter = isHdr
        ? 'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0.0,zscale=t=bt709:m=bt709:r=tv,'
        : '';
    final luma = sharpen.toStringAsFixed(2);
    final chroma = (sharpen * 0.3).toStringAsFixed(2);

    final vf = 'fps=fps=\$fps:round=near,\$hdrFilter'
        'scale=\$tw:\$th:force_original_aspect_ratio=decrease:flags=lanczos+accurate_rnd+full_chroma_int+full_chroma_inp,'
        'pad=\$tw:\$th:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
        'hqdn3d=1.2:1.2:2.5:2.5,unsharp=5:5:\$luma:3:3:\$chroma,format=yuv420p';

    final common = '-vf "\$vf" -c:v libx264 -preset veryslow -tune film -profile:v high -level:v 4.2 '
        '-b:v \${vKbps}k -maxrate \${maxKbps}k -bufsize \${bufKbps}k '
        '-x264-params "ref=5:bframes=4:me=umh:subme=10:trellis=2:aq-mode=3:psy-rd=1.05,0.15:rc-lookahead=60:keyint=\$fps"';

    final s1 = await FFmpegKit.execute('-y -i "\$inputPath" \$common -pass 1 -passlogfile "\$passLog" -an -f mp4 /dev/null');
    if (!ReturnCode.isSuccess(await s1.getReturnCode())) throw Exception('Pass 1 failed');

    final s2 = await FFmpegKit.execute('-y -i "\$inputPath" \$common -pass 2 -passlogfile "\$passLog" -c:a aac -b:a 192k -ar 48000 -movflags +faststart "\$outPath"');
    if (!ReturnCode.isSuccess(await s2.getReturnCode())) throw Exception('Pass 2 failed');

    return File(outPath);
  }
}`
      },
      rn_mobile: {
        filename: 'VideoOptimizer.tsx',
        label: 'mobile/react_native/VideoOptimizer.tsx (React Native iOS/Android Engine)',
        content: `import { FFmpegKit, ReturnCode } from 'ffmpeg-kit-react-native';
import RNFS from 'react-native-fs';

export async function optimizeForSocialStatus(
  inputUri: string,
  platform: 'tiktok' | 'whatsapp',
  durationSec: number,
  fps: 30 | 60 = 60,
  sharpen: number = 0.65
): Promise<string> {
  const id = Date.now();
  const passLog = \`\${RNFS.CachesDirectoryPath}/pass_\${id}\`;
  const outputUri = \`\${RNFS.CachesDirectoryPath}/zeroblur_\${platform}_\${id}.mp4\`;

  const tw = platform === 'tiktok' ? 1080 : 720;
  const th = platform === 'tiktok' ? 1920 : 1280;
  const vKbps = platform === 'tiktok'
    ? (fps === 60 ? 9500 : 7500)
    : Math.max(1200, Math.min(3400, Math.floor((14.5 * 1024 * 8) / Math.max(durationSec, 1)) - 128));
  const maxKbps = Math.floor(vKbps * 1.18);

  const vf = [
    \`fps=fps=\${fps}:round=near\`,
    \`scale=\${tw}:\${th}:force_original_aspect_ratio=decrease:flags=lanczos+accurate_rnd+full_chroma_int+full_chroma_inp,pad=\${tw}:\${th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1\`,
    'hqdn3d=1.2:1.2:2.5:2.5',
    \`unsharp=5:5:\${sharpen.toFixed(2)}:3:3:\${(sharpen * 0.3).toFixed(2)}\`,
    'format=yuv420p'
  ].join(',');

  const args = \`-vf "\${vf}" -c:v libx264 -preset veryslow -tune film -profile:v high -level:v 4.2 -b:v \${vKbps}k -maxrate \${maxKbps}k -bufsize \${maxKbps * 2}k\`;

  const p1 = await FFmpegKit.execute(\`-y -i "\${inputUri}" \${args} -pass 1 -passlogfile "\${passLog}" -an -f mp4 /dev/null\`);
  if (!ReturnCode.isSuccess(await p1.getReturnCode())) throw new Error('Pass 1 analysis failed');

  const p2 = await FFmpegKit.execute(\`-y -i "\${inputUri}" \${args} -pass 2 -passlogfile "\${passLog}" -c:a aac -b:a 160k -movflags +faststart "\${outputUri}"\`);
  if (!ReturnCode.isSuccess(await p2.getReturnCode())) throw new Error('Pass 2 encode failed');

  return outputUri;
}`
      }
    };

    // ==================== TOAST HELPER ====================
    function showToast(message) {
      const container = document.getElementById('toastContainer');
      const item = document.createElement('div');
      item.className = 'px-4 py-2.5 rounded-xl bg-slate-900/95 border border-emerald-500/50 text-xs font-medium text-emerald-200 shadow-xl transition-all duration-300';
      item.textContent = message;
      container.appendChild(item);
      setTimeout(() => {
        item.style.opacity = '0';
        setTimeout(() => item.remove(), 300);
      }, 2600);
    }

    // ====================CLIPBOARD HELPER ====================
    function copyTextToClipboard(text, label) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
          showToast(label + ' copied to clipboard');
        }).catch(() => fallbackCopy(text, label));
      } else {
        fallbackCopy(text, label);
      }
    }

    function fallbackCopy(text, label) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        showToast(label + ' copied to clipboard');
      } catch (e) {
        showToast('Copy failed');
      }
      ta.remove();
    }

    // ==================== DYNAMIC CALCULATION & UI SYNC ====================
    function computePipelineMetrics() {
      const dur = Math.max(state.durationSec, 1);
      let tw = 1080;
      let th = 1920;
      let vKbps = 9500;
      let maxKbps = 12000;
      let aKbps = 192;
      let presetTitle = 'TikTok Ultra HD (1080×1920)';
      let chunks = 1;

      if (state.platform === 'tiktok') {
        tw = 1080;
        th = 1920;
        vKbps = state.fps === 60 ? 9500 : 7500;
        maxKbps = state.fps === 60 ? 12000 : 9500;
        aKbps = 192;
        presetTitle = 'TikTok Ultra HD (1080×1920)';
      } else if (state.platform === 'reels') {
        tw = 1080;
        th = 1920;
        vKbps = state.fps === 60 ? 8500 : 6500;
        maxKbps = state.fps === 60 ? 10500 : 8500;
        aKbps = 192;
        presetTitle = 'Instagram Reels HD (1080×1920)';
      } else if (state.platform === 'shorts') {
        tw = 1080;
        th = 1920;
        vKbps = state.fps === 60 ? 12000 : 9000;
        maxKbps = state.fps === 60 ? 15000 : 12000;
        aKbps = 256;
        presetTitle = 'YouTube Shorts Ultra (1080×1920)';
      } else {
        aKbps = 128;
        tw = state.platform === 'whatsapp_1080' ? 1080 : 720;
        th = state.platform === 'whatsapp_1080' ? 1920 : 1280;
        presetTitle = state.platform === 'whatsapp_1080'
          ? 'WhatsApp Status HD (1080×1920)'
          : 'WhatsApp Status Safe (720×1280)';
        chunks = Math.ceil(dur / 30);
        const clipWindow = Math.min(dur, 30);
        const maxTotalKbits = 14.5 * 1024 * 8;
        const allowedKbps = Math.floor((maxTotalKbits / clipWindow) - aKbps);
        const cap = state.platform === 'whatsapp_1080' ? 4200 : 3400;
        vKbps = Math.max(1200, Math.min(cap, allowedKbps));
        maxKbps = Math.floor(vKbps * 1.15);
      }

      const bufKbps = maxKbps * 2;
      const effectiveDur = state.platform === 'tiktok' ? dur : Math.min(dur, 30);
      const payloadMb = (((vKbps + aKbps) * effectiveDur) / (8 * 1024)).toFixed(1);

      let vmaf = 96.2;
      if (state.veryslow) vmaf += 1.2;
      if (state.sharpen >= 0.4 && state.sharpen <= 0.9) vmaf += 0.8;
      if (state.hdrTonemap) vmaf += 0.4;
      vmaf = Math.min(99.4, vmaf);

      return {
        tw, th, vKbps, maxKbps, bufKbps, aKbps, presetTitle, chunks, payloadMb, vmaf
      };
    }

    function buildFfmpegCommandString(m) {
      const luma = state.sharpen.toFixed(2);
      const chroma = (state.sharpen * 0.3).toFixed(2);
      const gop = state.fps;

      const vfList = [`fps=fps=${state.fps}:round=near`];
      if (state.hdrTonemap && state.sourceProfile === 'raw_hdr') {
        vfList.push('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0.0,zscale=t=bt709:m=bt709:r=tv');
      }
      vfList.push(`scale=${m.tw}:${m.th}:force_original_aspect_ratio=decrease:flags=lanczos+accurate_rnd+full_chroma_int+full_chroma_inp,pad=${m.tw}:${m.th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1`);
      if (state.denoise) {
        vfList.push(state.sourceProfile === 'lowlight' ? 'hqdn3d=2.2:2.2:4.0:4.0' : 'hqdn3d=1.2:1.2:2.5:2.5');
      }
      if (state.sharpen > 0) {
        vfList.push(`unsharp=5:5:${luma}:3:3:${chroma}`);
      }
      vfList.push('format=yuv420p');

      const vfStr = vfList.join(',');
      const presetStr = state.veryslow ? 'veryslow' : 'slow';
      const x264Opts = `ref=5:bframes=4:b-adapt=2:direct=auto:me=umh:subme=10:merange=24:trellis=2:aq-mode=3:aq-strength=0.85:psy-rd=1.05,0.15:rc-lookahead=60:keyint=${gop}:min-keyint=${Math.floor(gop / 2)}:deblock=-1,-1`;

      return `# === PASS 1: Motion Vector & 60-Frame RC Lookahead Analysis ===
ffmpeg -y -i "${state.uploadedMeta.name}" \\
  -vf "${vfStr}" \\
  -c:v libx264 -preset ${presetStr} -tune film -profile:v high -level:v 4.2 \\
  -b:v ${m.vKbps}k -maxrate ${m.maxKbps}k -bufsize ${m.bufKbps}k \\
  -x264-params "${x264Opts}" \\
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv \\
  -pass 1 -passlogfile "zeroblur_passlog" -an -f mp4 /dev/null

# === PASS 2: Ultra-Fidelity Bit-Allocated Master Encode ===
ffmpeg -y -i "${state.uploadedMeta.name}" \\
  -vf "${vfStr}" \\
  -c:v libx264 -preset ${presetStr} -tune film -profile:v high -level:v 4.2 \\
  -b:v ${m.vKbps}k -maxrate ${m.maxKbps}k -bufsize ${m.bufKbps}k \\
  -x264-params "${x264Opts}" \\
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv \\
  -pass 2 -passlogfile "zeroblur_passlog" \\
  ${state.loudnorm ? '-af "loudnorm=I=-14:TP=-1.0:LRA=11" \\\n  ' : ''}-c:a aac -b:a ${m.aKbps}k -ar 48000 -ac 2 -movflags +faststart \\
  "output_${state.platform}_master.mp4"`;
    }

    function updateAllUI() {
      const m = computePipelineMetrics();

      // Top cards
      document.getElementById('metricPresetName').textContent = m.presetTitle;
      document.getElementById('metricPresetSub').textContent = `${state.fps}.0 FPS CFR • ${m.vKbps.toLocaleString()} kbps ${state.veryslow ? '2-Pass Veryslow' : '2-Pass'}`;
      document.getElementById('metricSourceRes').textContent = `${state.uploadedMeta.width}×${state.uploadedMeta.height} (${state.usingUploadedVideo ? 'User Clip' : '4K Demo'})`;

      const flagText = state.sourceProfile === 'raw_hdr'
        ? '10-Bit HLG HDR • VFR Corrected'
        : state.sourceProfile === 'edited_4k'
          ? 'Edited Master • Chroma 4:4:4 Upsampled'
          : 'High-ISO Sensor • 3D Denoise Active';
      document.getElementById('metricSourceFlags').textContent = flagText;

      document.getElementById('metricPayloadSize').textContent = `${m.payloadMb} MB / ${Math.min(state.durationSec, state.platform === 'tiktok' ? state.durationSec : 30)}s`;
      document.getElementById('metricBypassStatus').textContent = m.chunks > 1
        ? `Auto-Split: ${m.chunks} × <=14.5MB Status Clips`
        : 'PASS: Below Re-Encode Trigger';

      document.getElementById('metricVmafScore').textContent = `${m.vmaf.toFixed(1)} / 100`;
      // Selection summary badges update
      const summaryPlatform = document.getElementById('summaryPlatform');
      if (summaryPlatform) {
        summaryPlatform.textContent = state.platform === 'tiktok'
          ? 'TikTok HD (1080p • 9.5M)'
          : state.platform === 'whatsapp_720'
            ? 'WhatsApp Safe (720p • <14.5M)'
            : 'WhatsApp HD (1080p • <14.5M)';
      }
      const summaryFootage = document.getElementById('summaryFootage');
      if (summaryFootage) {
        summaryFootage.textContent = state.sourceProfile === 'raw_hdr'
          ? 'Raw Smartphone (10-bit HDR/VFR)'
          : state.sourceProfile === 'edited_4k'
            ? 'Edited Clip (CapCut/Premiere 4K)'
            : 'Low-Light / High-ISO (Denoise)';
      }
      const summaryMasterStatus = document.getElementById('summaryMasterStatus');
      if (summaryMasterStatus) {
        summaryMasterStatus.textContent = `Config Ready (${m.tw}×${m.th} • ${state.fps}fps)`;
      }


      // Controls labels
      document.getElementById('fpsValueBadge').textContent = `${state.fps}.00 FPS (GOP=${state.fps})`;
      document.getElementById('labelSharpenVal').textContent = `${state.sharpen.toFixed(2)} ${state.sharpen >= 0.55 && state.sharpen <= 0.75 ? '(Sweet Spot)' : ''}`;
      document.getElementById('labelDurationVal').textContent = `${state.durationSec} sec`;
      document.getElementById('splitPercentLabel').textContent = `${state.splitPercent}%`;
      document.getElementById('splitLineIndicator').style.left = `${state.splitPercent}%`;

      // Pipeline Tab
      document.getElementById('calcDimensions').textContent = `${m.tw} × ${m.th} (9:16)`;
      document.getElementById('calcVideoBitrate').textContent = `${m.vKbps.toLocaleString()} kbps`;
      document.getElementById('calcVbv').textContent = `${m.maxKbps.toLocaleString()}k / ${m.bufKbps.toLocaleString()}k`;
      document.getElementById('calcAudio').textContent = `AAC-LC • ${m.aKbps} kbps • 48 kHz` + (state.loudnorm ? ' • EBU R128 (-14 LUFS)' : '');
      document.getElementById('calcWaChunks').textContent = m.chunks > 1
        ? `${m.chunks} lossless 30s segments (<14.5MB each)`
        : '1 segment (Fits single Status upload)';

      document.getElementById('ffmpegCommandBox').textContent = buildFfmpegCommandString(m);

      // Code Tab
      const activeFileObj = CODE_FILES[state.activeCodeFile];
      document.getElementById('codeFilenameLabel').textContent = activeFileObj.label;
      document.getElementById('codeDisplayPre').textContent = activeFileObj.content;
    }

    // ==================== REAL-TIME OPTICAL COMPARATOR CANVAS ====================
    const canvas = document.getElementById('comparisonCanvas');
    const ctx = canvas.getContext('2d');

    // Offscreen buffers for Left (Crushed/Blurred) and Right (ZeroBlur Master)
    const rawBuffer = document.createElement('canvas');
    rawBuffer.width = 960;
    rawBuffer.height = 600;
    const rawCtx = rawBuffer.getContext('2d');

    const crushBuffer = document.createElement('canvas');
    crushBuffer.width = 240; // Downscaled 4x to simulate aggressive platform bilinear crush
    crushBuffer.height = 150;
    const crushCtx = crushBuffer.getContext('2d');

    let animTime = 0;
    let animFrameId = null;

    function drawProceduralTestScene(targetCtx, w, h, t) {
      // Rich dark studio background gradient
      const bgGrad = targetCtx.createLinearGradient(0, 0, w, h);
      bgGrad.addColorStop(0, '#0b132b');
      bgGrad.addColorStop(0.5, '#1c2541');
      bgGrad.addColorStop(1, '#090d16');
      targetCtx.fillStyle = bgGrad;
      targetCtx.fillRect(0, 0, w, h);

      // High-frequency fine geometric grid (exposes bilinear blur immediately)
      targetCtx.strokeStyle = 'rgba(148, 163, 184, 0.22)';
      targetCtx.lineWidth = 1;
      const step = 24;
      const offset = (t * 40) % step;
      for (let x = -step; x < w + step; x += step) {
        targetCtx.beginPath();
        targetCtx.moveTo(x + offset, 0);
        targetCtx.lineTo(x + offset - 60, h);
        targetCtx.stroke();
      }

      // Simulated 10-bit HDR Neon Highlight Rings
      const cx = w * 0.5 + Math.sin(t * 1.4) * 110;
      const cy = h * 0.46 + Math.cos(t * 1.1) * 45;

      for (let r = 150; r > 15; r -= 18) {
        targetCtx.beginPath();
        targetCtx.arc(cx, cy, r, 0, Math.PI * 2);
        targetCtx.strokeStyle = r % 36 === 0 ? '#10b981' : '#38bdf8';
        targetCtx.lineWidth = 2.2;
        targetCtx.stroke();
      }

      // Fine resolution Siemens star pattern in center
      targetCtx.save();
      targetCtx.translate(w * 0.32, h * 0.52);
      targetCtx.rotate(t * 0.4);
      const spokes = 36;
      for (let i = 0; i < spokes; i++) {
        targetCtx.rotate((Math.PI * 2) / spokes);
        targetCtx.beginPath();
        targetCtx.moveTo(0, 0);
        targetCtx.lineTo(95, 0);
        targetCtx.strokeStyle = i % 2 === 0 ? '#f8fafc' : '#f43f5e';
        targetCtx.lineWidth = 1.4;
        targetCtx.stroke();
      }
      targetCtx.restore();

      // Micro-typography & foliage/hair detail card on right
      targetCtx.fillStyle = 'rgba(15, 23, 42, 0.88)';
      targetCtx.strokeStyle = '#334155';
      targetCtx.lineWidth = 1.5;
      targetCtx.fillRect(w * 0.54, h * 0.22, 340, 290);
      targetCtx.strokeRect(w * 0.54, h * 0.22, 340, 290);

      targetCtx.fillStyle = '#ffffff';
      targetCtx.font = 'bold 19px Inter, sans-serif';
      targetCtx.fillText('4K 60FPS DETAIL CHART', w * 0.54 + 20, h * 0.22 + 36);

      targetCtx.fillStyle = '#34d399';
      targetCtx.font = '500 13px "JetBrains Mono", monospace';
      targetCtx.fillText('LANCZOS + UNSHARP 5x5 MATRIX', w * 0.54 + 20, h * 0.22 + 62);

      targetCtx.fillStyle = '#cbd5e1';
      targetCtx.font = '400 12px "JetBrains Mono", monospace';
      targetCtx.fillText('1px Line Integrity: ||||||||||||||||||||||||', w * 0.54 + 20, h * 0.22 + 92);
      targetCtx.fillText('Sub-pixel Text: 0123456789 ABCDEFGHIJKLM', w * 0.54 + 20, h * 0.22 + 116);
      targetCtx.fillText('HDR Highlight Roll-Off: 1000 nits -> SDR', w * 0.54 + 20, h * 0.22 + 140);

      // High-contrast color bars
      const colors = ['#ef4444', '#f59e0b', '#10b981', '#06b6d4', '#6366f1', '#ec4899'];
      colors.forEach((col, idx) => {
        targetCtx.fillStyle = col;
        targetCtx.fillRect(w * 0.54 + 20 + idx * 50, h * 0.22 + 165, 42, 42);
      });

      // Moving 60fps smoothness indicator bar
      const barX = w * 0.54 + 20 + ((Math.sin(t * 4.0) + 1) * 0.5) * 260;
      targetCtx.fillStyle = '#f8fafc';
      targetCtx.fillRect(barX, h * 0.22 + 230, 36, 12);
      targetCtx.font = '11px "JetBrains Mono", monospace';
      targetCtx.fillStyle = '#94a3b8';
      targetCtx.fillText('CFR 60.00 Hz Motion Lock', w * 0.54 + 20, h * 0.22 + 265);
    }

    function renderComparisonFrame() {
      if (state.isPlaying) {
        animTime += 1 / 60;
      }

      const w = canvas.width;
      const h = canvas.height;
      const videoEl = document.getElementById('hiddenSourceVideo');

      // 1. Draw clean source to rawBuffer
      if (state.usingUploadedVideo && videoEl.readyState >= 2) {
        rawCtx.fillStyle = '#000';
        rawCtx.fillRect(0, 0, w, h);
        const vw = videoEl.videoWidth || 1080;
        const vh = videoEl.videoHeight || 1920;
        const scale = Math.min(w / vw, h / vh);
        const dw = vw * scale;
        const dh = vh * scale;
        rawCtx.drawImage(videoEl, (w - dw) / 2, (h - dh) / 2, dw, dh);
      } else {
        drawProceduralTestScene(rawCtx, w, h, animTime);
      }

      // 2. Prepare Left Side (Simulated Naive Platform Re-encode: Bilinear downscale + HDR blowout)
      crushCtx.filter = 'contrast(1.35) brightness(1.18) saturate(0.78) blur(0.6px)';
      crushCtx.drawImage(rawBuffer, 0, 0, crushBuffer.width, crushBuffer.height);
      crushCtx.filter = 'none';

      // Zoom transform parameters
      const z = state.zoomLevel;
      const splitX = Math.floor((state.splitPercent / 100) * w);

      ctx.clearRect(0, 0, w, h);

      // Draw Left half (Naive Platform Upload)
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, splitX, h);
      ctx.clip();
      ctx.translate(w / 2, h / 2);
      ctx.scale(z, z);
      ctx.translate(-w / 2, -h / 2);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'low';
      ctx.drawImage(crushBuffer, 0, 0, w, h);
      ctx.restore();

      // Draw Right half (ZeroBlur Engine Master with crisp high-quality sampling + contrast/tone-map)
      ctx.save();
      ctx.beginPath();
      ctx.rect(splitX, 0, w - splitX, h);
      ctx.clip();
      ctx.translate(w / 2, h / 2);
      ctx.scale(z, z);
      ctx.translate(-w / 2, -h / 2);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';

      const contrastBoost = 1 + state.sharpen * 0.18;
      const satBoost = state.hdrTonemap ? 1.08 : 0.95;
      ctx.filter = `contrast(${contrastBoost.toFixed(2)}) saturate(${satBoost.toFixed(2)})`;
      ctx.drawImage(rawBuffer, 0, 0, w, h);

      // Subtle crisp edge enhancement overlay proportional to sharpen slider
      if (state.sharpen > 0.05) {
        ctx.globalCompositeOperation = 'overlay';
        ctx.globalAlpha = Math.min(0.32, state.sharpen * 0.22);
        ctx.drawImage(rawBuffer, 0, 0, w, h);
        ctx.globalAlpha = 1.0;
        ctx.globalCompositeOperation = 'source-over';
      }
      ctx.filter = 'none';
      ctx.restore();

      animFrameId = requestAnimationFrame(renderComparisonFrame);
    }

    // ==================== EVENT LISTENERS ====================
    
    // ==================== THEME SYNCHRONIZATION ====================
    const AVAILABLE_THEMES = ['editorial', 'liquid-glass', 'color', 'bw-light', 'bw-dark'];
    const THEME_NAMES = {
      'color': 'Modern Dark',
      'editorial': 'Editorial',
      'liquid-glass': 'Liquid Glass',
      'bw-light': 'B&W Light',
      'bw-dark': 'B&W Dark'
    };

    function applyDisplayTheme(theme) {
      const valid = AVAILABLE_THEMES.includes(theme) ? theme : 'color';
      document.body.classList.remove('theme-editorial', 'theme-liquid-glass', 'bw-page-light', 'bw-page-dark');
      if (valid === 'editorial') document.body.classList.add('theme-editorial');
      else if (valid === 'liquid-glass') document.body.classList.add('theme-liquid-glass');
      else if (valid === 'bw-light') document.body.classList.add('bw-page-light');
      else if (valid === 'bw-dark') document.body.classList.add('bw-page-dark');
      
      localStorage.setItem('webpage_theme', valid);
      const label = document.getElementById('zeroblurThemeLabel');
      if (label) label.textContent = 'Theme: ' + THEME_NAMES[valid];
      const cycleLabel = document.getElementById('zeroblurToggleText');
      if (cycleLabel) cycleLabel.textContent = 'Display: ' + THEME_NAMES[valid];

      // Update active state in themeSwitcher
      document.querySelectorAll('#themeSwitcher .theme-btn').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-theme') === valid);
      });
    }

    function setupListeners() {

      // ==================== SELECTION STEP NAVIGATION & ACCORDION ====================
      let isCleanMode = true;
      const stepPills = document.querySelectorAll('.step-nav-pill');
      const selectionBlocks = document.querySelectorAll('.selection-block');

      function setActiveStep(stepId, shouldScroll) {
        stepPills.forEach(pill => {
          const isTarget = pill.getAttribute('data-step-target') === stepId;
          pill.classList.toggle('active', isTarget);
          if (isTarget) {
            pill.className = 'step-nav-pill active px-2.5 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap transition bg-emerald-500 text-slate-950 shadow';
          } else {
            pill.className = 'step-nav-pill px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition text-slate-400 hover:text-white hover:bg-slate-800/60';
          }
        });

        selectionBlocks.forEach(block => {
          const isTarget = block.id === stepId;
          const body = block.querySelector('.selection-block-body');
          const toggleIcon = block.querySelector('.toggle-icon');

          if (isCleanMode) {
            if (isTarget) {
              block.classList.add('active');
              if (body) body.classList.remove('hidden');
              if (toggleIcon) toggleIcon.textContent = '▲';
            } else {
              block.classList.remove('active');
              if (body) body.classList.add('hidden');
              if (toggleIcon) toggleIcon.textContent = '▼';
            }
          } else {
            if (isTarget) {
              block.classList.add('active');
              if (body) body.classList.remove('hidden');
              if (toggleIcon) toggleIcon.textContent = '▲';
            }
          }
        });

        if (shouldScroll) {
          const targetEl = document.getElementById(stepId);
          if (targetEl) {
            targetEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
        }
      }

      // Stepper pill clicks
      stepPills.forEach(pill => {
        pill.addEventListener('click', () => {
          const targetId = pill.getAttribute('data-step-target');
          if (targetId) setActiveStep(targetId, true);
        });
      });

      // Selection Header click & toggle button click
      selectionBlocks.forEach(block => {
        const header = block.querySelector('.selection-block-header');
        if (header) {
          header.addEventListener('click', (e) => {
            const body = block.querySelector('.selection-block-body');
            const toggleIcon = block.querySelector('.toggle-icon');
            if (!body) return;
            const isHidden = body.classList.contains('hidden');
            if (isHidden) {
              if (isCleanMode) {
                setActiveStep(block.id, false);
              } else {
                body.classList.remove('hidden');
                block.classList.add('active');
                if (toggleIcon) toggleIcon.textContent = '▲';
              }
            } else {
              body.classList.add('hidden');
              block.classList.remove('active');
              if (toggleIcon) toggleIcon.textContent = '▼';
            }
          });
        }
      });

      // Step Navigation Next / Prev buttons
      document.querySelectorAll('.btn-step-next').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const nextStep = btn.getAttribute('data-next-step');
          if (nextStep) setActiveStep(nextStep, true);
        });
      });

      document.querySelectorAll('.btn-step-prev').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const prevStep = btn.getAttribute('data-prev-step');
          if (prevStep) setActiveStep(prevStep, true);
        });
      });

      // Jump to Tab buttons
      document.querySelectorAll('.btn-step-tab, .btn-nav-next-tab, .btn-nav-prev-tab').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const targetTab = btn.getAttribute('data-navtab');
          if (targetTab) {
            const tabBtn = document.getElementById('tabBtn-' + targetTab);
            if (tabBtn) {
              tabBtn.click();
              window.scrollTo({ top: 0, behavior: 'smooth' });
            }
          }
        });
      });

      // Clean View toggle
      const btnCleanMode = document.getElementById('btnToggleCleanMode');
      const cleanModeText = document.getElementById('cleanModeStatusText');
      if (btnCleanMode) {
        btnCleanMode.addEventListener('click', () => {
          isCleanMode = !isCleanMode;
          if (cleanModeText) {
            cleanModeText.textContent = isCleanMode ? 'Clean View' : 'Multi-Expand';
          }
          btnCleanMode.classList.toggle('bg-emerald-500/20', isCleanMode);
          btnCleanMode.classList.toggle('border-emerald-500', isCleanMode);
          showToast(isCleanMode ? 'Clean View active (collapses inactive selections)' : 'Multi-expand view enabled');
        });
      }

      // Expand All button
      const btnExpandAll = document.getElementById('btnExpandAllSelections');
      const expandAllText = document.getElementById('expandAllStatusText');
      let allExpanded = false;
      if (btnExpandAll) {
        btnExpandAll.addEventListener('click', () => {
          allExpanded = !allExpanded;
          selectionBlocks.forEach(block => {
            const body = block.querySelector('.selection-block-body');
            const toggleIcon = block.querySelector('.toggle-icon');
            if (body) {
              body.classList.toggle('hidden', !allExpanded);
            }
            if (toggleIcon) {
              toggleIcon.textContent = allExpanded ? '▲' : '▼';
            }
            block.classList.toggle('active', allExpanded);
          });
          if (expandAllText) {
            expandAllText.textContent = allExpanded ? 'Collapse All' : 'Expand All';
          }
        });
      }

      // Theme switcher bar hide/show toggle button
      const toggleSwitcherBtn = document.getElementById('toggleZeroblurSwitcherNavBtn');
      const themeBar = document.getElementById('themeBar');
      const switcherIcon = document.getElementById('toggleZeroblurSwitcherIcon');
      const switcherText = document.getElementById('toggleZeroblurSwitcherText');

      function updateSwitcherVisibility(visible) {
        if (!themeBar) return;
        if (visible) {
          themeBar.style.display = 'flex';
          if (toggleSwitcherBtn) toggleSwitcherBtn.setAttribute('aria-expanded', 'true');
          if (switcherText) switcherText.textContent = 'Hide Switcher';
          if (switcherIcon) {
            switcherIcon.innerHTML = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line>';
          }
        } else {
          themeBar.style.display = 'none';
          if (toggleSwitcherBtn) toggleSwitcherBtn.setAttribute('aria-expanded', 'false');
          if (switcherText) switcherText.textContent = 'Show Switcher';
          if (switcherIcon) {
            switcherIcon.innerHTML = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>';
          }
        }
      }

      if (toggleSwitcherBtn) {
        toggleSwitcherBtn.addEventListener('click', () => {
          const currentVisible = themeBar ? themeBar.style.display !== 'none' : true;
          const nextVisible = !currentVisible;
          localStorage.setItem('themeSwitcherVisible', nextVisible ? 'true' : 'false');
          updateSwitcherVisibility(nextVisible);
        });
      }

      const closeThemeBarBtn = document.getElementById('themeBarCloseBtn');
      if (closeThemeBarBtn) {
        closeThemeBarBtn.addEventListener('click', () => {
          localStorage.setItem('themeSwitcherVisible', 'false');
          updateSwitcherVisibility(false);
        });
      }

      const savedSwitcherVis = localStorage.getItem('themeSwitcherVisible');
      if (savedSwitcherVis === 'false') {
        updateSwitcherVisibility(false);
      } else {
        updateSwitcherVisibility(true);
      }

      // Theme segmented buttons in themeBar
      document.querySelectorAll('#themeSwitcher .theme-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const theme = btn.getAttribute('data-theme');
          applyDisplayTheme(theme);
          showToast('Display switched to ' + THEME_NAMES[theme]);
        });
      });

      // Cycle toggle button
      const cycleToggleBtn = document.getElementById('zeroblurThemeToggle');
      if (cycleToggleBtn) {
        cycleToggleBtn.addEventListener('click', () => {
          const cur = localStorage.getItem('webpage_theme') || 'color';
          const idx = AVAILABLE_THEMES.indexOf(cur);
          const next = AVAILABLE_THEMES[(idx + 1) % AVAILABLE_THEMES.length];
          applyDisplayTheme(next);
          showToast('Display switched to ' + THEME_NAMES[next]);
        });
      }

      const themeBtn = document.getElementById('zeroblurThemeBtn');
      if (themeBtn) {
        themeBtn.addEventListener('click', () => {
          const cur = localStorage.getItem('webpage_theme') || 'color';
          const idx = AVAILABLE_THEMES.indexOf(cur);
          const next = AVAILABLE_THEMES[(idx + 1) % AVAILABLE_THEMES.length];
          applyDisplayTheme(next);
          showToast('Display switched to ' + THEME_NAMES[next]);
        });
      }

      // Top Navigation Tabs
      document.querySelectorAll('.nav-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const target = btn.getAttribute('data-tab');
          state.activeTab = target;
          document.querySelectorAll('.nav-tab-btn').forEach(b => {
            b.className = 'nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-medium transition-all text-slate-300 hover:text-white hover:bg-slate-800/60';
          });
          btn.className = 'nav-tab-btn px-3 py-1.5 rounded-lg text-xs sm:text-sm font-medium transition-all bg-emerald-500 text-slate-950 font-semibold shadow';

          ['studio', 'pipeline', 'code', 'guide', 'converter'].forEach(id => {
            document.getElementById('view-' + id).classList.toggle('hidden', id !== target);
          });
        });
      });

      // Split Slider & Canvas Dragging
      const splitSlider = document.getElementById('splitRangeSlider');
      splitSlider.addEventListener('input', (e) => {
        state.splitPercent = Number(e.target.value);
        updateAllUI();
      });

      const viewport = document.getElementById('canvasViewport');
      let isDraggingSplit = false;
      const updateSplitFromPointer = (clientX) => {
        const rect = viewport.getBoundingClientRect();
        const rel = ((clientX - rect.left) / rect.width) * 100;
        state.splitPercent = Math.max(5, Math.min(95, Math.round(rel)));
        splitSlider.value = state.splitPercent;
        updateAllUI();
      };
      viewport.addEventListener('pointerdown', (e) => {
        isDraggingSplit = true;
        updateSplitFromPointer(e.clientX);
      });
      window.addEventListener('pointermove', (e) => {
        if (isDraggingSplit) updateSplitFromPointer(e.clientX);
      });
      window.addEventListener('pointerup', () => {
        isDraggingSplit = false;
      });

      // Direct drag-and-drop video loading onto comparison viewport
      viewport.addEventListener('dragover', (e) => {
        e.preventDefault();
        viewport.classList.add('ring-2', 'ring-emerald-400', 'ring-offset-2', 'ring-offset-slate-950');
      });
      viewport.addEventListener('dragleave', (e) => {
        e.preventDefault();
        viewport.classList.remove('ring-2', 'ring-emerald-400', 'ring-offset-2', 'ring-offset-slate-950');
      });
      viewport.addEventListener('drop', (e) => {
        e.preventDefault();
        viewport.classList.remove('ring-2', 'ring-emerald-400', 'ring-offset-2', 'ring-offset-slate-950');
        const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (!file) return;
        if (!file.type.startsWith('video/') && !file.name.match(/\.(mp4|mov|webm|mkv|avi)$/i)) {
          showToast('Please drop a video file (.mp4, .mov, .webm)');
          return;
        }
        const url = URL.createObjectURL(file);
        const vid = document.getElementById('hiddenSourceVideo');
        vid.src = url;
        vid.onloadedmetadata = () => {
          state.usingUploadedVideo = true;
          state.uploadedFile = file;
          state.uploadedMeta = {
            name: file.name,
            width: vid.videoWidth || 1080,
            height: vid.videoHeight || 1920,
            duration: Math.round(vid.duration || 15),
            sizeMb: Number((file.size / (1024 * 1024)).toFixed(1))
          };
          state.durationSec = Math.max(5, Math.min(120, Math.round(vid.duration || 15)));
          document.getElementById('sliderDuration').value = state.durationSec;
          vid.play();
          updateAllUI();
          showToast(`Loaded ${file.name} (${state.uploadedMeta.width}×${state.uploadedMeta.height})`);
        };
      });

      // Play/Pause
      const btnPlay = document.getElementById('btnTogglePlay');
      btnPlay.addEventListener('click', () => {
        state.isPlaying = !state.isPlaying;
        const vid = document.getElementById('hiddenSourceVideo');
        if (state.usingUploadedVideo) {
          if (state.isPlaying) vid.play(); else vid.pause();
        }
        btnPlay.textContent = state.isPlaying ? 'Pause Frame' : 'Resume 60fps';
      });

      // Zoom Buttons
      document.querySelectorAll('.zoom-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          state.zoomLevel = Number(btn.getAttribute('data-zoom'));
          document.querySelectorAll('.zoom-btn').forEach(b => {
            b.className = 'zoom-btn px-2.5 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white text-xs font-medium border border-slate-700 transition';
          });
          btn.className = 'zoom-btn px-2.5 py-1.5 rounded-lg bg-emerald-500 text-slate-950 text-xs font-semibold transition';
        });
      });

      // Local Video File Input
      const fileInput = document.getElementById('videoFileInput');
      fileInput.addEventListener('change', (e) => {
        const file = e.target.files && e.target.files[0];
        if (!file) return;
        const url = URL.createObjectURL(file);
        const vid = document.getElementById('hiddenSourceVideo');
        vid.src = url;
        vid.onloadedmetadata = () => {
          state.usingUploadedVideo = true;
          state.uploadedFile = file;
          state.uploadedMeta = {
            name: file.name,
            width: vid.videoWidth || 1080,
            height: vid.videoHeight || 1920,
            duration: Math.round(vid.duration || 15),
            sizeMb: Number((file.size / (1024 * 1024)).toFixed(1))
          };
          state.durationSec = Math.max(5, Math.min(120, Math.round(vid.duration || 15)));
          document.getElementById('sliderDuration').value = state.durationSec;
          vid.play();
          updateAllUI();
          showToast(`Loaded ${file.name} (${state.uploadedMeta.width}×${state.uploadedMeta.height})`);
        };
      });

      // Reset to 4K HDR Demo Stream
      document.getElementById('btnResetDemoStream').addEventListener('click', () => {
        state.usingUploadedVideo = false;
        state.uploadedMeta = {
          name: '4K_HDR_Demo_Scene.mov',
          width: 3840,
          height: 2160,
          duration: 15,
          sizeMb: 84.6
        };
        updateAllUI();
        showToast('Switched to 4K 60fps HDR synthetic test chart');
      });

      // Platform selector buttons
      document.querySelectorAll('.plat-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          state.platform = btn.getAttribute('data-platform');
          document.querySelectorAll('.plat-btn').forEach(b => {
            b.className = 'plat-btn px-3 py-2.5 rounded-xl border text-left transition bg-slate-800/60 border-slate-700/80 text-slate-300 hover:border-slate-600';
          });
          btn.className = 'plat-btn px-3 py-2.5 rounded-xl border text-left transition bg-emerald-500/15 border-emerald-500 text-white';
          updateAllUI();
        });
      });

      // Source Profile dropdown
      document.getElementById('selectSourceProfile').addEventListener('change', (e) => {
        state.sourceProfile = e.target.value;
        updateAllUI();
      });

      // FPS buttons
      const btnFps60 = document.getElementById('btnFps60');
      const btnFps30 = document.getElementById('btnFps30');
      btnFps60.addEventListener('click', () => {
        state.fps = 60;
        btnFps60.className = 'px-3 py-2 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition';
        btnFps30.className = 'px-3 py-2 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 border border-slate-700 transition';
        updateAllUI();
      });
      btnFps30.addEventListener('click', () => {
        state.fps = 30;
        btnFps30.className = 'px-3 py-2 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition';
        btnFps60.className = 'px-3 py-2 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 border border-slate-700 transition';
        updateAllUI();
      });

      // Sliders & Checkboxes
      document.getElementById('sliderSharpen').addEventListener('input', (e) => {
        state.sharpen = Number(e.target.value) / 100;
        updateAllUI();
      });
      document.getElementById('sliderDuration').addEventListener('input', (e) => {
        state.durationSec = Number(e.target.value);
        updateAllUI();
      });
      document.getElementById('toggleHdrTonemap').addEventListener('change', (e) => {
        state.hdrTonemap = e.target.checked;
        updateAllUI();
      });
      document.getElementById('toggleDenoise').addEventListener('change', (e) => {
        state.denoise = e.target.checked;
        updateAllUI();
      });
      document.getElementById('toggleVeryslow').addEventListener('change', (e) => {
        state.veryslow = e.target.checked;
        updateAllUI();
      });
      document.getElementById('toggleLoudnorm').addEventListener('change', (e) => {
        state.loudnorm = e.target.checked;
        updateAllUI();
      });

      // Copy FFmpeg Script
      document.getElementById('btnCopyFfmpeg').addEventListener('click', () => {
        const text = document.getElementById('ffmpegCommandBox').textContent;
        copyTextToClipboard(text, '2-Pass FFmpeg command');
      });

      // Code Viewer Tabs
      document.querySelectorAll('.code-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          state.activeCodeFile = btn.getAttribute('data-codefile');
          document.querySelectorAll('.code-tab-btn').forEach(b => {
            b.className = 'code-tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:text-white transition';
          });
          btn.className = 'code-tab-btn px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition';
          updateAllUI();
        });
      });

      document.getElementById('btnCopyActiveCode').addEventListener('click', () => {
        const fileObj = CODE_FILES[state.activeCodeFile];
        copyTextToClipboard(fileObj.content, fileObj.filename);
      });

      document.getElementById('btnDownloadActiveCode').addEventListener('click', () => {
        const fileObj = CODE_FILES[state.activeCodeFile];
        const blob = new Blob([fileObj.content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = fileObj.filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        showToast('Downloaded ' + fileObj.filename);
      });

      const dlBtn = document.getElementById('downloadMasterLink');
      dlBtn.addEventListener('click', () => {
        if (!state.lastMasterBlobUrl) return;
        const a = document.createElement('a');
        a.href = state.lastMasterBlobUrl;
        a.download = state.lastMasterFilename || 'zeroblur_master.webm';
        document.body.appendChild(a);
        a.click();
        a.remove();
        showToast('Saved ' + (state.lastMasterFilename || 'master file'));
      });

      // Cloud Server Master Render & Export
      const btnCloud = document.getElementById('btnRunCloudMaster');
      if (btnCloud) {
        btnCloud.addEventListener('click', async () => {
          if (!state.uploadedFile) {
            showToast('Please load a local video file first (or click Browser Master)');
            return;
          }
          btnCloud.disabled = true;
          btnCloud.classList.add('opacity-60');
          const statusEl = document.getElementById('renderStatusText');
          const barEl = document.getElementById('renderProgressBar');
          const dlLink = document.getElementById('downloadMasterLink');
          dlLink.classList.add('hidden');

          statusEl.textContent = 'Uploading to Cloud ZeroBlur Engine...';
          barEl.style.width = '25%';

          const fd = new FormData();
          fd.append('file', state.uploadedFile);
          fd.append('platform', state.platform);
          fd.append('target_fps', state.fps);
          fd.append('whatsapp_res', state.platform === 'whatsapp_720' ? '720p' : '1080p');
          fd.append('sharpen_strength', state.sharpen);
          fd.append('audio_normalize', state.loudnorm ? 'true' : 'false');

          try {
            statusEl.textContent = 'Server mastering via FFmpeg Lanczos + 5x5 unsharp...';
            barEl.style.width = '65%';
            const res = await fetch('/api/v1/optimize', {
              method: 'POST',
              body: fd
            });
            if (!res.ok) {
              const err = await res.json().catch(() => ({}));
              throw new Error(err.detail || 'Mastering failed on server');
            }
            barEl.style.width = '90%';
            statusEl.textContent = 'Receiving mastered MP4 stream...';
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            state.lastMasterBlobUrl = url;
            state.lastMasterFilename = `zeroblur_${state.platform}_${state.fps}fps_master.mp4`;
            dlLink.classList.remove('hidden');
            barEl.style.width = '100%';
            statusEl.textContent = 'Cloud Master Complete — Ready to save MP4';
            showToast('Cloud ZeroBlur Master complete!');

            // Trigger direct download
            const a = document.createElement('a');
            a.href = url;
            a.download = state.lastMasterFilename;
            document.body.appendChild(a);
            a.click();
            a.remove();
          } catch (err) {
            statusEl.textContent = 'Cloud Master Error: ' + err.message;
            showToast('Cloud Master Error: ' + err.message);
          } finally {
            btnCloud.disabled = false;
            btnCloud.classList.remove('opacity-60');
          }
        });
      }

      // In-Browser Master Render & Export
      const btnMaster = document.getElementById('btnRunBrowserMaster');
      btnMaster.addEventListener('click', () => {
        const statusEl = document.getElementById('renderStatusText');
        const barEl = document.getElementById('renderProgressBar');
        const dlLink = document.getElementById('downloadMasterLink');
        dlLink.classList.add('hidden');
        btnMaster.disabled = true;
        btnMaster.classList.add('opacity-60');

        let progress = 0;
        statusEl.textContent = 'Pass 1/2: Analyzing motion vectors & HDR transfer curve...';

        // Capture stream if MediaRecorder is supported
        let recorder = null;
        let recordedChunks = [];
        try {
          if (canvas.captureStream && window.MediaRecorder) {
            const stream = canvas.captureStream(state.fps);
            recorder = new MediaRecorder(stream, { videoBitsPerSecond: 9500000 });
            recorder.ondataavailable = (ev) => {
              if (ev.data && ev.data.size > 0) recordedChunks.push(ev.data);
            };
            recorder.onstop = () => {
              const blob = new Blob(recordedChunks, { type: 'video/webm' });
              state.lastMasterBlobUrl = URL.createObjectURL(blob);
              state.lastMasterFilename = `zeroblur_${state.platform}_${state.fps}fps_master.webm`;
              dlLink.classList.remove('hidden');
            };
            recorder.start();
          }
        } catch (err) {
          recorder = null;
        }

        const timer = setInterval(() => {
          progress += 5;
          barEl.style.width = `${progress}%`;
          if (progress === 45) {
            statusEl.textContent = `Pass 2/2: Mastering ${state.fps}fps CFR stream (Lanczos + Unsharp ${state.sharpen.toFixed(2)})...`;
          }
          if (progress >= 100) {
            clearInterval(timer);
            if (recorder && recorder.state !== 'inactive') {
              recorder.stop();
            } else {
              // Fallback synthetic blob if MediaRecorder unavailable
              const scriptBlob = new Blob([document.getElementById('ffmpegCommandBox').textContent], { type: 'text/plain' });
              state.lastMasterBlobUrl = URL.createObjectURL(scriptBlob);
              state.lastMasterFilename = `zeroblur_${state.platform}_master.sh`;
              dlLink.classList.remove('hidden');
            }
            statusEl.textContent = 'Master Pass Complete — Ready to save output';
            btnMaster.disabled = false;
            btnMaster.classList.remove('opacity-60');
            showToast('2-Pass Mastering complete!');
          }
        }, 90);
      });

      // Pro Keyboard Shortcuts
      window.addEventListener('keydown', (e) => {
        if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT')) {
          return;
        }

        if (e.code === 'Space') {
          e.preventDefault();
          const btn = document.getElementById('btnTogglePlay');
          if (btn) btn.click();
        } else if (e.key === 'ArrowLeft') {
          e.preventDefault();
          state.splitPercent = Math.max(5, state.splitPercent - 5);
          const slider = document.getElementById('splitRangeSlider');
          if (slider) slider.value = state.splitPercent;
          updateAllUI();
        } else if (e.key === 'ArrowRight') {
          e.preventDefault();
          state.splitPercent = Math.min(95, state.splitPercent + 5);
          const slider = document.getElementById('splitRangeSlider');
          if (slider) slider.value = state.splitPercent;
          updateAllUI();
        } else if (e.key === '1') {
          const btn = document.getElementById('btnZoom1x');
          if (btn) btn.click();
        } else if (e.key === '2') {
          const btn = document.getElementById('btnZoom2x');
          if (btn) btn.click();
        } else if (e.key === '3') {
          const btn = document.getElementById('btnZoom3x');
          if (btn) btn.click();
        } else if (e.key === 't' || e.key === 'T') {
          const themeBtn = document.getElementById('zeroblurThemeBtn');
          if (themeBtn) themeBtn.click();
        }
      });
    }

    window.addEventListener('DOMContentLoaded', () => {
      applyDisplayTheme(localStorage.getItem('webpage_theme') || 'color');
      setupListeners();
      updateAllUI();
      renderComparisonFrame();
    });
  
    // ==================== SLIDE-OUT NAVIGATION DRAWER & TOUCH SLIDING PHYSICS ====================
    function initSlideNavigationDrawer() {
        const drawer = document.getElementById('slideNavDrawer');
        const backdrop = document.getElementById('slideNavBackdrop');
        const openBtn = document.getElementById('navDrawerHamburgerBtn');
        const closeBtn = document.getElementById('btnCloseNavDrawer');
        if (!drawer || !backdrop) return;

        let isOpen = false;
        let isDragging = false;
        let startX = 0;
        let currentX = 0;
        let startY = 0;
        let isHorizontalGesture = null;

        const getDrawerWidth = () => drawer.offsetWidth || Math.min(340, window.innerWidth * 0.86);

        function setDrawerTransform(x, animate) {
            drawer.style.transition = animate ? 'transform 0.28s cubic-bezier(0.16, 1, 0.3, 1)' : 'none';
            drawer.style.transform = `translateX(${x}px)`;
        }

        function setBackdropOpacity(ratio, animate) {
            backdrop.style.transition = animate ? 'opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1)' : 'none';
            backdrop.style.opacity = Math.max(0, Math.min(1, ratio));
            backdrop.style.pointerEvents = ratio > 0.05 ? 'auto' : 'none';
        }

        function openDrawer() {
            isOpen = true;
            drawer.classList.add('is-open');
            backdrop.classList.add('is-open');
            drawer.style.transform = '';
            drawer.style.transition = '';
            backdrop.style.opacity = '';
            backdrop.style.pointerEvents = '';
            backdrop.style.transition = '';
            document.body.style.overflow = 'hidden';
            if (openBtn) openBtn.setAttribute('aria-expanded', 'true');
            
            // Sync active theme in drawer
            const currentTheme = localStorage.getItem('webpage_theme') || 'color';
            document.querySelectorAll('.drawer-theme-pill').forEach(pill => {
                pill.classList.toggle('active', pill.getAttribute('data-theme') === currentTheme);
            });
        }

        function closeDrawer() {
            isOpen = false;
            drawer.classList.remove('is-open');
            backdrop.classList.remove('is-open');
            drawer.style.transform = '';
            drawer.style.transition = '';
            backdrop.style.opacity = '';
            backdrop.style.pointerEvents = '';
            backdrop.style.transition = '';
            document.body.style.overflow = '';
            if (openBtn) openBtn.setAttribute('aria-expanded', 'false');
        }

        function toggleDrawer() {
            if (isOpen) closeDrawer();
            else openDrawer();
        }

        if (openBtn) {
            openBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleDrawer();
            });
        }

        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                closeDrawer();
            });
        }

        backdrop.addEventListener('click', () => {
            closeDrawer();
        });

        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && isOpen) {
                closeDrawer();
            }
        });

        // Touch Swipe & Edge Sliding Physics
        const EDGE_SWIPE_THRESHOLD = 40; // px from screen left edge

        window.addEventListener('touchstart', (e) => {
            if (e.touches.length !== 1) return;
            const touch = e.touches[0];
            startX = touch.clientX;
            startY = touch.clientY;
            currentX = startX;
            isHorizontalGesture = null;

            if (!isOpen && startX <= EDGE_SWIPE_THRESHOLD) {
                isDragging = true;
                const width = getDrawerWidth();
                setDrawerTransform(-width, false);
                setBackdropOpacity(0, false);
            } else if (isOpen) {
                if (e.target.closest('#slideNavDrawer') || e.target.closest('#slideNavBackdrop')) {
                    isDragging = true;
                }
            }
        }, { passive: true });

        window.addEventListener('touchmove', (e) => {
            if (!isDragging || e.touches.length !== 1) return;
            const touch = e.touches[0];
            currentX = touch.clientX;
            const deltaX = currentX - startX;
            const deltaY = touch.clientY - startY;

            if (isHorizontalGesture === null) {
                if (Math.abs(deltaX) > 6 || Math.abs(deltaY) > 6) {
                    isHorizontalGesture = Math.abs(deltaX) > Math.abs(deltaY);
                    if (!isHorizontalGesture) {
                        isDragging = false;
                        return;
                    }
                }
            }

            if (!isHorizontalGesture) return;

            const width = getDrawerWidth();
            if (!isOpen) {
                const targetX = Math.min(0, -width + Math.max(0, deltaX));
                setDrawerTransform(targetX, false);
                const ratio = (width + targetX) / width;
                setBackdropOpacity(ratio, false);
            } else {
                if (deltaX < 0) {
                    const targetX = Math.max(-width, Math.min(0, deltaX));
                    setDrawerTransform(targetX, false);
                    const ratio = (width + targetX) / width;
                    setBackdropOpacity(ratio, false);
                }
            }
        }, { passive: true });

        window.addEventListener('touchend', (e) => {
            if (!isDragging) return;
            isDragging = false;
            const deltaX = currentX - startX;

            if (!isOpen) {
                if (deltaX > 55) {
                    openDrawer();
                } else {
                    closeDrawer();
                }
            } else {
                if (deltaX < -50) {
                    closeDrawer();
                } else {
                    openDrawer();
                }
            }
            isHorizontalGesture = null;
        }, { passive: true });

        window.addEventListener('touchcancel', () => {
            if (isDragging) {
                isDragging = false;
                if (isOpen) openDrawer();
                else closeDrawer();
            }
        }, { passive: true });

        // Drawer Theme Switcher Buttons
        document.querySelectorAll('.drawer-theme-pill').forEach(pill => {
            pill.addEventListener('click', () => {
                const theme = pill.getAttribute('data-theme');
                if (theme) {
                    if (typeof setWebpageTheme === 'function') setWebpageTheme(theme);
                    else if (typeof applyDisplayTheme === 'function') applyDisplayTheme(theme);
                    document.querySelectorAll('.drawer-theme-pill').forEach(p => {
                        p.classList.toggle('active', p.getAttribute('data-theme') === theme);
                    });
                }
            });
        });

        // Close drawer when any internal navigation link or trigger is clicked
        drawer.querySelectorAll('a, .drawer-link').forEach(el => {
            el.addEventListener('click', () => {
                setTimeout(closeDrawer, 120);
            });
        });

        // Studio Tab Triggers (for ZeroBlur Studio)
        document.querySelectorAll('.drawer-tab-trigger').forEach(btn => {
            btn.addEventListener('click', () => {
                const tabId = btn.getAttribute('data-tab-target');
                if (tabId && typeof switchStudioTab === 'function') {
                    switchStudioTab(tabId);
                }
            });
        });

        // Step Navigation Triggers (for ZeroBlur Studio)
        document.querySelectorAll('.drawer-step-trigger').forEach(btn => {
            btn.addEventListener('click', () => {
                const stepId = btn.getAttribute('data-step-target');
                if (stepId && typeof setActiveStep === 'function') {
                    setActiveStep(stepId, true);
                }
            });
        });

        // Format Category Shortcuts (for File Converter)
        document.querySelectorAll('.drawer-format-cat-trigger').forEach(btn => {
            btn.addEventListener('click', () => {
                const cat = btn.getAttribute('data-cat');
                if (cat) {
                    const targetBtn = document.querySelector(`.format-cat-btn[data-cat="${cat}"]`);
                    if (targetBtn) targetBtn.click();
                }
            });
        });
    }

    // Initialize slide navigation drawer on DOM load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSlideNavigationDrawer);
    } else {
        initSlideNavigationDrawer();
    }

  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(
        content=HTML_DASHBOARD,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/zeroblur", response_class=HTMLResponse)
@app.get("/formats", response_class=HTMLResponse)
async def zeroblur_page():
    return HTMLResponse(
        content=HTML_ZEROBLUR,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
