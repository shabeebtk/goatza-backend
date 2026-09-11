"""
The public reference on a problem report — ``GZ-XXXXXX``.

WHAT IT IS FOR, AND THE ONE THING IT MUST NEVER BECOME
------------------------------------------------------
A reference exists for CORRESPONDENCE: quoting the report in a reply, and a
reporter writing back with "any update on GZ-7K4M2P". That is the whole job.

It must never become a lookup URL — no ``/support/GZ-7K4M2P``, public or
otherwise. Six characters out of a 32-symbol alphabet is a code somebody reads
off a screen and types into an email, which means it is also a code somebody
can guess, and anything a guessed code opens is effectively published. What
sits behind this one is a description of a bug in somebody's own words, their
screenshots, their contact email, their IP and their user agent.

Same reasoning as the waitlist ``ref_code`` note in ``core/public_urls.py``:
there the card endpoint is an allow-list of five harmless fields precisely
because the code is short, public and screenshotted. Nothing here is harmless,
so the answer is not a smaller payload — it is no public route at all.

THE ALPHABET
------------
32 characters: A-Z and 2-9, minus ``0``/``O`` and ``1``/``I``. People
transcribe these by eye from a confirmation screen into an email client, and
those four are the pairs that get transcribed wrong. Dropping them costs a
little entropy (32^6 is still ~1.07 billion codes) and buys back every support
thread that would otherwise start by working out which character was meant.

``secrets``, not ``random``: the value is a handle on somebody's private report
and a predictable sequence would let one be guessed from another.

COLLISIONS
----------
Deliberately NOT resolved here with a "does it exist yet?" pre-check loop. That
gap between the SELECT and the INSERT is exactly the race two concurrent
reports would win. The caller inserts and retries on IntegrityError, and the
unique constraint on ``ProblemReport.reference`` is the arbiter — the same
reasoning as ``UsernameService.claim``.
"""

import secrets

# No 0/O, no 1/I — see the module docstring.
REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# "GZ-7K4M2P" is 9 characters, inside ``reference``'s 12 with room to spare.
REFERENCE_PREFIX = "GZ-"
REFERENCE_LENGTH = 6


def generate_reference() -> str:
    """One candidate reference. Uniqueness is the database's call, not ours."""
    body = "".join(
        secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH)
    )

    return f"{REFERENCE_PREFIX}{body}"
