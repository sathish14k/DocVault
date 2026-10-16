FROM python:3.12-slim

# LibreOffice: Word/Excel/PowerPoint -> PDF (reader preview + combined binder PDF)
# Poppler + Tesseract: OCR for scanned PDFs and images so they become searchable
# Add more OCR languages with e.g. tesseract-ocr-hin and set OCR_LANGS=eng+ara+hin
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      fonts-dejavu fonts-liberation tzdata \
      poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data TZ=Asia/Dubai OCR_LANGS=eng+ara
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
