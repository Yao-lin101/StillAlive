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

# 确保日志目录存在并设置权限
echo "=== Setting up Directories ==="
mkdir -p /app/logs /app/media /app/staticfiles
chmod -R 755 /app/logs /app/media /app/staticfiles
echo "✓ Directories setup completed"

# 收集静态文件
echo "=== Collecting Static Files ==="
python manage.py collectstatic --noinput
echo "✓ Static files collected"

# 应用数据库迁移
echo "=== Applying Database Migrations ==="
python manage.py migrate --noinput
echo "✓ Database migrations applied"

# 启动应用
echo "=== Starting Application ==="
exec "$@" 