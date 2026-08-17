"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),

    # The one anonymous-reachable prefix — see core/public_urls.py. Everything
    # outside it stays behind IsAuthenticated.
    path('public/', include('core.public_urls')),

    # ABOVE 'user/': accounts.urls owns '<str:username>/details', so a later
    # include would let a user called "cv" shadow the CV settings endpoint.
    path('user/cv/', include('cv.urls')),

    path('user/', include('accounts.urls')),
    path('sports/', include('sports.urls')),
    path('connections/', include('connections.urls')),
    path('posts/', include('posts.urls')),
    path('feed/', include('feed.urls')),
    path('notifications/', include('notifications.urls')),
    path('conversations/', include('messaging.urls')),
    path('organizations/', include('organization.urls')),
    path('recruitments/', include('recruitments.urls')),
    path('highlights/', include('highlights.urls')),
    path('careers/', include('careers.urls')),
    path('achievements/', include('achievements.urls')),

    # No username-shadowing hazard here, unlike cv: the diary hangs off its own
    # top-level prefix rather than under 'user/', so ordering does not matter.
    path('matches/', include('matches.urls')),
]
