import string, random

def generate_random_password():
    return "".join(random.choices(string.ascii_letters + string.digits, k=12))
                        