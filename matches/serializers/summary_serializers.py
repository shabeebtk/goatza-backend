"""
Shaping for the season summary.

Plain ``Serializer`` classes over the dict ``summary_selectors.match_summary``
returns, not ModelSerializers — there is no model here, the whole payload is
aggregates.

The serializer exists rather than returning the selector's dict straight out
because it PINS THE CONTRACT: the summary sits at the top of the screen people
open most, and a key quietly renamed in a selector should break here, in one
declared place, rather than in a chart that silently stops drawing.
"""

from rest_framework import serializers


class MatchSummaryStatSerializer(serializers.Serializer):
    """
    One stat's season line.

    Three numbers that are deliberately different from each other:

      * ``total``         — the sum. "23 goals".
      * ``entries_count`` — how many matches it was logged in. The denominator
        behind any per-match average, and the honest one: a player who logged
        goals in 8 of 12 matches has an 8-match sample, not a 12-match one.
      * ``zero_count``    — how many of those were logged AS ZERO, which is not
        the same fact as never having logged it.

    ``zero_count`` is what makes clean sheets work with no football anywhere in
    the backend: the client reads it off "Goals conceded". Nothing in this app
    knows what a clean sheet is, and nothing should learn.
    """

    stat_field_id = serializers.UUIDField()
    name = serializers.CharField()
    short_label = serializers.CharField()
    unit = serializers.CharField()
    value_type = serializers.CharField()
    total = serializers.DecimalField(max_digits=12, decimal_places=2)
    entries_count = serializers.IntegerField()
    zero_count = serializers.IntegerField()


class MatchSummarySerializer(serializers.Serializer):
    """
    The whole summary block.

    ``average_rating`` is nullable and that is meaningful: null means nobody has
    rated a single match, which the screen should render as an empty state
    rather than as a zero.

    ``form`` keeps its nulls for the same reason. A player who rated six of
    their last ten matches gets six values and four nulls, oldest first, so the
    chart can draw the gaps honestly instead of pretending to a ten-match line.
    """

    total_matches = serializers.IntegerField()
    wins = serializers.IntegerField()
    losses = serializers.IntegerField()
    draws = serializers.IntegerField()
    minutes_total = serializers.IntegerField()
    average_rating = serializers.FloatField(allow_null=True)
    form = serializers.ListField(
        child=serializers.IntegerField(allow_null=True)
    )
    stats = MatchSummaryStatSerializer(many=True)


class ShowcaseMatchSummarySerializer(MatchSummarySerializer):
    """
    The same block as somebody else sees it, plus the streak.

    The streak rides along here and not on the owner's own summary because it
    is the thing a visiting coach is actually reading for. Totals say what a
    player did; "11 weeks running" says they keep turning up, which is the
    harder claim and the one a recruiter cannot get from a highlight reel.

    Nothing is subtracted for the visitor, because there is nothing personal in
    the payload to subtract — it is entirely aggregates. The individual entries,
    which carry opponents, notes and photos, are not reachable from here at all.
    """

    username = serializers.CharField()
    current_streak_weeks = serializers.IntegerField()
    longest_streak_weeks = serializers.IntegerField()
    is_owner = serializers.BooleanField()
