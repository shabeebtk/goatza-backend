class CacheKeys:
    @staticmethod
    def user_details(username, list_type="mini"):
        return f"user:{username}:list_type:{list_type}"

    @staticmethod
    def email_otp(email):
        return f"otp:{email}"

    @staticmethod
    def google_state(state):
        return f"google:state:{state}"
    

    @staticmethod
    def sports_list():
        return f"sports:list"