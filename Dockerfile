# 使用 Python 3.11 作为基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    DEBIAN_FRONTEND=noninteractive

# 安装系统依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-libmysqlclient-dev \
        pkg-config \
        build-essential \
        netcat-traditional \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.trusted-host mirrors.aliyun.com \
    && pip config set global.timeout 300 \
    && pip config set global.retries 5 \
    && pip install --no-cache-dir pip setuptools wheel --upgrade \
    && pip install --no-cache-dir pillow==11.1.0 psycopg2-binary==2.9.10

# 在 Python 依赖安装后，创建 celery 用户和组
RUN groupadd -r celery && useradd -r -g celery celery

# 确保目录权限正确
RUN mkdir -p /app/logs/celery /app/celerybeat-data && \
    chown -R celery:celery /app/logs /app/celerybeat-data

# 复制 requirements.txt 并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 创建必要的目录
RUN mkdir -p /app/logs/celery /app/media /app/staticfiles \
    && chmod -R 755 /app/logs /app/media /app/staticfiles

# 复制启动脚本并设置权限
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 复制项目文件
COPY . .

# 暴露端口
EXPOSE 8000

# 设置启动命令
ENTRYPOINT ["docker-entrypoint.sh"]

# 默认启动命令（会被 docker-compose 中的 command 覆盖）
CMD ["gunicorn", "config.wsgi:application", "--config=/app/gunicorn.conf.py"] 