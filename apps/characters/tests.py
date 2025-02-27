from django.test import TestCase
import uuid
import tempfile
from PIL import Image
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase
from apps.users.models import User
from .models import Character

def create_test_image():
    """创建测试用的图片文件"""
    image = Image.new('RGB', (100, 100), color='red')
    tmp_file = tempfile.NamedTemporaryFile(suffix='.jpg')
    image.save(tmp_file)
    tmp_file.seek(0)
    return tmp_file

class CharacterAPITests(APITestCase):
    def setUp(self):
        """测试前创建用户并登录"""
        # 创建测试用户
        self.user = User.objects.create_user(
            email='test@example.com',
            password='testpass123',
            is_email_verified=True
        )
        self.other_user = User.objects.create_user(
            email='other@example.com',
            password='testpass123',
            is_email_verified=True
        )
        
        # 登录用户
        response = self.client.post(reverse('token_obtain_pair'), {
            'email': 'test@example.com',
            'password': 'testpass123'
        })
        self.token = response.data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')
        
        # 创建测试角色
        self.character = Character.objects.create(
            user=self.user,
            name='Test Character',
            bio='Test Bio'
        )
        
        # 其他用户的角色
        self.other_character = Character.objects.create(
            user=self.other_user,
            name='Other Character',
            bio='Other Bio'
        )

    def test_create_character(self):
        """测试创建角色"""
        # 准备测试图片
        test_image = create_test_image()
        
        data = {
            'name': 'New Character',
            'bio': 'New Character Bio',
            'avatar': SimpleUploadedFile(
                name='test_image.jpg',
                content=test_image.read(),
                content_type='image/jpeg'
            ),
            'display_url': 'http://example.com/character'
        }
        
        response = self.client.post(reverse('character-list'), data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Character.objects.count(), 3)
        self.assertEqual(response.data['name'], 'New Character')
        self.assertIn('secret_key', response.data)
        
        test_image.close()

    def test_list_characters(self):
        """测试获取角色列表"""
        response = self.client.get(reverse('character-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)  # 只能看到自己的角色

    def test_retrieve_character(self):
        """测试获取单个角色详情"""
        response = self.client.get(
            reverse('character-detail', kwargs={'pk': self.character.uid})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'Test Character')
        self.assertIn('secret_key', response.data)

    def test_update_character(self):
        """测试更新角色信息"""
        data = {
            'name': 'Updated Name',
            'bio': 'Updated Bio'
        }
        response = self.client.patch(
            reverse('character-detail', kwargs={'pk': self.character.uid}),
            data
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'Updated Name')

    def test_delete_character(self):
        """测试删除角色"""
        response = self.client.delete(
            reverse('character-detail', kwargs={'pk': self.character.uid})
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Character.objects.filter(user=self.user).count(), 0)

    def test_get_secret_key(self):
        """测试获取角色secret_key"""
        response = self.client.get(
            reverse('character-secret-key', kwargs={'pk': self.character.uid})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('secret_key', response.data)

    def test_regenerate_secret_key(self):
        """测试重新生成secret_key"""
        old_key = self.character.secret_key
        response = self.client.post(
            reverse('character-regenerate-secret-key', kwargs={'pk': self.character.uid})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('secret_key', response.data)
        self.assertNotEqual(str(old_key), str(response.data['secret_key']))

    def test_cannot_access_other_user_character(self):
        """测试无法访问其他用户的角色"""
        # 尝试获取其他用户的角色详情
        response = self.client.get(
            reverse('character-detail', kwargs={'pk': self.other_character.uid})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        
        # 尝试更新其他用户的角色
        response = self.client.patch(
            reverse('character-detail', kwargs={'pk': self.other_character.uid}),
            {'name': 'Hacked Name'}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        
        # 尝试删除其他用户的角色
        response = self.client.delete(
            reverse('character-detail', kwargs={'pk': self.other_character.uid})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_access(self):
        """测试未认证访问"""
        # 移除认证信息
        self.client.credentials()
        
        # 尝试获取角色列表
        response = self.client.get(reverse('character-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        
        # 尝试创建角色
        response = self.client.post(reverse('character-list'), {
            'name': 'Unauthorized Character'
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
