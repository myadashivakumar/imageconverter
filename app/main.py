import base64
import html
import io
import tempfile
import zipfile
from pathlib import Path

import img2pdf
import pikepdf
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pdf2docx import Converter
from PIL import Image, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LEGAL, LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="JPG to PDF Converter")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

templates = Jinja2Templates(directory=APP_DIR / "templates")

MIME_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "png": "image/png",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "zip": "application/zip",
}


def file_payload(data: bytes, ext: str) -> dict:
    """Base64-encode file bytes for inline delivery in a JSON response.

    No file is ever written to disk: the client builds a data: URI from this
    directly. That keeps the app stateless, which matters on Lambda (each
    request can land on a different, disk-isolated execution environment) and
    is simply one less thing to clean up everywhere else.
    """
    return {
        "file_data": base64.b64encode(data).decode("ascii"),
        "mime_type": MIME_TYPES[ext],
    }


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


def normalize_image(contents: bytes, filename: str) -> bytes:
    """Return image bytes img2pdf can embed directly.

    img2pdf embeds JPEG/PNG/GIF/BMP/WEBP/TIFF directly without recompression.
    For anything it can't embed as-is, fall back to re-encoding via Pillow.
    """
    try:
        img2pdf.convert(contents)
        return contents
    except img2pdf.ImageOpenError:
        try:
            image = Image.open(io.BytesIO(contents))
            image.load()
        except UnidentifiedImageError:
            raise HTTPException(status_code=400, detail=f"{filename}: unsupported or corrupt image file")

        if image.mode not in ("RGB", "L", "1"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


PAGE_SIZES_PT = {
    "a4": (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297)),
    "letter": (img2pdf.in_to_pt(8.5), img2pdf.in_to_pt(11)),
    "legal": (img2pdf.in_to_pt(8.5), img2pdf.in_to_pt(14)),
}


def get_page_layout(page_size: str):
    """Return an img2pdf layout_fun for a fixed page size, or None to fit each page to its image."""
    pagesize = PAGE_SIZES_PT.get(page_size)
    if not pagesize:
        return None
    return img2pdf.get_layout_fun(pagesize=pagesize, fit=img2pdf.FitMode.into)


@app.get("/jpgtopdf/upload/", response_class=HTMLResponse)
def jpgtopdf_form(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@app.post("/jpgtopdf/upload/")
async def jpgtopdf_upload(files: list[UploadFile] = File(...), page_size: str = Form("fit")):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    normalized_images = []
    for upload in files:
        contents = await upload.read()
        if not contents:
            raise HTTPException(status_code=400, detail=f"{upload.filename}: empty file")
        normalized_images.append(normalize_image(contents, upload.filename or "file"))

    layout_fun = get_page_layout(page_size)
    pdf_bytes = (
        img2pdf.convert(normalized_images, layout_fun=layout_fun)
        if layout_fun
        else img2pdf.convert(normalized_images)
    )

    first_stem = Path(files[0].filename).stem or "converted"
    download_stem = first_stem if len(files) == 1 else f"{first_stem}-and-{len(files) - 1}-more"

    return {
        **file_payload(pdf_bytes, "pdf"),
        "download_name": f"{download_stem}.pdf",
        "page_count": len(files),
        "output_size": len(pdf_bytes),
    }


def encode_image(image: Image.Image, quality: int) -> tuple[bytes, str]:
    """Encode an image at the given quality.

    Images with transparency are kept as palette-quantized PNGs (quality maps to
    palette size); everything else is re-encoded as JPEG at the given quality.
    """
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    buffer = io.BytesIO()

    if has_alpha:
        colors = max(16, min(256, round(16 + (quality / 100) * 240)))
        encoded = image.convert("RGBA").convert("P", palette=Image.ADAPTIVE, colors=colors)
        encoded.save(buffer, format="PNG", optimize=True)
        ext = "png"
    else:
        encoded = image.convert("RGB") if image.mode != "RGB" else image
        encoded.save(buffer, format="JPEG", quality=quality, optimize=True)
        ext = "jpg"

    return buffer.getvalue(), ext


MIN_QUALITY = 40  # never auto-compress below this: quality stays the priority


def compress_to_quality(image: Image.Image, quality: int, max_dimension: int) -> tuple[bytes, str, int, int, int]:
    if max_dimension and max(image.size) > max_dimension:
        image = image.copy()
        image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    data, ext = encode_image(image, quality)
    return data, ext, quality, image.width, image.height


def compress_to_target_size(
    image: Image.Image, target_bytes: int, max_dimension: int = 0
) -> tuple[bytes, str, int, bool, int, int]:
    """Binary-search quality within [MIN_QUALITY, 95] to land as close under target_bytes as
    possible, without ever dropping quality below MIN_QUALITY. Dimensions are only reduced if
    the caller passed a max_dimension explicitly - there is no automatic/hidden downscaling.

    Returns (data, ext, quality_used, floor_hit, width, height).
    """
    working = image
    if max_dimension and max(image.size) > max_dimension:
        working = image.copy()
        working.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

    floor_data, floor_ext = encode_image(working, MIN_QUALITY)
    if len(floor_data) > target_bytes:
        # Can't reach the target without going below the quality floor - stop here.
        return floor_data, floor_ext, MIN_QUALITY, True, working.width, working.height

    lo, hi = MIN_QUALITY, 95
    best = (floor_data, floor_ext, MIN_QUALITY)
    while lo <= hi:
        mid = (lo + hi) // 2
        data, ext = encode_image(working, mid)
        if len(data) <= target_bytes:
            best = (data, ext, mid)
            lo = mid + 1
        else:
            hi = mid - 1

    return best[0], best[1], best[2], False, working.width, working.height


@app.get("/compress/upload/", response_class=HTMLResponse)
def compress_form(request: Request):
    return templates.TemplateResponse(request, "compress.html", {})


@app.post("/compress/upload/")
async def compress_upload(
    file_photo: UploadFile = File(...),
    quality: int = Form(75),
    max_dimension: int = Form(0),
    target_size_mb: float = Form(0),
):
    contents = await file_photo.read()
    if not contents:
        raise HTTPException(status_code=400, detail="No file uploaded")

    try:
        image = Image.open(io.BytesIO(contents))
        image.load()
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="Unsupported or corrupt image file")

    max_dimension = max(0, max_dimension)
    quality_floor_hit = False

    if target_size_mb and target_size_mb > 0:
        target_bytes = int(target_size_mb * 1024 * 1024)
        if target_bytes < 1:
            raise HTTPException(status_code=400, detail="Target size must be greater than 0")
        output_bytes, ext, quality_used, quality_floor_hit, out_w, out_h = compress_to_target_size(
            image, target_bytes, max_dimension
        )
    else:
        quality = max(10, min(95, quality))
        output_bytes, ext, quality_used, out_w, out_h = compress_to_quality(image, quality, max_dimension)

    stem = Path(file_photo.filename).stem or "image"
    original_size = len(contents)
    compressed_size = len(output_bytes)
    reduction_percent = round((1 - compressed_size / original_size) * 100, 1) if original_size else 0

    return {
        **file_payload(output_bytes, ext),
        "download_name": f"{stem}-compressed.{ext}",
        "original_size": original_size,
        "compressed_size": compressed_size,
        "reduction_percent": reduction_percent,
        "quality_used": quality_used,
        "quality_floor_hit": quality_floor_hit,
        "output_width": out_w,
        "output_height": out_h,
    }


def merge_pdfs(files_bytes: list[bytes], page_size: str) -> tuple[bytes, int]:
    """Merge PDFs in order. page_size "original" keeps each source page's own size;
    otherwise every page is scaled (preserving aspect ratio, centered) onto a
    uniform target page size."""
    target = PAGE_SIZES_PT.get(page_size)
    output = pikepdf.Pdf.new()
    page_count = 0

    for data in files_bytes:
        try:
            with pikepdf.open(io.BytesIO(data)) as src:
                if target is None:
                    output.pages.extend(src.pages)
                    page_count += len(src.pages)
                else:
                    w, h = target
                    rect = pikepdf.Rectangle(0, 0, w, h)
                    for src_page in src.pages:
                        new_page = output.add_blank_page(page_size=(w, h))
                        new_page.add_overlay(src_page, rect)
                        page_count += 1
        except pikepdf.PdfError as exc:
            raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}")

    buffer = io.BytesIO()
    output.save(buffer)
    return buffer.getvalue(), page_count


@app.get("/mergepdf/upload/", response_class=HTMLResponse)
def mergepdf_form(request: Request):
    return templates.TemplateResponse(request, "mergepdf.html", {})


@app.post("/mergepdf/upload/")
async def mergepdf_upload(files: list[UploadFile] = File(...), page_size: str = Form("original")):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Select at least 2 PDFs to merge")

    files_bytes = []
    for upload in files:
        contents = await upload.read()
        if not contents:
            raise HTTPException(status_code=400, detail=f"{upload.filename}: empty file")
        files_bytes.append(contents)

    pdf_bytes, page_count = merge_pdfs(files_bytes, page_size)

    first_stem = Path(files[0].filename).stem or "merged"
    download_stem = f"{first_stem}-merged"

    return {
        **file_payload(pdf_bytes, "pdf"),
        "download_name": f"{download_stem}.pdf",
        "page_count": page_count,
        "output_size": len(pdf_bytes),
    }


REPORTLAB_PAGE_SIZES = {"a4": A4, "letter": LETTER, "legal": LEGAL}


def iter_block_items(document: Document):
    """Walk a docx body in document order, yielding Paragraph/Table wrappers.

    python-docx's .paragraphs and .tables are separate lists that don't preserve
    their relative order; this walks the underlying XML instead.
    """
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield DocxParagraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield DocxTable(child, document)


def run_markup(run) -> str:
    text = html.escape(run.text)
    if not text:
        return text
    if run.bold:
        text = f"<b>{text}</b>"
    if run.italic:
        text = f"<i>{text}</i>"
    if run.underline:
        text = f"<u>{text}</u>"
    return text


def heading_style(styles, style_name: str):
    if style_name == "Title":
        return styles["Title"]
    if style_name.startswith("Heading"):
        digits = "".join(c for c in style_name if c.isdigit())
        level = min(max(int(digits), 1), 4) if digits.isdigit() else 1
        return styles[f"Heading{level}"]
    return styles["Normal"]


def docx_to_pdf_bytes(files_bytes: list[bytes], pagesize) -> bytes:
    """Render one or more .docx files (in order, with a page break between them) to a
    single PDF. Preserves paragraph/heading structure, run-level bold/italic/underline,
    and tables. Images and complex layouts in the source are not carried over."""
    styles = getSampleStyleSheet()
    story = []

    for idx, data in enumerate(files_bytes):
        try:
            doc = Document(io.BytesIO(data))
        except Exception:
            raise HTTPException(status_code=400, detail="Unsupported or corrupt Word document")

        for block in iter_block_items(doc):
            if isinstance(block, DocxParagraph):
                if not block.text.strip():
                    story.append(Spacer(1, 8))
                    continue
                markup = "".join(run_markup(r) for r in block.runs) or html.escape(block.text)
                story.append(Paragraph(markup, heading_style(styles, block.style.name)))
                story.append(Spacer(1, 4))
            elif isinstance(block, DocxTable):
                rows = [[html.escape(cell.text) for cell in row.cells] for row in block.rows]
                if not rows:
                    continue
                table = Table(rows)
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ]
                    )
                )
                story.append(table)
                story.append(Spacer(1, 8))

        if idx < len(files_bytes) - 1:
            story.append(PageBreak())

    if not story:
        raise HTTPException(status_code=400, detail="No readable content found in the uploaded document(s)")

    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=pagesize).build(story)
    return buffer.getvalue()


@app.get("/wordtopdf/upload/", response_class=HTMLResponse)
def wordtopdf_form(request: Request):
    return templates.TemplateResponse(request, "wordtopdf.html", {})


@app.post("/wordtopdf/upload/")
async def wordtopdf_upload(files: list[UploadFile] = File(...), page_size: str = Form("a4")):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    files_bytes = []
    for upload in files:
        contents = await upload.read()
        if not contents:
            raise HTTPException(status_code=400, detail=f"{upload.filename}: empty file")
        files_bytes.append(contents)

    pagesize = REPORTLAB_PAGE_SIZES.get(page_size, A4)
    pdf_bytes = docx_to_pdf_bytes(files_bytes, pagesize)

    first_stem = Path(files[0].filename).stem or "converted"
    download_stem = first_stem if len(files) == 1 else f"{first_stem}-and-{len(files) - 1}-more"

    return {
        **file_payload(pdf_bytes, "pdf"),
        "download_name": f"{download_stem}.pdf",
        "page_count": len(files),
        "output_size": len(pdf_bytes),
    }


def pdf_to_docx_bytes(pdf_bytes: bytes) -> bytes:
    """Convert a single PDF to a .docx via pdf2docx, which needs real file paths."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_pdf_path = str(Path(tmp_dir) / "input.pdf")
        tmp_docx_path = str(Path(tmp_dir) / "output.docx")
        Path(tmp_pdf_path).write_bytes(pdf_bytes)

        try:
            converter = Converter(tmp_pdf_path)
            try:
                converter.convert(tmp_docx_path)
            finally:
                converter.close()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not convert PDF: {exc}")

        return Path(tmp_docx_path).read_bytes()


@app.get("/pdftoword/upload/", response_class=HTMLResponse)
def pdftoword_form(request: Request):
    return templates.TemplateResponse(request, "pdftoword.html", {})


@app.post("/pdftoword/upload/")
async def pdftoword_upload(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    results = []
    for upload in files:
        contents = await upload.read()
        if not contents:
            raise HTTPException(status_code=400, detail=f"{upload.filename}: empty file")
        docx_bytes = pdf_to_docx_bytes(contents)
        stem = Path(upload.filename).stem or "document"
        results.append((stem, docx_bytes))

    if len(results) == 1:
        stem, docx_bytes = results[0]
        return {
            **file_payload(docx_bytes, "docx"),
            "download_name": f"{stem}.docx",
            "output_size": len(docx_bytes),
            "is_zip": False,
            "file_count": 1,
        }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for stem, docx_bytes in results:
            name = f"{stem}.docx"
            suffix = 2
            while name in used_names:
                name = f"{stem}-{suffix}.docx"
                suffix += 1
            used_names.add(name)
            zf.writestr(name, docx_bytes)
    zip_bytes = buffer.getvalue()

    return {
        **file_payload(zip_bytes, "zip"),
        "download_name": "converted-documents.zip",
        "output_size": len(zip_bytes),
        "is_zip": True,
        "file_count": len(results),
    }


# Lambda entrypoint (unused outside Lambda - harmless everywhere else).
# Mangum adapts API Gateway/Function URL events to ASGI calls into `app`.
from mangum import Mangum  # noqa: E402

handler = Mangum(app)
