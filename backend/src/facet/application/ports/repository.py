"""Persistence ports.

Documents are plain text, so the default adapter is the filesystem and the
"clone this project onto another station" story is a file copy. The port exists
so that a database-backed or object-store adapter can replace it without the
application layer noticing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from facet.domain.document import Document


@dataclass(frozen=True)
class ProjectSummary:
    """Enough to render a project list without loading every document."""

    id: str
    name: str
    updated_at: str = ""
    feature_count: int = 0
    parameter_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "updatedAt": self.updated_at,
            "featureCount": self.feature_count,
            "parameterCount": self.parameter_count,
        }


class ProjectNotFound(Exception):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"no project '{project_id}'")
        self.project_id = project_id


class ProjectExists(Exception):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"project '{project_id}' already exists")
        self.project_id = project_id


@runtime_checkable
class DocumentRepository(Protocol):
    def list_projects(self) -> Sequence[ProjectSummary]:
        ...

    def exists(self, project_id: str) -> bool:
        ...

    def load(self, project_id: str) -> Document:
        ...

    def save(self, project_id: str, document: Document) -> ProjectSummary:
        ...

    def create(self, project_id: str, document: Document) -> ProjectSummary:
        ...

    def delete(self, project_id: str) -> None:
        ...
