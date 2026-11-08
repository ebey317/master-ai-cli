"""API entry point."""

from validators import is_email_ok as validate_email
from validators import is_username_ok as validate_username


def register(email, username):
    return validate_email(email) and validate_username(username)
