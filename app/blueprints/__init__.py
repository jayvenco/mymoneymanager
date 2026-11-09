from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def admin_required(view):
    """Alleen voor de beheerder (na inloggen)."""

    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper
