"""Package requirements a project may record: a name, extras and a version constraint."""

from packaging.requirements import InvalidRequirement, Requirement

from vecten_shared.output import ExitCode, Failure

REQUIREMENT_RULE = (
    "use a package name with optional extras/version; "
    "URLs, paths, options and markers are not supported"
)


def parse_requirement(text: str) -> Requirement | None:
    """The requirement `text` names, or None when it is not one Vecten records."""
    try:
        parsed = Requirement(text)
    except InvalidRequirement:
        return None
    # A URL or path installs code no index vouches for; a marker would make the locks
    # differ by machine; a newline would smuggle a pip option into a requirements file.
    if parsed.url or parsed.marker or any(c in text for c in "\r\n"):
        return None
    return parsed


def requirement(text: str) -> Requirement:
    parsed = parse_requirement(text)
    if parsed is None:
        raise Failure(ExitCode.USAGE, REQUIREMENT_RULE)
    return parsed
