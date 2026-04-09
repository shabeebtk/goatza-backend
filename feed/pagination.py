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