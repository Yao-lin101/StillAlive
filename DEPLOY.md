# 部署说明 / DEPLOY

本文件汇总以下改动的上线步骤：

1. **向量后端 Milvus → pgvector**（长期记忆语义检索改用 Postgres 自带 pgvector，可降级，不再强依赖 Milvus）
2. **嵌入模型后台可配置**（admin「嵌入模型配置」，支持 Ollama / OpenAI 兼容，可热改 base_url / api_key / 模型 / 维度）
3. **开放 admin 访问**（nginx 用 Basic Auth 保护 `/admin/`，并反代 `/static/`）
4. **LLM 通过 LangChain 接入**（admin「LLM 供应商」内管理模型，「LLM 调用链配置」选择与排序调用模型）

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

## 6. LLM 供应商、模型与自动降级（LangChain）

模型目录与调用顺序分别管理：

- **LLM 供应商**：统一管理连接配置及其下属模型。供应商包含名称、协议、Base URL、API Key、超时和重试；下属模型包含实际模型 ID、显示名称、Token 上限、温度和启用状态。同一供应商可添加多个模型，同一协议也可配置多个供应商。模型没有独立后台入口。
- **LLM 调用链配置**：独立的全局配置，从所有供应商的模型目录中选择模型，按列表从上到下调用。只有被选中的模型参与自动调用。

打开 `/admin/` →「角色管理」→「LLM 供应商」，左侧选择或新增供应商，右侧维护当前供应商。
展开「供应商配置」设置协议、地址、密钥和请求参数；在「已配置的模型」中添加或搜索模型、启用停用、编辑参数、测试连接或删除。
点击「保存配置」会一次保存供应商及其模型。新增或启用模型不会自动将其加入调用链。
供应商的查看/修改权限同时控制其下属模型；只读用户不能修改模型、获取模型列表或发送连接测试。

点击「获取模型列表」会使用当前表单中的协议、Base URL、密钥、超时和重试配置，从供应商读取模型目录。
密钥留空时沿用已保存值；支持在尚未保存的新供应商页面直接拉取，也不会提前保存连接配置。
结果临时显示在下方「可用模型」区域，支持按 ID 或显示名称搜索、多选及全选当前搜索结果。
点击「添加所选模型」将选中的模型加入上方编辑列表；已在配置列表中的模型会标记并禁选，防止重复添加。
之后点击「保存配置」才会写入数据库，未选中的结果不会保存，新增模型也不会自动进入调用链。
临时目录不写入数据库或浏览器持久存储；刷新页面后清空。修改连接配置会清除旧列表，避免混用不同供应商的结果。

列表接口支持 Anthropic、Gemini、OpenAI 兼容及 OpenAI Responses，沿用供应商 Base URL 并自动读取分页。
Gemini 的模型 ID 会去除 `models/` 前缀，并排除明确不支持 `generateContent` 的模型。
部分兼容供应商不提供模型目录接口，此时可继续使用「添加模型」手动配置；拉取失败不会删除或覆盖已有模型。
目录接口规范见 [Anthropic](https://platform.claude.com/docs/en/api/models/list)、[Gemini](https://ai.google.dev/api/models#method:-models.list) 和 [OpenAI](https://developers.openai.com/api/reference/resources/models/methods/list)。

### 调用与降级顺序

打开 `/admin/` →「角色管理」→「LLM 调用链配置」→「默认 LLM 调用链」。
在「调用模型」下拉框中选择模型，它会自动追加到列表末尾。列表显示「#1 首选」「#2 备用」等自动编号。
拖动左侧手柄或使用右侧上下按钮调整位置，点击 × 将模型移出列表；不需要手填任何顺序数字。
手柄获得焦点后也可用上下方向键排序。下拉框显示供应商、实际模型 ID 和显示名称，已选模型不可重复添加。
点击「保存」后，下次调用使用新的列表顺序。移除后可重新从下拉框选择该模型。

运行时只读取调用链中的模型，严格按保存的列表从上到下执行。
供应商或模型被停用时会跳过对应条目。关闭调用链或列表为空时不发送自动请求，也不会转而扫描整个模型目录。
例如，目录中可配置 A 主模型、A 备用模型、B 备用模型和 B 实验模型，但调用链只选择以下三个：

| 列表位置（自动编号） | 供应商 | 模型 | 用途 |
| --- | --- | --- | --- |
| #1 | 供应商 A | 主模型 | 首选 |
| #2 | 供应商 A | 备用模型 | 同供应商内降级 |
| #3 | 供应商 B | 备用模型 | 跨供应商降级 |

每次新请求从首选模型开始。当前模型按供应商设置完成重试后仍失败，就调用下一个候选。
超时、限流、接口或配置错误、空回复均会触发降级；日报、审计、事件抽取和检索词改写还会验证 JSON，格式无效时也会继续降级。
只有调用链中全部可用候选失败时才返回汇总错误；目录里的其他模型始终不会被自动追加。
调用链中没有可用模型时直接提示配置错误，不会从旧环境变量重新创建服务。
从列表移除卡片只移除调用关系，会保留模型及其所有参数；要删除仍被引用的模型或供应商，需要先从列表移除相应模型。

每次调用只读取一次调用链，因此后台保存后的下一次调用使用新配置；正在执行的请求继续使用其开始时的调用链。
Web 和 Celery 不缓存配置，无需为切换模型而重启或重建镜像。

模型生成参数独立配置。输出 Token 上限与任务自身上限取较小值；Temperature 留空沿用任务温度。
供应商的超时单位为秒，重试次数为 0 表示不重试。一次请求的最坏等待时间取决于所有候选各自的超时和重试次数。

响应元数据包含实际使用的供应商、模型和降级次数；侧写历史记录实际成功的模型。

### 支持的协议与自定义地址

| 接口类型 | LangChain 接入 | 官方 Base URL（留空默认） | 自定义地址填写方式 |
| --- | --- | --- | --- |
| Anthropic | `ChatAnthropic` | `https://api.anthropic.com` | 服务根地址，SDK 追加 `/v1/messages` |
| Gemini | `ChatGoogleGenerativeAI` | `https://generativelanguage.googleapis.com` | Gemini 原生协议服务根地址，SDK 追加 `/v1beta/models/...:generateContent` |
| OpenAI 兼容（Chat Completions） | `ChatOpenAI`，强制 Chat Completions | `https://api.openai.com/v1` | 通常填写到 `/v1`，SDK 追加 `/chat/completions` |
| OpenAI Responses | `ChatOpenAI`，强制 Responses | `https://api.openai.com/v1` | 通常填写到 `/v1`，SDK 追加 `/responses` |

自定义地址支持 HTTP、HTTPS、路径前缀和 Docker 内部主机名。无鉴权的 OpenAI 自建服务填写 Base URL 后可将 API Key 留空。
密钥输入框留空保留现有值；填写新值替换，勾选清除可删除。密钥在数据库中明文存储，页面不回显，只读用户仅看到是否已配置。

供应商页面中，每个已保存模型都有「测试连接」按钮，直接测试该模型已保存的配置；新增或修改参数后需要先保存。
手动测试可用于尚未加入调用链的模型，关闭重试且不使用降级，结果直接显示在对应模型卡片下方；测试会发送短请求。
已加入调用链的模型不能直接删除；应先在调用链中移除引用，再回到供应商页面删除模型。

### 上线与现有配置迁移

首次安装本次 LangChain 依赖需要重建一次应用镜像。已有部署需迁移到 `0027_llm_routing`；迁移会自动补齐之前尚未应用的版本。
Python 依赖默认从官方 PyPI（`https://pypi.org/simple/`）安装，避免第三方镜像同步延迟导致固定版本找不到。
如部署环境需使用其他源，可在构建时传入 `--build-arg PIP_INDEX_URL=https://你的索引地址/simple/`。
系统包使用 Debian 官方 HTTPS 源。

`web`、`celery_worker`、`celery_beat` 共用构建代理，Linux 服务器默认使用 `host` 构建网络和 `http://127.0.0.1:7897`。
此地址对应 Mihomo Compose 的 `7897:7897` 宿主机端口映射。
Mihomo 位于独立的 `proxy-net` 时，默认构建网络到其容器 IP 的连接可能被跨网络隔离阻断，因此通过宿主机发布端口访问。
Compose 的服务 `networks` 控制运行容器；构建期间 `RUN` 的网络由 `build.network` 单独控制。

`web`、`celery_worker`、`celery_beat` 运行时同时接入 `stillalive_network` 和已有的外部网络 `proxy-net`。
容器内可通过 `http://mihomo:7897` 访问同一网络中的代理服务；是否使用代理由应用调用配置决定。
`proxy-net` 由 Mihomo 部署维护。更新网络配置后，运行 `docker compose up -d web celery_worker celery_beat` 使容器重新创建并接入网络。

代理通过 Docker 预定义的 `HTTP_PROXY` / `HTTPS_PROXY` 及对应小写构建参数供 apt 和 pip 使用，不写入镜像的运行环境。
为兼容旧版 Docker 构建器，显式提供两种大小写；apt 读取小写 `http_proxy`，旧构建器不会自动补齐该变量。
可在 `Server/.env` 设置 `BUILD_PROXY` 覆盖地址；设置 `BUILD_PROXY=` 可关闭代理。
也可以临时直连构建：`BUILD_PROXY= BUILD_NETWORK=default docker compose build web celery_worker celery_beat`。
`BUILD_NO_PROXY` 默认是 `localhost,127.0.0.1,::1`。
已有 `.env` 中若保存了旧的容器 IP 或 `BUILD_NETWORK=default`，需要更新为以下值；环境变量会覆盖 Compose 默认值：

```dotenv
BUILD_PROXY=http://127.0.0.1:7897
BUILD_NETWORK=host
```

Docker Desktop 等环境可将 `BUILD_NETWORK` 改为 `default`，同时将 `BUILD_PROXY` 改为该构建网络可访问的代理地址，或留空关闭代理。
这些构建参数只影响 Dockerfile 中的下载；拉取基础镜像或其他服务镜像所需的代理，应在 Docker daemon / BuildKit 中配置。

如果构建日志显示直接连接 `deb.debian.org` 的公网 IP 并超时，优先检查代理是否生效：
服务器是否已更新 Compose 文件，以及 `BUILD_PROXY` 是否被留空关闭、`BUILD_NO_PROXY` 是否绕过了 Debian 域名。
若日志仍显示连接旧的 `172.30.0.4:7897`，检查 `.env` 或 shell 中是否仍覆盖了旧地址。
先在服务器宿主机验证发布端口和代理上游是否可用：

```bash
curl --noproxy '' --proxy http://127.0.0.1:7897 \
  --connect-timeout 5 --max-time 20 -I \
  https://deb.debian.org/debian/dists/trixie/InRelease
```

如果此命令也失败，先检查 `mihomo` 容器状态及挂载的 `./config/config.yaml`，端口映射不会自动开启 Mihomo 的代理监听。
其 HTTP 或混合监听端口应为 `7897`，例如：

```yaml
mixed-port: 7897
allow-lan: true
bind-address: '*'
```

若宿主机发布端口仅用于本机构建，可将 Mihomo Compose 的端口映射写为 `127.0.0.1:7897:7897`。
`apt-get update` 已开启 `--error-on=any`，索引下载失败会直接终止构建，避免后续出现误导性的 `Unable to locate package`。

升级时先迁移，再启动新代码的 Web 和 Celery：

```bash
cd /path/to/StillAlive/Server
docker compose build web celery_worker celery_beat
docker compose stop celery_worker celery_beat
docker compose run --rm web python manage.py migrate --noinput
docker compose up -d web celery_worker celery_beat
```

`0025` 首次导入原 `ANTHROPIC_*` 配置，`0026` 将原单例拆为「原 LLM 配置」供应商及其首选模型（优先级 10）。
密钥、接口类型、Base URL、超时、重试、Token 上限、温度与停用状态全部保留；数据转移后移除旧单例表。
`0027` 将升级前实际生效的模型及其顺序一次性转移到独立调用链，并移除模型记录上的优先级字段。
升级后可在调用链列表移除不需要参与降级的模型；之后新增的模型不会自动进入调用链。
供应商内管理模型与有序调用列表沿用 `0027` 的数据结构，不新增数据库迁移，已有供应商、模型和调用顺序均保留。
更新界面后需重新收集静态文件（Web 容器启动脚本会执行 `collectstatic`），确保加载新的排序脚本和样式。
尚未部署前一版本时也可直接迁移到最新版本，无需手动补配置。后续修改 `.env` 不会覆盖后台保存的供应商与模型。

运行隔离测试（SQLite + 内存缓存，HTTP 传输被模拟，不消耗模型额度）：

```bash
python manage.py test apps.characters.tests --settings=config.settings.test
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
```
