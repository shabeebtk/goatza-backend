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