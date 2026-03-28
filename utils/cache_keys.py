class CacheKeys:
    @staticmethod
    def user_details(user_id, detail="mini"):
        return f"user:{user_id}:detail:{detail}"

    @staticmethod
    def email_otp(email):
        return f"otp:{email}"

    @staticmethod
    def google_state(state):
        return f"google:state:{state}"
    

    @staticmethod
    def sports_list():
        return f"sports:list"