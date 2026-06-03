# 部署说明 / DEPLOY

本文件汇总三块改动的上线步骤：

1. **向量后端 Milvus → pgvector**（长期记忆语义检索改用 Postgres 自带 pgvector，可降级，不再强依赖 Milvus）
2. **嵌入模型后台可配置**（admin「嵌入模型配置」，支持 Ollama / OpenAI 兼容，可热改 base_url / api_key / 模型 / 维度）
3. **开放 admin 访问**（nginx 用 Basic Auth 保护 `/admin/`，并反代 `/static/`）

---

## 0. 改动涉及的新依赖 / 配置

- 新增 pip 依赖：`pgvector==0.3.6`、`whitenoise==6.7.0`（已写入 `requirements.txt`）→ **必须重建应用镜像**，光重启不行（`models.py` 顶部 import pgvector，中间件 import whitenoise）。
- Postgres 镜像：`postgres:15-alpine` → `pgvector/pgvector:pg15`（`Server/docker-compose.yml`）。数据卷兼容（同为 PG15），无需 dump/restore。
- 新迁移：`0023_importantevent_embedding`（建 vector 扩展 + 不固定维度的 `embedding` 列）、`0024_embeddingconfig`（嵌入配置单例表）。
- 新环境变量：`VECTOR_BACKEND`（默认 `auto`，优先 pgvector）。`OLLAMA_*` 降级为「EmbeddingConfig 首次播种的默认值」，运行时以 admin 配置为准。

---

## 1. 服务器上线步骤

> 应用 compose 在 `Server/`，服务名：`web` / `celery_worker` / `celery_beat` / `db` / `redis`。
> nginx 是独立 compose（目录 `all_nginx/`，容器名 `all_nginx`）。

```bash
cd /path/to/StillAlive/Server
git pull

# 1) 先用 pgvector 镜像重建 db 容器（数据保留）
docker compose up -d db

# 2) 重建应用镜像（pgvector + whitenoise 新依赖），三个跑 Django 的服务都要
docker compose build web celery_worker celery_beat
docker compose up -d web celery_worker celery_beat

# 3) 迁移（建 vector 扩展 + embedding 列 + EmbeddingConfig 表）
docker compose exec web python manage.py migrate

# 4) 收集静态文件（admin 的 CSS/JS，WhiteNoise 提供）
docker compose exec web python manage.py collectstatic --noinput

# 5) 回填已有事件的向量（用当前嵌入配置重新生成）
docker compose exec web python manage.py rebuild_milvus_memory --clean --force

# 6) 没有管理员账号则创建
docker compose exec web python manage.py createsuperuser
```

### nginx（开放 admin）
- `all_nginx/nginx/stillalive.conf` 已加 `location /admin/`（Basic Auth）和 `location /static/`（反代 gunicorn）。
- **创建 Basic Auth 口令文件**（一次性，住宅 IP 动态，不再用 IP 白名单）：

```bash
# 口令文件须放在容器内 /etc/nginx/conf.d/.htpasswd（= 宿主 all_nginx/nginx/.htpasswd）
# 方式 A：用 nginx 容器自带的 openssl 生成（无需装 htpasswd）
docker exec all_nginx sh -c \
  "printf 'admin:%s\n' \"\$(openssl passwd -apr1 '你的强密码')\" > /etc/nginx/conf.d/.htpasswd"

# 方式 B：宿主机有 htpasswd 时
#   htpasswd -cb /www/wwwroot/all_nginx/.../nginx/.htpasswd admin '你的强密码'

# 校验配置并重载
docker exec all_nginx nginx -t && docker exec all_nginx nginx -s reload
```

访问 `https://alive.ineed.asia/admin/`：先过浏览器密码框（上面设的 admin / 强密码），再进 Django 登录页。任意网络可用，无需再改 nginx。

---

## 2. 配置嵌入模型（admin）

`/admin/` →「嵌入模型配置」（单例，首次自动用当前 `OLLAMA_*` 环境变量播种）：

- `provider`：`ollama` 或 `openai`（OpenAI 兼容）。
- `base_url`：Ollama 如 `http://host:11434`；OpenAI 兼容如 `https://api.openai.com/v1`（也可指向 Azure / vLLM / LocalAI / 硅基流动等）。
- `api_key`：OpenAI 兼容端点需要；Ollama 留空。**明文存库**，仅在受信任的自托管环境使用。
- `model` / `dimensions` / `max_chars` / `timeout`。
- 保存即时生效（多进程无需重启）。
- 两个操作按钮（选中那行 → Action）：
  - **测试连接**：用当前配置生成一次嵌入，返回维度即成功。
  - **重建向量**：后台线程重新 embed 全部事件。**改了模型或维度后必须执行**（见下）。

---

## 3. 重要注意事项

### 维度一致性（最容易踩）
- pgvector 的 `embedding` 列是**不固定维度**的，因为 dev 与 prod 用不同模型：
  - dev：`nomic-embed-text-v2-moe`（768 维）
  - prod：`mxbai-embed-large`（1024 维）
- 同一个迁移因此能同时服务两套环境。代价：不建 HNSW 索引，走精确搜索（个人数据量足够）。
- **改模型/维度后旧向量维度与新查询不一致，cosine 检索会失效** → 必须点 admin「重建向量」或跑 `rebuild_milvus_memory --clean --force`。

### admin 访问
- `/admin/` 由 nginx Basic Auth 保护（口令文件 `all_nginx/nginx/.htpasswd`）。设一次永久生效，任意网络可用——住宅 IP 动态，**不要**用 IP 白名单。
- admin 登录是 HTTPS 反代 POST，`CSRF_TRUSTED_ORIGINS=['https://alive.ineed.asia']` 已在 `production.py` 配好；换域名记得同步加。
- 改 Basic Auth 密码：重跑上面的 htpasswd 生成命令覆盖 `.htpasswd`，再 `nginx -s reload`。

### 想继续用 Milvus（而非 pgvector）
- 在服务器 env 设 `VECTOR_BACKEND=milvus`，并保留 Milvus 那套 docker 栈。
- 不设则 `auto` 会选 pgvector（旧 Milvus 向量弃用，数据在 Postgres，回填即可）。

---

## 4. 本地开发环境（原生 Homebrew Postgres）

本地用的是原生 `postgresql@15`（非 docker），需手动装 pgvector 扩展（一次性）：

```bash
# pgvector 扩展（针对 postgresql@15 源码编译，避免 brew 拉错 PG 版本）
git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector
cd /tmp/pgvector
PGC=/opt/homebrew/opt/postgresql@15/bin/pg_config
make PG_CONFIG=$PGC && make install PG_CONFIG=$PGC
brew services restart postgresql@15

# Python 依赖
cd /path/to/StillAlive/Server
pip install pgvector==0.3.6 whitenoise==6.7.0

# 迁移 + 回填（dev 是 768 维，需 Ollama 在跑）
python manage.py migrate
python manage.py rebuild_milvus_memory --clean --force
```

---

## 5. 排错速查

| 现象 | 原因 / 处理 |
|------|------|
| admin 打开是前端页面 | nginx 没路由 `/admin/`（确认 `stillalive.conf` 改动已 reload） |
| admin 登录 403 CSRF | `CSRF_TRUSTED_ORIGINS` 未含当前域名 |
| admin 样式全丢 | 没跑 `collectstatic`，或 `/static/` 未反代 / whitenoise 中间件缺失 |
| admin 弹密码框但进不去 | Basic Auth 密码错，或 `.htpasswd` 路径/格式不对（须在 `/etc/nginx/conf.d/.htpasswd`） |
| admin 不弹密码框直接 401 | `.htpasswd` 文件不存在或 nginx 没 reload |
| 检索无语义结果、日志报维度不匹配 | 改过模型/维度未重建 → 点「重建向量」 |
| 嵌入失败 502 / 超时 | 检查 `base_url` 指向的 Ollama / OpenAI 端点是否可达 |
| 容器起不来 ImportError pgvector/whitenoise | 镜像没重建，`docker compose build` 后再 up |
