import base64
from rest_framework.exceptions import NotFound


class PostSearchCursorPagination:
    """
    Keyset (cursor) pagination for the posts search endpoint. Modeled on
    ``feed.pagination.FeedCursorPagination`` but keyed purely on ``id``.

    Search results are ordered by ``-id`` and PKs are UUIDv7 (time-sortable per
    CLAUDE.md), so the single-column ``id`` keyset is a total order — no score
    tie-break column is needed. The base64 cursor carries the last row's ``id``;
    the next page filters ``id__lt=last_id``.
    """
    page_size = 15

    def paginate_queryset(self, queryset, request):
        cursor = request.query_params.get("cursor")

        if cursor:
            try:
                last_id = base64.b64decode(cursor.encode()).decode()
                queryset = queryset.filter(id__lt=last_id)
            except Exception:
                raise NotFound("Invalid cursor")

        self.page = list(queryset[: self.page_size])
        return self.page

    def get_next_cursor(self):
        # A short page means the ordered set is exhausted — no further cursor.
        if not self.page or len(self.page) < self.page_size:
            return None

        last = self.page[-1]
        return base64.b64encode(str(last.id).encode()).decode()
