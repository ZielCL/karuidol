# Usa una imagen ligera de Python
FROM python:3.10-slim

# Evita crear archivos .pyc y buffering en logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copia e instala dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código
COPY . .

# Usa gunicorn para servir tu Flask app
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:$PORT", "--workers", "1", "--threads", "4"]
