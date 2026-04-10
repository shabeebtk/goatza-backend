from rest_framework.pagination import CursorPagination


class NotificationCursorPagination(CursorPagination):
    page_size = 20
    ordering = "-created_at"

    def get_next_cursor(self):
        if not self.cursor:
            return None
        return self.encode_cursor(self.cursor)