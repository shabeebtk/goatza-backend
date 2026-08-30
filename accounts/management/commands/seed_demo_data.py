"""
Seed demo data: 50 football players + 20 organizations.

Usage:
    python manage.py seed_demo_data          # create (idempotent, safe to re-run)
    python manage.py seed_demo_data --wipe   # delete previously seeded data

All seeded users share the password: strong#password
Seeded user emails end with @seed.goatza so they are easy to identify/wipe.

Requires the sports catalog to exist first:
    python manage.py data_add
"""

import random
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.hashers import make_password
from django.db import transaction

from accounts.models import User, UserProfile
from shared.models import Location
from sports.models import (
    Sport,
    SportPosition,
    SportAttribute,
    UserSport,
    UserSportPosition,
    UserAttributeValue,
)
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationLocation,
    OrganizationMember,
    OrganizationSport,
)
from connections.models import Follow
from usernames.models import UsernameRegistry
from usernames.services.username_service import UsernameService
from utils.validations import USERNAME_MAX_LENGTH


SEED_PASSWORD = "strong#password"
SEED_EMAIL_DOMAIN = "seed.goatza"

# ---------------------------------------------------------------------------
# Cities: (name, city, state, country, country_code, lat, lng)
# Mostly Kerala (so "nearby" rails light up for a Kerala test account),
# plus a few far cities to verify distance sorting / radius fallback.
# ---------------------------------------------------------------------------
CITIES = [
    ("Kozhikode", "Kozhikode", "Kerala", "India", "IN", 11.2588, 75.7804),
    ("Kochi", "Kochi", "Kerala", "India", "IN", 9.9312, 76.2673),
    ("Thrissur", "Thrissur", "Kerala", "India", "IN", 10.5276, 76.2144),
    ("Malappuram", "Malappuram", "Kerala", "India", "IN", 11.0510, 76.0711),
    ("Kannur", "Kannur", "Kerala", "India", "IN", 11.8745, 75.3704),
    ("Thiruvananthapuram", "Thiruvananthapuram", "Kerala", "India", "IN", 8.5241, 76.9366),
    ("Palakkad", "Palakkad", "Kerala", "India", "IN", 10.7867, 76.6548),
    ("Kollam", "Kollam", "Kerala", "India", "IN", 8.8932, 76.6141),
    ("Manjeri", "Manjeri", "Kerala", "India", "IN", 11.1203, 76.1200),
    ("Vadakara", "Vadakara", "Kerala", "India", "IN", 11.6085, 75.5917),
    ("Bengaluru", "Bengaluru", "Karnataka", "India", "IN", 12.9716, 77.5946),
    ("Chennai", "Chennai", "Tamil Nadu", "India", "IN", 13.0827, 80.2707),
    ("Mumbai", "Mumbai", "Maharashtra", "India", "IN", 19.0760, 72.8777),
    ("Hyderabad", "Hyderabad", "Telangana", "India", "IN", 17.3850, 78.4867),
    ("Panaji", "Panaji", "Goa", "India", "IN", 15.4909, 73.8278),
]

# Weight: 80% of players land in the first 10 (Kerala) cities.
KERALA_CITY_COUNT = 10

# (name, gender)
PLAYER_NAMES = [
    ("Arjun Nair", "male"), ("Rahul Menon", "male"), ("Sreejith Kumar", "male"),
    ("Aditya Varma", "male"), ("Nikhil Raj", "male"), ("Vishnu Prasad", "male"),
    ("Fahad Rahman", "male"), ("Aslam Karim", "male"), ("Rizwan Ali", "male"),
    ("Akhil Dev", "male"), ("Sandeep Pillai", "male"), ("Vineeth Krishnan", "male"),
    ("Jithin Jose", "male"), ("Alan Thomas", "male"), ("Kevin George", "male"),
    ("Rohan Das", "male"), ("Aravind Suresh", "male"), ("Midhun Mohan", "male"),
    ("Shyam Sundar", "male"), ("Abhinav Shaji", "male"), ("Naveen Chandran", "male"),
    ("Hari Govind", "male"), ("Ajmal Khan", "male"), ("Salman Faris", "male"),
    ("Dinesh Babu", "male"), ("Pranav Anil", "male"), ("Yadhu Krishna", "male"),
    ("Gokul Ramesh", "male"), ("Adarsh Vijayan", "male"), ("Nithin Shibu", "male"),
    ("Basil Mathew", "male"), ("Jerin Philip", "male"), ("Amal Antony", "male"),
    ("Riyas Muhammed", "male"), ("Shafeeq Hassan", "male"), ("Deepak Warrier", "male"),
    ("Unnikrishnan V", "male"), ("Manu Prakash", "male"),
    ("Anjali Krishnan", "female"), ("Devika Menon", "female"),
    ("Aparna Suresh", "female"), ("Meera Nambiar", "female"),
    ("Sneha Ravi", "female"), ("Gayathri Mohan", "female"),
    ("Aleena Joseph", "female"), ("Fathima Noor", "female"),
    ("Riya Vinod", "female"), ("Nandana Pradeep", "female"),
    ("Lakshmi Warrier", "female"), ("Diya Ashok", "female"),
]

HEADLINE_TEMPLATES = [
    "{position} | {level} footballer from {city}",
    "Football {position} • {level} level • Open to trials",
    "{position} chasing the next big opportunity ⚽",
    "Aspiring professional {position} | Based in {city}",
    "{level} {position} | District & club football",
    "{position} | Team player, big dreams",
    "Football is life ⚽ | {position} from {city}",
    "{position} • {level} • Looking for a club",
]

ABOUT_TEMPLATES = [
    "Football player from {city} playing as a {position}. {years}+ years on the pitch across school, district and club level. Training hard every day and looking for the right team to grow with.",
    "Started playing football at a young age in {city}. Mainly a {position} but comfortable adapting to the team's needs. Strong work ethic, coachable, and hungry for competitive game time.",
    "{position} based in {city} with {years} years of playing experience. Represented local clubs and tournaments. Focused on improving fitness, tactical awareness and consistency this season.",
    "Passionate {position} from {city}. I believe in discipline, teamwork and showing up every single day. Currently looking for trials and club opportunities where I can contribute and develop.",
]

# (name, type, level, verified)
ORGANIZATIONS = [
    ("Malabar United FC", "club", "semi_professional", True),
    ("Zamorin FC Kozhikode", "club", "professional", True),
    ("Spice Coast FC", "club", "semi_professional", False),
    ("Backwater Athletic Club", "club", "amateur", False),
    ("Calicut City FC", "club", "semi_professional", True),
    ("Kochi Mariners FC", "club", "professional", True),
    ("Nila Valley FC", "club", "amateur", False),
    ("Thrissur Titans", "team", "semi_professional", False),
    ("Kannur Warriors FC", "team", "amateur", False),
    ("Palakkad Strikers", "team", "amateur", False),
    ("Trivandrum Royals", "team", "semi_professional", True),
    ("Malappuram Lions", "team", "amateur", False),
    ("Elite Football Academy Kozhikode", "academy", "youth", True),
    ("NextGen Soccer Academy", "academy", "youth", False),
    ("Golden Boot Academy Kochi", "academy", "youth", True),
    ("Future Stars Football Academy", "academy", "youth", False),
    ("Coastal Football Academy Kannur", "academy", "youth", False),
    ("ProPath Football Academy", "academy", "professional", True),
    ("St. Xavier's College Kozhikode", "school", "youth", False),
    ("Green Valley Higher Secondary School", "school", "youth", False),
]

ORG_HEADLINES = {
    "club": "Competitive football club — building squads for the upcoming season",
    "team": "Local football team competing in district & regional tournaments",
    "academy": "Football academy developing the next generation of talent",
    "school": "School football program — trials, training and tournaments",
}

ORG_DESCRIPTIONS = {
    "club": "We are a football club committed to competitive excellence. We run senior and youth squads, participate in league and cup competitions, and regularly hold open trials for talented players. Join us and be part of a club that plays to win and develops its players.",
    "team": "A passionate local football team competing across district and regional tournaments. We train multiple times a week and are always on the lookout for committed players who want regular competitive football.",
    "academy": "Our academy focuses on structured player development — technical training, tactical education, fitness and match exposure. We run age-group batches with qualified coaches and a clear pathway toward competitive club football.",
    "school": "Our institution runs an active football program with regular coaching, inter-school tournaments and annual selection trials. We believe in balancing sport and education to build well-rounded athletes.",
}

LOGO_COLORS = ["0D8ABC", "B91C1C", "047857", "7C3AED", "D97706", "1D4ED8", "0F766E", "9333EA"]


def _avatar(i: int) -> str:
    # pravatar has 70 distinct images
    return f"https://i.pravatar.cc/300?img={(i % 70) + 1}"


def _cover(seed: str) -> str:
    return f"https://picsum.photos/seed/goatza-{seed}/1200/400"


def _org_logo(name: str, i: int) -> str:
    initials = "+".join(name.split()[:2])
    color = LOGO_COLORS[i % len(LOGO_COLORS)]
    return (
        f"https://ui-avatars.com/api/?name={initials}"
        f"&background={color}&color=fff&size=256&bold=true"
    )


def _slug(name: str, max_length: int = USERNAME_MAX_LENGTH) -> str:
    """
    Deterministic handle from a display name.

    CAPPED at the validator's bound, and re-stripped after the cut so the
    truncation cannot leave a trailing underscore. Seeded rows go through
    UsernameService.claim like real ones, so "Green Valley Higher Secondary
    School" has to come out the other side as something the validator accepts
    — uncapped, it produced a 36-character handle that claim() rejected.

    Determinism matters beyond the create path: --wipe rebuilds the same list
    to find what it seeded last time, so both callers must slug identically.
    """
    s = "".join(c if c.isalnum() else "_" for c in name.lower())
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")[:max_length].strip("_")


def _jitter(value: float, spread: float = 0.05) -> float:
    return round(value + random.uniform(-spread, spread), 6)


class Command(BaseCommand):
    help = "Seed 50 football players and 20 organizations with full details"

    def add_arguments(self, parser):
        parser.add_argument(
            "--wipe",
            action="store_true",
            help="Delete previously seeded users and organizations, then exit",
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        random.seed(42)  # reproducible data

        if options["wipe"]:
            self._wipe()
            return

        try:
            football = Sport.objects.get(name="Football")
        except Sport.DoesNotExist:
            raise CommandError(
                "Football sport not found. Run `python manage.py data_add` first."
            )

        positions = list(SportPosition.objects.filter(sport=football))
        if not positions:
            raise CommandError("No football positions found. Run `python manage.py data_add` first.")

        with transaction.atomic():
            locations = self._seed_locations()
            users = self._seed_players(football, positions, locations)
            orgs = self._seed_organizations(football, users)
            self._seed_follows(users, orgs)
            self._sync_counters(users, orgs)

        self.stdout.write(self.style.SUCCESS(
            f"\n✅ Done: {len(users)} players, {len(orgs)} organizations seeded."
        ))
        self.stdout.write(f"   Login with any seeded email (e.g. player01@{SEED_EMAIL_DOMAIN})")
        self.stdout.write(f"   Password for all: {SEED_PASSWORD}")

    # ------------------------------------------------------------------
    def _wipe(self):
        org_usernames = [_slug(name) for name, *_ in ORGANIZATIONS]
        org_deleted, _ = Organization.objects.filter(username__in=org_usernames).delete()
        user_deleted, _ = User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}").delete()
        self.stdout.write(self.style.SUCCESS(
            f"Wiped seeded data (orgs cascade: {org_deleted}, users cascade: {user_deleted})."
        ))

    # ------------------------------------------------------------------
    def _seed_locations(self):
        locations = {}
        for name, city, state, country, code, lat, lng in CITIES:
            # provider=manual: these are hand-written coordinates with no
            # place_id behind them, and the refresh job must not try to look
            # them up against Google.
            loc, _ = Location.objects.get_or_create(
                name=name,
                provider=Location.Provider.MANUAL,
                latitude=lat,
                longitude=lng,
                defaults={
                    "type": Location.Type.CITY,
                    "city": city,
                    "state": state,
                    "country": country,
                    "country_code": code,
                },
            )
            locations[name] = loc
        self.stdout.write(f"Locations ready: {len(locations)}")
        return locations

    # ------------------------------------------------------------------
    def _seed_players(self, football, positions, locations):
        hashed_password = make_password(SEED_PASSWORD)  # hash once, reuse

        strong_foot_attr = SportAttribute.objects.filter(
            sport=football, name="Strong Foot"
        ).prefetch_related("options").first()
        preferred_role_attr = SportAttribute.objects.filter(
            sport=football, name="Preferred Role"
        ).prefetch_related("options").first()

        users = []
        # Read from the shared namespace, not from User alone — a seeded player
        # must not be handed a handle one of the seeded orgs already holds.
        used_usernames = set(
            UsernameRegistry.objects.values_list("username_lower", flat=True)
        )

        for i, (name, gender) in enumerate(PLAYER_NAMES[:50], start=1):
            email = f"player{i:02d}@{SEED_EMAIL_DOMAIN}"

            existing = User.objects.filter(email=email).first()
            if existing:
                users.append(existing)
                continue

            # unique username from name. Three characters short of the bound:
            # the de-dupe below appends a counter, and the result still has to
            # pass validate_username_format.
            base = _slug(name, USERNAME_MAX_LENGTH - 3)
            username = base
            n = 1
            while username in used_usernames:
                n += 1
                username = f"{base}{n}"
            used_usernames.add(username)

            user = User(
                email=email,
                role=User.Role.PLAYER,
                is_email_verified=True,
                is_active=True,
                password=hashed_password,
            )
            user.save()

            # Registers the handle AND writes the display column. Seeded rows
            # go through the same door as real ones, or /[username] would not
            # resolve any of them.
            UsernameService.claim(username, user=user)

            # --- location: 80% Kerala, 20% elsewhere -------------------
            if random.random() < 0.8:
                city_row = CITIES[random.randrange(KERALA_CITY_COUNT)]
            else:
                city_row = CITIES[random.randrange(KERALA_CITY_COUNT, len(CITIES))]
            loc_name, city, state, country, code, base_lat, base_lng = city_row

            position = random.choice(positions)
            level = random.choice(UserSport.ExperienceLevel.values)
            birth_year = random.randint(1996, 2008)  # ages ~18–30
            years_playing = random.randint(3, 12)

            headline = random.choice(HEADLINE_TEMPLATES).format(
                position=position.name, level=level.replace("_", " ").title(), city=city
            )
            about = random.choice(ABOUT_TEMPLATES).format(
                position=position.name, city=city, years=years_playing
            )

            UserProfile.objects.create(
                user=user,
                name=name,
                headline=headline,
                about=about,
                profile_photo=_avatar(i),
                cover_photo=_cover(f"user{i}"),
                gender=gender,
                birthdate=date(birth_year, random.randint(1, 12), random.randint(1, 28)),
                height_cm=random.randint(158, 195),
                weight_kg=random.randint(52, 88),
                location=locations[loc_name],
                location_name=f"{city}, {state}, {country}",
                city=city,
                country_code=code,
                latitude=_jitter(base_lat),
                longitude=_jitter(base_lng),
            )

            # --- football: sport + positions + attributes --------------
            UserSport.objects.create(
                user=user, sport=football, is_primary=True, experience_level=level
            )

            UserSportPosition.objects.create(
                user=user, sport=football, position=position, is_primary=True
            )
            # ~40% get a secondary position
            if random.random() < 0.4:
                secondary = random.choice([p for p in positions if p.id != position.id])
                UserSportPosition.objects.create(
                    user=user, sport=football, position=secondary, is_primary=False
                )

            if strong_foot_attr:
                option = random.choice(list(strong_foot_attr.options.all()))
                UserAttributeValue.objects.create(
                    user=user, sport=football, attribute=strong_foot_attr, option=option
                )
            if preferred_role_attr:
                opts = random.sample(
                    list(preferred_role_attr.options.all()), k=random.randint(1, 2)
                )
                for option in opts:
                    UserAttributeValue.objects.create(
                        user=user, sport=football,
                        attribute=preferred_role_attr, option=option,
                    )

            users.append(user)

        self.stdout.write(f"Players ready: {len(users)}")
        return users

    # ------------------------------------------------------------------
    def _seed_organizations(self, football, users):
        orgs = []
        for i, (name, org_type, level, verified) in enumerate(ORGANIZATIONS, start=1):
            username = _slug(name)

            existing = Organization.objects.filter(username=username).first()
            if existing:
                orgs.append(existing)
                continue

            owner = random.choice(users)

            org = Organization.objects.create(
                name=name,
                username=username,
                type=org_type,
                created_by=owner,
                is_verified=verified,
                is_active=True,
            )

            UsernameService.claim(username, organization=org)

            OrganizationProfile.objects.create(
                organization=org,
                logo=_org_logo(name, i),
                cover_image=_cover(f"org{i}"),
                headline=ORG_HEADLINES[org_type],
                description=ORG_DESCRIPTIONS[org_type],
                website=f"https://{username.replace('_', '')}.example.com",
                level=level,
            )

            # location — mostly Kerala, a couple elsewhere
            if random.random() < 0.85:
                city_row = CITIES[random.randrange(KERALA_CITY_COUNT)]
            else:
                city_row = CITIES[random.randrange(KERALA_CITY_COUNT, len(CITIES))]
            loc_name, city, state, country, code, base_lat, base_lng = city_row

            OrganizationLocation.objects.create(
                organization=org,
                name="Main Ground",
                address=f"{name} Ground, {city}",
                city=city,
                state=state,
                country_code=code,
                latitude=_jitter(base_lat),
                longitude=_jitter(base_lng),
                is_primary=True,
            )

            OrganizationSport.objects.create(
                organization=org, sport=football, is_primary=True
            )

            # members: owner + 1–2 extra members
            OrganizationMember.objects.create(
                organization=org, user=owner, role=OrganizationMember.Role.OWNER
            )
            extras = random.sample([u for u in users if u.id != owner.id], k=random.randint(1, 2))
            for member in extras:
                OrganizationMember.objects.create(
                    organization=org,
                    user=member,
                    role=random.choice(
                        [OrganizationMember.Role.ADMIN, OrganizationMember.Role.COACH]
                    ),
                )

            orgs.append(org)

        self.stdout.write(f"Organizations ready: {len(orgs)}")
        return orgs

    # ------------------------------------------------------------------
    def _seed_follows(self, users, orgs):
        """Random follow graph so followers_count varies (powers 'popular' sorting)."""
        # idempotency: if seeded follows already exist, don't pile more on re-runs
        if Follow.objects.filter(
            follower_user__email__endswith=f"@{SEED_EMAIL_DOMAIN}"
        ).exists():
            self.stdout.write("Follows already seeded — skipping")
            return

        created = 0

        # a few "star" players + orgs that attract more followers
        stars = random.sample(users, k=8)
        star_orgs = random.sample(orgs, k=5)

        for user in users:
            # follow 3–8 users (stars are more likely)
            candidates = [u for u in users if u.id != user.id]
            weights = [5 if u in stars else 1 for u in candidates]
            k = random.randint(3, 8)
            targets = set()
            while len(targets) < k:
                targets.add(random.choices(candidates, weights=weights, k=1)[0])
            for target in targets:
                _, was_created = Follow.objects.get_or_create(
                    follower_user=user, following_user=target
                )
                created += was_created

            # follow 1–4 orgs
            org_weights = [5 if o in star_orgs else 1 for o in orgs]
            k = random.randint(1, 4)
            org_targets = set()
            while len(org_targets) < k:
                org_targets.add(random.choices(orgs, weights=org_weights, k=1)[0])
            for org in org_targets:
                _, was_created = Follow.objects.get_or_create(
                    follower_user=user, following_org=org
                )
                created += was_created

        # guarantee every org has at least a couple of followers
        for org in orgs:
            if not Follow.objects.filter(following_org=org).exists():
                for user in random.sample(users, k=random.randint(2, 4)):
                    _, was_created = Follow.objects.get_or_create(
                        follower_user=user, following_org=org
                    )
                    created += was_created

        # orgs follow a few users back
        for org in orgs:
            for target in random.sample(users, k=random.randint(0, 3)):
                _, was_created = Follow.objects.get_or_create(
                    follower_org=org, following_user=target
                )
                created += was_created

        self.stdout.write(f"Follows created: {created}")

    # ------------------------------------------------------------------
    def _sync_counters(self, users, orgs):
        """Recompute denormalized counters from the real Follow rows."""
        for user in users:
            followers = Follow.objects.filter(following_user=user).count()
            following = Follow.objects.filter(follower_user=user).count()

            # connections = mutual user↔user follows
            i_follow = set(
                Follow.objects.filter(
                    follower_user=user, following_user__isnull=False
                ).values_list("following_user_id", flat=True)
            )
            follow_me = set(
                Follow.objects.filter(
                    following_user=user, follower_user__isnull=False
                ).values_list("follower_user_id", flat=True)
            )
            connections = len(i_follow & follow_me)

            UserProfile.objects.filter(user=user).update(
                followers_count=followers,
                following_count=following,
                connections_count=connections,
            )

        for org in orgs:
            followers = Follow.objects.filter(following_org=org).count()
            following = Follow.objects.filter(follower_org=org).count()
            OrganizationProfile.objects.filter(organization=org).update(
                followers_count=followers,
                following_count=following,
            )

        self.stdout.write("Counters synced (followers / following / connections)")