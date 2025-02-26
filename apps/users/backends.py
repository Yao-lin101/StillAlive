from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

User = get_user_model()

class EmailBackend(ModelBackend):
    """
    邮箱认证后端
    
    允许用户使用邮箱和密码进行登录
    """
    
    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            # 支持使用邮箱或用户名登录
            user = User.objects.get(
                Q(username=username) | Q(email=username)
            )
            if user.check_password(password):
                return user
        except User.DoesNotExist:
            return None
        
    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None 