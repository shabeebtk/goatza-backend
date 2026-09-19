'''
organization views - CRUD
user chat system -
recruitment planning 



Frontend (Next.js)
   ↓ WebSocket (real-time)
Django Channels (ASGI)
   ↓
Redis (message broker)
   ↓
PostgreSQL (persistent storage)
   ↓
FCM (offline fallback)


| Component      | Tool            |
| -------------- | --------------- |
| Web framework  | Django          |
| Async layer    | Django Channels |
| Message broker | Redis           |
| DB             | PostgreSQL      |
| Push fallback  | Firebase FCM    |


| Purpose      | Tool             |
| ------------ | ---------------- |
| UI           | React (Next.js)  |
| WebSocket    | native WebSocket |
| State        | Zustand          |
| API fallback | Axios            |
| Cache        | React Query      |


Free Redis
https://upstash.com

'''

# The Celery app must be created whenever Django loads — not only when a
# worker starts — so that shared_task and autodiscovery bind to THIS app in
# the web process too. See core/celery.py.
from core.celery import app as celery_app

__all__ = ("celery_app",)
