"""The data layer: constraints, the token, the sport config and its seed."""

from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.careers.models import CareerEntry
from apps.sports.models import Sport, SportPosition
from apps.squads.models import Squad, SquadMember, SportSquadConfig
from apps.squads.selectors.config_selectors import squad_config_for
from apps.squads.tests.base import SquadTestBase

Status = SquadMember.Status


class ConstraintTests(SquadTestBase):

    def _row(self, user, status, **fields):
        fields.setdefault("source", SquadMember.Source.INVITE)
        return SquadMember.objects.create(
            squad=self.squad, user=user, status=status, **fields
        )

    def test_one_live_row_per_squad_and_player(self):
        self._row(self.p1, Status.ACTIVE)
        for status in (Status.PENDING, Status.INVITED, Status.ACTIVE):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self._row(self.p1, status)

    def test_history_rows_are_allowed_beside_a_live_one(self):
        self._row(self.p1, Status.LEFT)
        self._row(self.p1, Status.REMOVED)
        self._row(self.p1, Status.MOVED)
        self._row(self.p1, Status.LEFT)
        self._row(self.p1, Status.ACTIVE)

        self.assertEqual(
            SquadMember.objects.filter(squad=self.squad, user=self.p1).count(), 5
        )

    def test_one_active_captain(self):
        self._row(self.p1, Status.ACTIVE, is_captain=True)
        self._row(self.p2, Status.LEFT, is_captain=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(self.p3, Status.ACTIVE, is_captain=True)

    def test_one_active_holder_of_a_jersey_number(self):
        self._row(self.p1, Status.ACTIVE, jersey_number=9)
        self._row(self.p2, Status.LEFT, jersey_number=9)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(self.p3, Status.ACTIVE, jersey_number=9)

        # Another squad of the same club is free to use it.
        other = self.make_squad("Seniors")
        SquadMember.objects.create(
            squad=other, user=self.p3, status=Status.ACTIVE,
            source=SquadMember.Source.INVITE, jersey_number=9,
        )

    def test_jersey_number_is_0_to_99(self):
        self._row(self.p1, Status.ACTIVE, jersey_number=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(self.p2, Status.ACTIVE, jersey_number=100)

    def test_live_squad_names_are_unique_per_org_ignoring_case(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_squad("u17 BOYS")

        Squad.objects.filter(id=self.squad.id).update(
            is_deleted=True, deleted_at=timezone.now()
        )
        self.make_squad("U17 Boys")  # a deleted squad frees its name

    def test_every_squad_gets_its_own_invite_token(self):
        other = self.make_squad("Seniors")
        self.assertEqual(len(self.squad.invite_token), 32)
        self.assertNotEqual(self.squad.invite_token, other.invite_token)

    def test_levels_match_career_squad_levels(self):
        # The career entry a membership creates copies the level verbatim.
        self.assertEqual(Squad.Level.choices, CareerEntry.SquadLevel.choices)


class SportConfigTests(SquadTestBase):

    def test_a_sport_with_no_config_gets_the_permissive_default(self):
        config = squad_config_for(self.cricket)

        self.assertTrue(config["uses_positions"])
        self.assertFalse(config["uses_jersey_numbers"])
        self.assertEqual(config["member_noun_plural"], "players")
        self.assertEqual(config["group_order"], [])
        self.assertEqual(
            config["positions"],
            [{"id": str(self.batsman.id), "name": "Batsman", "group": "Batters"}],
        )

    def test_the_configured_sport(self):
        config = squad_config_for(self.football)
        self.assertTrue(config["uses_jersey_numbers"])
        self.assertEqual(config["group_order"][0], "Goalkeepers")
        self.assertEqual(
            [position["name"] for position in config["positions"]],
            ["Defender", "Goalkeeper", "Striker"],
        )
        self.assertNotIn(
            "positions", squad_config_for(self.football, with_positions=False)
        )


class SeedSquadConfigsTests(SquadTestBase):

    def setUp(self):
        super().setUp()
        SportSquadConfig.objects.all().delete()
        SportPosition.objects.update(group="")
        Sport.objects.filter(name="Cricket").delete()

    def test_seeds_football_and_warns_about_what_is_missing(self):
        out = StringIO()
        call_command("seed_squad_configs", stdout=out)

        config = SportSquadConfig.objects.get(sport=self.football)
        self.assertTrue(config.uses_jersey_numbers)
        self.assertEqual(config.typical_squad_size, 25)
        self.assertEqual(
            config.group_order, ["Goalkeepers", "Defenders", "Midfielders", "Forwards"]
        )

        self.gk.refresh_from_db()
        self.striker.refresh_from_db()
        self.assertEqual(self.gk.group, "Goalkeepers")
        self.assertEqual(self.striker.group, "Forwards")

        # Cricket is not in this database; Midfielder is not a position here.
        self.assertIn("Sport 'Cricket' not found", out.getvalue())
        self.assertIn("'Midfielder' not found", out.getvalue())

    def test_is_idempotent(self):
        call_command("seed_squad_configs", stdout=StringIO())
        call_command("seed_squad_configs", stdout=StringIO())

        self.assertEqual(SportSquadConfig.objects.count(), 1)
        self.assertEqual(SportPosition.objects.filter(group="Forwards").count(), 1)
