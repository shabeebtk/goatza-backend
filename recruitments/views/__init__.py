'''
player side 

GET    /recruitments/
GET    /recruitments/{id}/
POST   /recruitments/{id}/apply/
GET    /me/applications/
GET    /applications/{id}/
POST   /applications/{id}/withdraw/



org 

POST   /organizations/{id}/recruitments/
PATCH  /recruitments/{id}/
POST   /recruitments/{id}/publish/
POST   /recruitments/{id}/close/

GET    /recruitments/{id}/applications/
POST   /applications/{id}/status/




{
  "title": "U17 Open Football Trials",
  "short_description": "Looking for talented attacking players",

  "description": "Official open trials for the upcoming Kerala Youth League season. Players with district-level experience preferred.",

  "recruitment_type": "open_trial",

  "visibility": "public",

  "gender": "male",

  "sport_id": "d8c17e77-4a68-4f6b-92e5-b3a98f11f8a1",

  "min_age": 15,
  "max_age": 17,

  "experience_level": "district",

  "application_deadline": "2026-06-20T12:00:00Z",

  "event_date": "2026-06-22T10:00:00Z",

  "is_remote": false,

  "max_applications": 200,

  "is_paid": true,
  "fee_amount": "300.00",
  "fee_currency": "INR",
  "payment_note": "Payment handled directly by organization after shortlisting.",

  "location": {
    "name": "Kannur Municipal Stadium",
    "city": "Kannur",
    "country_code": "IN",
    "latitude": 11.8745,
    "longitude": 75.3704
  },

  "positions": [
    {
      "position_id": "fd1d8e1c-72d0-45a8-b67c-3d1124b54f21",
      "is_primary": true
    },
    {
      "position_id": "bd85c991-93fc-4d11-9a0e-f5f87e4bc911",
      "is_primary": false
    }
  ],

  "questions": [
    {
      "question": "Preferred foot?",
      "field_type": "radio",
      "is_required": true,
      "options": [
        {
          "value": "Left"
        },
        {
          "value": "Right"
        },
        {
          "value": "Both"
        }
      ]
    },
    {
      "question": "Describe your playing style",
      "field_type": "long_text",
      "is_required": false
    }
  ],

  "media": [
    {
      "file_url": "https://cdn.goatza.com/recruitments/banner1.jpg",
      "public_id": "recruitments/banner1",
      "media_type": "image",
      "order": 0
    }
  ]
}


'''