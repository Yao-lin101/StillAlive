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
        
        # 记录详细的时间信息
        # logger.info(f"\n{'='*50}")
        # logger.info(f"Request: {request.method} {request.path}")
        # logger.info(f"Total time: {total_time:.3f}s")
        # logger.info(f"Middleware timing breakdown:")
        # logger.info(f"- {current_middleware}: {middleware_time:.3f}s")
        
        # 如果有视图处理时间，也记录它
        if hasattr(request, '_view_time'):
            view_time = request._view_time
            logger.info(f"View execution time: {view_time:.3f}s")
        
        logger.info(f"{'='*50}\n")
        
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