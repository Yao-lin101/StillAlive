#!/bin/bash

# 获取脚本所在目录的绝对路径
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd $PROJECT_DIR

# 激活虚拟环境（如果存在）
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# 加载环境变量
if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# 创建必要的目录
mkdir -p logs
mkdir -p media
mkdir -p staticfiles

# 收集静态文件
echo "Collecting static files..."
python manage.py collectstatic --noinput

# 执行数据库迁移
echo "Applying database migrations..."
python manage.py migrate

# 启动 Gunicorn
echo "Starting Gunicorn..."
exec gunicorn config.wsgi:application -c gunicorn.conf.py 