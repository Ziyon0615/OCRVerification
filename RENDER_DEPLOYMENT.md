# Render Deployment

This project is configured for Render with Docker so the app can install OCR system tools:

- `tesseract-ocr` for `pytesseract`
- `poppler-utils` for PDF conversion with `pdf2image`
- `gunicorn` for production Flask serving

## Deploy

1. Push this repo to GitHub.
2. In Render, create a new Blueprint or Web Service from this repository.
3. Render will use `render.yaml` and the `Dockerfile`.
4. After the build finishes, open the Render URL and log in:
   - `admin / jethro123`
   - `officer / officer123`

## Notes

- The first load on Render's free plan can be slow because free services sleep after inactivity.
- OCR analysis is CPU-heavy, so free instances can take longer to verify IDs.
- The current first Render version still uses local SQLite and local uploaded files. Data can reset on redeploy/restart unless you later add persistent storage or migrate to Neon PostgreSQL.
- The next recommended step is moving the database from SQLite to Neon.
