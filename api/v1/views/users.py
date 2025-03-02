from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from django.core.mail import send_mail
from django.core.cache import cache
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.db.models import Q
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import random
import logging
import time
import os

from apps.users.models import User, BlacklistedUser, InvitationCode
from apps.users.serializers import (
    EmailRegisterSerializer,
    UserProfileSerializer,
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    BlacklistedUserSerializer,
    DeleteAccountSerializer,
    ResetPasswordSerializer,
    InvitationCodeSerializer,
    CreateInvitationCodeSerializer,
)
from apps.users.permissions import IsSuperUser
from apps.users.pagination import StandardResultsSetPagination
from django.views.generic import TemplateView
from rest_framework.permissions import AllowAny

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

class UserViewSet(viewsets.GenericViewSet):
    """
    用户管理 API v1
    
    提供用户注册、登录、资料管理等功能
    """
    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_serializer_class(self):
        if self.action == 'register_email':
            return EmailRegisterSerializer
        elif self.action in ['profile', 'update_profile']:
            return UserProfileSerializer
        elif self.action == 'change_password':
            return ChangePasswordSerializer
        elif self.action == 'delete_account':
            return DeleteAccountSerializer
        elif self.action == 'create_invitation':
            return CreateInvitationCodeSerializer
        elif self.action == 'list_invitations':
            return InvitationCodeSerializer
        return EmailRegisterSerializer

    def get_permissions(self):
        if self.action in ['ban', 'list', 'create_invitation', 'list_invitations']:
            permission_classes = [permissions.IsAuthenticated, IsSuperUser]
        else:
            permission_classes = [permissions.AllowAny]
        return [permission() for permission in permission_classes]

    def _generate_verify_code(self):
        """生成6位数字验证码"""
        return ''.join([str(random.randint(0, 9)) for _ in range(6)])

    def _get_verify_code_cache_key(self, email):
        """获取验证码缓存键"""
        return f'email_verify_code_{email}'

    def _get_verify_code_limit_cache_key(self, email):
        """获取验证码发送频率限制缓存键"""
        return f'email_verify_code_limit_{email}'

    @swagger_auto_schema(
        operation_summary="邮箱注册",
        operation_description="使用邮箱注册新用户，需要提供邮箱、密码和验证码",
        request_body=EmailRegisterSerializer,
        responses={201: openapi.Response(
            description="注册成功，返回用户信息和JWT token",
            schema=openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    'user': openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'uid': openapi.Schema(type=openapi.TYPE_STRING),
                            'username': openapi.Schema(type=openapi.TYPE_STRING),
                            'email': openapi.Schema(type=openapi.TYPE_STRING),
                            'is_email_verified': openapi.Schema(type=openapi.TYPE_BOOLEAN)
                        }
                    ),
                    'token': openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'refresh': openapi.Schema(type=openapi.TYPE_STRING),
                            'access': openapi.Schema(type=openapi.TYPE_STRING)
                        }
                    )
                }
            )
        )}
    )
    @action(detail=False, methods=['post'])
    def register_email(self, request):
        """邮箱注册"""
        logger = logging.getLogger('utils.middleware')
        
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"注册数据验证失败: {serializer.errors}")
            return Response(
                {'error': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user = serializer.save()
        logger.info(f"用户注册成功: {user.email}")
        
        # 生成 JWT token
        refresh = RefreshToken.for_user(user)
        
        # 返回用户信息和认证令牌
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': UserProfileSerializer(user).data
        }, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        operation_summary="发送邮箱验证码",
        operation_description="向指定邮箱发送6位数字验证码，有效期10分钟，每个邮箱每分钟只能发送一次",
        manual_parameters=[
            openapi.Parameter(
                'email',
                openapi.IN_QUERY,
                description="需要验证的邮箱地址",
                type=openapi.TYPE_STRING,
                format='email'
            )
        ],
        responses={
            200: openapi.Response(description="验证码发送成功"),
            400: openapi.Response(description="请求参数错误"),
            429: openapi.Response(description="请求过于频繁"),
            500: openapi.Response(description="服务器内部错误")
        }
    )
    @action(detail=False, methods=['post'])
    def send_verify_code(self, request):
        """发送邮箱验证码"""
        logger = logging.getLogger('utils.middleware')
        
        # 1. 验证邮箱格式
        email = request.data.get('email')
        if not email:
            return Response(
                {'error': '请提供邮箱地址'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. 检查邮箱是否已被注册
        try:
            if User.objects.filter(email=email).exists():
                return Response(
                    {'error': '该邮箱已被注册'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
        except Exception as e:
            logger.error(f"检查邮箱是否存在时出错: {str(e)}")
            return Response(
                {'error': '系统错误，请稍后重试'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 3. 检查发送频率限制
        limit_key = self._get_verify_code_limit_cache_key(email)
        try:
            if cache.get(limit_key):
                return Response(
                    {'error': '发送太频繁，请稍后再试'}, 
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )
        except Exception as e:
            logger.error(f"检查发送频率限制时出错: {str(e)}")
            return Response(
                {'error': '系统错误，请稍后重试'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
        # 4. 生成验证码
        verify_code = self._generate_verify_code()
        
        # 5. 准备邮件内容
        try:
            html_message = render_to_string('users/verify_code_email.html', {
                'verify_code': verify_code,
                'valid_minutes': 10
            })
            plain_message = strip_tags(html_message)
        except Exception as e:
            logger.error(f"准备邮件内容时出错: {str(e)}")
            return Response(
                {'error': '系统错误，请稍后重试'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # 6. 发送邮件
        try:
            send_mail(
                subject='SMTX - 邮箱验证码',
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"验证码已发送至: {email}")
        except Exception as e:
            logger.error(f"发送邮件失败: {str(e)}")
            # 根据错误类型返回更具体的错误信息
            error_message = str(e).lower()
            if "smtp" in error_message:
                if "recipient address rejected" in error_message:
                    return Response(
                        {'error': '邮箱地址不存在，请检查后重试'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                else:
                    return Response(
                        {'error': '邮件发送失败，请稍后重试'}, 
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
            return Response(
                {'error': '邮件发送失败，请检查邮箱地址是否正确'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # 7. 保存验证码到缓存
        try:
            code_key = self._get_verify_code_cache_key(email)
            cache.set(code_key, verify_code, timeout=60 * 10)  # 10分钟有效期
            cache.set(limit_key, 1, timeout=60)  # 1分钟内不能重复发送
        except Exception as e:
            logger.error(f"保存验证码到缓存时出错: {str(e)}")
            return Response(
                {'error': '系统错误，请稍后重试'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return Response({'message': '验证码发送成功'})

    @swagger_auto_schema(
        method='get',
        operation_summary="获取用户资料",
        operation_description="获取当前用户的资料信息",
        responses={
            200: UserProfileSerializer,
            401: "未认证"
        }
    )
    @swagger_auto_schema(
        method='put',
        operation_summary="更新用户资料",
        operation_description="更新当前用户的资料信息",
        request_body=UserProfileSerializer,
        responses={
            200: UserProfileSerializer,
            401: "未认证"
        }
    )
    @action(detail=False, methods=['get', 'put'], 
            permission_classes=[permissions.IsAuthenticated])
    def profile(self, request):
        """获取或更新用户资料"""
        if request.method == 'GET':
            serializer = self.get_serializer(request.user, context={'request': request})
            return Response(serializer.data)
        else:
            serializer = self.get_serializer(request.user, data=request.data, 
                                           context={'request': request},
                                           partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

    @swagger_auto_schema(
        operation_summary="修改密码",
        operation_description="修改当前用户的登录密码",
        request_body=ChangePasswordSerializer,
        responses={
            200: "密码修改成功",
            400: "请求参数错误",
            401: "未认证"
        }
    )
    @action(detail=False, methods=['post'], 
            permission_classes=[permissions.IsAuthenticated])
    def change_password(self, request):
        """修改密码"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # 修改密码
        request.user.set_password(serializer.validated_data['new_password'])
        request.user.save()
        
        return Response({'message': '密码修改成功'})

    @swagger_auto_schema(
        operation_summary="上传头像",
        operation_description="上传用户头像图片",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'avatar': openapi.Schema(
                    type=openapi.TYPE_FILE,
                    description="头像图片文件"
                )
            }
        ),
        responses={
            200: UserProfileSerializer,
            400: "请求参数错误",
            401: "未认证"
        }
    )
    @action(detail=False, methods=['post'], 
            permission_classes=[permissions.IsAuthenticated])
    def upload_avatar(self, request):
        """上传用户头像"""
        if 'avatar' not in request.FILES:
            return Response(
                {'error': '请选择要上传的图片'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        avatar = request.FILES['avatar']
        
        # 验证文件类型
        if not avatar.content_type.startswith('image/'):
            return Response(
                {'error': '请上传图片文件'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # 验证文件大小（限制为 5MB）
        if avatar.size > 5 * 1024 * 1024:
            return Response(
                {'error': '图片大小不能超过 5MB'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # 更新用户头像
        request.user.avatar = avatar
        request.user.save()
        
        # 返回更新后的用户信息
        serializer = UserProfileSerializer(request.user, context={'request': request})
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_summary="封禁/解封用户",
        operation_description="超级管理员可以封禁或解封指定用户",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'is_active': openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="是否激活用户"
                )
            }
        ),
        responses={
            200: "操作成功",
            404: "用户不存在"
        }
    )
    @action(detail=True, methods=['post'])
    def ban(self, request, pk=None):
        """封禁或解封用户"""
        try:
            user = User.objects.get(uid=pk)
            is_active = request.data.get('is_active', not user.is_active)
            user.is_active = is_active
            user.save()
            
            message = '用户已被解封' if is_active else '用户已被封禁'
            return Response({'message': message})
        except User.DoesNotExist:
            return Response(
                {'error': '用户不存在'}, 
                status=status.HTTP_404_NOT_FOUND
            )

    @swagger_auto_schema(
        operation_summary="获取用户列表",
        operation_description="获取用户列表，支持搜索功能",
        manual_parameters=[
            openapi.Parameter(
                'search',
                openapi.IN_QUERY,
                description="搜索关键词(UID、用户名或邮箱)",
                type=openapi.TYPE_STRING,
                required=False
            )
        ],
        responses={
            200: UserProfileSerializer(many=True),
            401: "未认证"
        }
    )
    def list(self, request):
        """获取用户列表（不包括超级用户）"""
        search = request.query_params.get('search', '')
        
        # 基础查询：过滤掉超级用户
        queryset = self.get_queryset().filter(is_superuser=False)
        
        # 如果有搜索关键词，添加搜索条件
        if search:
            queryset = queryset.filter(
                Q(uid__icontains=search) |
                Q(username__icontains=search) |
                Q(email__icontains=search)
            )
            
        # 使用分页
        page = self.paginate_queryset(queryset)
        serializer = UserProfileSerializer(page, many=True, context={'request': request})
        return self.get_paginated_response(serializer.data)

    @swagger_auto_schema(
        operation_summary="获取用户总数",
        operation_description="获取系统中的总用户数（不包括超级用户）",
        responses={
            200: openapi.Response(
                description="用户总数",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'total': openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="用户总数"
                        )
                    }
                )
            )
        }
    )
    @action(detail=False, methods=['get'])
    def count(self, request):
        """获取用户总数"""
        total = self.get_queryset().filter(is_superuser=False).count()
        return Response({'total': total}) 

    @swagger_auto_schema(
        operation_summary="拉黑用户",
        operation_description="将指定用户添加到黑名单",
        request_body=BlacklistedUserSerializer,
        responses={
            201: BlacklistedUserSerializer,
            400: "请求参数错误"
        }
    )
    @action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def blacklist(self, request):
        """拉黑用户"""
        print("收到拉黑请求，数据:", request.data)
        serializer = BlacklistedUserSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            print("序列化器验证失败:", serializer.errors)
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            blocked_user = User.objects.get(uid=serializer.validated_data['uid'])
            
            # 不能拉黑自己
            if blocked_user == request.user:
                return Response(
                    {'error': '不能拉黑自己'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 检查是否已经拉黑
            if BlacklistedUser.objects.filter(
                user=request.user,
                blocked_user=blocked_user
            ).exists():
                return Response(
                    {'error': '您已经拉黑了该用户'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 创建拉黑记录
            blacklisted_user = BlacklistedUser.objects.create(
                user=request.user,
                blocked_user=blocked_user
            )
            
            return Response(
                BlacklistedUserSerializer(blacklisted_user).data,
                status=status.HTTP_201_CREATED
            )
        except User.DoesNotExist:
            return Response(
                {'error': '用户不存在'},
                status=status.HTTP_404_NOT_FOUND
            )

    @swagger_auto_schema(
        operation_summary="从黑名单移除用户",
        operation_description="从黑名单中移除指定用户",
        manual_parameters=[
            openapi.Parameter(
                'uid',
                openapi.IN_QUERY,
                description="用户UID",
                type=openapi.TYPE_STRING,
                required=True
            )
        ],
        responses={
            204: "操作成功",
            404: "用户不存在或不在黑名单中"
        }
    )
    @action(detail=False, methods=['delete'], permission_classes=[permissions.IsAuthenticated])
    def unblacklist(self, request):
        """从黑名单移除用户"""
        uid = request.query_params.get('uid')
        if not uid:
            return Response(
                {'error': '请提供用户 UID'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        try:
            blocked_user = User.objects.get(uid=uid)
            blacklisted_user = BlacklistedUser.objects.get(
                user=request.user,
                blocked_user=blocked_user
            )
            
            # 删除黑名单记录
            blacklisted_user.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except User.DoesNotExist:
            return Response(
                {'error': '用户不存在'},
                status=status.HTTP_404_NOT_FOUND
            )
        except BlacklistedUser.DoesNotExist:
            return Response(
                {'error': '该用户不在黑名单中'},
                status=status.HTTP_404_NOT_FOUND
            )

    @swagger_auto_schema(
        operation_summary="获取黑名单列表",
        operation_description="获取所有被加入黑名单的用户",
        responses={200: BlacklistedUserSerializer(many=True)}
    )
    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def blacklist_list(self, request):
        """获取黑名单列表"""
        blacklisted_users = BlacklistedUser.objects.filter(user=request.user)
        page = self.paginate_queryset(blacklisted_users)
        serializer = BlacklistedUserSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @swagger_auto_schema(
        operation_summary="删除账号",
        operation_description="永久删除当前用户账号，需要提供密码确认",
        request_body=DeleteAccountSerializer,
        responses={
            204: "账号删除成功",
            400: "密码错误",
            500: "服务器内部错误"
        }
    )
    @action(detail=False, methods=['delete'], permission_classes=[permissions.IsAuthenticated])
    def delete_account(self, request):
        """删除账号"""
        serializer = DeleteAccountSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response(
                {'error': '密码错误'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            user = request.user
            # 1. 删除用户头像
            if user.avatar:
                if os.path.isfile(user.avatar.path):
                    os.remove(user.avatar.path)
            
            # 2. 删除用户的黑名单记录
            BlacklistedUser.objects.filter(
                Q(user=user) | Q(blocked_user=user)
            ).delete()
            
            # 3. 最后删除用户账号
            user.delete()
            
            return Response(status=status.HTTP_204_NO_CONTENT)
        
        except Exception as e:
            logger = logging.getLogger('utils.middleware')
            logger.error(f"删除账号失败: {str(e)}")
            return Response(
                {'error': '删除账号失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @swagger_auto_schema(
        operation_summary="发送重置密码验证码",
        operation_description="向指定邮箱发送重置密码验证码",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'email': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    format='email'
                )
            }
        ),
        responses={
            200: "验证码发送成功",
            400: "请求参数错误"
        }
    )
    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny])
    def send_reset_code(self, request):
        """发送重置密码验证码"""
        email = request.data.get('email')
        if not email:
            return Response(
                {'error': '请提供邮箱地址'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {'error': '该邮箱未注册'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 检查发送频率限制
        limit_key = self._get_verify_code_limit_cache_key(email)
        if cache.get(limit_key):
            return Response(
                {'error': '验证码发送过于频繁，请稍后再试'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 生成验证码并缓存
        verify_code = self._generate_verify_code()
        cache_key = self._get_verify_code_cache_key(email)
        cache.set(cache_key, verify_code, timeout=300)  # 5分钟有效期
        cache.set(limit_key, True, timeout=60)  # 1分钟发送限制
        
        # 发送邮件
        try:
            html_message = render_to_string('users/verify_code_email.html', {
                'verify_code': verify_code,
                'action_name': '重置密码'  # 指定操作类型
            })
            plain_message = strip_tags(html_message)
            
            send_mail(
                subject='复读喵 - 重置密码验证码',
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                html_message=html_message
            )
            return Response(status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {'error': '验证码发送失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @swagger_auto_schema(
        operation_summary="重置密码",
        operation_description="使用验证码重置密码",
        request_body=ResetPasswordSerializer,
        responses={
            200: "密码重置成功",
            400: "验证码错误或已过期"
        }
    )
    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny])
    def reset_password(self, request):
        """重置密码"""
        serializer = ResetPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'error': '验证码错误或已过期'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        email = serializer.validated_data['email']
        verify_code = serializer.validated_data['verify_code']
        new_password = serializer.validated_data['new_password']
        
        # 验证验证码
        cache_key = self._get_verify_code_cache_key(email)
        cached_code = cache.get(cache_key)
        if not cached_code or cached_code != verify_code:
            return Response(
                {'error': '验证码错误或已过期'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            user = User.objects.get(email=email)
            user.set_password(new_password)
            user.save()
            
            # 清除验证码缓存
            cache.delete(cache_key)
            
            return Response(status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {'error': '重置密码失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @swagger_auto_schema(
        operation_summary="创建邀请码",
        operation_description="超级管理员创建新的邀请码",
        request_body=CreateInvitationCodeSerializer,
        responses={
            201: InvitationCodeSerializer,
            403: "没有权限"
        }
    )
    @action(detail=False, methods=['post'])
    def create_invitation(self, request):
        """创建邀请码"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        invitation = InvitationCode.create_invitation_code(
            created_by=request.user,
            note=serializer.validated_data.get('note')
        )
        
        return Response(
            InvitationCodeSerializer(invitation).data,
            status=status.HTTP_201_CREATED
        )

    @swagger_auto_schema(
        operation_summary="获取邀请码列表",
        operation_description="获取所有邀请码列表",
        responses={200: InvitationCodeSerializer(many=True)}
    )
    @action(detail=False, methods=['get'])
    def list_invitations(self, request):
        """获取邀请码列表"""
        invitations = InvitationCode.objects.all()
        page = self.paginate_queryset(invitations)
        serializer = InvitationCodeSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

class PrivacyPolicyView(TemplateView):
    template_name = 'users/privacy_policy.html'
    permission_classes = [AllowAny]

class TermsOfServiceView(TemplateView):
    template_name = 'users/terms_of_service.html'
    permission_classes = [AllowAny]

class TechnicalSupportView(TemplateView):
    template_name = 'users/technical_support.html'
    permission_classes = [AllowAny] 