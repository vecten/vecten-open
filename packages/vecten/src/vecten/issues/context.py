"""What the adapters read through: Airflow's task instances, the artifact store and logs, the
admin API, and the secret values evidence must never carry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from vecten_shared.config import VectenConfig

# Airflow keeps task logs this long by default, so an older failure has no evidence left.
WINDOW = timedelta(days=14)


class SourceUnavailable(Exception):
    """A source could not be read; the message says what the person does about it."""


@dataclass(frozen=True)
class TaskAttempt:
    """The final attempt of one task instance, as Airflow's task-instance listing reports it."""

    dag_id: str
    task_id: str
    run_id: str
    map_index: int
    rendered_map_index: str | None
    try_number: int
    state: str | None
    run_after: datetime

    @classmethod
    def from_api(cls, doc: dict[str, Any]) -> TaskAttempt:
        when = doc.get("run_after") or doc.get("start_date") or doc.get("end_date")
        return cls(
            dag_id=doc["dag_id"],
            task_id=doc["task_id"],
            run_id=doc["dag_run_id"],
            map_index=int(doc.get("map_index", -1)),
            rendered_map_index=doc.get("rendered_map_index"),
            try_number=int(doc.get("try_number") or 1),
            state=doc.get("state"),
            run_after=parse_time(when) if when else datetime.min.replace(tzinfo=UTC),
        )


class TaskSource(Protocol):
    def task_instances(self, since: datetime) -> list[TaskAttempt]: ...


class ArtifactReader(Protocol):
    def read(self, key: str) -> bytes | None: ...

    def location(self, key: str) -> str: ...


class LogReader(Protocol):
    def read(self, attempt: TaskAttempt) -> str | None: ...

    def location(self, attempt: TaskAttempt) -> str: ...


def parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def iso(value: datetime | float | int | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_time(value)
    elif not isinstance(value, datetime):
        value = datetime.fromtimestamp(value, UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_key(attempt: TaskAttempt) -> str:
    """Airflow's default `log_filename_template`, which remote logging writes under its folder."""
    mapped = f"map_index={attempt.map_index}/" if attempt.map_index >= 0 else ""
    return (
        f"dag_id={attempt.dag_id}/run_id={attempt.run_id}/task_id={attempt.task_id}/"
        f"{mapped}attempt={attempt.try_number}.log"
    )


@dataclass(frozen=True)
class PrefixedReader:
    """The objects under `prefix` of a store, read by their keys below it."""

    store: ArtifactReader
    prefix: str

    def read(self, key: str) -> bytes | None:
        return self.store.read(self.prefix + key)

    def location(self, key: str) -> str:
        return self.store.location(self.prefix + key)


@dataclass(frozen=True)
class BucketLogs:
    """Task logs that remote logging wrote under `prefix` in the artifact store."""

    store: ArtifactReader
    prefix: str

    def read(self, attempt: TaskAttempt) -> str | None:
        body = self.store.read(self.prefix + log_key(attempt))
        return body.decode("utf-8", errors="replace") if body is not None else None

    def location(self, attempt: TaskAttempt) -> str:
        return self.store.location(self.prefix + log_key(attempt))


@dataclass
class DirectoryArtifacts:
    """The laptop stack's artifacts, bind-mounted from the project's `.vecten/artifacts`."""

    root: Path

    def read(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.is_file() else None

    def location(self, key: str) -> str:
        return str(self.root / key)


@dataclass
class IssueContext:
    root: Path
    config: VectenConfig
    env: str
    airflow: TaskSource | None
    artifacts: ArtifactReader | None
    logs: LogReader | None
    admin: Callable[[str, dict[str, str]], Any] | None
    secrets: tuple[str, ...]
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    _attempts: list[TaskAttempt] | None = field(default=None, init=False, repr=False)

    def attempts(self) -> list[TaskAttempt]:
        """Every final attempt in the window, read once for every adapter that needs them."""
        if self._attempts is None:
            if self.airflow is None:
                raise SourceUnavailable("Airflow is not reachable from here")
            since = self.now - WINDOW
            self._attempts = [a for a in self.airflow.task_instances(since) if a.run_after >= since]
        return self._attempts
