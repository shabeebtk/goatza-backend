class UsernameTaken(Exception):
    """
    The handle is already registered — to another actor, or to this one under a
    row that lost the race.

    Deliberately NOT a subclass of ValueError: callers have to be able to tell
    "taken" apart from "malformed", because the two produce different UI copy
    and only one of them is worth retrying with a different value.
    """

    def __init__(self, username: str, message: str = "Username already taken"):
        self.username = username
        super().__init__(message)
