#!/bin/bash

echo "=== Starting SMTX Application ==="
echo "Environment: $DJANGO_SETTINGS_MODULE"
echo "Database Host: $DB_HOST:$DB_PORT"
echo "Redis Host: $REDIS_HOST:6379"

# 等待 MySQL 准备就绪
echo "=== Checking MySQL Connection ==="
echo "Waiting for MySQL..."
while ! nc -z $DB_HOST $DB_PORT; do
    echo "MySQL is unavailable - sleeping"
    sleep 1
done
echo "✓ MySQL is up and running"

# 等待 Redis 准备就绪
echo "=== Checking Redis Connection ==="
echo "Waiting for Redis..."
while ! nc -z $REDIS_HOST 6379; do
    echo "Redis is unavailable - sleeping"
    sleep 1
done
echo "✓ Redis is up and running"

# 收集静态文件
echo "=== Collecting Static Files ==="
python manage.py collectstatic --noinput
echo "✓ Static files collected"

# 应用数据库迁移
echo "=== Applying Database Migrations ==="
python manage.py migrate --noinput
echo "✓ Database migrations applied"

# 初始化语言分区
echo "=== Initializing Language Sections ==="
python manage.py init_language_sections
echo "✓ Language sections initialized"

# 启动应用
echo "=== Starting Gunicorn Server ==="
echo "Workers: 4"
echo "Bind: 0.0.0.0:8000"
echo "Config: /app/gunicorn.conf.py"
exec "$@" 