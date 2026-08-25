# Stage 1 — Cloudflare R2 storage provider

Adds R2 as a storage provider behind `FILE_STORAGE_PROVIDER` (default `r2`) and a
**new POST handler** on the existing upload-config endpoint that hands the client
presigned PUTs.

Cloudinary is untouched and still fully working: no Cloudinary code, setting or
package was removed, no attach/delete call site was changed, and every existing
consumer still uses the unchanged GET handler. Flipping
`FILE_STORAGE_PROVIDER=cloudinary` is the complete rollback.

Nothing is committed — all of this is uncommitted working-tree changes.

---

## Files

### Created

| File | What |
|---|---|
| `services/storage/r2.py` | `R2Service` — lazy boto3 S3 client, presigned PUTs, delete, paginated prefix sweep |
| `services/storage/paths.py` | The object-path scheme, now shared by **both** providers |
| `.env.example` | Was missing; created with the six new keys plus the existing ones |
| `STAGE1_NOTES.md` | This file |

### Modified

| File | What |
|---|---|
| `core/settings.py` | New `MEDIA STORAGE PROVIDER` + `CLOUDFLARE R2` blocks. Every `CLOUDINARY_*` setting left as-is. |
| `services/storage/factory.py` | `r2` (default) → `R2Service`, `cloudinary` → `CloudinaryService`, anything else → `ValueError` naming the bad value |
| `services/storage/validators.py` | **Added** `is_valid_media_url`, `extract_key_from_url`, `R2_VIDEO_EXTENSIONS`. Every Cloudinary validator untouched. |
| `services/storage/cloudinary.py` | Folder/public_id construction delegated to `paths.py`. Behaviour-identical — see "Deviations" below. |
| `accounts/views/user_upload_signature_views.py` | Added `POLICY` + `post()`. GET handler byte-for-byte unchanged, marked `TODO(cleanup-stage)`. |
| `requirements.txt` | `boto3==1.43.79` + its pinned deps (`botocore`, `jmespath`, `python-dateutil`, `s3transfer`) — already installed in `goatza-env`, so no install step needed |

### Deleted

None.

---

## Object keys

Identical scheme to Cloudinary, plus an extension derived from the validated
`content_type` (`image/webp→.webp`, `image/jpeg→.jpg`, `image/png→.png`,
`video/mp4→.mp4`, `video/webm→.webm`).

```
profile              users/<user_id>/profile.<ext>                    (fixed, overwrites)
cover                users/<user_id>/cover.<ext>                      (fixed, overwrites)
organization_logo    organizations/<org_id>/logo.<ext>                (fixed, overwrites)
organization_cover   organizations/<org_id>/cover.<ext>               (fixed, overwrites)
posts                users/<user_id>/posts/<temp_post_id>/<uuid>.<ext>
                     organizations/<org_id>/posts/<temp_post_id>/<uuid>.<ext>
recruitments         organizations/<org_id>/recruitments/<temp_id>/<uuid>.<ext>
chat                 chat/users/<user_id>/<uuid>.<ext>
                     chat/organizations/<org_id>/<uuid>.<ext>
achievements         users/<user_id>/achievements/<uuid>.<ext>
matches              users/<user_id>/matches/<uuid>.<ext>
highlights           users/<user_id>/highlights/<uuid>.<ext>          (new — see Deviations)
```

`temp_post_id` generation and its response key are unchanged from Cloudinary:
a fresh uuid4 per request for `posts` **and** `recruitments`, returned as
`temp_post_id` in both cases.

Uploads come back **in the same order as the request `files` array** — that
positional pairing is how the client maps an upload back to the file it picked,
and a video to its thumb.

---

## Exercising the endpoint

`POST /user/get/upload/signature` (same URL as the GET).

```bash
curl -X POST 'http://127.0.0.1:8000/user/get/upload/signature' \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'X-Actor-Type: user' \
  -H 'Content-Type: application/json' \
  -d '{
        "type": "posts",
        "files": [
          {"content_type": "video/mp4",  "size_bytes": 12000000, "kind": "video"},
          {"content_type": "image/jpeg", "size_bytes": 48000,    "kind": "thumb"}
        ]
      }'
```

```json
{
  "success": true,
  "message": "",
  "data": {
    "provider": "r2",
    "uploads": [
      {
        "method": "PUT",
        "upload_url": "https://<acct>.r2.cloudflarestorage.com/goatza-media/users/e6395b25-.../posts/916cd334-.../fc2bcc59-....mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=...&X-Amz-Date=20260825T111451Z&X-Amz-Expires=600&X-Amz-SignedHeaders=content-type%3Bhost&X-Amz-Signature=...",
        "key": "users/e6395b25-.../posts/916cd334-.../fc2bcc59-....mp4",
        "public_url": "https://media.goatza.com/users/e6395b25-.../posts/916cd334-.../fc2bcc59-....mp4",
        "headers": { "Content-Type": "video/mp4" },
        "expires_in": 600
      },
      {
        "method": "PUT",
        "upload_url": "https://<acct>.r2.cloudflarestorage.com/goatza-media/users/e6395b25-.../posts/916cd334-.../319711d1-....jpg?X-Amz-...",
        "key": "users/e6395b25-.../posts/916cd334-.../319711d1-....jpg",
        "public_url": "https://media.goatza.com/users/e6395b25-.../posts/916cd334-.../319711d1-....jpg",
        "headers": { "Content-Type": "image/jpeg" },
        "expires_in": 600
      }
    ],
    "temp_post_id": "916cd334-bef9-446e-b214-bc4465247eae"
  }
}
```

Then, per entry, the browser does exactly one request — the `Content-Type` header
**must** match, it is bound into the signature:

```bash
curl -X PUT "<upload_url>" -H "Content-Type: video/mp4" --data-binary @clip.mp4
```

Acting for an org: send `X-Actor-Type: organization` + `X-Actor-Id: <org_id>`, or
pass `"org_id": "<uuid>"` in the body (same membership check as GET).

### Policy at a glance

| type | allowed |
|---|---|
| `profile` `cover` `organization_logo` `organization_cover` `achievements` `matches` | exactly 1 image ≤ 5 MB |
| `posts` | up to 10 images ≤ 5 MB (each may carry a thumb) **OR** 1 video ≤ 80 MB + 1 thumb |
| `recruitments` | images ≤ 5 MB **and/or** 1 video ≤ 80 MB + 1 thumb |
| `highlights` | 1 video ≤ 40 MB + 1 thumb |
| `chat` | 1 image ≤ 5 MB (optional thumb) **OR** 1 video ≤ 80 MB + required thumb |

Content types: images `image/webp` `image/jpeg` `image/png`; videos `video/mp4`
`video/webm`; thumbs are images ≤ 1 MB. Any `video` requires exactly one `thumb`
in the same request; a `thumb` with no parent is rejected.

---

## Verification

- `python manage.py check` → **System check identified no issues (0 silenced).**
- `manage.py runserver` boots; the endpoint answers `401` unauthenticated.
- Ad-hoc (not committed, per the no-tests rule): 44 policy cases across every
  type — allow/reject verdicts and messages — all as intended; R2 keys diffed
  against the Cloudinary `folder`/`public_id` pairs for all 10 existing types;
  presigned URLs verified to be SigV4 with `content-type;host` signed and
  `X-Amz-Expires=600`; GET still returns Cloudinary params unchanged.

---

## Ambiguities resolved

Each was decided toward the reading most consistent with the existing code.

**1. `highlights` is in the POST policy but not in the GET `ALLOWED_TYPES`.**
The brief's POLICY names `highlights`, but the current endpoint has no such type
— highlights are signed as `posts` today, which the frontend documents in
`highlightUpload.service.ts` as a deliberate stopgap ("Add a dedicated
`highlights` type to `ALLOWED_TYPES` server-side if you want them stored
apart"). Resolution: `highlights` is a **POST-only** type. It is a key in
`POLICY`, and the GET `ALLOWED_TYPES` was left untouched, since GET had to stay
byte-for-byte identical. Its folder follows the achievements/matches shape
(`users/<id>/highlights/<uuid>`) and it is **user-only**, because a `Highlight`
row is owned by a `User` and the service layer already restricts it to players.

**2. The key for fixed-slot types.**
Cloudinary stores `profile` as folder `users/<id>/profile` + public_id `profile`
(i.e. `users/<id>/profile/profile`). The brief's scheme says
`profile → users/<user_id>/profile`. Resolution: the brief's literal reading —
the R2 key is `users/<id>/profile.<ext>`, not `.../profile/profile.<ext>`. Both
satisfy the `users/<id>/` prefix that `validate_public_id` enforces, and the
flatter key is the one the brief spells out.
*Known consequence for a later stage:* because the extension is part of the key,
replacing a `.webp` avatar with a `.jpg` writes a new key instead of overwriting,
leaving the old object behind. Harmless (the DB points at the new URL), but the
cleanup stage may want a delete-old-then-write on the fixed slots.

**3. "Reuse/extract the Cloudinary folder logic."**
Rather than importing from `cloudinary.py` (which would make the R2 path depend
on the module being deleted at cleanup) or copying the strings (which would let
them drift), the folder/public_id construction was **extracted** into a new
`services/storage/paths.py` that both providers now call. `cloudinary.py` keeps
every branch, comment and return shape; only the f-strings became
`build_folder(...)` / `build_object_name(...)` calls, verified to produce
identical output for all 10 types × both actors. Two incidental effects:
- An invalid actor on the `posts` branch now raises `ValueError("Invalid actor
  for posts upload")` instead of `NameError` on an unbound `folder`. Unreachable
  through the view (the actor guards run first).
- A few now-unused locals (`user = actor.user`, `org = actor.organization`)
  remain in `cloudinary.py`. Left in place deliberately — removing Cloudinary
  code is the cleanup stage's job.

**4. `env` keys that already exist but are empty.**
`.env` already carries `FILE_STORAGE_PROVIDER=`, `R2_BUCKET=` and
`MEDIA_PUBLIC_BASE_URL=` with **empty values**, and `os.getenv(k, default)`
returns `""` for those, not the default. An empty provider would raise on every
upload and an empty base URL would make `is_valid_media_url()` accept any URL at
all. So those three read `os.getenv(k) or <default>`.

**5. `MEDIA_PUBLIC_BASE_URL` trailing slash.**
Normalised with `.rstrip("/")` at settings load, since every stored URL is
`base + "/" + key` and a trailing slash would produce `//` in every media URL.

**6. Request-level file cap.**
Not specified. Set to `MAX_FILES_PER_REQUEST = 20` — the largest legitimate batch
is a post with 10 images and 10 thumbs. It is a cheap guard before the per-file
loop; the per-type counts are still what actually decide.

**7. Error messages for the new policy checks.**
Every guard the GET handler already had keeps its exact message (`Invalid upload
type`, `You are not a member of this organization`, `Organization not found`,
`Switch to your personal account for this upload`, `Switch to your organization
account for this upload`). The over-count guard is built as
`f"Invalid count (1-{max} allowed)"`, which reproduces GET's
`Invalid count (1-10 allowed)` verbatim for `posts`/`recruitments`; single-image
types say `Only one image per upload` rather than the nonsensical
`Invalid count (1-1 allowed)`. Genuinely new rules (kinds, sizes, pairing) get
new messages, in the same `response_data` + `error_body(msg, field)` shape.

**8. `?v=` in `extract_key_from_url`.**
Only the `?v=` cache-buster is stripped, as specified — not the whole query
string. Unlike the Cloudinary extractor, the **extension is kept**: on R2 the
extension is part of the object key, so dropping it would produce a key that
does not exist.

---

## Not in this stage (by design)

- No call site was migrated — every attach/delete path still goes through
  Cloudinary public_ids. `delete_file` / `delete_folder_data` on `R2Service`
  take an object key / prefix and are ready, but nothing calls them yet.
- No frontend change; nothing sends the POST yet.
- No tests written or updated (per the brief). The ad-hoc checks above were run
  from a scratch script and not saved.
- `get_media_metadata` returns `{}` and `ensure_video_derivatives` is a no-op —
  metadata and derivatives now come from the client, which encodes video before
  upload. The callers that consume these still run against Cloudinary today, so
  they will need real values from the client payload in a later stage.
- Bucket-side setup is not code: R2 needs a CORS rule allowing `PUT` with
  `Content-Type` from the app origin, and `MEDIA_PUBLIC_BASE_URL` needs public
  read (CDN domain or `r2.dev`).
