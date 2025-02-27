from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import users
from .views.characters import (
    CharacterViewSet, CharacterDisplayView,
    update_character_status, get_character_status
)

# 创建路由器
router = DefaultRouter()

# 注册视图集
router.register(r'users', users.UserViewSet, basename='user')
router.register(r'characters', CharacterViewSet, basename='character')

urlpatterns = [
    # 不需要认证的路由放在最前面
    path('status/update/', update_character_status, name='status-update'),
    path('d/<str:code>/status/', get_character_status, name='status-get'),
    path('d/<str:code>/', CharacterDisplayView.as_view(), name='character-display'),
    
    # JWT 认证
    path('auth/token/', users.CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    
    # 需要认证的路由放在后面
    path('', include(router.urls)),
] 