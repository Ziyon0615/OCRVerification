# OCRverification

## Overview

Small Flask app for verifying OCR'd identity documents (drivers license, national ID, passport).

## Prerequisites

- Python 3.10 or later (add to PATH)
- Git (optional)
- Tesseract OCR (required for `pytesseract`):
  - Windows: download and install from https://github.com/tesseract-ocr/tesseract (installer).
  - Default Windows path: `C:\Program Files\Tesseract-OCR\tesseract.exe` — ensure this exists or update `app.py`.
- Poppler (required for `pdf2image` when processing PDFs):
  - Windows: download a Poppler binary (e.g. from http://blog.alivate.com.au/poppler-windows/) and add its `bin` folder to your PATH.

## Project files to keep when transferring to another PC

- `requirements.txt` — Python packages
- `app.py` — main application
- `references/` — reference documents used for comparison (keep subfolders: `drivers_license`, `national_id`, `passport`)
- `reports/` and `uploads/` — runtime data (optional to keep or migrate)

## Setup (Windows) — copy project and run

1. Copy the entire project folder to the target machine (preserve `references/` if you need the same reference files).

2. Install system dependencies:

```powershell
# Install Python (from python.org) and ensure python is on PATH
# Install Tesseract: run the Windows installer and accept default path
# Add Poppler's `bin` folder to PATH (if you need PDF support)
```

3. Create and activate a virtual environment in the project folder:

PowerShell:
```powershell
python -m venv venv
.\venv\Scripts\Activate
```

Command Prompt:
```cmd
python -m venv venv
venv\Scripts\activate.bat
```

4. Upgrade pip and install Python dependencies:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

5. Verify Tesseract is reachable by the app:
- Use the default path `C:\Program Files\Tesseract-OCR\tesseract.exe` (app already tries this). If you installed Tesseract to a different location, either:
  - Edit `app.py` and set `pytesseract.pytesseract.tesseract_cmd = r'<full-path-to>\tesseract.exe'` near the top, or
  - Add the Tesseract folder to your system PATH so the binary is discoverable.

6. Verify Poppler is in PATH if you need PDF support (pdf2image -> `convert_from_path`).

## Running the app

From the activated virtualenv, start the server:

```powershell
python -3 app.py
```

Open your browser at http://localhost:5000 (the app runs on port 5000 by default).

## Common troubleshooting

- "TesseractNotFound" or OCR not working: confirm `tesseract.exe` path and update `app.py` or PATH.
- `pdf2image` errors: install Poppler and ensure `poppler/bin` is on PATH.
- Windows permission errors writing to folders: ensure the `uploads/`, `reports/`, and `references/` folders are writable by the user running the app.

## Notes for production

- The app uses a built-in secret and simple credentials — replace them and use a proper database and HTTPS in production.
- Consider running behind a production WSGI server (Gunicorn/Waitress) and using HTTPS.

---

If you want, I can also run the app locally and verify startup, or add a short script to create the necessary folders automatically.
# OCRverification
