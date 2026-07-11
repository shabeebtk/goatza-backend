import base64
from rest_framework.exceptions import NotFound
from django.db.models import Q


class FeedCursorPagination:
    page_size = 15

    def paginate_queryset(self, queryset, request):
        cursor = request.query_params.get("cursor")

        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode()).decode()
                last_score, last_id = decoded.split("|")
                last_score = float(last_score)

                queryset = queryset.filter(
                    Q(final_score__lt=last_score) |
                    Q(final_score=last_score, id__lt=last_id)
                )
            except Exception:
                raise NotFound("Invalid cursor")

        self.page = list(queryset[: self.page_size])
        return self.page

    def get_next_cursor(self):
        if not self.page:
            return None

        last = self.page[-1]
        cursor = f"{last.final_score}|{last.id}"
        return base64.b64encode(cursor.encode()).decode()


class ExploreCursorPagination:
    """
    Generic keyset (cursor) pagination for the explore discovery endpoints
    (players / organizations). Modeled on ``FeedCursorPagination``.

    The active mode is baked INTO the cursor so a scroll that started in one
    mode never silently flips to the other mid-way:

        cursor payload = base64("<mode>|<sort_value>|<id>")

        - nearby  → sort_value = distance_km      (ORDER BY distance_km ASC,  id ASC)
        - popular → sort_value = followers_count   (ORDER BY followers_count DESC, id DESC)

    The queryset handed in must already be annotated + ordered by the service
    (``distance_km`` for nearby, ``followers_count`` for popular). The paginator
    only appends the keyset WHERE clause and the slice — it stays agnostic of
    whether the rows are Users or Organizations.
    """
    page_size = 10

    NEARBY = "nearby"
    POPULAR = "popular"

    def decode_mode(self, cursor):
        """Peek the locked mode out of a cursor without paginating."""
        return self._decode(cursor)[0]

    def paginate_queryset(self, queryset, request, mode):
        self.mode = mode
        cursor = request.query_params.get("cursor")

        if cursor:
            cursor_mode, last_value, last_id = self._decode(cursor)
            # The cursor is the source of truth mid-scroll — trust its mode over
            # the caller's so the keyset direction always matches the ordering.
            self.mode = cursor_mode
            queryset = self._apply_keyset(queryset, cursor_mode, last_value, last_id)

        self.page = list(queryset[: self.page_size])
        return self.page

    def _apply_keyset(self, queryset, mode, last_value, last_id):
        if mode == self.NEARBY:
            # distance_km ASC, id ASC
            return queryset.filter(
                Q(distance_km__gt=last_value)
                | Q(distance_km=last_value, id__gt=last_id)
            )
        # POPULAR: followers_count DESC, id DESC
        return queryset.filter(
            Q(followers_count__lt=last_value)
            | Q(followers_count=last_value, id__lt=last_id)
        )

    def get_next_cursor(self):
        # A short page means the ordered set is exhausted — no further cursor.
        if not self.page or len(self.page) < self.page_size:
            return None

        last = self.page[-1]
        if self.mode == self.NEARBY:
            sort_value = last.distance_km
        else:
            sort_value = last.followers_count

        cursor = f"{self.mode}|{sort_value}|{last.id}"
        return base64.b64encode(cursor.encode()).decode()

    def _decode(self, cursor):
        try:
            decoded = base64.b64decode(cursor.encode()).decode()
            mode, raw_value, last_id = decoded.split("|")

            if mode == self.NEARBY:
                last_value = float(raw_value)
            elif mode == self.POPULAR:
                last_value = int(raw_value)
            else:
                raise ValueError("unknown mode")

            return mode, last_value, last_id
        except Exception:
            raise NotFound("Invalid cursor")