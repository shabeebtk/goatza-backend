from usernames.services.username_service import UsernameService


class UserOrganizationService:

    def get_user_or_org_by_username(username):
        """
        Resolves a username to either a User or Organization.
        Returns:
            {
                "type": "user" | "organization",
                "id": UUID
            }
        Raises:
            ValueError if not found

        Thin wrapper kept for its existing call sites (connections, messaging,
        posts, recruitments). The lookup itself is UsernameService.resolve: one
        indexed query on the shared namespace instead of the old User-then-
        Organization fallback, which silently made an org unreachable whenever
        a user held the same handle.
        """
        return UsernameService.resolve(username)
