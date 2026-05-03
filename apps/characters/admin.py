from django.contrib import admin
from .models import ImportantEvent


@admin.register(ImportantEvent)
class ImportantEventAdmin(admin.ModelAdmin):
    list_display = (
        'character',
        'date',
        'title',
        'event_type',
        'importance_score',
        'confidence',
        'is_active',
        'milvus_synced',
    )
    list_filter = ('is_active', 'milvus_synced', 'event_type', 'date')
    search_fields = ('title', 'summary', 'event_key')
    readonly_fields = ('created_at', 'updated_at', 'milvus_synced_at')
