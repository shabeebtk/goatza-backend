'''

feed system

Following posts (HIGH priority)
Interest-based posts (sports, role)
Location-based posts
Trending posts
New user exploration posts



🏠 Home Feed (Main Feed)

Hybrid feed (IMPORTANT)

Includes:

Followed users posts
Same sport posts
Nearby players
Trending posts



🔥 Explore Feed
For discovery

Trending posts
Viral content
Popular players
New creators




feed flow ----
Request
  ↓
Parse seen_ids
  ↓
FeedService
  ↓
  Get following
  ↓
  Get user sports
  ↓
  Filter posts
  ↓
  Remove seen posts
  ↓
  Apply scoring
  ↓
  Sort
  ↓
Pagination (cursor)
  ↓
Diversify posts
  ↓
Fetch reactions
  ↓
Serialize
  ↓
Response
'''