'''

User location (profile)
Post location (optional tagging)
Search (clubs/posts near location)
Future: feed ranking boost


src/shared/
  services/
    mapbox.service.ts          ← new
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