# Imagen base
FROM python:3.10-slim

# Variables de entorno para que Python no genere .pyc
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copia e instala dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el código
COPY . .

CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:$PORT", "--workers", "1", "--threads", "4"]
