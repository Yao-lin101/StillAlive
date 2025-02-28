from rest_framework import serializers
from django.contrib.auth import get_user_model, authenticate
from django.core.cache import cache
from django.conf import settings
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
import random
from apps.users.models import BlacklistedUser, InvitationCode

User = get_user_model()

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = 'email'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'] = serializers.EmailField()
        self.fields.pop('username', None)  # 移除 username 字段

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        if email and password:
            user = authenticate(request=self.context.get('request'),
                              username=email, password=password)  # 使用 username 参数传递 email
            if not user:
                msg = '账号或密码错误'
                raise serializers.ValidationError(
                    {'error': msg},
                    code='authorization'
                )
            # 检查用户是否被封禁
            if not user.is_active:
                raise serializers.ValidationError(
                    {'error': '账号已被封禁'},
                    code='authorization'
                )
        else:
            msg = '请输入邮箱和密码'
            raise serializers.ValidationError(
                {'error': msg},
                code='authorization'
            )

        refresh = self.get_token(user)
        data = {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': UserProfileSerializer(user, context=self.context).data
        }
        return data

class InvitationCodeSerializer(serializers.ModelSerializer):
    """邀请码序列化器"""
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    used_by_username = serializers.CharField(source='used_by.username', read_only=True)
    
    class Meta:
        model = InvitationCode
        fields = ['code', 'created_by_username', 'used_by_username', 
                 'is_used', 'created_at', 'used_at', 'note']
        read_only_fields = ['code', 'created_by_username', 'used_by_username', 
                          'is_used', 'created_at', 'used_at']

class CreateInvitationCodeSerializer(serializers.ModelSerializer):
    """创建邀请码序列化器"""
    class Meta:
        model = InvitationCode
        fields = ['note']

class EmailRegisterSerializer(serializers.ModelSerializer):
    """邮箱注册序列化器"""
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True, min_length=6,
                                   style={'input_type': 'password'})
    verify_code = serializers.CharField(required=True, write_only=True)
    invitation_code = serializers.CharField(required=True, write_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'verify_code', 'invitation_code']

    def validate_email(self, value):
        """验证邮箱是否已被注册"""
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("该邮箱已被注册")
        return value

    def validate_verify_code(self, value):
        """验证邮箱验证码"""
        email = self.initial_data.get('email')
        cache_key = f'email_verify_code_{email}'
        cached_code = cache.get(cache_key)
        if not cached_code or cached_code != value:
            raise serializers.ValidationError("验证码错误或已过期")
        return value

    def validate_invitation_code(self, value):
        """验证邀请码"""
        try:
            invitation = InvitationCode.objects.get(code=value)
            if not invitation.is_valid:
                raise serializers.ValidationError("邀请码已被使用")
            return invitation
        except InvitationCode.DoesNotExist:
            raise serializers.ValidationError("邀请码不存在")

    def create(self, validated_data):
        """创建用户"""
        email = validated_data['email']
        password = validated_data['password']
        invitation = validated_data['invitation_code']
        
        # 创建用户
        user = User.objects.create_user(
            email=email,
            password=password,
            is_email_verified=True
        )
        
        # 使用邀请码
        invitation.use(user)
        
        return user

class UserProfileSerializer(serializers.ModelSerializer):
    """用户资料序列化器"""
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'uid', 'username', 'email', 'avatar', 'bio',
            'is_email_verified', 'is_wechat_verified', 'wechat_id',
            'created_at', 'is_superuser', 'is_active'
        ]
        read_only_fields = ['uid', 'email', 'is_email_verified', 
                          'is_wechat_verified', 'is_superuser', 'created_at']

    def get_avatar(self, obj):
        if hasattr(obj, 'avatar') and obj.avatar:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.avatar.url)
            return obj.avatar.url
        return None

class ChangePasswordSerializer(serializers.Serializer):
    """修改密码序列化器"""
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True, min_length=6)

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("原密码错误")
        return value 

class BlacklistedUserSerializer(serializers.ModelSerializer):
    uid = serializers.CharField(write_only=True)  # 用于创建时接收被拉黑用户的 uid
    username = serializers.CharField(source='blocked_user.username', read_only=True)
    userUid = serializers.CharField(source='blocked_user.uid', read_only=True)  # 改为驼峰命名
    avatar = serializers.SerializerMethodField()  # 添加头像字段
    
    class Meta:
        model = BlacklistedUser
        fields = ['uid', 'username', 'userUid', 'avatar']  # 只返回必要字段
        read_only_fields = ['username', 'userUid', 'avatar']
    
    def validate_uid(self, value):
        try:
            blocked_user = User.objects.get(uid=value)
            # 检查是否已经拉黑
            if BlacklistedUser.objects.filter(
                user=self.context['request'].user,
                blocked_user=blocked_user
            ).exists():
                raise serializers.ValidationError("您已经拉黑了该用户")
            return value
        except User.DoesNotExist:
            raise serializers.ValidationError("用户不存在")
            
    def get_avatar(self, obj):
        if obj.blocked_user.avatar:
            return obj.blocked_user.avatar.url
        return None 

class DeleteAccountSerializer(serializers.Serializer):
    """删除账号序列化器"""
    password = serializers.CharField(required=True, write_only=True)
    
    def validate_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("密码错误")
        return value 

class ResetPasswordSerializer(serializers.Serializer):
    """重置密码序列化器"""
    email = serializers.EmailField(required=True)
    verify_code = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True, min_length=6)

    def validate_email(self, value):
        try:
            User.objects.get(email=value)
            return value
        except User.DoesNotExist:
            raise serializers.ValidationError("该邮箱未注册")

    def validate_new_password(self, value):
        if len(value) < 6:
            raise serializers.ValidationError("密码长度不能少于6位")
        return value 