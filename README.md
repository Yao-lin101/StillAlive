# StillAlive

StillAlive 是一个简单而有趣的状态同步和展示系统，让你可以实时分享你的状态给关心你的人。

## 主要功能

- 🔄 实时状态同步
  - 自定义状态监控（如电量、位置、当前使用的应用等）
  - 支持数值和文本类型的状态
  - 最多支持10个自定义状态

- 🎨 个性化展示
  - 自定义背景主题
  - 自定义超时提示消息
  - 支持网易云音乐播放

- 📱 多端支持
  - 提供 API 接口，支持各种客户端接入
  - 支持通过密钥进行安全认证
  - 提供公开展示链接

- 📬 亡语功能
  - 设置触发条件和通知邮箱
  - 自定义通知内容
  - 支持多个抄送邮箱

## 技术栈

### 后端
- Python 3.10+
- Django 4.2
- Django REST framework
- PostgreSQL
- Redis
- Celery

### 前端
- React 18
- TypeScript
- TailwindCSS
- shadcn/ui

## 快速开始

1. 克隆项目
```bash
git clone https://github.com/yourusername/stillalive.git
cd stillalive
```

2. 安装依赖
```bash
# 后端
cd Server
pip install -r requirements.txt

# 前端
cd StillAlive
npm install
```

3. 配置环境变量
```bash
# 后端
cp .env.example .env
# 编辑 .env 文件，填写必要的配置

# 前端
cp .env.example .env.local
# 编辑 .env.local 文件，填写必要的配置
```

## 状态同步 API

### 更新状态
```http
POST https://your-domain/api/v1/status/update/
Content-Type: application/json
X-Character-Key: YOUR_CHARACTER_KEY

{
    "type": "vital_signs",
    "data": {
        "battery": 85,        // 电量百分比
        "phone": "微信",      // 正在使用的应用名称
        "location": "北京",   // 位置信息
        "weather": "晴朗"     // 天气信息
    }
}
```

### 说明
- `X-Character-Key`: 角色密钥，在角色详情页可以获取
- `type`: 更新类型，可选值：`vital_signs`、`other`，控制在50个字符以内，前端会完整展示每个类型的最新状态
- `data`: 包含要更新的状态数据
  - 字段名需要与角色配置中的状态键名完全匹配
  - 数值类型的状态必须传入数字
  - 文本类型的状态传入字符串

### 响应示例
```json
{
    "status": "success",
    "message": "状态更新成功"
}
```

4. 运行开发服务器
```bash
# 后端
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000

# 前端
npm run dev
```

## 部署

1. 后端部署
- 使用 Docker Compose 进行容器化部署
- 配置 Nginx 作为反向代理
- 设置 SSL 证书实现 HTTPS

2. 前端部署
- 构建静态文件：`npm run build`
- 使用 Nginx 托管静态文件
- 配置 CORS 和缓存策略

## 贡献指南

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License
