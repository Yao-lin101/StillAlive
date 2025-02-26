from rest_framework import permissions

class IsSuperUser(permissions.BasePermission):
    """
    只允许超级用户访问
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_superuser) 