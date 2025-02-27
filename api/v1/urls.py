from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import users
from .views.characters import CharacterViewSet, CharacterDisplayView

# 创建路由器
router = DefaultRouter()

# 注册视图集
router.register(r'users', users.UserViewSet, basename='user')
router.register(r'characters', CharacterViewSet, basename='character')

urlpatterns = [
    # 角色公开展示 - 放在最前面以确保优先匹配
    path('d/<str:code>/', CharacterDisplayView.as_view(), name='character-display'),
    
    # JWT 认证
    path('auth/token/', users.CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    
    # 包含路由器 URLs
    path('', include(router.urls)),
] 