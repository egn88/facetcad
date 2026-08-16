"""Filesystem document repository — one YAML file per project.

YAML rather than JSON because these files are meant to be read and edited by
hand and reviewed in a diff. Writes are atomic (write to a temporary file, then
rename) so an interrupted save cannot leave a half-written project behind.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

from facet.application.ports.repository import (
    ProjectExists,
    ProjectNotFound,
    ProjectSummary,
)
from facet.domain.document import Document
from facet.domain.errors import DocumentError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class FilesystemDocumentRepository:
    """Stores documents as ``<root>/<project_id>.yaml``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # -- queries -----------------------------------------------------------

    def list_projects(self) -> Sequence[ProjectSummary]:
        summaries: list[ProjectSummary] = []
        for path in sorted(self._root.glob("*.yaml")):
            try:
                summaries.append(self._summarise(path.stem, self._read(path), path))
            except (DocumentError, yaml.YAMLError):
                # A corrupt file must not hide the healthy ones from the list.
                summaries.append(
                    ProjectSummary(id=path.stem, name=f"{path.stem} (unreadable)")
                )
        return summaries

    def exists(self, project_id: str) -> bool:
        return self._path(project_id).exists()

    def load(self, project_id: str) -> Document:
        path = self._path(project_id)
        if not path.exists():
            raise ProjectNotFound(project_id)
        return self._read(path)

    # -- mutations ---------------------------------------------------------

    def create(self, project_id: str, document: Document) -> ProjectSummary:
        path = self._path(project_id)
        if path.exists():
            raise ProjectExists(project_id)
        return self._write(project_id, document, path)

    def save(self, project_id: str, document: Document) -> ProjectSummary:
        path = self._path(project_id)
        if not path.exists():
            raise ProjectNotFound(project_id)
        return self._write(project_id, document, path)

    def delete(self, project_id: str) -> None:
        path = self._path(project_id)
        if not path.exists():
            raise ProjectNotFound(project_id)
        path.unlink()

    # -- internals ---------------------------------------------------------

    def _path(self, project_id: str) -> Path:
        if not _SAFE_ID.match(project_id):
            raise DocumentError(
                reason=(
                    f"project id {project_id!r} must be 1-64 characters of letters, "
                    "digits, hyphen or underscore"
                )
            )
        return self._root / f"{project_id}.yaml"

    def _read(self, path: Path) -> Document:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Document.from_dict(raw)

    def _write(self, project_id: str, document: Document, path: Path) -> ProjectSummary:
        payload = yaml.safe_dump(
            document.to_dict(), sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        # Atomic replace: a crash mid-write leaves the previous version intact.
        handle, temporary = tempfile.mkstemp(dir=str(self._root), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return self._summarise(project_id, document, path)

    def _summarise(self, project_id: str, document: Document, path: Path) -> ProjectSummary:
        stamp = dt.datetime.fromtimestamp(
            path.stat().st_mtime, tz=dt.UTC
        ).isoformat(timespec="seconds")
        return ProjectSummary(
            id=project_id,
            name=document.name,
            updated_at=stamp,
            feature_count=len(document.features),
            parameter_count=len(document.parameters),
        )


def yaml_text(document: Document) -> str:
    """The document's canonical YAML, used by the export and download paths."""
    return yaml.safe_dump(
        document.to_dict(), sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def document_from_yaml(text: str) -> Document:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise DocumentError(reason=f"invalid YAML: {exc}") from exc
    return Document.from_dict(raw)
