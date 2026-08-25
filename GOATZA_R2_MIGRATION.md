# Goatza Media Migration — Cloudinary → Cloudflare R2

**Status:** Approved spec, ready to build · **Version:** 1.0 · **Date:** 25 Aug 2026
**Purpose of this document:** Complete, self-contained specification of the media-storage migration. Paste it (or reference it) in any new Claude / Claude Code session to give full context. It contains: the why, the target architecture, a full inventory of current code, the exact change spec for backend + frontend + infra, security requirements, locked decisions, staged Claude Code build prompts, and a test checklist.

---

## 1. Context — what Goatza is and why we're migrating

**Goatza** is a sports networking + talent recruitment platform (India-first launch):
- **Backend:** Django 6 + DRF + Channels (Daphne/ASGI), PostgreSQL, Redis (Upstash) channel layer, JWT (SimpleJWT), FCM push. Hosted on **Render free tier, Singapore region** (kept alive by UptimeRobot pings).
- **Frontend:** Next.js 16 (App Router) + React 19 + TypeScript, TanStack Query, Zustand, Axios, react-hook-form + Zod, PWA. Hosted on **Vercel**.
- **Dual-actor model:** every request acts as a User OR an Organization via `X-Actor-Type` / `X-Actor-Id` headers. Media folders are scoped per actor.
- **Media today:** Cloudinary (free plan, 25 credits/month) — signed direct browser uploads, on-the-fly transformations, video transcoding, auto video posters.

**Why migrate:** Cloudinary's free plan (25 credits ≈ ~25 GB combined bandwidth/storage/transforms on a rolling 30-day window) cannot survive real users on a media-heavy social app, and the next plan is **$99/month** — far beyond budget (hard cap: ₹600/month total infra). Cloudinary bills on **views**; Cloudflare R2 bills on **storage** with **zero egress fees forever**, which matches social-app economics (a viral clip costs ₹0 in bandwidth).

**Decision (locked):** ALL media — images AND videos — moves to a single provider: **Cloudflare R2**, served through a custom domain on Cloudflare's CDN. Cloudinary is removed completely.

**Data migration:** NOT needed. All existing media references point to localhost/dev data. We start with a fresh, empty R2 bucket.

---

## 2. Target architecture

```
┌──────────────┐   1. request upload config (JWT + actor headers,
│   Next.js    │      per-file {content_type, size_bytes})
│   client     │ ─────────────────────────────────────────────▶ ┌─────────────┐
│              │                                                 │  Django API │
│  compress/   │   2. presigned PUT URLs + final public URLs     │  (Render)   │
│  encode      │ ◀───────────────────────────────────────────── └─────────────┘
│  FIRST       │
│              │   3. XHR PUT file bytes directly (progress events)
│              │ ─────────────────────────────────────────────▶ ┌─────────────┐
│              │                                                 │ Cloudflare  │
│              │   4. attach: POST url/key/thumb/dims/duration   │     R2      │
│              │ ─────────────────────────────▶ Django validates └─────────────┘
└──────────────┘      key prefix + domain + caps, saves to DB           │
                                                                        ▼
       Viewers ◀──────── https://media.goatza.com/<key> ◀──── Cloudflare CDN
                         (cached at edge, egress = ₹0)
```

Key properties:
- **Client encodes first, then asks for upload config** (reversed from today, where the signature is fetched before compression). This lets the backend bind exact `Content-Type` + size into the presigned URL.
- Files never pass through the Django server (same as today).
- The DB stores the **key** (in the existing `public_id` columns) and the **full public URL** (in the existing `*_url` columns). URL = `MEDIA_PUBLIC_BASE_URL + "/" + key`.
- Delivery is the **exact file uploaded** — no on-the-fly transforms. Everything that Cloudinary transformed at delivery time is now produced **client-side at upload time** (see §4).

---

## 3. Current-state inventory (from full code review, Aug 2026)

### 3.1 Backend — storage abstraction (already provider-ready)

```
services/storage/
├── base.py        # BaseStorageService interface
├── factory.py     # get_storage_service() — has a commented-out slot: `# if provider == "s3": ...`
├── cloudinary.py  # CloudinaryService (~413 lines) — the only implementation
└── validators.py  # URL/extension/ownership validation shared by all modules
```

`BaseStorageService` interface (all call sites go through this):
| Method | What it does today |
|---|---|
| `get_upload_config(actor, upload_type, count)` | Returns signed Cloudinary POST params per file |
| `delete_file(public_id)` | `cloudinary.uploader.destroy` |
| `delete_folder_data(folder_path)` | Delete all resources under a prefix + the folder |
| `get_media_metadata(public_id, media_type)` | Server fetches width/height/duration from Cloudinary API ("never trust client") |
| `ensure_video_derivatives(public_id)` | Eagerly pre-transcodes `c_limit,h_1280,w_1280,q_auto:good,vc_h264` → mp4 (+ optional HLS `sp_hd`, flag-gated OFF) |

### 3.2 Upload types & folder structure (must be preserved as R2 key prefixes)

Endpoint: `GET /.../upload-config?type=<t>&count=<n>[&org_id=...]` → `accounts/views/user_upload_signature_views.py` (`GetUploadConfigAPIView`). Enforces actor-type rules and org membership.

| `upload_type` | Actor | Folder (→ R2 key prefix) | Fixed name / overwrite? |
|---|---|---|---|
| `profile`, `cover` | user only | `users/<id>/profile` etc. | fixed `public_id`, overwrite=true |
| `posts` | user or org | `users/<id>/posts/<temp_post_id>/` or `organizations/<id>/posts/<temp_post_id>/` | random UUID names |
| `recruitments` | org only | `organizations/<id>/recruitments/<temp_id>/` | random UUID names |
| `chat` | user or org | `chat/users/<id>/` or `chat/organizations/<id>/` | random UUID names |
| `achievements`, `matches` | user only | `users/<id>/achievements` / `.../matches` | random UUID names |
| `organization_logo`, `organization_cover` | org only | `organizations/<id>/logo` etc. | fixed name, overwrite=true |

### 3.3 Backend call sites (all via `get_storage_service()` — interfaces unchanged where possible)

- `posts/views/posts_views.py` — attach media: calls `get_media_metadata` + `ensure_video_derivatives`
- `posts/services/post_service.py` — delete post: `delete_folder_data`
- `highlights/services/highlight_services.py` — `ensure_video_derivatives` (async best-effort)
- `messaging/services/message_service.py` — chat media: validates URL/prefix (replay protection), `build_video_thumbnail_url`, `ensure_video_derivatives`
- `recruitments/services/recruitment_service.py` — validates thumbnail URL, `delete_file`
- `matches/services/match_services.py`, `accounts/views/user_views.py`, `organization/views/organization_views.py` — `delete_file` on replace/remove
- `posts/management/commands/backfill_media_dimensions.py` + `backfill_video_derivatives.py` — Cloudinary-only backfills (to be retired)

### 3.4 Database (NO schema changes needed)

Every media reference already stores a provider-agnostic pair — examples:
- `posts.Media`: `file_url`, `public_id`, `thumbnail_url` (blank for images today), + media_type/order (+ width/height/duration handled via metadata path)
- `accounts.Profile`: `profile_photo` + `profile_photo_public_id`, `cover_photo` + `_public_id`
- `organization`: `logo` + `logo_public_id`, `cover_image` + `_public_id`
- `highlights.Highlight`: `file_url`, `public_id`, `thumbnail_url`
- `messaging.Message`: `media_url`, `media_public_id`, `media_thumbnail_url`
- `achievements`: `image` + `image_public_id` · `matches`: `photo_url` + `photo_public_id`
- `recruitments` media: `file_url`, `public_id`, `thumbnail_url`

`public_id` columns are `max_length=255` — R2 keys (same folder scheme + `.ext`) fit comfortably.

### 3.5 Frontend — upload services (7, all share one pattern)

`getUploadSignatureApi(type, count)` → per-file `FormData` POST to Cloudinary with XHR progress:
- `features/posts/services/postUpload.service.ts` — images: `browser-image-compression` → WebP, `maxSizeMB: 2.5`, `maxWidthOrHeight: 2560`, quality 0.9. **Videos: uploaded RAW** (≤300 MB, ≤5 min), thumbnail built as Cloudinary `so_0` URL string.
- `features/highlights/services/highlightUpload.service.ts` — **RAW video** ≤100 MB, ≤90 s; 9:16 poster via Cloudinary `so_0,c_fill` URL.
- `features/messages/services/chatUpload.service.ts` — chat image/video; `cloudinaryThumb(url, 640)` builds `c_limit,w_640,q_auto,f_auto` thumb URLs on the fly.
- `features/profile/hooks/usePhotoUpload.ts`, `features/organization/hooks/useOrgPhotoUpload.ts` — profile/cover/logo (with `react-easy-crop`).
- `features/achievements/hooks/useAchievementImageUpload.ts`, `features/matchDiary/.../useMatchPhotoUpload.ts` — single images.

### 3.6 Frontend — delivery layer

- `shared/services/cloudinaryDelivery.ts` (285 lines): rewrites stored URLs into `c_limit,h_1280,w_1280,q_auto:good,vc_h264` mp4 delivery URLs + optional HLS (`sp_hd` → .m3u8). Comment states: *"Uploads store the RAW original: file_url can be a 4K HEVC clip straight off an iPhone."*
- `shared/hooks/useAdaptiveVideo.ts` + `hls.js`: plays HLS where possible, mp4 fallback everywhere. (HLS flag currently OFF on both sides.)
- `next.config.ts`: `images.remotePatterns` allows only `res.cloudinary.com`.
- `src/constants.ts`: hardcoded Goatza logo URL on `res.cloudinary.com`.
- `features/profile/utils/ogImage.ts`: special-cases `res.cloudinary.com` URLs for share cards.

### 3.7 Dependencies

- Backend `requirements.txt`: `cloudinary==1.44.1`, `django-cloudinary-storage==0.3.0` (and `google-cloud-storage==3.10.1`, which appears **unused** — remove during cleanup). `settings.py` also registers `cloudinary` + `cloudinary_storage` apps and `DEFAULT_FILE_STORAGE` (no model uses FileField/ImageField, so removable).
- Frontend: `browser-image-compression`, `hls.js`, `react-easy-crop` (all stay).

---

## 4. Capability gaps — what Cloudinary did that R2 will not, and the replacement for each

| # | Cloudinary capability (in active use) | R2 replacement (locked decision) |
|---|---|---|
| G1 | **Video transcoding on delivery** — raw 4K/HEVC originals are stored; every `<video>` plays a derivative (`c_limit,h_1280,…,vc_h264` mp4) | **Client-side compression BEFORE upload** (WebCodecs): output H.264 MP4, longest side ≤ 1280, AAC audio, faststart. The stored file IS the playable file. Also fixes storage economics (raw 300 MB uploads would consume the 10 GB free tier in ~35 videos). |
| G2 | **Auto video poster** — `so_0` frame URL built by string manipulation (client + `build_video_thumbnail_url` server-side) | **Client captures a poster frame** (canvas) at upload, uploads it as a WebP/JPEG object alongside the video; URL saved into the existing `thumbnail_url` / `media_thumbnail_url` columns. |
| G3 | **On-the-fly image resizing** — `cloudinaryThumb(url, 640)` = `c_limit,w_640,q_auto,f_auto` | **Upload a 640 px WebP thumb variant** alongside the full image for feed/chat lists; store in the existing `thumbnail_url` field (currently blank for images). Full image stays ≤2560 px WebP as today. |
| G4 | **Server-side trusted metadata** — width/height/duration fetched from Cloudinary API | **Client submits** width/height/duration/size at attach time; **server clamps + sanity-validates** (positive ints, duration ≤ type limit, size ≤ type cap). Trust-model change accepted: values are cosmetic (layout/labels), and abuse is bounded by clamps. |
| G5 | **HLS adaptive streaming** (flag-gated, currently OFF) | **Parked.** `useAdaptiveVideo` keeps its mp4-only path (`hlsSrc` stays empty). Future: Bunny Stream slots into the same hook (it serves HLS natively; `hls.js` already integrated). Not part of this migration. |
| G6 | **URL versioning on overwrite** — profile/logo reuse a fixed public_id; Cloudinary's `/v<ts>/` busts caches | Fixed-name objects get a **`?v=<unix-ts>` query param** appended to the stored URL on every replace, so the CDN serves the new file immediately. Random-UUID objects are immutable and need nothing. |

---

## 5. Change specification — Backend (Django)

### 5.1 New: `services/storage/r2.py` — `R2Service`

Implements `BaseStorageService` using `boto3` against R2's S3-compatible endpoint.

**Upload config — new response contract** (the frontend maps `key ↔ public_id`, `public_url ↔ secure_url`):

```json
{
  "provider": "r2",
  "temp_post_id": "…",            // only for posts / recruitments (unchanged)
  "uploads": [
    {
      "method": "PUT",
      "upload_url": "https://<account>.r2.cloudflarestorage.com/<bucket>/<key>?X-Amz-…",
      "key": "users/<uid>/posts/<temp>/<uuid>.mp4",
      "public_url": "https://media.goatza.com/users/<uid>/posts/<temp>/<uuid>.mp4",
      "headers": { "Content-Type": "video/mp4" },
      "expires_in": 600
    }
  ]
}
```

**Request contract change:** the endpoint becomes `POST /upload-config` with body
`{ "type": "posts", "org_id": …?, "files": [{ "content_type": "image/webp", "size_bytes": 812345, "kind": "image" | "video" | "thumb" }] }`
(GET with `count` is removed — the client now encodes first and declares exactly what it will upload). Backend:
1. Keeps ALL existing actor/type/org-membership guards from `GetUploadConfigAPIView` unchanged.
2. Validates each file against a per-type policy table (allowed content types + max `size_bytes` — see §7).
3. Generates keys using the **same folder scheme** as today (§3.2), appending a proper extension derived from the validated content type.
4. Signs a presigned **PUT** per file with `ContentType` bound (client must send the identical `Content-Type` header) and short expiry (`600 s`).
5. For a video the client requests **two** entries in one call (`kind: "video"` + `kind: "thumb"`); both land in the same folder.

**Other methods:**
- `delete_file(key)` → `s3.delete_object`
- `delete_folder_data(prefix)` → paginated `list_objects_v2` + batched `delete_objects` (1000/batch)
- `get_media_metadata(...)` → returns `{}` (dead path; call sites switch to client-supplied values, §5.3)
- `ensure_video_derivatives(...)` → no-op (removed from call sites, §5.3)

### 5.2 `services/storage/` shared changes

- `factory.py`: `FILE_STORAGE_PROVIDER` setting selects `"r2"` (default) or `"cloudinary"` (kept only until Stage 6 cleanup — acts as an instant rollback flag during the build).
- `validators.py`:
  - `is_valid_cloudinary_url` → `is_valid_media_url(url)`: URL must start with `settings.MEDIA_PUBLIC_BASE_URL`.
  - `extract_public_id_from_url` → strip the base URL + leading slash + any `?v=` suffix → key. (Keep the old regex only behind the cloudinary provider until cleanup.)
  - `validate_public_id` (actor-prefix ownership) — **unchanged**, folder scheme is identical.
  - Delete `build_video_thumbnail_url` (G2 makes it obsolete).
  - Extension allowlists: images `{webp, jpg, jpeg, png}`; videos `{mp4, webm}` (**`mov` removed** — after G1 the client always produces mp4; a raw `.mov` should never be stored).

### 5.3 Call-site changes

| File | Change |
|---|---|
| `accounts/views/user_upload_signature_views.py` | GET→POST body contract (§5.1); guards/messages untouched |
| `posts/views/posts_views.py` | Remove `get_media_metadata` + `ensure_video_derivatives`; accept `width`, `height`, `duration`, `size_bytes` from the attach payload and clamp (ints > 0; duration ≤ 300 s; caps per §7). Validate `file_url`/`thumbnail_url` with `is_valid_media_url` + actor-prefix check (same pattern messaging already uses) |
| `highlights/services/highlight_services.py` | Remove `ensure_video_derivatives`; require client `thumbnail_url` (validated: our domain + same folder as the video); clamp duration ≤ 90 s |
| `messaging/services/message_service.py` | `_validate_chat_media_url` keeps its exact replay-protection logic with the new URL check; video messages now require a client-uploaded `media_thumbnail_url` (validated to the sender's chat prefix) instead of `build_video_thumbnail_url` |
| `recruitments/services/recruitment_service.py` | Swap URL validator; delete logic unchanged |
| `matches`, `accounts/user_views`, `organization_views` | No logic change (`delete_file` interface identical). Add `?v=<ts>` cache-buster to stored URL on profile/cover/logo replace (G6) |
| `posts/management/commands/backfill_*.py` | Delete both (Cloudinary-only; dev data) |

### 5.4 Settings & dependencies

```python
# core/settings.py  — remove the CLOUDINARY block, INSTALLED_APPS entries
# ('cloudinary', 'cloudinary_storage') and DEFAULT_FILE_STORAGE. Add:
FILE_STORAGE_PROVIDER   = os.getenv("FILE_STORAGE_PROVIDER", "r2")
R2_ACCOUNT_ID           = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID        = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY    = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET               = os.getenv("R2_BUCKET", "goatza-media")
MEDIA_PUBLIC_BASE_URL   = os.getenv("MEDIA_PUBLIC_BASE_URL", "https://media.goatza.com")
```

`requirements.txt`: **add** `boto3`; **remove** `cloudinary`, `django-cloudinary-storage`, `google-cloud-storage` (unused).

---

## 6. Change specification — Frontend (Next.js)

### 6.1 New shared module: `shared/services/mediaUpload.ts`

- `putToR2(file: Blob, upload: R2Upload, onProgress): Promise<void>` — XHR `PUT` to `upload.upload_url` with the bound `Content-Type` header; keeps today's progress-callback shape; reject on non-2xx / network / abort.
- `getUploadConfigApi(type, files, orgId?)` — the new POST contract (§5.1).
- Result assembly uses `upload.public_url` as `file_url` and `upload.key` as `public_id` — so every existing attach API keeps its current payload shape.

### 6.2 New shared module: `shared/services/videoEncode.ts` (G1 — the real work)

Spec (implementation library is the builder's choice — candidates: **mediabunny** (WebCodecs mux/transcode), `@remotion/webcodecs`; `ffmpeg.wasm` only as last resort):
- Input: any browser-decodable video `File` (incl. iPhone HEVC `.mov`).
- Output: **MP4, H.264 (baseline/main), longest side ≤ 1280, ~1.5–2.5 Mbps video, AAC audio preserved, faststart** (moov at front), plus `{ width, height, duration }`.
- Skip re-encode when the source is already H.264 MP4 within the size/dimension budget (fast path).
- Progress callback (encode is the slow phase; surface it in the existing upload progress UI).
- **Failure path:** if WebCodecs is unavailable or encoding fails → if the original is `.mp4`/`.webm` AND under the type's size cap, upload it as-is; otherwise block with a friendly "This video format isn't supported on this device — try a shorter clip or a different file" error. Never upload a raw `.mov`.
- `capturePoster(file | objectURL, { aspect? }): Promise<Blob>` — seek ~0 s, draw to canvas, export WebP (feed poster ≤1280, highlight poster 9:16 ~360×640 c_fill-style crop to match today's tile).

### 6.3 New: `shared/services/imageVariants.ts` (G3)

- `makeThumb(compressedFull: Blob, 640): Promise<Blob>` — second `browser-image-compression` pass (`maxWidthOrHeight: 640`, WebP). Post images + chat images upload `[full, thumb]`; thumb URL → existing `thumbnail_url` field. Profile/logo/achievement/match photos stay single-file (already small).

### 6.4 Per-service rewiring (all 7 upload services/hooks)

Common new order everywhere: **encode/compress → request config with exact `{content_type, size_bytes}` → PUT → attach with client metadata.**
- `postUpload.service.ts`: images → full+thumb; video → `videoEncode` + poster; attach payload adds `width/height/duration/size_bytes`; `MAX_VIDEO_MB` becomes a post-encode cap (80 MB) with the 5-min duration check moved before encoding.
- `highlightUpload.service.ts`: encode + 9:16 poster; post-encode cap 40 MB.
- `chatUpload.service.ts`: `cloudinaryThumb()` deleted; image messages upload full+thumb; video messages encode + poster; `media_thumbnail_url` now sent by the client.
- Profile / org / achievement / match hooks: only the transport swaps (compress → config → PUT); crop flows untouched.

### 6.5 Delivery layer

- `cloudinaryDelivery.ts` → replace with a tiny `mediaDelivery.ts`: `videoSrc(m) = m.file_url`, `posterSrc(m) = m.thumbnail_url`, `thumbSrc(m) = m.thumbnail_url || m.file_url`, `hlsSrc() = ""` (G5). Passes `blob:`/`data:` through untouched (preview behaviour preserved). Update its ~10 consumer files (MediaCarousel, HighlightViewer, Image/VideoMessage, RecruitmentDetail, EditPostModal, …).
- `useAdaptiveVideo.ts`: **unchanged** (mp4 path already the fallback; empty `hlsSrc` is a supported input).
- `next.config.ts`: `remotePatterns` → `media.goatza.com` (keep `res.cloudinary.com` temporarily until Stage 6).
- `src/constants.ts`: logo → `/public/` asset. `ogImage.ts`: drop the Cloudinary special-case; share-card fetch works with any absolute URL.

---

## 7. Upload policy table (server-enforced caps — single source of truth)

| Type | Kinds | Allowed content types | Max size (post-encode) | Extra |
|---|---|---|---|---|
| profile / cover / org logo / org cover | image | `image/webp`, `image/jpeg`, `image/png` | 5 MB | fixed key, `?v=<ts>` on replace |
| posts (image) | image + thumb | same | 5 MB full / 1 MB thumb | ≤10 images/post (unchanged) |
| posts (video) | video + thumb | `video/mp4`, `video/webm` + image thumb | 80 MB / 1 MB | duration ≤ 300 s; 1 video/post |
| highlights | video + thumb | same | 40 MB / 1 MB | duration ≤ 90 s |
| chat image | image + thumb | image types | 5 MB / 1 MB | sender-prefix replay guard (unchanged) |
| chat video | video + thumb | video types | 80 MB / 1 MB | same guard |
| achievements / matches | image | image types | 5 MB | user-only (unchanged) |
| recruitments | image / video + thumb | both | 5 MB img / 80 MB video | org-only (unchanged) |

Server clamps for client-supplied metadata: `1 ≤ width,height ≤ 8192`; `1 ≤ duration ≤ type limit`; `size_bytes ≤ type cap`.

## 8. Security requirements (all preserved or strengthened)

1. **Presigned PUT only** — 10-min expiry, `Content-Type` bound, one key per signature; the API token used by Django is scoped to Object Read & Write on the single bucket (no admin scopes).
2. **Key ownership** — keys are generated server-side under the requesting actor's folder (never client-chosen); every attach endpoint re-validates `public_id` starts with the actor's prefix (existing `validate_public_id` + messaging's replay guard stay).
3. **Domain pinning** — any client-submitted URL must start with `MEDIA_PUBLIC_BASE_URL` and its embedded key must equal the submitted `public_id` (existing messaging check, extended to posts/highlights/recruitments).
4. **Caps enforced twice** — at config time (declared size vs policy) and at attach time (clamps). Note: presigned PUT cannot hard-enforce byte length at the storage layer; declared-size validation + short expiry + per-actor keys bound the abuse surface. Acceptable for launch; revisit if abused.
5. **CORS** on the bucket allows `PUT` only from production + localhost origins (§9).
6. Public access ONLY via the custom domain (no public `r2.dev` URL enabled).

## 9. Infra setup checklist (manual, ~30 min — do BEFORE Stage 1)

1. Cloudflare account → move `goatza.com` DNS to Cloudflare (free plan) if not already.
2. R2 → create bucket `goatza-media` (location hint: APAC).
3. R2 API token: **Object Read & Write**, scoped to `goatza-media` only → copy Account ID, Access Key ID, Secret.
4. Bucket → Settings → Custom Domains → connect `media.goatza.com` (leave r2.dev dev URL disabled).
5. Bucket CORS policy:
```json
[
  {
    "AllowedOrigins": ["https://goatza.com", "https://www.goatza.com", "https://<vercel-preview-domain>", "http://localhost:3000"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["Content-Type"],
    "MaxAgeSeconds": 3600
  }
]
```
6. Cache: default CDN caching on the custom domain is fine (immutable UUID keys); optionally a Cache Rule: `media.goatza.com/*` → Edge TTL 1 month.
7. Env vars → Render (backend): the 5 `R2_*`/`MEDIA_PUBLIC_BASE_URL` vars + `FILE_STORAGE_PROVIDER=r2`. Vercel: `NEXT_PUBLIC_MEDIA_BASE_URL=https://media.goatza.com` (used only by next.config remotePatterns/constants if needed). Local `.env` files mirror these.

## 10. Locked decisions (do NOT re-litigate in build chats)

- One provider for ALL media: R2. No Cloudinary anywhere after Stage 6.
- No DB schema changes; keys live in existing `public_id` columns, URLs in `*_url`, thumbs in `thumbnail_url`.
- Folder scheme (§3.2) unchanged.
- Client-side encoding (G1) is required — never store raw `.mov`/HEVC.
- HLS stays off; `useAdaptiveVideo` untouched; Bunny Stream is a future add-on, out of scope.
- No data migration (dev data only). Fresh bucket.
- Budget: ₹0 for media at launch (R2 free tier), scaling ≈ ₹160/mo at ~10k users.

---

## 11. Build plan — staged Claude Code prompts

Run stages **in order**, each on a branch, review + test between stages. Give Claude Code this document (`GOATZA_R2_MIGRATION.md`) in the repo root of BOTH projects before starting. Suggested per-stage prompt texts:

### Stage 1 — Backend: R2Service + config endpoint (goatza-backend)
> Read GOATZA_R2_MIGRATION.md fully. Implement Stage 1 only: (1) `services/storage/r2.py` `R2Service` per §5.1 using boto3 presigned PUT; (2) update `factory.py` with `FILE_STORAGE_PROVIDER` (default "r2", "cloudinary" still selectable); (3) settings + env vars per §5.4, add boto3 to requirements (do NOT remove cloudinary packages yet); (4) convert the upload-config endpoint to the POST contract in §5.1 with the policy table in §7, preserving every existing actor/org guard and error message; (5) update `validators.py` per §5.2 keeping cloudinary variants working behind the provider flag. Write/adjust unit tests for the new endpoint (mock boto3) covering: each upload_type, policy rejections (bad content_type, oversize), org-membership failure, video+thumb pairing. Do not touch call sites yet.

### Stage 2 — Backend: call sites + metadata trust model (goatza-backend)
> Read GOATZA_R2_MIGRATION.md. Implement Stage 2 per §5.3: remove `ensure_video_derivatives`/`get_media_metadata` usage; attach endpoints accept and clamp client `width/height/duration/size_bytes` (§7 clamps); require + validate client `thumbnail_url` for videos in posts/highlights/messaging (domain pin + actor-prefix, reusing messaging's pattern); add `?v=<ts>` cache-buster on profile/cover/logo replace; delete the two backfill commands. Update all affected tests; run the full backend test suite green.

### Stage 3 — Frontend: shared uploader + image-only flows (goatza-frontend)
> Read GOATZA_R2_MIGRATION.md. Implement §6.1 (`mediaUpload.ts`: new POST config API + `putToR2` with progress) and §6.3 (`imageVariants.ts`), then rewire the image-only flows: profile, org, achievements, matchDiary, and chat IMAGE messages (full+thumb). Order everywhere: compress → config → PUT → attach. Keep all crop UX. Update affected unit tests; typecheck clean.

### Stage 4 — Frontend: delivery layer swap (goatza-frontend)
> Read GOATZA_R2_MIGRATION.md §6.5. Replace `cloudinaryDelivery.ts` with `mediaDelivery.ts` (URL passthrough helpers), update all consumers (MediaCarousel, HighlightViewer, ImageMessage, VideoMessage, RecruitmentDetail, EditPostModal, ogImage.ts, constants.ts logo), and add `media.goatza.com` to next.config remotePatterns (keep cloudinary pattern for now). `useAdaptiveVideo` must not change. All existing component tests updated and green.

### Stage 5 — Frontend: video encoding pipeline (goatza-frontend)
> Read GOATZA_R2_MIGRATION.md §6.2. Implement `videoEncode.ts` (choose mediabunny or @remotion/webcodecs; justify choice in a code comment) with the exact output spec, fast path, failure path, and `capturePoster` (feed + 9:16 highlight variants). Wire into postUpload, highlightUpload, and chat video flows with encode progress surfaced in the existing progress UI. Add tests for the decision logic (mock the encoder); document manual device-test steps in the PR description.

### Stage 6 — Both repos: Cloudinary removal + cleanup
> Read GOATZA_R2_MIGRATION.md. Remove everything Cloudinary: backend packages (`cloudinary`, `django-cloudinary-storage`, unused `google-cloud-storage`), INSTALLED_APPS entries, `DEFAULT_FILE_STORAGE`, `cloudinary.py` service + cloudinary branches in factory/validators, all `CLOUDINARY_*` settings/env references; frontend: cloudinary remotePattern, any leftover helpers/tests/fixtures referencing `res.cloudinary.com`. Grep both repos for "cloudinary" — zero functional hits allowed (docs/changelog mentions fine). Full test suites green.

## 12. End-to-end test checklist (after Stage 5, again after Stage 6)

Per feature — upload → appears in feed/detail → thumbnail correct → delete removes object(s) from R2:
- [ ] Profile photo + cover (replace twice → new image shows immediately = cache-buster works)
- [ ] Org logo + cover (as org actor) · [ ] Post: 10 images · [ ] Post: 1 video (portrait AND landscape)
- [ ] Highlight ≤90 s (9:16 poster correct) · [ ] Chat: image + video, both actor types; replaying another user's URL is rejected
- [ ] Achievement, match photo, recruitment media
- Video matrix: iPhone HEVC `.mov` source (Safari + Chrome), Android Chrome, desktop Chrome/Firefox — output plays with seek; encode of a 1-min clip completes on a mid-range phone; unsupported-device failure path shows the friendly error
- [ ] Oversize/bad-type rejected at config step · [ ] Feed scroll uses thumbs (Network tab: 640px WebPs, not full images)
- [ ] Post delete removes the whole post folder from R2 (list bucket prefix = empty)

## 13. Rollback & future

- **During build:** `FILE_STORAGE_PROVIDER=cloudinary` flips the backend back instantly until Stage 6 lands.
- **Future (out of scope):** Bunny Stream for HLS adaptive video — feeds `useAdaptiveVideo.hlsSrc`; R2 remains source-of-truth storage. Consider only when real traction makes multi-quality streaming matter (~₹900/mo at 10k active users).

*End of document.*
