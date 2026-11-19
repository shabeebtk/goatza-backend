"""
Every rule of the org page that is a list or a number, in one place.

The section keys are a contract with the frontend: add one, never rename one.
"""

# The sections a club can switch off. A key missing from PageSettings.sections
# means ON. "posts" is always shown and has no switch.
SWITCHABLE_SECTIONS = (
    "about",
    "programs",
    "facilities",
    "coaches",
    "players",
    "squads",
    "locations",
    "trials",
)
ALWAYS_SHOWN_SECTIONS = ("posts",)

# The display order on the public page, by org type. A switched-off key leaves
# the order; "posts" is always last. Squads, locations, trials and posts come
# from their own endpoints, so the client also drops those when they are empty.
ACADEMY_ORDER = (
    "about", "programs", "facilities", "coaches", "players",
    "squads", "locations", "trials", "posts",
)
CLUB_ORDER = (
    "about", "squads", "trials", "coaches", "players",
    "programs", "facilities", "locations", "posts",
)
SECTION_ORDER_BY_TYPE = {
    "academy": ACADEMY_ORDER,
    "club": CLUB_ORDER,
    "team": CLUB_ORDER,
}

# Week order. Every stored list of days is in this order, duplicates dropped.
WEEK_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Limits, enforced in the services under the PageSettings row lock.
MAX_PROGRAMS = 30
MAX_COACHES = 30
MAX_FEATURED_PLAYERS = 60
MAX_PHOTOS_PER_LOCATION = 12
MAX_CUSTOM_FACILITIES = 10
MAX_COACHING_LANGUAGES = 6

# Ranges.
AGE_MIN = 3
AGE_MAX = 80
FOUNDED_YEAR_MIN = 1850
YEARS_COACHING_MAX = 60
FEE_AMOUNT_MIN = 1
FEE_AMOUNT_MAX = 1_000_000

# Text lengths.
NAME_MIN = 2
NAME_MAX = 80
LANGUAGE_MAX = 30
CUSTOM_FACILITY_MIN = 2
CUSTOM_FACILITY_MAX = 40
COACH_TITLE_MAX = 40
COACH_LICENCE_MAX = 60
HIGHLIGHT_MAX = 60
SOCIAL_URL_MAX = 300
PHOTO_URL_MAX = 500
PHOTO_ID_MAX = 300

# The hosts a social link may point at. A host matches itself and any
# subdomain of it (www., m.).
SOCIAL_HOSTS = {
    "instagram_url": ("instagram.com",),
    "youtube_url": ("youtube.com", "youtu.be"),
    "facebook_url": ("facebook.com", "fb.com"),
}

# Where an org_page upload lands (services/storage/paths.py). Attaching a
# photo checks the key starts with this, for the acting org.
def page_photo_prefix(organization_id) -> str:
    return f"organizations/{organization_id}/page/"
