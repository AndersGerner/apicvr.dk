FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY apis/ ./apis/
COPY modules/ ./modules/
COPY frontend/ ./frontend/
COPY main.py ./
RUN useradd --system --uid 10001 cvr && mkdir /data && chown cvr /data
USER cvr
ENV STATS_DB_PATH=/data/stats.db
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-3450} --no-access-log"]
