# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Jangan buffer stdout/stderr & jangan tulis .pyc (cocok untuk container).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency app (layer terpisah agar cache build tetap valid saat kode berubah).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Salin seluruh source code aplikasi.
COPY . .

EXPOSE 8000

# Uvicorn melayani di 0.0.0.0 agar endpoint dapat diakses dari luar container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]