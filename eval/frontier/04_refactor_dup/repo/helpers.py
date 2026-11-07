"""Helper functions."""


def validate_email(email):
    """Return True if email looks valid."""
    if not email or "@" not in email:
        return False
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return False
    return True


def validate_username(username):
    """Return True if username looks valid."""
    if not username or len(username) < 3:
        return False
    if not username.isalnum():
        return False
    return True
