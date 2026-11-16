FROM python:3.11-slim

# 纯标准库实现，不需要 pip install，镜像就是基础镜像 + 几十 KB 代码
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8123 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai

WORKDIR /app

COPY app /app/app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8123

# 健康检查：能打开首页就算活着
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8123/api/status', timeout=4).status==200 else 1)"

CMD ["python", "-u", "-m", "app.main"]
