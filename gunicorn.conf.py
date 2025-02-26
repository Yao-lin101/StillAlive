# 项目目录
chdir = '/app'

# 指定进程数
workers = 4

# 指定每个进程开启的线程数
threads = 2

# 启动模式
worker_class = 'sync'

# 绑定的ip与端口
bind = '0.0.0.0:8000'

# 设置进程文件目录
pidfile = '/app/logs/gunicorn.pid'

# 设置访问日志和错误信息日志路径
accesslog = '/app/logs/gunicorn_access.log'
errorlog = '/app/logs/gunicorn_error.log'

# 日志级别
loglevel = 'warning'

# 自定义设置项
# 设置访问日志格式，记录详细的访问信息
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s'

# 工作进程相关设置
max_requests = 10000        # 设置工作进程处理的最大请求数
max_requests_jitter = 1000  # 随机偏移值，防止所有工作进程同时重启
timeout = 60               # 超时时间
keepalive = 75            # keepalive 超时时间

# 是否后台运行
daemon = False

# 是否热加载
reload = False

# 错误日志输出格式
error_log_format = '%(asctime)s [%(process)d] [%(levelname)s] %(message)s'