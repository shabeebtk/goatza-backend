from django.contrib import admin

# Register your models here.
from apps.posts.models import Post, PostMedia, Comment, Like


admin.site.register(Post)
admin.site.register(PostMedia)
admin.site.register(Comment)
admin.site.register(Like)