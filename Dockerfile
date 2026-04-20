FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias del sistema para firebase-admin (grpc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código del backend
COPY app/ .

# La clave de Firebase se monta como volumen en producción
# (no se incluye en la imagen)

CMD ["python", "-u", "main.py"]
