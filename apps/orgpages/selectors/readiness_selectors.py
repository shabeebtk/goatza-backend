"""
How ready the page is — the hub's progress bar and its "what next".

    readiness: {percent, next: {key, label} | null, items: [{key, label, done}]}

Items, weights (100 in all) and when each is done, in the order ``next`` reads:

    programs    20  "Add your programs"            ≥ 1 program
    facilities  15  "Add your facilities"          ≥ 3 items or ≥ 1 photo on any branch
    coaches     15  "Add your coaches"             ≥ 1 visible coach
    about       10  "Write about your page"        description ≥ 40 characters
    facts       10  "Add key facts"                ≥ 3 filled (FACT_GROUPS)
    photos      10  "Add a logo and cover"         both
    location    10  "Add a location"               ≥ 1 branch
    contact     10  "Choose how people reach you"  any contact option on

A SWITCHED-OFF SECTION DOESN'T COUNT: its item leaves the list, its weight is
dropped and the rest scale to 100. Only five items have a section switch
(``ITEM_SECTION``); facts, photos and contact always count.
"""

ITEMS = (
    ("programs", "Add your programs", 20),
    ("facilities", "Add your facilities", 15),
    ("coaches", "Add your coaches", 15),
    ("about", "Write about your page", 10),
    ("facts", "Add key facts", 10),
    ("photos", "Add a logo and cover", 10),
    ("location", "Add a location", 10),
    ("contact", "Choose how people reach you", 10),
)

# item → the section switch that removes it from the score.
ITEM_SECTION = {
    "programs": "programs",
    "facilities": "facilities",
    "coaches": "coaches",
    "about": "about",
    "location": "locations",
}

ABOUT_MIN_LENGTH = 40
FACTS_NEEDED = 3
FACILITY_ITEMS_NEEDED = 3


def facts_filled(page) -> int:
    """
    How many of the six facts are filled. The age range is ONE fact (either
    end), and the social links are one (any of them).
    """
    return sum(
        (
            page.founded_year is not None,
            page.age_min is not None or page.age_max is not None,
            bool(page.coaching_languages),
            bool(page.training_days),
            bool(page.student_range),
            bool(page.instagram_url or page.youtube_url or page.facebook_url),
        )
    )


def _done(page, status):
    facilities = status["facilities"]
    return {
        "programs": status["programs"] >= 1,
        "facilities": (
            facilities["max_items_on_a_branch"] >= FACILITY_ITEMS_NEEDED
            or facilities["max_photos_on_a_branch"] >= 1
        ),
        "coaches": status["coaches"] >= 1,
        "about": status["description_length"] >= ABOUT_MIN_LENGTH,
        "facts": facts_filled(page) >= FACTS_NEEDED,
        "photos": status["has_logo"] and status["has_cover"],
        "location": status["locations"] >= 1,
        "contact": any(
            (
                page.enquiry_form_enabled,
                page.whatsapp_enabled,
                page.call_enabled,
                page.email_enabled,
            )
        ),
    }


def readiness(organization, page, status):
    done = _done(page, status)

    counted = [
        (key, label, weight)
        for key, label, weight in ITEMS
        if key not in ITEM_SECTION or page.section_enabled(ITEM_SECTION[key])
    ]
    total = sum(weight for _, _, weight in counted)
    earned = sum(weight for key, _, weight in counted if done[key])
    # Half up, not Python's banker's rounding: 62.5 reads as 63.
    percent = int(earned * 100 / total + 0.5) if total else 100

    next_item = next(
        ({"key": key, "label": label} for key, label, _ in counted if not done[key]),
        None,
    )

    return {
        "percent": percent,
        "next": next_item,
        "items": [
            {"key": key, "label": label, "done": done[key]}
            for key, label, _ in counted
        ],
    }
