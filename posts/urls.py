from django.urls import path
from posts.views.posts_views import CreatePostAPIView, ListPostsAPIView, DeletePost, UpdatePostAPIView
from posts.views.like_views import ToggleLikeAPIView, ListPostLikesAPIView
from posts.views.comments_views import ListCommentsAPIView, CreateCommentAPIView, ListRepliesAPIView, DeleteCommentAPIView
from posts.views.search_views import PostSearchAPIView
from posts.views.mention_views import MyMentionsAPIView, MentionSuggestAPIView
from posts.views.save_views import ToggleSavePostAPIView, SavedPostsListAPIView

# base endpoint '/posts/

urlpatterns = [
    path('create', CreatePostAPIView.as_view(), name='create-post'),
    path('update', UpdatePostAPIView.as_view(), name='update-post'),
    path('list', ListPostsAPIView.as_view(), name='list-posts'),
    path('search', PostSearchAPIView.as_view(), name='search-posts'),
    path('delete', DeletePost.as_view(), name='delete-posts'),

    path('mentions/my', MyMentionsAPIView.as_view(), name='my-mentions'),
    path('mention/suggest', MentionSuggestAPIView.as_view(), name='mention-suggest'),

    path('save', ToggleSavePostAPIView.as_view(), name='toggle-save'),
    path('saved/list', SavedPostsListAPIView.as_view(), name='list-saved-posts'),

    path('like', ToggleLikeAPIView.as_view(), name='toggle-like'),
    path('likes/list', ListPostLikesAPIView.as_view(), name='list-likes'),

    path('comments/create', CreateCommentAPIView.as_view(), name='create-comment'),
    path('comments/delete', DeleteCommentAPIView.as_view(), name='delete-comment'),
    path('comments/list', ListCommentsAPIView.as_view(), name='list-comments'), # add replies data here itself
    path('comments/list/replies', ListRepliesAPIView.as_view(), name='list-comment-replies'),
]
