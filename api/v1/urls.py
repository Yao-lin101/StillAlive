from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenRefreshView
from .views import users
from .views.characters import (
    CharacterViewSet, CharacterDisplayView,
    update_character_status, get_character_status,
    WillConfigViewSet
)

# 创建路由器
router = DefaultRouter()

# 注册视图集
router.register(r'users', users.UserViewSet, basename='user')
router.register(r'characters', CharacterViewSet, basename='character')

# 注册遗嘱配置路由
character_router = DefaultRouter()
character_router.register(r'will', WillConfigViewSet, basename='character-will')

@api_view(['GET'])
def api_root(request):
    return Response({
        'status': 'ok',
        'version': 'v1',
        'endpoints': {
            'users': '/api/v1/users/',
            'characters': '/api/v1/characters/',
            'auth': '/api/v1/auth/token/',
        }
    })

urlpatterns = [
    # API 根路径
    path('', api_root, name='api-root'),
    
    # 不需要认证的路由放在最前面
    path('status/update/', update_character_status, name='status-update'),
    path('d/<str:code>/status/', get_character_status, name='status-get'),
    path('d/<str:code>/', CharacterDisplayView.as_view(), name='character-display'),
    
    # JWT 认证
    path('auth/token/', users.CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    
    # 需要认证的路由放在后面
    path('', include(router.urls)),
    # 遗嘱配置路由
    path('characters/<str:character_pk>/', include(character_router.urls)),
] 