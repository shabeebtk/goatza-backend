# Stage 1 — R2Service + upload-config endpoint

Backend half of the Cloudinary → Cloudflare R2 migration (`GOATZA_R2_MIGRATION.md`).
Scope is deliberately narrow: a working R2 storage provider and the new
`POST /upload-config` contract. **No call site was touched and no Cloudinary code
or package was removed** — Cloudinary still works behind
`FILE_STORAGE_PROVIDER=cloudinary` until Stage 6.

---

## ⚠️ Read this before running the app locally

`FILE_STORAGE_PROVIDER` now defaults to **`r2`** (doc §5.4). The current frontend
still calls the **GET** upload-config contract, which only the Cloudinary
provider can serve — so with the default in place, every upload in the existing
app fails with a 400 until Stage 3 rewires the client.

To keep working on the app as it is today, put this in your local `.env`:

```
FILE_STORAGE_PROVIDER=cloudinary
```

The R2 keys are **not** in `.env` yet. Copy `.env.example` and fill them in from
the Cloudflare dashboard (doc §9) before attempting the live round-trip in
`verification-checklist.md`.

---

## Files changed

| File | What |
|---|---|
| `services/storage/r2.py` | **new** — `R2Service`: presigned PUT per declared file, Cloudinary-identical folder scheme, paginated/batched prefix delete, no-op metadata + derivatives |
| `services/storage/factory.py` | provider switch on `FILE_STORAGE_PROVIDER` (default `r2`; `cloudinary` kept as the rollback path) |
| `services/storage/validators.py` | **added** `is_valid_media_url` + `extract_key_from_media_url`, `MEDIA_IMAGE_EXTENSIONS` / `MEDIA_VIDEO_EXTENSIONS`; existing Cloudinary helpers untouched, marked with stage TODOs |
| `services/storage/base.py` | `get_upload_config` signature widened to `(actor, upload_type, **kwargs)` and documented — the two providers take different kwargs (`files` vs `count`) |
| `services/storage/__init__.py` | **new** — empty; makes `services.storage` a regular package so its `tests/` are discovered by the test runner |
| `accounts/views/user_upload_signature_views.py` | **added** the `post` handler (v2 contract) + module-level `POLICY`; GET kept, guards extracted into `_resolve_org_actor` / `_guard_actor_type` shared by both verbs |
| `core/settings.py` | new `MEDIA STORAGE` block (6 settings); Cloudinary block untouched, marked `TODO(stage-6)` |
| `requirements.txt` | **added** `boto3`, `botocore`, `jmespath`, `python-dateutil`, `s3transfer`. Nothing removed. |
| `.env.example` | **new** — the R2 vars + where to get each one |
| `services/storage/tests/test_r2_service.py` | **new** — 33 tests |
| `accounts/tests/test_upload_config.py` | **new** — 55 tests |

---

## Running the tests

```bash
# Stage 1 only
python manage.py test services.storage.tests.test_r2_service accounts.tests.test_upload_config

# full suite (the definition of done)
python manage.py test
```

Everything is mocked at `services.storage.r2.boto3.client` — no network, no
bucket, no credentials needed. One test (`test_presigned_url_is_signed_for_r2`)
uses the real boto3 client on purpose: presigning is pure local HMAC, and it is
the only cheap way to catch a wrong endpoint, region, or signature version
before a live round-trip.

If a run dies mid-way, Postgres can be left holding `test_goatza-db`; re-run
with `--noinput` to drop and recreate it.

---

## The new request/response

```bash
curl -X POST 'http://localhost:8000/user/get/upload/signature' \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'X-Actor-Type: user' \
  -H 'Content-Type: application/json' \
  -d '{
        "type": "profile",
        "files": [
          { "kind": "image", "content_type": "image/webp", "size_bytes": 812345 }
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
        "upload_url": "https://<account>.r2.cloudflarestorage.com/goatza-media/users/<uid>/profile.webp?X-Amz-Algorithm=AWS4-HMAC-SHA256&…&X-Amz-Expires=600&X-Amz-Signature=…",
        "key": "users/<uid>/profile.webp",
        "public_url": "https://media.goatza.com/users/<uid>/profile.webp",
        "headers": { "Content-Type": "image/webp" },
        "expires_in": 600
      }
    ]
  }
}
```

Then push the bytes — the `Content-Type` header must match the one in `headers`
exactly, because it is bound into the signature:

```bash
curl -X PUT -H 'Content-Type: image/webp' --data-binary @test.webp "<upload_url>"
```

A post/recruitment response additionally carries `temp_post_id` (unchanged from
the Cloudinary contract). `uploads[i]` always corresponds to `files[i]`.

An org-scoped call adds `"org_id": "<uuid>"` to the body, or sends the
`X-Actor-Type: organization` / `X-Actor-Id` headers — both paths work, exactly as
they did on GET.

---

## Decisions taken where the doc left room

Listed rather than asked, per the build prompt. Each one is the reading that
matches the existing code.

**1. Fixed-slot keys are `<folder>.<ext>`, not `<folder>/<public_id>.<ext>`.**
Cloudinary's `folder=users/<id>/profile` + `public_id=profile` composes to
`users/<id>/profile/profile`. `verification-checklist.md` (Stage 1) states the
object should appear at `users/<id>/profile.webp`, so the folder path itself
becomes the key and the redundant leaf is dropped. Random-name types are
unaffected: `users/<id>/posts/<temp>/<uuid>.<ext>`.

**2. No `highlights` upload type was added.** Doc §7 lists a highlights row, but
there is no such type today — `highlightUpload.service.ts` signs clips as
`posts` deliberately (its header comment says so), and adding one would have
changed `ALLOWED_TYPES`, which the prompt said to preserve. Consequence: the
highlight-specific caps (40 MB, ≤90 s) stay client-side and the server applies
the `posts` video cap (80 MB). **Stage 2/5 should decide** whether to add the
type or leave highlights riding on `posts`.

**3. The file-count error message changed on POST only.** GET still answers
`"Invalid count (1-10 allowed)"` verbatim. POST cannot: the unit is no longer a
count but a `files` array whose length includes thumbs, and the ceiling is
per-type (1 for a fixed slot, 20 for a post). It answers
`"Too many files for this upload type (max N)"`. Every other guard message —
`"Invalid upload type"`, `"You are not a member of this organization"`,
`"Organization not found"`, `"Switch to your personal account for this upload"`,
`"Switch to your organization account for this upload"` — is byte-identical and
now shared by both verbs.

**4. Per-type file ceilings.** Doc §7 gives caps per *file*, not per *request*:

| Type | max files | max primaries (image/video) |
|---|---|---|
| `profile`, `cover`, `organization_logo`, `organization_cover` | 1 | 1 |
| `achievements`, `matches` | 10 | 10 |
| `posts`, `recruitments` | 20 | 10 |
| `chat` | 2 | 1 |

Fixed-slot types are capped at **1**, tightening the old `count ≤ 10`: those keys
are deterministic, so a second entry would sign the identical key twice. The
pair-capable types allow twice their primary count because every image/video may
bring one thumb (doc §4 G2/G3). `achievements`/`matches` keep 10 — random keys,
so the old ceiling was harmless and narrowing it would break nothing but could
surprise.

**5. Thumb-pairing rules, spelled out.** The doc states the video↔thumb rule;
these follow from it plus §6.3:
- a request must contain at least one primary — a thumbs-only request is refused
  (`"A thumbnail must accompany the image or video it belongs to"`);
- `thumbs ≤ primaries`, so a thumb always has an owner;
- if any `video` is present, the request must be **exactly** one video + one
  thumb and nothing else — that covers "1 video/post", rejects a video with no
  poster, and rejects mixing a video with images
  (`"A video upload must be requested on its own, with exactly one thumbnail"`).

**6. `GET` now returns 400 when the provider is `r2`.** Not in the prompt, but
required: `R2Service.get_upload_config` takes `files`, so the old `count=` call
would have raised `TypeError` and surfaced as a 500. It answers
`"upload config v1 requires the cloudinary provider"`, the mirror of the POST
side's `"upload config v2 requires the r2 provider"`.

**7. `public_url` carries no `?v=` cache-buster.** G6 belongs to the *replace*
paths (profile/cover/logo), which Stage 2 owns per doc §5.3.
`extract_key_from_media_url` already strips a `?v=` suffix, so Stage 2 only has
to append it.

**8. `boto3` is unpinned in the doc, pinned here** to `1.43.79` (current at time
of writing) along with `botocore` and the three transitive deps, matching the
fully-pinned style of the rest of `requirements.txt`.

**9. `services/storage/__init__.py` was added.** The directory was an implicit
namespace package; `unittest` discovery does not descend into those, so its
`tests/` would never have run. Purely additive — every existing import was
already resolving.

---

## Definition-of-done checks

```bash
# boto3 lives in exactly one module
grep -rn "boto3" --include=*.py . | grep -v __pycache__
```

`import boto3` / `boto3.client(` appear **only** in `services/storage/r2.py`.
The grep also prints the two test modules, where the string occurs solely as the
`patch("services.storage.r2.boto3.client")` target and in docstrings — no test
imports or calls the library itself.

Still outstanding for the human (see `verification-checklist.md` → Stage 1): the
live round-trip against a real bucket. It needs the infra checklist in doc §9
done and the R2 keys in `.env` — neither is in the repo yet.
