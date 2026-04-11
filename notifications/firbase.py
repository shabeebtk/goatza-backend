
import firebase_admin
from firebase_admin import credentials

cred = credentials.Certificate("core/config/firebase-service-account.json")
firebase_admin.initialize_app(cred)



