# Verification checklist — run the relevant block after each stage

## After Stage 1 (backend)
- [ ] `python manage.py test` green
- [ ] Live round-trip with real keys: call `POST /upload-config` (type=profile, one image/webp file entry) via curl/HTTPie with a valid JWT + `X-Actor-Type: user`; take `upload_url` from the response and:
  `curl -X PUT -H "Content-Type: image/webp" --data-binary @test.webp "<upload_url>"`
- [ ] Object visible in the R2 dashboard under `users/<id>/profile.webp`; loads in the browser at `MEDIA_PUBLIC_BASE_URL/<key>`
- [ ] Same call with a disallowed type (`video/quicktime`) → 400; oversize `size_bytes` → 400; org type without membership → 400
- [ ] `FILE_STORAGE_PROVIDER=cloudinary` still serves the old GET config (rollback path alive)

## After Stage 2 (backend)
- [ ] Suite green; grep sweep clean (`get_media_metadata|ensure_video_derivatives|build_video_thumbnail_url` only inside services/storage/)
- [ ] Attach a post with a wrong-domain file_url via curl → 400 · video attach without thumbnail_url → 400
- [ ] Profile replace twice via API → stored URL carries fresh `?v=`; old object deletable

## After Stage 3 (frontend, dev servers + real dev bucket)
- [ ] Profile photo, cover, org logo (as org actor), achievement image, match photo, chat image, multi-image post — all upload, render, and persist after refresh
- [ ] DevTools Network: uploads are raw PUTs to r2.cloudflarestorage.com with progress; feed/chat lists request the 640px thumbs, not full images
- [ ] Video buttons show the "being upgraded" toast, nothing uploads
- [ ] R2 dashboard: keys land in the correct folders; post images sit beside their thumbs in one temp-post folder

## After Stage 4 (frontend)
- [ ] Visual pass: feed carousel, image lightbox, chat image/video bubbles (old video rows render poster-less gracefully), highlight viewer, recruitment detail, profile OG share card
- [ ] Greps: `cloudinaryDelivery` = 0 hits; `res.cloudinary.com` only in next.config TODO
- [ ] Logo renders from /public; `next build` clean

## After Stage 5 (frontend) — the real-device gate, do NOT merge without it
Desktop first:
- [ ] Chrome: 1080p mp4 post → encodes with visible "Optimizing…" progress → plays with seek; already-compliant 720p mp4 takes the fast path (near-instant, `wasReencoded=false` in logs)
- [ ] Firefox: same two cases (or graceful mp4 passthrough if encoder unsupported)
Phones (use a real iPhone-recorded HEVC `.mov` as the source, portrait AND landscape):
- [ ] Android Chrome (mid-range phone): .mov → encoded mp4 uploads and plays; a ~60 s clip encodes in acceptable time; progress bar moves
- [ ] iPhone Safari: same; resulting post plays on BOTH the iPhone and an Android/desktop viewer (this is the whole point — no HEVC leaked)
- [ ] Rotation correct (portrait not sideways) · [ ] Highlight poster is proper 9:16 · [ ] Chat video sends with poster
- [ ] Encoder-failure path: simulate (devtools override / unsupported browser) → mp4 source passes through, .mov source gets the friendly error, nothing raw uploads
- [ ] Abort mid-encode and mid-upload: no crash, no orphan UI state

## Final E2E matrix (after Stage 6, before first deploy)
Every row: upload → renders in list + detail → correct thumb/poster → survives refresh → delete removes object(s) from R2 (check bucket prefix empty):
- [ ] Profile + cover (×2 replaces → instant new image = cache-buster) · [ ] Org logo + cover
- [ ] Post: 10 images · [ ] Post: portrait video · [ ] Post: landscape video · [ ] Post delete wipes its whole R2 folder
- [ ] Highlight ≤90 s · [ ] Chat image + chat video, both actor directions; replaying another user's media URL rejected
- [ ] Achievement, match photo, recruitment image + video
- [ ] Oversize file and bad content_type rejected at config step with clean UI errors
- [ ] Repo greps: "cloudinary" only in changelog/spec docs, both repos
- [ ] README "Before your FIRST production deploy" list completed (custom domain, env swap, CORS, Render/Vercel vars)
