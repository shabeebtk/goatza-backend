# Goatza — Google Places Migration · Build Plan v2

Supersedes `Goatza_Google_Places_FINAL_Low_Cost_Implementation_Spec.docx`.
Corrected against the real `goatza-backend` / `goatza-frontend` code (Aug 2026).

Put a copy of this file in **both** repos at `docs/PLACES_MIGRATION.md`.
Every Claude Code prompt below starts with "read docs/PLACES_MIGRATION.md".

---

## 0. Decisions (locked)

| # | Decision |
|---|----------|
| 1 | Google **Places API (New)** for both city search and venue/POI search. Mapbox removed. |
| 2 | Google API key lives **only on the Django backend**. Frontend calls Goatza endpoints under `/places/`. No key in the browser. |
| 3 | Stored permanently: `place_id` + Goatza-owned data. Coordinates carry `coords_fetched_at`, are refreshed via `place_id` within 30 days, and nulled when stale ("B-lite"). |
| 4 | Nearby / distance ranking stays on the existing `services/geo/haversine.py`. **No PostGIS.** |
| 5 | Coordinate refresh = Django management command, run manually for now, Render cron later. No Celery. |
| 6 | No new tests during the build. Existing suites must stay green. Tests written at the end. |
| 7 | Old localhost data is disposable. Migrations don't need to preserve Mapbox rows. |

---

## 1. Google Cloud setup + keys (you do this before Stage B1)

### 1.1 Console steps

1. **Project** — `console.cloud.google.com`. Reuse the project you already use for Google OAuth / Firebase, or create `goatza-places`.
2. **Billing** — link a billing account. Required even for the free tier. Use an **India billing address, INR** — that is what gives you 70k free events per Essentials SKU instead of 10k.
3. **Enable API** — APIs & Services → Library → search **"Places API (New)"** → Enable.
   Do **not** enable the legacy "Places API" (no "(New)"). Different product, different pricing.
4. **Create key** — APIs & Services → Credentials → Create credentials → API key. Rename it `goatza-backend-places`.
5. **Restrict key** — edit the key:
   - API restrictions → *Restrict key* → tick only **Places API (New)**.
   - Application restrictions → *None* for now (server-side use). Later, if your Render service has static outbound IPs, switch to *IP addresses* and list them.
6. **Quota caps (hard billing backstop)** — Google Maps Platform → Quotas → Places API (New):
   - Set the per-day quota for **Autocomplete** to ~2,000/day.
   - Set the per-day quota for **Place Details** to ~1,000/day.
   - (Labels in the console may differ slightly; cap both methods.)
7. **Budget alert** — Billing → Budgets & alerts → new budget, e.g. ₹1,000/month, email at 50 / 90 / 100 %.

### 1.2 Environment variables

**Backend** (`.env` locally + Render environment):

```env
GOOGLE_PLACES_API_KEY=AIza...           # the key from step 4
PLACES_DAILY_CAP_AUTOCOMPLETE=2000      # Goatza-side circuit breaker (per UTC day)
PLACES_DAILY_CAP_DETAILS=1000
PLACES_COORDS_REFRESH_AFTER_DAYS=25     # refresh coords older than this (active rows)
PLACES_COORDS_EXPIRE_AFTER_DAYS=30      # null coords older than this (inactive rows)
PLACES_ACTIVE_USER_DAYS=30              # "active user" = logged in within this window
PLACES_CITY_PRIMARY_TYPES=(cities)      # city-picker type filter; widen later if villages are missing (see 4.1)
```

**Frontend** — **nothing new**. The frontend only talks to your backend.
Remove `NEXT_PUBLIC_MAPBOX_TOKEN` from `.env.local` and from Vercel after Stage F1.

### 1.3 Assets you download (before Stage F1)

- Official **"Powered by Google"** logo (light + dark variants) from Google's Places API policies page →
  save as `public/google/powered_by_google_on_white.png` and `public/google/powered_by_google_on_non_white.png` in the frontend.
  Required because Goatza shows Places results without a Google map.

---

## 2. What changes — overview

### Backend (`goatza-backend`)
- **New app `places/`** — Google proxy: `autocomplete` + `details` endpoints, throttles, daily circuit breaker, usage counters.
- **`shared.Location`** — gains `provider`, `coords_fetched_at`; coordinates become nullable; unique on `(provider, external_id)`.
- **`OrganizationLocation`** — gains a `location` FK to `shared.Location` (needed so org coords can be refreshed by `place_id`).
- **`LocationService`** — understands `provider`, stamps `coords_fetched_at`, refreshes a shared row's coords on every fresh selection, propagates coords to denormalized columns.
- **`refresh_place_coords`** management command + `ensure_fresh_for_user()` login hook.
- **Untouched**: haversine, explore, recruitment discovery, all denormalized `latitude/longitude` columns.

### Frontend (`goatza-frontend`)
- **`shared/services/mapbox.service.ts` → `shared/services/places.service.ts`** — provider interface, calls `/places/*`.
- **Session tokens** — UUID v4 per search session (`crypto.randomUUID()`).
- **`LocationPicker`** (city) and **`PostLocationPicker`** (venue) — same UI, new provider, 3-char minimum, 400 ms debounce, stale-response guard, Details-on-select, "Powered by Google" attribution.
- **Types** — `MapboxCity` / `MapboxPlace` → one `PlaceResult` type (≈10 importing files).
- **Payloads** — every location payload sent to the backend now includes `external_id` (= `place_id`) and `provider: "google"`. Recruitment and org payloads currently omit `external_id`; that changes.
- **Removed** — Mapbox service, Mapbox token, Mapbox mentions.

### Removed / not built
- PostGIS, Text Search, Nearby Search, Geocoding, Maps SDK, any Pro/Enterprise field.

---

## 3. Cost + legal rules the code must respect (non-negotiable)

1. **Place Details field mask is exactly** `location,addressComponents,types`. Never `displayName` (Pro), never photos/rating/hours/phone/website (Enterprise). The place **name comes from the autocomplete prediction**, not from Details.
2. **One session token per search session.** Same UUID v4 on every autocomplete request and on the single Details request. New token after a selection, a clear, or closing the picker. Never reuse across sessions.
3. **Minimum 3 characters, 400 ms debounce, abort/ignore stale responses.**
4. **Details is called only for the prediction the user selected.** Never for the list. Never Text Search / Nearby Search.
5. **Stored permanently from Google: `place_id` only.** Coordinates always carry `coords_fetched_at`. Nothing else from Google is persisted beyond the user-facing label the user picked.
6. **Attribution**: "Powered by Google" logo visible whenever predictions are shown.
7. **Server never logs or stores raw query text.** Counts only.
8. **No server-side caching of Google responses.** Every call goes to Google.
9. **Two backstops on spend**: Goatza daily cap (returns 503 when hit) + Google console quotas.

Cost reality check (India pricing): one completed search ≈ 3–5 Autocomplete events + 1 Details event. Each SKU has its own 70k free/month → roughly 15–20k completed searches/month at ₹0. Refresh cost is one Details call per **distinct** active place per ~25 days (one "Kannur" row serves every user in Kannur), so it stays tiny.

---

## 4. API contract (backend ↔ frontend)

Mounted at `places/` in `core/urls.py`. Both endpoints allow anonymous callers (the public `/join` form uses the city picker) with stricter IP throttling for anonymous.

### 4.1 `GET /places/autocomplete/`

Query params:

| Param | Required | Notes |
|-------|----------|-------|
| `q` | yes | 3–100 chars after trim |
| `session` | yes | UUID v4 |
| `mode` | yes | `city` or `place` |
| `lat`, `lng` | no | bias centre (actor's profile coords). Used as a 50 km `locationBias` circle. |

Response `200`:

```json
{
  "results": [
    {
      "place_id": "ChIJ...",
      "name": "Kannur",
      "label": "Kannur, Kerala, India",
      "secondary": "Kerala, India",
      "types": ["locality", "political"]
    }
  ]
}
```

Errors: `400` bad params · `429` throttled · `503 {"error":"search_unavailable"}` daily cap hit or Google 429 · `502` Google failure.

Google call behind it:

```
POST https://places.googleapis.com/v1/places:autocomplete
Headers: Content-Type: application/json
         X-Goog-Api-Key: <GOOGLE_PLACES_API_KEY>
         X-Goog-FieldMask: suggestions.placePrediction.placeId,suggestions.placePrediction.text,suggestions.placePrediction.structuredFormat,suggestions.placePrediction.types
Body: {
  "input": "<q>",
  "sessionToken": "<session>",
  "languageCode": "en",
  "includedPrimaryTypes": ["(cities)"],          // mode=city only; omit for mode=place
  "locationBias": { "circle": { "center": { "latitude": <lat>, "longitude": <lng> }, "radius": 50000.0 } }  // only if lat/lng given
}
```

Mapping: `name = structuredFormat.mainText.text`, `label = text.text`, `secondary = structuredFormat.secondaryText.text`, `types = types`. Ignore `queryPrediction` entries.

City-mode type list comes from a setting, not code: `PLACES_CITY_PRIMARY_TYPES` (comma-separated, default `(cities)`). `(cities)` = `locality` + `administrative_area_level_3`, which covers towns like Thalassery, Panoor, Kuthuparamba. If village-level places (e.g. Kadavathoor) are missing, set it to e.g. `locality,sublocality,neighborhood,administrative_area_level_3,administrative_area_level_4` (max 5 values; a collection like `(cities)` cannot be mixed with individual types). Test with the raw Google call, no type filter, and read the `types` Google returns for the missing place.

### 4.2 `GET /places/details/<place_id>/`

Query params: `session` (required, the same UUID v4 used for the autocompletes).

Response `200`:

```json
{
  "place_id": "ChIJ...",
  "latitude": 11.8745,
  "longitude": 75.3704,
  "city": "Kannur",
  "state": "Kerala",
  "country": "India",
  "country_code": "IN",
  "types": ["locality", "political"]
}
```

Google call behind it:

```
GET https://places.googleapis.com/v1/places/<place_id>?sessionToken=<session>&languageCode=en
Headers: X-Goog-Api-Key: <key>
         X-Goog-FieldMask: location,addressComponents,types
```

Address-component mapping (first match wins):
- `city` ← `locality` → `administrative_area_level_3` → `administrative_area_level_2` → `sublocality_level_1`
- `state` ← `administrative_area_level_1` (longText)
- `country` ← `country` (longText) · `country_code` ← `country` (shortText, upper-cased)

The refresh job calls the same Google endpoint **without** a session token and with field mask `location` only.

### 4.3 Throttle scopes

| Scope | Authenticated | Anonymous (by IP) |
|-------|---------------|-------------------|
| `places_autocomplete` | 60/min | 20/min |
| `places_details` | 20/min | 10/min |

### 4.4 Daily circuit breaker + usage counters

Redis cache keys `places:usage:<sku>:<YYYY-MM-DD>` where `sku ∈ {autocomplete, details, refresh}`, incremented per Google call, 48 h TTL. If `autocomplete` count ≥ `PLACES_DAILY_CAP_AUTOCOMPLETE` (or `details` + `refresh` ≥ `PLACES_DAILY_CAP_DETAILS`) → `503 search_unavailable` **before** calling Google. `python manage.py places_usage` prints today's and yesterday's counters.

---

## 5. Data model changes

### 5.1 `shared.Location`

```python
class Provider(models.TextChoices):
    GOOGLE = "google", "Google"
    MANUAL = "manual", "Manual"

provider          = CharField(max_length=20, choices=Provider.choices, default=Provider.GOOGLE)
external_id       = CharField(max_length=255, blank=True)      # Google place_id when provider=google
latitude          = FloatField(null=True, blank=True)          # was required
longitude         = FloatField(null=True, blank=True)          # was required
coords_fetched_at = DateTimeField(null=True, blank=True)       # when lat/lng last came from Google
```

Constraints / indexes:
- `UniqueConstraint(fields=["provider","external_id"], condition=~Q(external_id=""), name="unique_provider_external_location")` (replaces `unique_external_location`).
- Drop `unique_location_combination` (lat/lng/name) — meaningless with nullable coords.
- Add index on `coords_fetched_at`.

`type` stays (`city` / `place`); the frontend sets it from the picker mode.

### 5.2 `organization.OrganizationLocation`

Add `location = ForeignKey(shared.Location, null=True, blank=True, on_delete=SET_NULL, related_name="organization_locations")`.
Keep `city / state / country_code / latitude / longitude` as denormalized copies (explore reads them).

### 5.3 Everything else

`UserProfile`, `Post`, `Recruitment` already have `location` FK + denormalized `latitude/longitude` (nullable). No change.

### 5.4 Location payload accepted by all write paths

```json
{
  "provider": "google",
  "external_id": "ChIJ...",
  "name": "Kannur",
  "type": "city",
  "city": "Kannur",
  "state": "Kerala",
  "country": "India",
  "country_code": "IN",
  "latitude": 11.8745,
  "longitude": 75.3704
}
```

Write paths that must accept it: profile update, onboarding identity step, waitlist signup, post create/edit, recruitment create/edit, organization setup + org location create/edit. `provider` defaults to `google` when omitted.

### 5.5 `LocationService` behaviour

- `get_or_create_location(data)`:
  1. Lookup by `(provider, external_id)`.
  2. If found and payload has coords → **update** `latitude/longitude`, set `coords_fetched_at = now()` (the client just fetched them in-session — free refresh). Return row.
  3. Else create with `coords_fetched_at = now()`.
- `propagate_coords(location)` → bulk-update `latitude/longitude` on every `UserProfile`, `Post`, `Recruitment`, `OrganizationLocation` row whose `location_id` matches. Used by the refresh job and by step 2 above.
- `build_denormalized()` unchanged.

---

## 6. Coordinate lifecycle ("B-lite")

**Active** Location = referenced by at least one of:
- a `UserProfile` whose `user.last_login ≥ now − PLACES_ACTIVE_USER_DAYS` (or `profile.updated_at` within the window, for users who never logged in via a path that sets `last_login`);
- any `OrganizationLocation`;
- a `Recruitment` with `status in (draft, active)` and `is_deleted = False`.

Posts never keep a Location active on their own (they only need the label).

### 6.1 `python manage.py refresh_place_coords [--dry-run] [--limit N] [--sleep-ms 50]`

1. **Refresh** — google Locations that are active AND (`coords_fetched_at` is null OR older than `PLACES_COORDS_REFRESH_AFTER_DAYS`):
   one Details call (`location` only, no session token) → update `latitude/longitude/coords_fetched_at` → `propagate_coords`. Counts against the `details` daily cap under sku `refresh`. On Google `NOT_FOUND` for a `place_id`: leave the row, log it, continue.
2. **Expire** — google Locations that are NOT active AND coords not null AND `coords_fetched_at` older than `PLACES_COORDS_EXPIRE_AFTER_DAYS`:
   set `latitude/longitude = NULL` on the Location and propagate NULLs. No API call.
3. **Summary** — print `refreshed / expired / not_found / errors / google_calls`. `--dry-run` prints what would happen.

`haversine.distance_expr` already treats NULL coords as "unknown distance" and explore's bounding box drops them, so expired rows silently leave nearby results.

### 6.2 Login hook — `ensure_fresh_for_user(user)`

Called (fire-and-forget, wrapped in try/except, never blocks login) on every successful login path: email/phone password, OTP, Google OAuth. If `profile.location` is google and coords are null or older than `PLACES_COORDS_REFRESH_AFTER_DAYS` → one Details call → update + propagate.

Also verify every login path updates `user.last_login` (`SIMPLE_JWT["UPDATE_LAST_LOGIN"]` is already `True`, but custom login views must call `update_last_login` themselves).

### 6.3 Running it (for now)

```bash
python manage.py refresh_place_coords --dry-run
python manage.py refresh_place_coords
python manage.py places_usage
```

Later: Render Cron Job, daily, same command.

---

## 7. Build stages — Claude Code prompts

Order: **B1 → B2 → B3** in `goatza-backend`, then **F1** in `goatza-frontend`, then **E2E**.
Copy each block as-is. The doc must already be at `docs/PLACES_MIGRATION.md` in that repo.

### Stage B1 — Google Places proxy (backend)

```
Read docs/PLACES_MIGRATION.md fully, then read CLAUDE.md. We are replacing Mapbox with Google Places API (New). This stage builds ONLY the proxy layer described in sections 1.2, 3 and 4 of the doc.

Build:
1. A new Django app `places/` following the repo's layered convention:
   - places/services/google_places_client.py — thin HTTP client for Autocomplete (New) and Place Details (New), using `requests` (already in requirements). 4s timeout. Raises typed exceptions for timeout / 429 / other errors. Exactly the endpoints, headers and field masks in section 4. Never include any field outside the doc's field masks.
   - places/services/places_service.py — validation (q length 3–100, session must be UUID v4, mode in {city, place}, optional lat/lng), daily circuit breaker + usage counters per section 4.4 (Redis cache, keys `places:usage:<sku>:<YYYY-MM-DD>`), response normalisation exactly matching the JSON shapes in 4.1 and 4.2 including the address-component mapping.
   - places/views/places_views.py — thin views. AllowAny (public /join form uses the city picker). Use the repo's standard response helper (utils/responses) for the success envelope if other views do.
   - places/throttles.py — scoped throttles per section 4.3 (user + anon variants). Register the rates in REST_FRAMEWORK settings.
   - places/urls.py mounted at `places/` in core/urls.py.
   - A `places_usage` management command that prints today's and yesterday's counters.
2. Settings: read GOOGLE_PLACES_API_KEY, PLACES_DAILY_CAP_AUTOCOMPLETE, PLACES_DAILY_CAP_DETAILS from env the same way other settings are read. If the key is missing, the app must still boot; the endpoints return 503 and log a warning.
3. Logging: log counts and outcomes only. Never log the query text or the session token.
4. Add the env vars to whatever example env file the repo keeps, and add a short "places" entry to the app table in CLAUDE.md.

Rules:
- Do not touch models, LocationService, or any existing app in this stage.
- Do not write new tests. Run the existing test suite before finishing and keep it green.
- When done, give me: files added/changed, the two curl commands I can run locally to try autocomplete and details, and anything you had to assume.
```

### Stage B2 — Location model + services (backend)

```
Read docs/PLACES_MIGRATION.md sections 0, 5 and 6, then CLAUDE.md. Stage B1 (places/ proxy) is already done. This stage changes the data model and the location write paths. Old data is localhost-only and disposable; migrations do not need to preserve Mapbox rows.

Do:
1. shared.Location — exactly as section 5.1: add `provider` (choices google/manual, default google), make latitude/longitude nullable, add `coords_fetched_at`, replace the external_id unique constraint with (provider, external_id), drop `unique_location_combination`, index coords_fetched_at. One migration.
2. organization.OrganizationLocation — add the `location` FK per 5.2. Keep the denormalized columns. Migration.
3. services/location/location_service.py — implement section 5.5: lookup by (provider, external_id); update coords + coords_fetched_at on an existing row when the payload carries coords; `propagate_coords(location)` bulk-updating UserProfile, Post, Recruitment, OrganizationLocation. Keep `build_denormalized` and `validate`.
4. Every write path listed in 5.4 must accept the payload in 5.4 (provider defaults to google, external_id optional but expected): accounts profile update + onboarding, waitlist signup, posts create/edit, recruitments create/edit, organization setup and org-location create/edit. For org locations: call LocationService.get_or_create_location, set the FK, and fill the denormalized columns from build_denormalized. Do not change response shapes the frontend already reads (location_name, city, country_code, latitude, longitude) — only add fields if needed.
5. grep the backend for "mapbox" / "Mapbox" and fix comments/docstrings that describe the old provider.

Rules:
- Do not change haversine.py, explore, recruitment discovery, or any distance logic.
- No new tests. Run the existing suite; fix fixtures/factories that break because of `provider` or nullable coords; keep it green.
- Report files changed, the migration names, and any place where a write path did something unexpected with location data.
```

### Stage B3 — Coordinate refresh command + login hook (backend)

```
Read docs/PLACES_MIGRATION.md section 6 (and 4.2, 4.4 for the Google call and counters). Stages B1 and B2 are done. This stage builds the coordinate lifecycle.

Do:
1. places/services/coords_refresh_service.py:
   - `select_active_location_ids()` per the "Active" definition in section 6 (settings PLACES_ACTIVE_USER_DAYS).
   - `refresh_location(location)` — one Place Details call with field mask `location` only, no session token, via the B1 client; counts under sku `refresh` and respects the details daily cap; updates coords + coords_fetched_at; calls LocationService.propagate_coords. Handles NOT_FOUND / errors per 6.1.
   - `expire_location(location)` — nulls coords on the row and propagates NULLs. No API call.
   - `ensure_fresh_for_user(user)` per 6.2.
2. Management command `refresh_place_coords` with --dry-run, --limit, --sleep-ms, implementing steps 1–3 of 6.1 and printing the summary line.
3. Call `ensure_fresh_for_user` from every successful login path (password, OTP, Google OAuth). Wrap in try/except and log; it must never fail or slow a login by more than one Google call. Also confirm each login path updates user.last_login; add `update_last_login` where missing.
4. Read settings PLACES_COORDS_REFRESH_AFTER_DAYS, PLACES_COORDS_EXPIRE_AFTER_DAYS, PLACES_ACTIVE_USER_DAYS from env with the doc's defaults; add them to the example env file.

Rules:
- No Celery, no scheduler. It is a plain command I run by hand.
- No new tests; existing suite stays green.
- Report: files changed, the exact commands to run, and which login views you touched.
```

### Stage F1 — Frontend provider + pickers

```
Read docs/PLACES_MIGRATION.md sections 0, 1.2, 1.3, 2, 3, 4 and 5.4, then CLAUDE.md. The backend now exposes GET /places/autocomplete/ and GET /places/details/<place_id>/ exactly as section 4. This stage replaces Mapbox in the frontend end to end. The "Powered by Google" images are already in public/google/ (if not, render the images with a plain-text "Powered by Google" fallback and tell me).

Do:
1. Replace src/shared/services/mapbox.service.ts with src/shared/services/places.service.ts:
   - `PlaceSearchProvider` interface: searchCities(q, sessionToken, bias?) / searchPlaces(q, sessionToken, bias?) / getPlaceDetails(placeId, sessionToken). One `GooglePlacesProvider` implementation calling our backend through the existing axios client (JWT/actor headers attached when present; the /join page is anonymous and must still work).
   - Types: `PlaceSuggestion` (what autocomplete returns) and `PlaceResult` replacing BOTH MapboxCity and MapboxPlace. PlaceResult keeps the field names call sites already use (label, name, state, country_code, latitude, longitude, external_id) and adds provider: "google", place_type: "city" | "place", city, country, types. external_id holds the Google place_id.
   - `newSessionToken()` using crypto.randomUUID().
2. src/shared/components/LocationPicker (city) and src/features/posts/components/PostLocationPicker (venue): keep the UI. Change behaviour per section 3: min 3 chars, 400 ms debounce, ignore stale responses (LocationPicker needs the sequence guard PostLocationPicker already has) and abort in-flight requests, one session token per open picker regenerated after select/clear/close, on select call getPlaceDetails with the same token then merge into a PlaceResult and call onChange, show a loading state during details, keep the list open with an error message if details fails. Render the Powered by Google image directly under the results list in both pickers (dark/light variant by theme if the app has one). Update empty-state copy ("No places found") and aria labels that say "city" where it is now generic. Pass the actor's profile coords as bias when available.
3. Rename MapboxCity/MapboxPlace usages to PlaceResult across the codebase (explore filters, onboarding IdentityStep, profile edit, org setup + edit org modal, join form + types, create/edit post, create recruitment). Where components reconstruct a value from stored server data (external_id: "" today), keep that behaviour; provider "google" and place_type from context.
4. Every location payload sent to the backend must match section 5.4: include provider: "google", external_id, state, country, type. The recruitment payload and the organization payload currently omit external_id — add it. Do not otherwise change payload shapes the backend already accepts.
5. Remove NEXT_PUBLIC_MAPBOX_TOKEN from the code and from any example env file; remove every "mapbox"/"Mapbox" mention in comments, docs, README, CLAUDE.md.
6. Run lint + typecheck + the existing vitest suite. JoinPage.test.tsx mocks the Mapbox service — update the mock to the new module so the suite stays green. Do not add new tests.

Report: files changed, anything in the UI you had to decide yourself, and the exact places where a payload shape changed.
```

### Stage E2E — manual check (you, both repos running locally)

Walk section 8 below. Fix anything that fails by sending Claude Code the failing step + what you saw.

---

## 8. Manual verification checklist

Run backend + frontend locally with the real key.

**Search behaviour**
- [ ] Typing 2 chars does nothing; 3rd char triggers one request after ~400 ms.
- [ ] Network tab: same `session` UUID on every autocomplete and on the details call; new UUID after selecting or clearing.
- [ ] Profile city picker returns cities only; post/recruitment picker returns grounds, turfs, academies, stadiums.
- [ ] Selecting shows a brief loading state, then the pill with name + state/country.
- [ ] "Powered by Google" visible under results in both pickers.

**Persistence**
- [ ] Save profile city → `shared_location` row has `provider=google`, `external_id=ChIJ…`, coords, `coords_fetched_at`; profile denormalized coords filled.
- [ ] Two users pick the same city → one Location row, two profiles pointing at it.
- [ ] Create recruitment with a venue → Location row + recruitment coords; explore "nearby" and recruitment discovery still rank by distance.
- [ ] Org setup with a city → `organization_locations.location_id` set, coords filled.
- [ ] Post with a venue → label shows on the post.

**Lifecycle**
- [ ] `manage.py refresh_place_coords --dry-run` lists nothing (everything fresh).
- [ ] Manually set a Location's `coords_fetched_at` to 40 days ago on a user who is active → run command → refreshed, counters +1.
- [ ] Same on a Location only used by an old post / inactive user → expired (coords NULL, no Google call), that profile drops out of nearby.
- [ ] Log in as that inactive user → `ensure_fresh_for_user` refills coords.
- [ ] `manage.py places_usage` shows sane numbers.

**Guards**
- [ ] Set `PLACES_DAILY_CAP_AUTOCOMPLETE=3` → 4th search returns 503 and the picker shows a friendly error; reset.
- [ ] Hammer autocomplete anonymously → 429 after 20/min.
- [ ] Remove the key → app boots, search shows error, nothing crashes.

**Google console**
- [ ] Metrics show Autocomplete (New) + Place Details (New) requests only; Place Details billed at **Essentials** SKU (check "SKU" breakdown after a day).

---

## 9. Later (not part of this build)

1. **Tests** — proxy validation + circuit breaker, LocationService upsert/propagate, refresh command (mock Google), picker session-token behaviour, payload shapes.
2. **Render Cron Job** — daily `python manage.py refresh_place_coords`.
3. **Terms of Use + Privacy Policy** pages linking Google's Terms and Privacy Policy (a Maps Platform requirement for apps with end users).
4. **Key hardening** — restrict the key to Render's static outbound IPs.
5. **India pricing eligibility** — billing + majority usage must stay in India for the 70k free tier. When most usage moves abroad expect 10k free/SKU and roughly 3× the unit price. Re-check `places_usage` monthly.
6. **Optional** — Google fallback tuning for small towns (`includedPrimaryTypes`), per-user monthly budget if abuse appears.