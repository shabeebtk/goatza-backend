'''
Original location-feature sketch. The provider is now Google Places and the
authority on all of it is docs/PLACES_MIGRATION.md — this is kept only as the
list of surfaces location touches.

User location (profile)
Post location (optional tagging)
Search (clubs/posts near location)
Future: feed ranking boost


src/shared/
  services/
    places.service.ts          ← new
  components/ui/
    LocationPicker/
      LocationPicker.tsx       ← new (reusable anywhere)
      LocationPicker.module.css

src/features/profile/
  services/
    profile.api.ts             ← updated (LocationPayload + UserLocation types)
  components/
    EditProfileModal/
      EditProfileModal.tsx     ← updated
      EditProfileModal.module.css ← append EditProfileModal.additions.css to bottom


'''