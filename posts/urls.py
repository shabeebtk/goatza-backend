from django.urls import path
from posts.views.posts_views import CreatePostAPIView, ListPostsAPIView
from posts.views.like_views import ToggleLikeAPIView, ListPostLikesAPIView
from posts.views.comments_views import ListCommentsAPIView, CreateCommentAPIView, ListRepliesAPIView

# base endpoint '/posts/

urlpatterns = [
    path('create', CreatePostAPIView.as_view(), name='create-post'),
    path('list', ListPostsAPIView.as_view(), name='list-posts'),
    path('like', ToggleLikeAPIView.as_view(), name='toggle-like'),
    path('likes/list', ListPostLikesAPIView.as_view(), name='list-likes'),
    path('comments/list', ListCommentsAPIView.as_view(), name='list-comments'),
    path('comments/create', CreateCommentAPIView.as_view(), name='create-comment'),
    path('comments/list/replies', ListRepliesAPIView.as_view(), name='list-comment-replies'),
]
