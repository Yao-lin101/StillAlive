#!/bin/bash

echo "=== Starting StillAlive Application ==="
echo "Environment: $DJANGO_SETTINGS_MODULE"
echo "Database Host: $DB_HOST:$DB_PORT"
echo "Redis Host: $REDIS_HOST:6379"
echo "Celery Broker: $CELERY_BROKER_URL"

# 等待 PostgreSQL 准备就绪
echo "=== Checking PostgreSQL Connection ==="
echo "Waiting for PostgreSQL..."
while ! nc -z $DB_HOST $DB_PORT; do
    echo "PostgreSQL is unavailable - sleeping"
    sleep 1
done
echo "✓ PostgreSQL is up and running"

# 等待 Redis 准备就绪
echo "=== Checking Redis Connection ==="
echo "Waiting for Redis..."
while ! nc -z $REDIS_HOST 6379; do
    echo "Redis is unavailable - sleeping"
    sleep 1
done
echo "✓ Redis is up and running"

# 只有 Web 容器才负责初始化文件系统和静态资源
if [[ "$*" == *"gunicorn"* ]] || [[ "$*" == *"manage.py runserver"* ]]; then
    echo "=== Setting up Directories & Static Files ==="
    mkdir -p /app/logs /app/media /app/staticfiles
    # 允许 celery 组读写日志和静态目录（如果需要的话）
    chmod -R 775 /app/logs /app/media /app/staticfiles
    
    echo "=== Collecting Static Files ==="
    python manage.py collectstatic --noinput
    echo "✓ Setup completed"
else
    echo "=== Skipping Filesystem setup for non-web container ==="
fi

# 独立处理 celerybeat 数据目录，确保其始终存在
if [ ! -d "/app/celerybeat-data" ]; then
    mkdir -p /app/celerybeat-data
fi

# 应用数据库迁移 (仅在 Web 容器中执行，避免并发冲突)
if [[ "$*" == *"gunicorn"* ]] || [[ "$*" == *"manage.py runserver"* ]]; then
    echo "=== Applying Database Migrations ==="
    python manage.py migrate --noinput
    echo "✓ Database migrations applied"
else
    echo "=== Skipping Migrations for non-web container ==="
fi

# 启动应用
echo "=== Starting Application ==="
exec "$@" 