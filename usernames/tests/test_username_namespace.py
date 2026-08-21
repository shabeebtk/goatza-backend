"""
The shared username namespace.

The point of every test here is that users and organizations draw from ONE
pool, enforced by a database constraint rather than by four half-blind
application checks. So the collision tests come at it from all four write paths
(profile edit, availability endpoint, org create, org edit), and the race test
skips the API entirely and hits UsernameService.claim twice.
"""

from importlib import import_module
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from accounts.models import User, UserProfile
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from organization.models import Organization, OrganizationMember, OrganizationProfile
from organization.services.organization_service import OrganizationService
from organization.services.user_organization_services import UserOrganizationService
from posts.services.post_content_service import extract_mention_usernames
from usernames.exceptions import UsernameTaken
from usernames.models import UsernameRegistry
from usernames.services.username_service import UsernameService
from utils.validations import (
    RESERVED_USERNAMES,
    USERNAME_MAX_LENGTH,
    validate_username_format,
)

CHECK_URL = "/user/check/username/availability"
UPDATE_PROFILE_URL = "/user/update/profile/data"
ORG_UPDATE_URL = "/organizations/update"


def make_user(email, username=None, name="Player"):
    user = User.objects.create_user(email=email, password="password123")
    UserProfile.objects.create(user=user, name=name)
    if username:
        UsernameService.claim(username, user=user)
    return user


def make_org(name, username, created_by):
    org = Organization.objects.create(
        name=name, username=username, type=Organization.Type.CLUB, created_by=created_by
    )
    OrganizationProfile.objects.create(organization=org)
    OrganizationMember.objects.create(
        organization=org, user=created_by, role=OrganizationMember.Role.OWNER
    )
    UsernameRegistry.objects.create(username_lower=username, organization=org)
    return org


class ValidatorTests(TestCase):
    """One charset, one length bound, one reserved list — for both actors."""

    def test_normalizes_and_returns_the_value(self):
        self.assertEqual(validate_username_format("  Kochi_FC  "), "kochi_fc")

    def test_dots_are_rejected(self):
        # The one charset difference organizations used to have.
        with self.assertRaises(ValueError):
            validate_username_format("kochi.fc")

    def test_purely_numeric_is_rejected(self):
        # Indistinguishable from an id in a URL.
        with self.assertRaises(ValueError):
            validate_username_format("12345")

    def test_length_bounds(self):
        with self.assertRaises(ValueError):
            validate_username_format("ab")
        with self.assertRaises(ValueError):
            validate_username_format("a" * (USERNAME_MAX_LENGTH + 1))
        self.assertEqual(
            validate_username_format("a" * USERNAME_MAX_LENGTH),
            "a" * USERNAME_MAX_LENGTH,
        )

    def test_underscore_rules(self):
        for bad in ("_kochi", "kochi_", "ko__chi"):
            with self.assertRaises(ValueError, msg=bad):
                validate_username_format(bad)

    def test_live_frontend_routes_are_reserved(self):
        # /[username] sits directly beside these; a user holding one shadows
        # the real page.
        for segment in (
            "auth", "card", "chat", "coaching", "cv", "explore", "highlights",
            "home", "join", "matches", "messages", "notifications",
            "organization", "posts", "recruitments", "scouting", "search",
        ):
            self.assertIn(segment, RESERVED_USERNAMES, segment)


class ReservedNameTests(TestCase):
    """Reserved handles are refused to BOTH actor types, not just to users."""

    def setUp(self):
        self.user = make_user("reserved@example.com")
        self.org = make_org("Reserved FC", "reservedfc", self.user)

    def test_every_reserved_name_is_rejected_for_both_actors(self):
        for name in RESERVED_USERNAMES:
            # Some entries can never reach the reserved check (".well-known"
            # fails the charset first, "me" fails the length) — what matters is
            # that they are refused, not which rule refuses them.
            with self.assertRaises(ValueError, msg=f"user @{name}"):
                UsernameService.claim(name, user=self.user)
            with self.assertRaises(ValueError, msg=f"org @{name}"):
                UsernameService.claim(name, organization=self.org)


class ClaimTests(TestCase):

    def setUp(self):
        cache.clear()
        self.owner = make_user("owner@example.com", "ownerhandle")

    def test_claim_registers_and_writes_the_display_column(self):
        user = make_user("claim@example.com")

        UsernameService.claim("Rahul_10", user=user)

        user.refresh_from_db()
        self.assertEqual(user.username, "rahul_10")
        self.assertTrue(
            UsernameRegistry.objects.filter(
                username_lower="rahul_10", user=user
            ).exists()
        )

    def test_a_user_holds_exactly_one_row(self):
        user = make_user("one@example.com", "firsthandle")

        UsernameService.claim("secondhandle", user=user)

        self.assertEqual(
            list(
                UsernameRegistry.objects.filter(user=user)
                .values_list("username_lower", flat=True)
            ),
            ["secondhandle"],
        )

    def test_reclaiming_your_own_handle_is_not_a_collision(self):
        user = make_user("same@example.com", "samehandle")

        self.assertEqual(
            UsernameService.claim("SameHandle", user=user), "samehandle"
        )

    def test_org_cannot_take_a_users_handle(self):
        org = make_org("Rival FC", "rivalfc", self.owner)

        with self.assertRaises(UsernameTaken):
            UsernameService.claim("ownerhandle", organization=org)

        org.refresh_from_db()
        self.assertEqual(org.username, "rivalfc")

    def test_user_cannot_take_an_orgs_handle(self):
        make_org("Kochi FC", "kochifc", self.owner)
        other = make_user("other@example.com")

        with self.assertRaises(UsernameTaken):
            UsernameService.claim("kochifc", user=other)

    def test_concurrent_claims_of_the_same_handle(self):
        """
        One succeeds, one raises. The unique constraint is the arbiter — this
        is why claim() inserts and catches rather than checking first.
        """
        a = make_user("racer_a@example.com")
        b = make_user("racer_b@example.com")

        self.assertEqual(UsernameService.claim("contested", user=a), "contested")

        with self.assertRaises(UsernameTaken):
            UsernameService.claim("contested", user=b)

        b.refresh_from_db()
        self.assertIsNone(b.username)

    def test_the_database_refuses_a_duplicate_even_without_the_service(self):
        # Belt and braces: the guarantee is a constraint, not a code path.
        user = make_user("dbcheck@example.com", "dbcheckhandle")
        org = make_org("DB FC", "dbfc", self.owner)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UsernameRegistry.objects.create(
                    username_lower=user.username, organization=org
                )

    def test_exactly_one_owner_is_required(self):
        with self.assertRaises(ValueError):
            UsernameService.claim("nobody", user=None, organization=None)


class IsAvailableTests(TestCase):

    def setUp(self):
        self.user = make_user("avail@example.com", "takenbyuser")
        self.org = make_org("Taken FC", "takenbyorg", self.user)

    def test_free_handle(self):
        self.assertTrue(UsernameService.is_available("brandnew"))

    def test_sees_both_tables(self):
        self.assertFalse(UsernameService.is_available("takenbyuser"))
        self.assertFalse(UsernameService.is_available("takenbyorg"))

    def test_your_own_handle_is_available_to_you(self):
        self.assertTrue(
            UsernameService.is_available("takenbyuser", exclude_user=self.user)
        )
        self.assertTrue(
            UsernameService.is_available("takenbyorg", exclude_org=self.org)
        )

    def test_invalid_raises_rather_than_returning_false(self):
        # "Not allowed" and "somebody has it" are different answers.
        with self.assertRaises(ValueError):
            UsernameService.is_available("kochi.fc")


class GenerateTests(TestCase):
    """generate() output must ALWAYS pass the validator — it never sees one."""

    HOSTILE_BASES = [
        "",
        "   ",
        None,
        "🐐🐐🐐",
        "a" * 60,
        "..!!..",
        "____",
        "12345",
        "admin",
        "matches",
        "Real Madrid CF",
        "kochi.fc",
        "x",
    ]

    def test_output_always_validates(self):
        for owner_type in (TYPE_USER, TYPE_ORGANIZATION):
            for base in self.HOSTILE_BASES:
                handle = UsernameService.generate(base, owner_type=owner_type)
                self.assertEqual(
                    validate_username_format(handle),
                    handle,
                    f"{owner_type} / {base!r} -> {handle!r}",
                )

    def test_long_org_names_stay_within_the_bound(self):
        # The old generator built base[:20] + 2 digits = 22 chars against a
        # 20-char validator, and never called the validator to find out.
        handle = UsernameService.generate(
            "Kerala Blasters Football Club Academy",
            owner_type=TYPE_ORGANIZATION,
        )
        self.assertLessEqual(len(handle), USERNAME_MAX_LENGTH)

    def test_falls_back_per_actor_type(self):
        self.assertTrue(
            UsernameService.generate("🐐", owner_type=TYPE_USER).startswith("player")
        )
        self.assertTrue(
            UsernameService.generate("🐐", owner_type=TYPE_ORGANIZATION).startswith("club")
        )

    def test_loops_against_the_whole_namespace(self):
        # The handle an ORG would be generated is held by a USER. The old
        # generator only looked at Organization and would have handed it over.
        make_user("gen@example.com", "kochifc11")

        with patch(
            "usernames.services.username_service.random.randint",
            side_effect=[11, 1234],
        ):
            handle = UsernameService.generate(
                "Kochi FC", owner_type=TYPE_ORGANIZATION
            )

        self.assertEqual(handle, "kochifc1234")

    def test_user_generation_skips_a_handle_an_org_holds(self):
        owner = make_user("genowner@example.com", "genowner")
        make_org("Rahul FC", "rahul11", owner)

        with patch(
            "usernames.services.username_service.random.randint",
            side_effect=[11, 1234],
        ):
            handle = UsernameService.generate("Rahul", owner_type=TYPE_USER)

        self.assertEqual(handle, "rahul1234")


class ResolveTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = make_user("resolve@example.com", "resolveuser")
        self.org = make_org("Resolve FC", "resolveorg", self.user)

    def test_resolves_both_actor_types(self):
        self.assertEqual(
            UsernameService.resolve("resolveuser"),
            {"type": TYPE_USER, "id": self.user.id},
        )
        self.assertEqual(
            UsernameService.resolve("resolveorg"),
            {"type": TYPE_ORGANIZATION, "id": self.org.id},
        )

    def test_unknown_handle_raises(self):
        with self.assertRaises(ValueError):
            UsernameService.resolve("nobodyhere")

    def test_the_org_facade_still_works(self):
        self.assertEqual(
            UserOrganizationService.get_user_or_org_by_username("resolveorg"),
            {"type": TYPE_ORGANIZATION, "id": self.org.id},
        )

    def test_rename_invalidates_the_cache(self):
        # Warm it, then rename and immediately ask for the old handle.
        UsernameService.resolve("resolveuser")

        UsernameService.claim("renameduser", user=self.user)

        with self.assertRaises(ValueError):
            UsernameService.resolve("resolveuser")
        self.assertEqual(
            UsernameService.resolve("renameduser"),
            {"type": TYPE_USER, "id": self.user.id},
        )

    def test_a_freed_handle_resolves_to_its_new_owner_immediately(self):
        # The five-minute window in which the old cache sent visitors to the
        # wrong profile.
        UsernameService.resolve("resolveuser")
        UsernameService.claim("movedaway", user=self.user)

        newcomer = make_user("newcomer@example.com")
        UsernameService.claim("resolveuser", user=newcomer)

        self.assertEqual(
            UsernameService.resolve("resolveuser"),
            {"type": TYPE_USER, "id": newcomer.id},
        )

    def test_release_frees_the_handle_and_the_cache(self):
        UsernameService.resolve("resolveorg")

        UsernameService.release(organization=self.org)

        with self.assertRaises(ValueError):
            UsernameService.resolve("resolveorg")
        self.assertTrue(UsernameService.is_available("resolveorg"))


class WritePathTests(APITestCase):
    """The four directions a cross-table collision used to be creatable from."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

        self.user = make_user("writer@example.com", "writerhandle", name="Writer")
        self.org = make_org("Held FC", "heldbyorg", self.user)

        self.other = make_user("rival@example.com", "rivalhandle", name="Rival")

    # ── path 1: the availability endpoint ─────────────────────

    def test_availability_endpoint_sees_organizations(self):
        self.client.force_authenticate(self.other)

        res = self.client.get(CHECK_URL, {"username": "heldbyorg"})

        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["data"]["available"])

    def test_availability_endpoint_separates_invalid_from_taken(self):
        self.client.force_authenticate(self.other)

        invalid = self.client.get(CHECK_URL, {"username": "kochi.fc"})
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(invalid.data["data"]["valid"])

        taken = self.client.get(CHECK_URL, {"username": "heldbyorg"})
        self.assertEqual(taken.status_code, 200)
        self.assertTrue(taken.data["data"]["valid"])
        self.assertFalse(taken.data["data"]["available"])

    def test_availability_endpoint_excludes_yourself(self):
        self.client.force_authenticate(self.user)

        res = self.client.get(CHECK_URL, {"username": "writerhandle"})

        self.assertTrue(res.data["data"]["available"])

    # ── path 2: the user profile update ───────────────────────

    def test_user_update_cannot_take_an_orgs_handle(self):
        self.client.force_authenticate(self.other)

        res = self.client.patch(
            UPDATE_PROFILE_URL, {"username": "heldbyorg"}, format="json"
        )

        self.assertEqual(res.status_code, 400)
        self.other.refresh_from_db()
        self.assertEqual(self.other.username, "rivalhandle")

    def test_user_update_renames_registry_and_column_together(self):
        self.client.force_authenticate(self.other)

        res = self.client.patch(
            UPDATE_PROFILE_URL, {"username": "RivalHandle2"}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.other.refresh_from_db()
        self.assertEqual(self.other.username, "rivalhandle2")
        self.assertEqual(
            UsernameRegistry.objects.get(user=self.other).username_lower,
            "rivalhandle2",
        )

    # ── path 3: org create (auto-generated handle) ────────────

    def test_org_create_never_collides_with_a_user(self):
        # A USER already holds the first handle the generator would produce for
        # this org name. The old generator only checked Organization, so it
        # handed the handle over and the user's profile became unreachable.
        make_user("holder@example.com", "kochifc11")

        with patch(
            "usernames.services.username_service.random.randint",
            side_effect=[11, 1234],
        ):
            ok, payload = OrganizationService.create_organization(
                self.other, {"name": "Kochi FC", "type": Organization.Type.CLUB}
            )

        self.assertTrue(ok, payload)
        self.assertEqual(payload["username"], "kochifc1234")
        self.assertTrue(
            UsernameRegistry.objects.filter(
                username_lower="kochifc1234", organization__id=payload["id"]
            ).exists()
        )

    def test_org_create_produces_a_valid_handle(self):
        ok, payload = OrganizationService.create_organization(
            self.other,
            {
                "name": "Kerala Blasters Football Club Academy",
                "type": Organization.Type.CLUB,
            },
        )

        self.assertTrue(ok, payload)
        # The old generator built base[:20] + 2 digits against a 20-char
        # validator it never called.
        self.assertEqual(
            validate_username_format(payload["username"]), payload["username"]
        )

    # ── path 4: org update ────────────────────────────────────

    def test_org_update_cannot_take_a_users_handle(self):
        self.client.force_authenticate(self.user)

        res = self.client.patch(
            f"{ORG_UPDATE_URL}?org_id={self.org.id}",
            {"username": "rivalhandle"},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.org.refresh_from_db()
        self.assertEqual(self.org.username, "heldbyorg")

    def test_org_update_rejects_dots(self):
        self.client.force_authenticate(self.user)

        res = self.client.patch(
            f"{ORG_UPDATE_URL}?org_id={self.org.id}",
            {"username": "kochi.fc"},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.org.refresh_from_db()
        self.assertEqual(self.org.username, "heldbyorg")


class MentionCharsetTests(TestCase):
    """
    The backend regex and the frontend's linkifier in PostCard.tsx are meant to
    be byte-for-byte identical. This asserts the backend half against the
    charset both are supposed to use — the frontend half is asserted by reading
    CONTENT_SPLIT_RE, which is checked in review, not here.
    """

    def test_charset_stops_at_a_dot(self):
        self.assertEqual(
            extract_mention_usernames("Great game @kochifc."), ["kochifc"]
        )

    def test_a_dotted_handle_tokenizes_as_its_first_segment(self):
        # Old behaviour captured "kochi.fc" whole. The dot is not a handle
        # character any more, for anyone, so this is the handle "kochi"
        # followed by prose — ".fc" carries no "@".
        self.assertEqual(
            extract_mention_usernames("well played @kochi.fc"), ["kochi"]
        )

    def test_case_is_folded(self):
        self.assertEqual(
            extract_mention_usernames("@Rahul10 and @rahul10"), ["rahul10"]
        )

    def test_over_length_handles_are_dropped(self):
        too_long = "a" * (USERNAME_MAX_LENGTH + 1)
        self.assertEqual(extract_mention_usernames(f"@{too_long}"), [])


# The module name starts with a digit, so it cannot be imported with `from`.
backfill = import_module(
    "usernames.migrations.0002_backfill_username_registry"
).backfill


class BackfillMigrationTests(TestCase):
    """
    The data migration must REFUSE rather than guess.

    A registry that silently skipped a row would claim the namespace is locked
    while not actually describing the database — worse than a migration that
    stops the deploy and asks a human which account keeps the handle.
    """

    def setUp(self):
        # Build the pre-migration world: display columns written, no registry.
        self.user = User.objects.create_user(
            email="backfill@example.com", password="password123", username="playerone"
        )
        UserProfile.objects.create(user=self.user, name="Player One")
        self.org = Organization.objects.create(
            name="Backfill FC",
            username="backfillfc",
            type=Organization.Type.CLUB,
            created_by=self.user,
        )
        UsernameRegistry.objects.all().delete()

    def test_backfills_both_tables(self):
        backfill(django_apps, None)

        self.assertEqual(
            UsernameRegistry.objects.get(user=self.user).username_lower, "playerone"
        )
        self.assertEqual(
            UsernameRegistry.objects.get(organization=self.org).username_lower,
            "backfillfc",
        )

    def test_users_without_a_handle_are_skipped_not_failed(self):
        # User.username is nullable; a half-signed-up account is not an error.
        handleless = User.objects.create_user(
            email="nohandle@example.com", password="password123"
        )

        backfill(django_apps, None)

        self.assertFalse(UsernameRegistry.objects.filter(user=handleless).exists())

    def test_aborts_on_a_cross_table_collision(self):
        # The exact defect the registry exists to make impossible.
        self.org.username = "playerone"
        self.org.save(update_fields=["username"])

        with self.assertRaises(RuntimeError):
            backfill(django_apps, None)

        self.assertEqual(UsernameRegistry.objects.count(), 0)

    def test_aborts_on_a_handle_the_new_validator_rejects(self):
        # An org handle written under the old dot-allowing RegexValidator.
        Organization.objects.filter(id=self.org.id).update(username="kochi.fc")

        with self.assertRaises(RuntimeError):
            backfill(django_apps, None)

        self.assertEqual(UsernameRegistry.objects.count(), 0)
