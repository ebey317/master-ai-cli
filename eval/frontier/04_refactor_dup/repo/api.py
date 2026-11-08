"""API entry point."""

from helpers import validate_email, validate_username


def register(email, username):
    return validate_email(email) and validate_username(username)
