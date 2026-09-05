"""When an organization is allowed to be emailed about new applicants.

The decision is a pure function of four inputs so it can be reasoned about and
tested without a database, a clock, or an email backend. Everything that reads
rows or sends mail lives in ApplicationService.apply; this module only answers
"is the gap up yet".

The rule, in one line: find the tier this recruitment's total application count
falls into, and require that many seconds since the LAST ALERT EMAIL.

Measuring from the last email rather than the last application is the whole
design. A steady trickle of applicants would keep re-arming a
since-last-application timer and either alert on every single one or, past the
first tier, never alert at all. Measuring from the email gives a hard ceiling
on mail volume while guaranteeing that every quiet application still shows up
in the next alert's "and N more" — nothing is dropped, only batched.
"""


def should_send_applicant_alert(*, total_count, last_alert_at, now, tiers):
    """Return True if an applicant alert may be sent right now.

    `tiers` is settings.APPLICANT_ALERT_TIERS: (max_total, min_seconds) pairs
    read top-down, the first matching bound winning, `None` meaning unbounded.
    `total_count` includes the application that just arrived.

    A recruitment that has never been alerted (`last_alert_at is None`) always
    passes — the first applicant is the one the org most wants to hear about.
    """
    required_gap = _required_gap_seconds(total_count, tiers)

    if required_gap is None:
        return False

    if last_alert_at is None:
        return True

    return (now - last_alert_at).total_seconds() >= required_gap


def _required_gap_seconds(total_count, tiers):
    """Seconds required between alerts at this count, or None if no tier fits.

    No tier fitting means the table is misconfigured (every bound exceeded, no
    unbounded catch-all). Refusing to send is the safe reading: a config typo
    should cost a missed notification, not an uncapped mail loop.
    """
    for max_total, min_seconds in tiers:
        if max_total is None or total_count <= max_total:
            return min_seconds

    return None
