# 容器守望者：一体化 Docker 容器更新守望平台
# 数据持久化：挂载 /data（SQLite 数据库所在）
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY backend/ backend/
COPY frontend/dist/ frontend/dist/
COPY assets/ assets/

ENV VT_HOST=0.0.0.0 \
    VT_PORT=9412 \
    VT_DB_PATH=/data/vigiltainer.db \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
VOLUME /data
EXPOSE 9412

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:9412/api/public/health', timeout=3); sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["python", "run.py"]
