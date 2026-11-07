import os
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
        "has_pdfium": HAS_PDFIUM,
        "has_heif": HAS_HEIF,
        "has_docx": HAS_DOCX,
        "hosted_count": len([f for f in HOSTED_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]) if HOSTED_DIR.exists() else 0,
        "logs": CONVERSION_LOGS[-40:],
    }


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
            justify-content: center;
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
            justify-content: center;
            gap: 10px;
            margin: 6px 0 16px 0;
            flex-wrap: wrap;
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
            gap: 12px;
            flex-direction: column;
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
    </style>
</head>
<body>
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
                <div class="header-badge">⚡ Instant Conversion & Permanent Hosting</div>
                <div class="header-badge" style="margin-bottom:0; background:rgba(16, 185, 129, 0.15); color:#34d399; border-color:rgba(16, 185, 129, 0.35);">v1.4.0 • Mobile Shield & Photo Gallery</div>
                <button type="button" class="btn-diag btn-bw-toggle" id="bwPageToggle" title="Toggle webpage display style (Editorial, Liquid Glass, Modern Dark, B&W)">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"></circle>
                        <path d="M12 2a10 10 0 0 1 0 20z" fill="currentColor"></path>
                    </svg>
                    <span id="bwToggleText">Display: Modern Dark</span>
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
            <div class="theme-bar">
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

                <!-- Target Format Selection -->
                <div class="form-row">
                    <div>
                        <label for="targetFormat">Select Target Format</label>
                        <select id="targetFormat" name="target_format" required>
                            <optgroup label="Direct Hosting">
                                <option value="keep">Keep Original Format (Direct Host)</option>
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
        }

        initWebpageTheme();
        loadHostedFiles();
    </script>
</body>
</html>
"""


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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
