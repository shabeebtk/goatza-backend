# Goatza — Player Highlights: Implementation Spec

Feature: a curated collection of short videos on a player's profile, viewable by
recruiters and others depending on per-clip visibility. Built to make recruiter
review of many players fast.

This spec follows existing Goatza conventions: dual-actor identity
(`X-Actor-Type` / `X-Actor-Id`), service/selector layering, soft deletes,
UUIDv7 base model, denormalized counters, Cloudinary direct upload.

---

## 1. Product rules

- Owner: **players only**. Write access requires a user actor with
  `role == PLAYER`. Scouts, coaches, and organizations cannot create, edit,
  reorder, or delete highlights (they can view, per visibility rules).
  Organizations do not have highlights in v1.
- Max **10 highlights** per user (enforce in service, return clear error).
- Max clip duration **90 seconds** (validate on create; client validates before upload).
- Two ways to add:
  1. **Direct upload** — video goes straight to Highlights, never appears as a post.
  2. **Promote from post** — from any of the player's own video posts,
     "Add to Highlights" copies the media reference.
- Promotion **copies** `file_url`, `public_id`, `thumbnail_url`, `duration`,
  `width`, `height` from `PostMedia` into the Highlight row. Keep
  `source_post` FK (nullable, `on_delete=SET_NULL`) for attribution only.
  Deleting/soft-deleting the post must NOT affect the highlight.
- Per-clip visibility (default `followers_and_recruiters`):
  - `everyone`
  - `followers_and_recruiters`
  - `recruiters_only`
- Soft delete (`is_deleted`), consistent with posts/comments.
- Manual ordering (drag to reorder), `order` integer field.

### Who counts as a "recruiter" (viewer-side check)
A viewer is a recruiter if ANY of:
- acting actor is an **organization** (membership already verified by `core/actor.py`), or
- acting actor is a **user** with role `SCOUT` or `COACH`.

A viewer is a "follower" if their acting actor follows the highlight owner
(use existing `connections.Follow` — supports user→user and org→user).

Owner always sees all of their own highlights regardless of visibility.

---

## 2. Backend — new Django app `highlights`

Follow the structure of `posts`/`connections`: `models.py`, `services/`,
`selectors/`, `views/`, `urls.py`, `tests.py`, admin registration.

### Model

```python
class Highlight(BaseUUIDModel):
    class Visibility(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        FOLLOWERS_AND_RECRUITERS = "followers_and_recruiters", "Followers and recruiters"
        RECRUITERS_ONLY = "recruiters_only", "Recruiters only"

    user = FK(User, related_name="highlights", on_delete=CASCADE)
    title = CharField(max_length=80, blank=True)          # e.g. "Free kick vs St. Mary's"
    file_url = URLField()
    public_id = CharField(max_length=255)
    thumbnail_url = URLField(blank=True)
    duration = PositiveIntegerField(null=True, blank=True)  # seconds
    width = PositiveIntegerField(null=True, blank=True)
    height = PositiveIntegerField(null=True, blank=True)
    visibility = CharField(choices=Visibility, default=FOLLOWERS_AND_RECRUITERS)
    order = PositiveIntegerField(default=0)
    source_post = FK("posts.Post", null=True, blank=True, on_delete=SET_NULL,
                     related_name="promoted_highlights")
    views_count = PositiveIntegerField(default=0)         # denormalized
    is_deleted = BooleanField(default=False)
    created_at / updated_at

    Meta:
        db_table = "highlights"
        indexes: [user, is_deleted], [user, order]
```

Optional (phase 4): `HighlightView` model — `highlight` FK, viewer_user /
viewer_org (dual-actor, exactly-one constraint like Follow), `is_recruiter`
bool, unique per (highlight, viewer) per day. Powers "Seen by N recruiters".

### Service layer (`services/highlight_services.py`)
- `create_highlight(user, *, payload)` — validates count cap (10), duration cap
  (90s). Two modes:
  - direct: payload has `file_url`, `public_id`, etc. (from Cloudinary direct upload)
  - promote: payload has `source_media_id` → load the player's own `PostMedia`
    (must be `media_type=video`, post authored by the same user, not deleted),
    copy fields, set `source_post`.
  - New highlight gets `order = max(order) + 1`.
- `update_highlight(user, highlight_id, *, title?, visibility?)`
- `reorder_highlights(user, ordered_ids: list)` — bulk update in one transaction.
- `delete_highlight(user, highlight_id)` — soft delete, close the `order` gap lazily
  (not required to renumber).
- `record_view(actor, highlight)` — increment `views_count` (F expression);
  phase 4 writes `HighlightView` row. Never count the owner's own views.

### Selector layer (`selectors/highlight_selectors.py`)
- `is_recruiter(actor) -> bool` per §1 rules.
- `visible_highlights_for(owner_user, viewer_actor)`:
  - owner → all (not deleted), ordered by `order`
  - build allowed set: always `everyone`; add `followers_and_recruiters`
    if viewer follows owner OR is_recruiter; add `recruiters_only` if is_recruiter.
- `highlight_counts(owner)` for profile serializers (visible count for viewer).

### API (mount at `/api/highlights/`)
- `POST   /api/highlights/` — create (direct or promote mode)
- `GET    /api/highlights/user/<username>/` — list visible to current actor
- `PATCH  /api/highlights/<id>/` — title / visibility
- `PUT    /api/highlights/reorder/` — `{ordered_ids: [...]}`
- `DELETE /api/highlights/<id>/` — soft delete
- `POST   /api/highlights/<id>/view/` — record a view (fire-and-forget from client)

All endpoints use existing JWT auth + actor resolution. Write endpoints must
reject `X-Actor-Type: organization` AND any user whose role is not `PLAYER`
(403 with a clear message). Read endpoints stay open to all authenticated
actors, filtered by the visibility rules.

Reuse the **existing Cloudinary signature endpoint** for direct uploads —
no new media plumbing. Thumbnail: Cloudinary transform of the video public_id
(`so_0`, `f_jpg`, `q_auto`, sized ~360x640) generated client-side or stored at create.

Also: add `highlights_count` (visible-to-viewer) to the public profile
serializer so profile and player cards can show the "▶ Highlights (n)" chip
without an extra request.

### Tests
Follow `connections/tests.py` style. Cover: visibility matrix
(anon-ish viewer / non-follower player / follower / scout / coach /
acting-as-org / owner), cap of 10, 90s cap, promote copies fields and survives
post soft-delete, reorder, and writes rejected for org actors AND for
scout/coach role users (player-only writes).

---

## 3. Frontend — `src/features/highlights/`

Follow existing feature-folder conventions (api client w/ axios instance,
React Query hooks, Zod schemas, components).

### Components

**HighlightsRail** (profile page, above the posts tab)
- Horizontal scroll of 9:16 thumbnail cards (~96×160px), duration badge
  bottom-right, optional title 1-line ellipsis.
- Owner sees a leading "+" tile → opens upload flow. Only when the logged-in
  user is the profile owner AND `role === "player"` and acting as themselves
  (not as an organization).
- Hidden entirely when viewer sees 0 highlights (non-owner).
- Thumbnails are plain `<img>` (Cloudinary jpg) — the rail must load like images,
  not videos. Lazy-load offscreen ones.

**HighlightViewer** (full-screen modal / route overlay)
- Story-style segmented progress bar top (one segment per clip).
- Vertical 9:16 video, `object-fit: cover` letterboxed on desktop.
- Muted autoplay + tap-to-unmute; tap/hold to pause; swipe or arrow keys to
  move between clips; swipe down / Esc to close.
- `preload="metadata"` on the NEXT clip while current plays.
- Slim header: avatar, name, primary position, sport. Owner sees edit/delete
  menu per clip; visibility badge shown to owner only.
- Fire `POST /view/` once per clip per session (debounced).

**Upload flow**
1. Pick file → client validation (video mime, ≤90s via metadata, size cap).
2. Get signature (existing endpoint) → direct upload to Cloudinary with progress.
3. `POST /api/highlights/` with returned url/public_id/duration/dimensions.
4. Optimistically append to rail; toast via sonner.

**Promote from post**
- On the author's own video posts, post menu gets "Add to Highlights" —
  shown only when the viewer is the post author, is a player, and is acting
  as themselves.
- Calls create in promote mode with the `PostMedia` id. Toast: "Added to your
  highlights" with an inline link to manage.

**Manage screen** (owner, from rail overflow or profile edit)
- Drag-to-reorder grid → `PUT /reorder/` on drop (optimistic).
- Per-clip: edit title, change visibility (3-option segmented control with a
  one-line explanation of each level), delete with confirm.

### Recruiter surfaces (the priority UX)
- **Recruitment applicant list**: each applicant card shows a
  "▶ Highlights (n)" chip when n > 0. Opens `HighlightViewer` as a modal —
  recruiter never leaves the pipeline page.
- **Pipeline mode**: viewer receives the ordered applicant list; swiping past
  the last clip of a player advances to the next applicant's first clip.
  Header gains quick actions: View profile · Message · Shortlist/advance stage.
  Prefetch the next applicant's highlight list + first thumbnail in background.
- **Explore → Players**: same chip on player cards.

### Performance requirements
- Rail: images only. Viewer: one `<video>` element playing at a time.
- Cloudinary delivery params on video URLs: `q_auto`, `f_auto`.
- React Query: cache highlight lists per username; prefetch on chip
  hover (desktop) / press-start (mobile).
- Never block the applicant list render on highlight data — chip count comes
  from the applicant serializer, list fetches on open.

---

## 4. Build order (one Claude Code session each)

1. **Backend core** — app, model, migration, services, selectors, views, urls,
   admin, tests. Definition of done: visibility matrix tests pass.
2. **Player frontend** — feature folder, upload flow, HighlightsRail on
   profile, HighlightViewer, manage screen, promote-from-post menu item.
3. **Recruiter integration** — chip + modal viewer in applicant pipeline and
   Explore → Players, pipeline mode (cross-applicant swipe), prefetching,
   `highlights_count` in relevant serializers.
4. **Analytics polish** — HighlightView model, "Seen by N recruiters" on the
   owner's manage screen, per-clip view counts.

## 5. Explicit non-goals (v1)
- No org-owned highlights, no comments/likes on highlights, no in-app video
  trimming/editing, no highlights in the home feed, no download/share-out.
