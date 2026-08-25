# Stage 2 — every backend call site off Cloudinary-specific behaviour

Media metadata and poster frames used to be produced by Cloudinary. On R2 the
stored object is the exact bytes the browser uploaded, so both now arrive from
the client and the server's job changes from *asking the provider* to *checking
what it was handed*.

Cloudinary is untouched and still fully working behind `FILE_STORAGE_PROVIDER`;
the upload-config endpoint was not changed. Everything is uncommitted.

> **Run `python manage.py migrate` before using the dev server.** This stage adds
> one additive nullable column (`post_media.size_bytes`). It is generated but not
> applied — applying it to your database was your call, not mine.

---

## Files

### Created

| File | What |
|---|---|
| `services/storage/metadata.py` | The clamp helpers + every limit as a named constant |
| `posts/migrations/0012_postmedia_size_bytes.py` | Additive, nullable — see ambiguity 2 |
| `STAGE2_NOTES.md` | This file |

### Deleted

| File | Why |
|---|---|
| `posts/management/commands/backfill_media_dimensions.py` | Backfilled from `get_media_metadata`, which no longer exists off-provider |
| `posts/management/commands/backfill_video_derivatives.py` | Backfilled eager transcodes; nothing transcodes any more |

No references to either remained anywhere.

### Modified

| File | What |
|---|---|
| `services/storage/validators.py` | **Added** the provider-aware layer + `validate_thumbnail`, `same_storage_folder`, `with_cache_buster`. `validate_media` now routes through it. |
| `posts/views/posts_views.py` | Metadata from the client + clamps; thumbnail required for video; provider-aware extensions |
| `posts/models.py` | `PostMedia.size_bytes` added; width/height comment now describes the real trust model |
| `highlights/services/highlight_services.py` | Derivative scheduling gone; direct uploads validated + thumbnail required; duration clamped |
| `messaging/services/message_service.py` | Provider-aware source/key checks; client thumbnails; derivative scheduling gone |
| `messaging/serializers/media_serializers.py` | `thumbnail_url` — optional on image, required on video |
| `messaging/views/media_views.py` | Threads `thumbnail_url` through; docstring example updated |
| `recruitments/services/recruitment_service.py` | Provider-aware extensions + full thumbnail validation |
| `matches/services/match_services.py` | Provider-aware extensions (not in the brief — see ambiguity 1) |
| `accounts/views/user_views.py` | `?v=` cache-buster on profile/cover replace |
| `organization/views/organization_views.py` | `?v=` cache-buster on logo/cover replace |

---

## The shared layer (`services/storage/validators.py`)

`validate_media` is the one function every attach path already called, so the
provider switch went **inside** it rather than into six call sites:

```
is_valid_media_source(url)     r2 → is_valid_media_url      | cloudinary → is_valid_cloudinary_url
extract_storage_key(url)       r2 → extract_key_from_url    | cloudinary → extract_public_id_from_url
allowed_image_extensions()     r2 → {webp,jpg,jpeg,png}     | cloudinary → DEFAULT_IMAGE_EXTENSIONS
allowed_video_extensions()     r2 → {mp4,webm}              | cloudinary → {mp4,mov,webm}
```

Every Cloudinary branch is marked `TODO(cleanup-stage)`. Flipping the env var
back still gives the old behaviour end to end — verified, including that `.mov`
and `.heic` become acceptable again on the Cloudinary path.

**`validate_thumbnail(user, url, *, parent_key, org=None)`** is the new shared
rule: full URL validation as an image, plus `same_storage_folder(thumb, parent)`.
The folder check is what actually matters — the ownership prefix alone would let
a user pair a video with a poster frame lifted from any other post of their own,
and the upload-config endpoint hands out one presigned batch per folder, so
"same folder" is the evidence the two files came from the same upload.

---

## New attach-payload fields

### `POST /posts/` — per media item

| Field | Before | Now |
|---|---|---|
| `thumbnail_url` | ignored (server derived it) | **required when `media_type=="video"`**, optional for images; full URL validation + same folder as `public_id` |
| `width`, `height` | ignored (read from Cloudinary) | client-supplied, clamped 1–8192, else NULL |
| `duration` | **required** for video, 400 over 300 s | optional, clamped 1–300 s, else NULL; forced NULL on images |
| `size_bytes` | — | new, clamped ≤ 80 MB video / 5 MB image, else NULL |

### `POST /highlights/` — direct upload

`thumbnail_url` is now **required** ("The video thumbnail (thumbnail_url) is
required."), and `file_url`/`thumbnail_url` get full URL validation under the
player's own prefix. `duration` is clamped to 1–90 s instead of 400-ing. Promote
mode is unchanged.

### `POST /conversations/<id>/messages/media`

`thumbnail_url` is **required for `media_type=="video"`** and optional for
images (absent → the column stays blank, as before). It is validated exactly
like the media itself — our storage, image extension, the **sender's own** chat
prefix, URL↔key match — plus the same-folder rule.

Everything a video message needed before is unchanged, including both existing
gates (100 MB, 90 s).

---

## Cache-buster

`with_cache_buster(url)` appends `?v=<unix ts>` on every replace of the four
fixed-key slots: user profile/cover, org logo/cover. Those live at ONE key per
actor and are overwritten in place, so without it the CDN and every browser that
already fetched the URL keep serving the previous image.

The paired `*_public_id` column stores the **bare key**, and
`extract_key_from_url` strips `?v=` — verified both ways, so delete-after-replace
still targets the right object, and `get_file_extension` still reads `webp`
through the suffix. Re-stamping replaces the existing `?v=`, never stacks it.

---

## Sweep gate

```
grep -rn "get_media_metadata\|ensure_video_derivatives\|build_video_thumbnail_url" --include=*.py .
```

Matches only `services/storage/` (`base.py`, `cloudinary.py`, `r2.py`,
`validators.py`, plus one prose mention in `metadata.py`) and test files, which
this stage ignores. **Clean.**

---

## Verification

- `python manage.py check` → no issues; `runserver` boots and answers `401`.
- Every touched module imports cleanly.
- Ad-hoc (not committed, per the no-tests rule):
  - **Replay protection unchanged** — a sender presenting another actor's chat
    key is still rejected `Invalid media path`; URL↔key mismatch, the 100 MB cap
    and the 90 s cap all still fire.
  - Thumbnails: same-folder accepted, different-folder rejected, a video posing
    as a thumbnail rejected, another actor's thumbnail rejected, org-actor path
    accepted.
  - `.mov` rejected on the R2 path, accepted again after flipping to cloudinary.
  - Clamps: `301→None`, `300→300`, `91→None` (highlights), `99999→None`,
    `"800"→None`, `True→None`, `81MB→None`.
  - Cache-buster round-trip: key recovered, extension readable, no stacking.

---

## Ambiguities resolved

**1. `matches` was not in the change list but shares the same validator.**
`matches/services/match_services.py` calls `validate_media`, so it would have
broken the moment that function became provider-aware and left the match-diary
photo path validating against Cloudinary. It is updated to
`allowed_image_extensions()` — a no-op set-wise (both providers allow the same
four image formats), so the only real change is that it now follows the flag.
Same reasoning made the switch belong *inside* `validate_media` rather than at
each call site.

**2. `size_bytes` for posts had nowhere to go.**
The brief says to accept and clamp it, but `PostMedia` had no such column, and
accepting a field only to discard it would make the clamp meaningless. Added it
as `PositiveBigIntegerField(null=True, blank=True)` — additive, nullable, and an
exact mirror of the `Message.media_size_bytes` that already exists. Migration
`posts/migrations/0012_postmedia_size_bytes.py` is generated but **not applied**.

**3. Highlights: "clamp duration ≤ 90 s" vs "keep every business rule identical".**
The 90 s cap was a `ValidationError`. Clamping it away on the **promote** path
would silently break a real product rule — a 4-minute post video would become a
highlight with `duration=NULL` and no complaint. So the two paths were split:
- **direct** (client-supplied number, cosmetic) → clamped to NULL, per the
  METADATA CLAMPS rule that a bad value is never a 4xx;
- **promote** (duration we stored ourselves on `PostMedia`) → the existing
  `ValidationError` is kept verbatim.

**4. Chat duration/size were listed in METADATA CLAMPS but not in change 3.**
The clamp block says chat video 1–300 s and ≤ 80 MB; messaging already **rejects**
at 90 s and 100 MB with `InvalidMediaError`. Those are gates, not cosmetics, and
applying the clamps would have *loosened* an abuse bound (and produced the
incoherent state where a 90 MB video is accepted but its `size_bytes` is nulled).
Change 3 does not ask for them, so **both existing gates are untouched** and only
`width`/`height` are clamped there. Flagging it: if 300 s / 80 MB is the intended
chat policy, that is a deliberate product change and belongs in its own edit.

**5. `.heic` and `.mov` in chat.**
Cloudinary transcoded on delivery, so both rendered. R2 serves bytes verbatim, so
a `.heic` bubble would paint nothing. The R2 allowlists drop them
(`{jpg,jpeg,png,webp}` / `{mp4,webm}`); the Cloudinary sets are kept intact under
`CLOUDINARY_CHAT_*` names so the rollback is unaffected.

**6. Image thumbnails on posts.**
The brief makes `thumbnail_url` required for video and says to validate *every*
`file_url` and `thumbnail_url`. Images keep it optional (nothing previously
required one) but a supplied image thumbnail now gets the identical treatment,
same-folder rule included — a validated-or-absent field is easier to reason about
than one validated only in some branches.

**7. Post `duration` stopped being required.**
It was mandatory for video with a 400 over 300 s. Under METADATA CLAMPS it is
optional and out-of-range becomes NULL. It is also now forced to NULL on images,
which the old code never did — a duration badge on a photo is a rendering bug.

---

## Known gaps (pre-existing, deliberately not touched)

- **Org create sets a logo with no validation and no `public_id`**
  (`organization/services/organization_service.py:97`), so that first image can
  never be cleaned up. It is a create rather than a replace, so the cache-buster
  rule does not apply; the gap predates this stage.
- **`accounts/admin.py` uploads to Cloudinary directly**, bypassing the storage
  service entirely. Admin-only, and out of scope for a call-site sweep.
- **`PostMedia.file_url` is `URLField()` (200 chars).** A post-video key on the
  dev `r2.dev` base lands around 177 — inside the limit, but not by much. If the
  public base URL ever gets longer, widen it the way `Highlight.file_url` already
  was.
