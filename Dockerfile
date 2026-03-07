FROM python:3.12-slim

# Устанавливаем FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Railway использует переменную PORT
ENV PORT=8000
EXPOSE 8000

CMD ["python", "main.py"]
