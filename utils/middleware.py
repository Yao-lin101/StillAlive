import time
import logging
from typing import Callable
from django.conf import settings

logger = logging.getLogger(__name__)

class MiddlewareTimingMiddleware:
    def __init__(self, get_response: Callable):
        self.get_response = get_response
        self._middleware_classes = None
        self._start_times = {}

    def __call__(self, request):
        # 记录请求开始时间
        request_start = time.time()
        request._start_time = request_start
        
        # 获取所有中间件类（仅在第一次调用时执行）
        if self._middleware_classes is None:
            self._middleware_classes = [m.split('.')[-1] for m in settings.MIDDLEWARE]
        
        # 记录当前中间件开始时间
        current_middleware = self.__class__.__name__
        self._start_times[current_middleware] = time.time()
        
        # 执行请求
        response = self.get_response(request)
        
        # 计算并记录时间
        end_time = time.time()
        total_time = end_time - request_start
        middleware_time = end_time - self._start_times[current_middleware]
        
        # 只在DEBUG模式下或请求时间超过阈值时记录性能日志
        if settings.DEBUG or total_time > 1.0:  # 超过1秒的请求记录日志
            if hasattr(request, '_view_time'):
                view_time = request._view_time
                logger.info(f"Request: {request.method} {request.path} - Total: {total_time:.3f}s, View: {view_time:.3f}s")
        
        return response

class ViewTimingMiddleware:
    def __init__(self, get_response: Callable):
        self.get_response = get_response

    def __call__(self, request):
        # 记录视图开始时间
        view_start = time.time()
        
        # 执行视图
        response = self.get_response(request)
        
        # 计算视图执行时间
        view_time = time.time() - view_start
        request._view_time = view_time
        
        return response 