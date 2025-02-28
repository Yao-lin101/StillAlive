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
        cron \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.trusted-host mirrors.aliyun.com \
    && pip config set global.timeout 300 \
    && pip config set global.retries 5 \
    && pip install --no-cache-dir pip setuptools wheel --upgrade \
    && pip install --no-cache-dir pillow==11.1.0 psycopg2-binary==2.9.10

# 复制 requirements.txt 并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 创建必要的目录
RUN mkdir -p /app/logs /app/media /app/staticfiles \
    && mkdir -p /var/spool/cron/crontabs \
    && touch /app/logs/cron.log \
    && chmod 666 /app/logs/cron.log

# 复制启动脚本并设置权限
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 复制项目文件
COPY . .

# 暴露端口
EXPOSE 8000

# 设置启动命令
ENTRYPOINT ["docker-entrypoint.sh"]

# 在启动命令中添加 cron 服务
CMD service cron start && python manage.py crontab add && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4 --timeout 120 