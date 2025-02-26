# SMTX 部署指南

本文档介绍如何使用 Docker 部署 SMTX 服务端项目。

## 前置要求

- Docker (20.10+)
- Docker Compose (2.0+)
- 域名（用于生产环境）
- SSL 证书（推荐用于生产环境）

## 部署步骤

### 1. 准备环境

1. 克隆项目代码：
```bash
git clone <repository_url>
cd Server
```

2. 创建环境变量文件：
```bash
cp .env.example .env
```

3. 编辑 .env 文件，设置必要的环境变量：
- DJANGO_SECRET_KEY：Django 密钥
- ALLOWED_HOSTS：允许的域名
- DB_PASSWORD：数据库密码
- EMAIL_* 相关配置：邮件服务配置

### 2. 配置 Nginx

1. 编辑 nginx/smtx.conf 文件，修改 server_name 为你的域名：
```nginx
server_name your-domain.com;
```

2. 如果使用 HTTPS，将 SSL 证书文件放在适当位置并配置 Nginx。

### 3. 启动服务

1. 构建并启动所有服务：
```bash
docker-compose up -d --build
```

2. 查看服务状态：
```bash
docker-compose ps
```

3. 查看日志：
```bash
docker-compose logs -f
```

## 维护操作

### 日常维护

- 重启服务：
```bash
docker-compose restart
```

- 停止服务：
```bash
docker-compose down
```

- 查看服务日志：
```bash
docker-compose logs -f [service_name]
```

### 数据备份

1. 备份数据库：
```bash
docker-compose exec db mysqldump -u root -p${DB_PASSWORD} ${DB_NAME} > backup_$(date +%Y%m%d).sql
```

2. 备份媒体文件：
```bash
tar -czf media_backup_$(date +%Y%m%d).tar.gz media/
```

### 更新部署

1. 拉取最新代码：
```bash
git pull origin main
```

2. 重新构建并启动服务：
```bash
docker-compose up -d --build
```

## 目录结构

```
Server/
├── docker-compose.yml    # Docker Compose 配置文件
├── Dockerfile           # Docker 构建文件
├── docker-entrypoint.sh # Docker 启动脚本
├── nginx/              # Nginx 配置目录
├── logs/               # 日志目录
├── media/              # 媒体文件目录
└── staticfiles/        # 静态文件目录
```

## 性能优化

1. Gunicorn 配置
- 在 docker-compose.yml 中调整 workers 数量：
```yaml
command: gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4
```
建议 workers = (2 * CPU核心数 + 1)

2. Nginx 优化
- 配置静态文件缓存
- 启用 gzip 压缩
- 优化 worker 进程

3. 数据库优化
- 定期维护索引
- 配置适当的连接池大小
- 监控慢查询

## 监控和日志

1. 日志位置：
- Django 日志：`logs/django.log`
- Nginx 访问日志：`logs/nginx/access.log`
- Nginx 错误日志：`logs/nginx/error.log`

2. 监控建议：
- 使用 Prometheus + Grafana 监控系统资源
- 配置日志轮转
- 设置关键指标告警

## 安全建议

1. 基本安全措施：
- 及时更新系统和依赖包
- 使用强密码
- 限制管理后台访问IP
- 启用 HTTPS

2. 数据安全：
- 定期备份数据
- 加密敏感信息
- 实施访问控制

3. 网络安全：
- 配置防火墙规则
- 限制端口访问
- 使用 HTTPS 证书

## 故障排除

1. 服务无法启动：
- 检查日志：`docker-compose logs web`
- 验证环境变量配置
- 确认端口未被占用

2. 数据库连接问题：
- 检查数据库日志：`docker-compose logs db`
- 验证数据库凭据
- 确认数据库服务状态

3. 静态文件无法访问：
- 执行静态文件收集：`docker-compose exec web python manage.py collectstatic`
- 检查 Nginx 配置
- 验证文件权限

## 联系与支持

如遇到部署问题，请：
1. 查看详细日志
2. 检查配置文件
3. 参考故障排除章节
4. 联系技术支持团队

## 更新记录

- 2024-03-XX：初始版本
- [后续更新记录]
