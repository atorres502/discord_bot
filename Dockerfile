FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY discbot.py .

# Print logs immediately so `docker logs` shows them
ENV PYTHONUNBUFFERED=1
CMD ["python", "discbot.py"]
