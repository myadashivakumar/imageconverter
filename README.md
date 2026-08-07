# imageconvertfastapi

Image to PDF converter, built with FastAPI. Installable as a PWA. Supports JPG, PNG,
GIF, BMP, WEBP, and TIFF; select multiple images to merge them into a single
multi-page PDF.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/jpgtopdf/upload/ and upload one or more images.
