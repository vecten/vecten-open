"""The password links this machine's `user add` calls leave owed, read back by `next`.

Emails are kept stripped and lowercased, so an admin link and a later `user add` for the
same address in another case name one person.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import RECORDS_DIR, read_record, write_record


def record_name(env: str) -> str:
    return f"password-links/{env}"


def _key(email: str) -> str:
    return email.strip().lower()


def read_links(root: Path, env: str) -> list[dict[str, Any]]:
    record = read_record(root, record_name(env))
    if record is None:
        return []
    links = record.get("links") if isinstance(record, dict) else None
    if not isinstance(links, list) or not all(_is_entry(link) for link in links):
        path = root / RECORDS_DIR / f"{record_name(env)}.json"
        raise Failure(
            ExitCode.CONFIG,
            f"{path}: corrupt run record: expected links of id, email and issued",
        )
    return list(links)


def _is_entry(link: Any) -> bool:
    return isinstance(link, dict) and {"id", "email", "issued"} <= link.keys()


def _write(root: Path, env: str, links: list[dict[str, Any]]) -> None:
    write_record(root, record_name(env), {"links": links})


def owed(root: Path, env: str) -> list[dict[str, Any]]:
    return [link for link in read_links(root, env) if link["id"] and not link["issued"]]


def added(root: Path, env: str, principal_id: str, email: str) -> None:
    links = read_links(root, env)
    email = _key(email)
    for index, link in enumerate(links):
        if link["email"] == email:
            # A different id is a new person with no password yet.
            kept = link["issued"] and link["id"] in (None, principal_id)
            links[index] = {"id": principal_id, "email": email, "issued": kept}
            break
    else:
        links.append({"id": principal_id, "email": email, "issued": False})
    _write(root, env, links)


def issued(root: Path, env: str, principal_id: str) -> None:
    links = read_links(root, env)
    for link in links:
        if link["id"] == principal_id:
            link["issued"] = True
            _write(root, env, links)
            return


def admin_link_issued(root: Path, env: str, email: str) -> None:
    links = read_links(root, env)
    email = _key(email)
    for link in links:
        if link["email"] == email:
            link["issued"] = True
            break
    else:
        links.append({"id": None, "email": email, "issued": True})
    _write(root, env, links)


def dropped(root: Path, env: str, principal_id: str) -> None:
    links = read_links(root, env)
    kept = [link for link in links if link["id"] != principal_id]
    if len(kept) != len(links):
        _write(root, env, kept)
