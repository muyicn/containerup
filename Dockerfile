# 容器守望者：一体化 Docker 容器更新守望平台
# 数据持久化：挂载 /data（SQLite 数据库所在）
# 管理宿主机容器：运行时挂载 /var/run/docker.sock
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 内置 Docker CLI（官方静态客户端，仅取 docker 一个文件；供平台探测/操控宿主机引擎）
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz', '/tmp/docker.tgz')" \
    && tar -xzf /tmp/docker.tgz -C /tmp docker/docker \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz

# 内置 docker compose v2 插件：compose 管理的容器更新需 compose up 重建（配置保真）
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64', '/usr/local/lib/docker/cli-plugins/docker-compose')" \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && docker compose version

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
