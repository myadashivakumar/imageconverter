import io
import uuid
from pathlib import Path

import img2pdf
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_ROOT.mkdir(exist_ok=True)
STATIC_DIR = APP_DIR / "static"

app = FastAPI(title="JPG to PDF Converter")
app.mount("/media", StaticFiles(directory=MEDIA_ROOT), name="media")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=APP_DIR / "templates")


@app.get("/sw.js")
def service_worker():
    # Served from the root path (not /static/) so its default scope covers the whole app.
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")


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
