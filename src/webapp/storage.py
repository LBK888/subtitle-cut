"""Web 应用的数据存储抽象。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Snapshot:
    project_id: int
    version: int
    kind: str
    payload: Dict[str, Any]
    created_at: str


@dataclass
class ProjectFile:
    id: int
    project_id: int
    name: str
    payload: Dict[str, Any]
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "payload": self.payload,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProjectStorage:
    """面向 Web UI 的 SQLite 存储封装。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    # 基础设施
    # ---------------------------------------------------------------------
    def initialize(self) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_project_kind
                    ON snapshots(project_id, kind, version DESC)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS project_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_project_files_project
                    ON project_files(project_id, updated_at DESC)
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, detect_types=sqlite3.PARSE_DECLTYPES)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # 项目管理
    # ------------------------------------------------------------------
    def list_projects(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT id, name, created_at, updated_at FROM projects ORDER BY updated_at DESC"
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?",
                (project_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_project(self, name: str, transcript: Dict[str, Any]) -> Dict[str, Any]:
        created_at = self._utc_now()
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO projects (name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, created_at, created_at),
            )
            project_id = cursor.lastrowid
            self._insert_snapshot(cursor, project_id, 1, "transcript", transcript)
            default_payload = {
                "delete_ranges": [],
                "metadata": {"label": "默认工程文件"},
            }
            cursor.execute(
                """
                INSERT INTO project_files (project_id, name, payload, revision, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    project_id,
                    "默认工程文件",
                    json.dumps(default_payload, ensure_ascii=False),
                    created_at,
                    created_at,
                ),
            )
            default_file_id = cursor.lastrowid
            connection.commit()
        return {
            "id": project_id,
            "version": 1,
            "name": name,
            "created_at": created_at,
            "default_project_file": {
                "id": default_file_id,
                "project_id": project_id,
                "name": "默认工程文件",
                "payload": default_payload,
                "revision": 1,
                "created_at": created_at,
                "updated_at": created_at,
            },
        }

    def save_transcript(self, project_id: int, transcript: Dict[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.cursor()
            latest_version = self._latest_version(cursor, project_id, "transcript")
            next_version = latest_version + 1
            self._insert_snapshot(cursor, project_id, next_version, "transcript", transcript)
            cursor.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (self._utc_now(), project_id),
            )
            connection.commit()
            return next_version

    def save_selection(self, project_id: int, selection: Dict[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.cursor()
            latest_version = self._latest_version(cursor, project_id, "selection")
            next_version = latest_version + 1
            self._insert_snapshot(cursor, project_id, next_version, "selection", selection)
            cursor.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (self._utc_now(), project_id),
            )
            connection.commit()
            return next_version

    def save_metadata(self, project_id: int, metadata: Dict[str, Any]) -> int:
        return self.save_snapshot(project_id, "metadata", metadata)

    def save_snapshot(self, project_id: int, kind: str, payload: Dict[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.cursor()
            latest_version = self._latest_version(cursor, project_id, kind)
            next_version = latest_version + 1
            self._insert_snapshot(cursor, project_id, next_version, kind, payload)
            cursor.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (self._utc_now(), project_id),
            )
            connection.commit()
            return next_version

    def list_snapshots(self, project_id: int, kind: str) -> List[Snapshot]:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT project_id, version, kind, payload, created_at
                FROM snapshots
                WHERE project_id = ? AND kind = ?
                ORDER BY version DESC
                """,
                (project_id, kind),
            )
            rows = cursor.fetchall()
        return [
            Snapshot(
                project_id=row["project_id"],
                version=row["version"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def latest_snapshot(self, project_id: int, kind: str) -> Optional[Snapshot]:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT project_id, version, kind, payload, created_at
                FROM snapshots
                WHERE project_id = ? AND kind = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (project_id, kind),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Snapshot(
                project_id=row["project_id"],
                version=row["version"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )

    def get_snapshot(
        self,
        project_id: int,
        kind: str,
        version: Optional[int] = None,
    ) -> Optional[Snapshot]:
        with self._connect() as connection:
            cursor = connection.cursor()
            if version is None:
                return self.latest_snapshot(project_id, kind)
            cursor.execute(
                """
                SELECT project_id, version, kind, payload, created_at
                FROM snapshots
                WHERE project_id = ? AND kind = ? AND version = ?
                LIMIT 1
                """,
                (project_id, kind, version),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Snapshot(
                project_id=row["project_id"],
                version=row["version"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )

    def get_metadata(self, project_id: int) -> Optional[Dict[str, Any]]:
        snapshot = self.get_snapshot(project_id, "metadata")
        return snapshot.payload if snapshot else None

    def delete_project(self, project_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute("DELETE FROM snapshots WHERE project_id = ?", (project_id,))
            cursor.execute("DELETE FROM project_files WHERE project_id = ?", (project_id,))
            cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            connection.commit()

    # ------------------------------------------------------------------
    # 工程文件管理
    # ------------------------------------------------------------------
    def list_project_files(self, project_id: int) -> List[ProjectFile]:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, project_id, name, payload, revision, created_at, updated_at
                FROM project_files
                WHERE project_id = ?
                ORDER BY updated_at DESC
                """,
                (project_id,),
            )
            rows = cursor.fetchall()
            return [
                ProjectFile(
                    id=row["id"],
                    project_id=row["project_id"],
                    name=row["name"],
                    payload=json.loads(row["payload"]),
                    revision=row["revision"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def get_project_file(self, project_file_id: int) -> Optional[ProjectFile]:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, project_id, name, payload, revision, created_at, updated_at
                FROM project_files
                WHERE id = ?
                """,
                (project_file_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return ProjectFile(
                id=row["id"],
                project_id=row["project_id"],
                name=row["name"],
                payload=json.loads(row["payload"]),
                revision=row["revision"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def create_project_file(self, project_id: int, name: str, payload: Dict[str, Any]) -> ProjectFile:
        created_at = self._utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO project_files (project_id, name, payload, revision, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (project_id, name, payload_json, created_at, created_at),
            )
            project_file_id = cursor.lastrowid
            cursor.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (created_at, project_id),
            )
            connection.commit()
        return self.get_project_file(project_file_id)

    def update_project_file(
        self,
        project_file_id: int,
        payload: Dict[str, Any],
        *,
        name: Optional[str] = None,
    ) -> Optional[ProjectFile]:
        now = self._utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._connect() as connection:
            cursor = connection.cursor()
            if name is not None:
                cursor.execute(
                    """
                    UPDATE project_files
                    SET payload = ?, revision = revision + 1, updated_at = ?, name = ?
                    WHERE id = ?
                    """,
                    (payload_json, now, name, project_file_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE project_files
                    SET payload = ?, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (payload_json, now, project_file_id),
                )
            if cursor.rowcount == 0:
                return None
            cursor.execute(
                """
                SELECT project_id FROM project_files WHERE id = ?
                """,
                (project_file_id,),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?",
                    (now, row["project_id"]),
                )
            connection.commit()
        return self.get_project_file(project_file_id)


    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _insert_snapshot(
        self,
        cursor: sqlite3.Cursor,
        project_id: int,
        version: int,
        kind: str,
        payload: Dict[str, Any],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO snapshots (project_id, version, kind, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                project_id,
                version,
                kind,
                json.dumps(payload, ensure_ascii=False),
                self._utc_now(),
            ),
        )

    def _latest_version(self, cursor: sqlite3.Cursor, project_id: int, kind: str) -> int:
        cursor.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS latest
            FROM snapshots
            WHERE project_id = ? AND kind = ?
            """,
            (project_id, kind),
        )
        row = cursor.fetchone()
        return int(row["latest"]) if row else 0
