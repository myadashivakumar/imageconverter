import io
import uuid
from pathlib import Path

import img2pdf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="JPG to PDF Converter")
app.mount("/media", StaticFiles(directory=MEDIA_ROOT), name="media")

templates = Jinja2Templates(directory=APP_DIR / "templates")


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


@app.get("/jpgtopdf/upload/", response_class=HTMLResponse)
def jpgtopdf_form(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@app.post("/jpgtopdf/upload/")
async def jpgtopdf_upload(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    normalized_images = []
    for upload in files:
        contents = await upload.read()
        if not contents:
            raise HTTPException(status_code=400, detail=f"{upload.filename}: empty file")
        normalized_images.append(normalize_image(contents, upload.filename or "file"))

    pdf_bytes = img2pdf.convert(normalized_images)

    first_stem = Path(files[0].filename).stem or "converted"
    download_stem = first_stem if len(files) == 1 else f"{first_stem}-and-{len(files) - 1}-more"
    pdf_name = f"{download_stem}-{uuid.uuid4().hex[:8]}.pdf"
    (MEDIA_ROOT / pdf_name).write_bytes(pdf_bytes)

    return {
        "pdf_url": f"/media/{pdf_name}",
        "download_name": f"{download_stem}.pdf",
        "page_count": len(files),
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


def compress_to_quality(image: Image.Image, quality: int, max_dimension: int) -> tuple[bytes, str, int]:
    if max_dimension and max(image.size) > max_dimension:
        image = image.copy()
        image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    data, ext = encode_image(image, quality)
    return data, ext, quality


def compress_to_target_size(image: Image.Image, target_bytes: int) -> tuple[bytes, str, int]:
    """Binary-search quality, downscaling further if even the lowest quality is still too big,
    to land as close under target_bytes as possible."""
    scale = 1.0
    last_attempt = None

    while True:
        working = image
        if scale < 1.0:
            working = image.copy()
            working.thumbnail(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.LANCZOS,
            )

        lo, hi = 10, 95
        best_under_target = None
        while lo <= hi:
            mid = (lo + hi) // 2
            data, ext = encode_image(working, mid)
            last_attempt = (data, ext, mid)
            if len(data) <= target_bytes:
                best_under_target = (data, ext, mid)
                lo = mid + 1
            else:
                hi = mid - 1

        if best_under_target:
            return best_under_target
        if scale <= 0.25:
            return last_attempt
        scale -= 0.15


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

    if target_size_mb and target_size_mb > 0:
        target_bytes = int(target_size_mb * 1024 * 1024)
        if target_bytes < 1:
            raise HTTPException(status_code=400, detail="Target size must be greater than 0")
        output_bytes, ext, quality_used = compress_to_target_size(image, target_bytes)
    else:
        quality = max(10, min(95, quality))
        max_dimension = max(0, max_dimension)
        output_bytes, ext, quality_used = compress_to_quality(image, quality, max_dimension)

    stem = Path(file_photo.filename).stem or "image"
    out_name = f"{stem}-compressed-{uuid.uuid4().hex[:8]}.{ext}"
    (MEDIA_ROOT / out_name).write_bytes(output_bytes)

    original_size = len(contents)
    compressed_size = len(output_bytes)
    reduction_percent = round((1 - compressed_size / original_size) * 100, 1) if original_size else 0

    return {
        "image_url": f"/media/{out_name}",
        "download_name": f"{stem}-compressed.{ext}",
        "original_size": original_size,
        "compressed_size": compressed_size,
        "reduction_percent": reduction_percent,
        "quality_used": quality_used,
    }
