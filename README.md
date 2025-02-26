# Login

Login 是一个基于 Django REST framework 构建的现代化用户管理系统。

## 主要功能

- 用户认证与授权
  - 邮箱注册
  - JWT 令牌认证
  - 权限管理
- 用户管理
  - 个人资料管理
  - 头像上传
  - 密码修改
  - 账号删除
- 用户互动
  - 用户黑名单
  - 用户搜索
- 管理员功能
  - 用户封禁/解封
  - 用户列表管理
- 安全特性
  - 邮箱验证
  - 密码重置
  - 频率限制

## 技术栈

- Python 3.11+
- Django 5.1+
- Django REST framework
- PostgreSQL
- JWT 认证
- Swagger/OpenAPI 文档

## 安装

1. 克隆项目
```bash
git clone https://github.com/yourusername/Login.git
cd Login
```

2. 创建虚拟环境
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
.\venv\Scripts\activate  # Windows
```

3. 安装依赖
```bash
cd Server
pip install -r requirements.txt
```

4. 配置环境变量
创建 `.env` 文件并配置以下变量：
```
DEBUG=True
SECRET_KEY=your-secret-key
DATABASE_URL=postgres://user:password@localhost:5432/dbname
EMAIL_HOST=smtp.your-email-provider.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-email@example.com
EMAIL_HOST_PASSWORD=your-email-password
```

5. 数据库迁移
```bash
python manage.py migrate
```

6. 创建超级用户
```bash
python manage.py createsuperuser
```

7. 运行开发服务器
```bash
python manage.py runserver
```

## API 文档

启动服务器后，访问以下地址查看 API 文档：
- Swagger UI: http://localhost:8000/api/docs/
- Admin 界面: http://localhost:8000/admin/

## 开发指南

### 项目结构
```
Server/
├── apps/
│   └── users/              # 用户应用
│       ├── models.py       # 数据模型
│       ├── serializers.py  # 序列化器
│       ├── permissions.py  # 权限类
│       └── views.py        # 视图
├── api/
│   └── v1/                 # API v1
│       ├── urls.py         # URL 配置
│       └── views/          # API 视图
└── config/                 # 项目配置
    ├── settings.py
    └── urls.py
```

### 代码规范
- 遵循 PEP 8 编码规范
- 使用类型注解
- 编写单元测试
- 保持代码文档更新

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。 