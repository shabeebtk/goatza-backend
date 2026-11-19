"""
The one refusal type for enquiries: a stable code the client switches on and a
human sentence it can show.

The codes are a contract with the frontend. Add one; never rename one.

    not_found, forbidden, switch_to_org, switch_to_personal, invalid,
    guardian_needed, too_many_enquiries, throttled

``not_found`` on the public form is ONE answer for every miss — an unknown or
inactive or suspended org, a form that is switched off, a block — and never
says which. ``forbidden`` and ``throttled`` come from the views' error mixin.
"""

from apps.orgpages.errors import PageError

# The same class as the page's refusals, so one error mixin renders both.
EnquiryError = PageError

FORM_NOT_FOUND_MESSAGE = "This page wasn't found."
ENQUIRY_NOT_FOUND_MESSAGE = "This enquiry wasn't found."
GUARDIAN_NEEDED_MESSAGE = (
    "If you're under 18, ask a parent or guardian to send this for you."
)
TOO_MANY_MESSAGE = "You've already sent a few enquiries today. They'll be in touch."
SWITCH_TO_PERSONAL_MESSAGE = "Switch to your personal account to do this."


def form_not_found():
    return EnquiryError("not_found", FORM_NOT_FOUND_MESSAGE, status_code=404)


def enquiry_not_found():
    return EnquiryError("not_found", ENQUIRY_NOT_FOUND_MESSAGE, status_code=404)


def guardian_needed():
    return EnquiryError("guardian_needed", GUARDIAN_NEEDED_MESSAGE, field="for_whom")


def too_many_enquiries():
    return EnquiryError("too_many_enquiries", TOO_MANY_MESSAGE, field="phone")
