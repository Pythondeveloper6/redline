"""Helpers for explicit synchronous user-identifier resolvers."""


def from_attr(path):
    """Read a dotted attribute path, e.g. user.pk or state.user_id; return only its id."""
    parts = path.split(".")
    if any(not part.isidentifier() or part.startswith("_") for part in parts):
        raise ValueError("Use public attribute names")

    def resolve(request):
        value = request
        for part in parts:
            value = getattr(value, part, None)
        return value

    return resolve
