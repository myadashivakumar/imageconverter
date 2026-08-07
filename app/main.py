import io
import uuid
from pathlib import Path

import img2pdf
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="JPG to PDF Converter")
app.mount("/media", StaticFiles(directory=MEDIA_ROOT), name="media")

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def to_pdf_bytes(contents: bytes) -> bytes:
    """Convert arbitrary image bytes to a PDF.

    img2pdf embeds JPEG/PNG/GIF/BMP/WEBP/TIFF directly without recompression.
    For anything it can't embed as-is, fall back to re-encoding via Pillow.
    """
    try:
        return img2pdf.convert(contents)
    except img2pdf.ImageOpenError:
        try:
            image = Image.open(io.BytesIO(contents))
            image.load()
        except UnidentifiedImageError:
            raise HTTPException(status_code=400, detail="Unsupported or corrupt image file")

        if image.mode not in ("RGB", "L", "1"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return img2pdf.convert(buffer.getvalue())


@app.get("/jpgtopdf/upload/", response_class=HTMLResponse)
def jpgtopdf_form(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@app.post("/jpgtopdf/upload/")
async def jpgtopdf_upload(file_photo: UploadFile = File(...)):
    contents = await file_photo.read()
    if not contents:
        raise HTTPException(status_code=400, detail="No file uploaded")

    pdf_bytes = to_pdf_bytes(contents)

    file_stem = Path(file_photo.filename).stem or "converted"
    pdf_name = f"{file_stem}-{uuid.uuid4().hex[:8]}.pdf"
    (MEDIA_ROOT / pdf_name).write_bytes(pdf_bytes)

    return {
        "pdf_url": f"/media/{pdf_name}",
        "download_name": f"{file_stem}.pdf",
    }
