FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static

# maps.db (and optional config.json) live here; mount it to Unraid appdata.
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 5000

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "app:app"]
