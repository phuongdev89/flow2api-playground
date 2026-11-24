"""Standalone persistence for Playground sessions, projects and jobs.

Copy từ flow2api src/core/playground_database.py, thêm:
- cột ``api_key`` trên playground_sessions: playground cần key thật để gọi
  flow2api /v1/chat/completions cho job nền (key do user nhập lúc login).
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger("playground.database")


class PlaygroundDatabase:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            data_dir = Path(__file__).resolve().parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "playground.db")
        self.db_path = db_path
        self._ready = False
        self._init_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.db_path, timeout=30) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA busy_timeout = 30000")
            yield connection

    async def _initialize(self):
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            async with self._connect() as db:
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS playground_sessions (
                        token_hash TEXT PRIMARY KEY,
                        owner_hash TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        api_key TEXT,
                        api_url TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_playground_sessions_expiry
                        ON playground_sessions(expires_at);
                    CREATE TABLE IF NOT EXISTS playground_projects (
                        id TEXT PRIMARY KEY,
                        owner_hash TEXT NOT NULL,
                        name TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        prompt TEXT NOT NULL DEFAULT '',
                        results_json TEXT NOT NULL DEFAULT '[]'
                    );
                    CREATE INDEX IF NOT EXISTS idx_playground_projects_owner_activity
                        ON playground_projects(owner_hash, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS playground_jobs (
                        id TEXT PRIMARY KEY,
                        owner_hash TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'running',
                        error TEXT,
                        results_json TEXT NOT NULL DEFAULT '[]',
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_playground_jobs_owner_project
                        ON playground_jobs(owner_hash, project_id, created_at DESC);
                    """
                )
                # Migration từ DB copy từ flow2api cũ: bảng playground_sessions có
                # thể chưa có cột api_key/api_url — thêm nếu thiếu (DB cũ giữ nguyên
                # data; session cũ không có api_url → fallback theo env config).
                cursor = await db.execute("PRAGMA table_info(playground_sessions)")
                columns = {row[1] for row in await cursor.fetchall()}
                if "api_key" not in columns:
                    await db.execute(
                        "ALTER TABLE playground_sessions ADD COLUMN api_key TEXT"
                    )
                if "api_url" not in columns:
                    await db.execute(
                        "ALTER TABLE playground_sessions ADD COLUMN api_url TEXT"
                    )
                # Job lưu params gốc (additive) để nút Retry chạy lại đúng combo —
                # DB cũ thiếu cột thì thêm; job cũ không có params là không retry được.
                cursor = await db.execute("PRAGMA table_info(playground_jobs)")
                job_columns = {row[1] for row in await cursor.fetchall()}
                if "params_json" not in job_columns:
                    await db.execute(
                        "ALTER TABLE playground_jobs ADD COLUMN params_json TEXT"
                    )
                # Job 'running' cũ (quá JOB_ORPHAN_STALE_MS) sau khi server khởi động
                # lại là job mồ côi (background task đã mất) → đánh dấu lỗi để client
                # không poll vô hạn. Job đang chạy gần đây thì giữ nguyên — có thể
                # thuộc instance khác cùng dùng file DB này.
                await db.execute(
                    "UPDATE playground_jobs SET status = 'error', error = ?, updated_at = ? "
                    "WHERE status = 'running' AND updated_at <= ?",
                    (
                        "Server khởi động lại, job bị gián đoạn.",
                        int(time.time() * 1000),
                        int(time.time() * 1000) - self.JOB_ORPHAN_STALE_MS,
                    ),
                )
                await db.commit()
            self._ready = True

    async def create_session(
        self,
        token_hash: str,
        owner_hash: str,
        expires_at: int,
        api_key: str | None = None,
        api_url: str | None = None,
    ):
        await self._initialize()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM playground_sessions WHERE expires_at <= ?", (int(time.time()),)
            )
            await db.execute(
                "INSERT INTO playground_sessions(token_hash, owner_hash, expires_at, api_key, api_url) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, owner_hash, expires_at, api_key, api_url),
            )
            await db.commit()

    async def owner_for_session(self, token_hash: str) -> str | None:
        await self._initialize()
        now = int(time.time())
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT owner_hash FROM playground_sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            )
            row = await cursor.fetchone()
            await db.execute("DELETE FROM playground_sessions WHERE expires_at <= ?", (now,))
            await db.commit()
        return row["owner_hash"] if row else None

    async def api_credentials_for_owner(self, owner_hash: str) -> tuple[str | None, str | None]:
        """(api_key, api_url) của owner (session còn hạn gần nhất) — dùng cho job nền.

        Session cũ trước khi thêm api_url trả api_url=None → caller fallback về
        endpoint mặc định theo env config.
        """
        await self._initialize()
        now = int(time.time())
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT api_key, api_url FROM playground_sessions "
                "WHERE owner_hash = ? AND expires_at > ? AND api_key IS NOT NULL "
                "ORDER BY expires_at DESC LIMIT 1",
                (owner_hash, now),
            )
            row = await cursor.fetchone()
        if row is None:
            return None, None
        return row["api_key"], row["api_url"]

    async def revoke_session(self, token_hash: str):
        await self._initialize()
        async with self._connect() as db:
            await db.execute("DELETE FROM playground_sessions WHERE token_hash = ?", (token_hash,))
            await db.commit()

    @staticmethod
    def _project(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "updated_at": row["updated_at"],
            "prompt": row["prompt"],
            "results": json.loads(row["results_json"]),
        }

    async def list_projects(self, owner_hash: str) -> list[dict]:
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM playground_projects WHERE owner_hash = ? ORDER BY updated_at DESC, id",
                (owner_hash,),
            )
            rows = await cursor.fetchall()
        return [self._project(row) for row in rows]

    async def create_project(
        self, project_id: str, owner_hash: str, name: str, prompt: str, results: list
    ) -> dict:
        await self._initialize()
        now = int(time.time() * 1000)
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO playground_projects(id, owner_hash, name, updated_at, prompt, results_json) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, owner_hash, name, now, prompt, json.dumps(results)),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            row = await cursor.fetchone()
        return self._project(row)

    async def get_project(self, owner_hash: str, project_id: str) -> dict | None:
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            row = await cursor.fetchone()
        return self._project(row) if row else None

    async def update_project(self, owner_hash: str, project_id: str, fields: dict) -> dict | None:
        await self._initialize()
        allowed = {key: value for key, value in fields.items() if key in {"name", "prompt", "results"}}
        if not allowed:
            return await self.get_project(owner_hash, project_id)
        columns = []
        values = []
        for key, value in allowed.items():
            column = "results_json" if key == "results" else key
            columns.append(f"{column} = ?")
            values.append(json.dumps(value) if key == "results" else value)
        columns.append("updated_at = MAX(?, updated_at + 1)")
        values.append(int(time.time() * 1000))
        values.extend((project_id, owner_hash))
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE playground_projects SET {', '.join(columns)} WHERE id = ? AND owner_hash = ?",
                values,
            )
            await db.commit()
            if cursor.rowcount == 0:
                return None
            cursor = await db.execute(
                "SELECT * FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            row = await cursor.fetchone()
        return self._project(row) if row else None

    async def touch_project(self, owner_hash: str, project_id: str) -> dict | None:
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE playground_projects SET updated_at = MAX(?, updated_at + 1) WHERE id = ? AND owner_hash = ?",
                (int(time.time() * 1000), project_id, owner_hash),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return None
            cursor = await db.execute(
                "SELECT * FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            row = await cursor.fetchone()
        return self._project(row) if row else None

    async def delete_project(self, owner_hash: str, project_id: str) -> bool:
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            await db.commit()
        return cursor.rowcount > 0

    # ── Generation jobs ──────────────────────────────────────────────

    JOB_RUNNING = "running"
    JOB_DONE = "done"
    JOB_ERROR = "error"
    # Job 'running' chỉ bị coi là mồ côi khi cũ hơn ngưỡng này — tránh việc
    # một instance khác trên cùng file DB giết nhầm job đang chạy thật
    # (video job hợp lệ có thể chạy vài phút).
    JOB_ORPHAN_STALE_MS = 30 * 60 * 1000

    @staticmethod
    def _job(row, include_params: bool = False):
        job = {
            "job_id": row["id"],
            "status": row["status"],
            "error": row["error"],
            "results": json.loads(row["results_json"]),
            "created_at": row["created_at"],
            # Additive (frontend đọc thêm tuỳ ý); job list endpoint dùng để
            # khôi phục queue sau F5 nên có updated_at là tiện cho sắp xếp UI.
            "updated_at": row["updated_at"],
        }
        if include_params:
            # Chỉ endpoint retry đọc params — payload list client giữ gọn.
            job["params"] = json.loads(row["params_json"]) if row["params_json"] else None
        return job

    async def create_job(
        self, job_id: str, owner_hash: str, project_id: str, params: dict | None = None
    ) -> dict:
        """Đăng ký job mới (status 'running'); dọn job cũ đã xong hơn 24 giờ.

        ``params`` là bộ tham số đã resolve (reference_urls, extend_media_id…)
        — job lỗi sẽ dùng lại cho nút Retry.
        """
        await self._initialize()
        now = int(time.time() * 1000)
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM playground_jobs WHERE status IN ('done', 'error') "
                "AND updated_at <= ?",
                (now - 24 * 60 * 60 * 1000,),
            )
            await db.execute(
                "UPDATE playground_jobs SET status = 'error', error = ?, updated_at = ? "
                "WHERE status = 'running' AND updated_at <= ?",
                (
                    "Job hết thời gian chạy (quá 30 phút) nên bị hủy.",
                    now,
                    now - self.JOB_ORPHAN_STALE_MS,
                ),
            )
            await db.execute(
                "INSERT INTO playground_jobs("
                "id, owner_hash, project_id, status, error, results_json, params_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, NULL, '[]', ?, ?, ?)",
                (
                    job_id,
                    owner_hash,
                    project_id,
                    self.JOB_RUNNING,
                    json.dumps(params) if params else None,
                    now,
                    now,
                ),
            )
            await db.commit()
        job = await self.get_job(owner_hash, project_id, job_id)
        if job is None:
            raise RuntimeError(f"Failed to create playground job {job_id}")
        return job

    async def count_running_jobs(self, owner_hash: str) -> int:
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM playground_jobs WHERE owner_hash = ? AND status = 'running'",
                (owner_hash,),
            )
            row = await cursor.fetchone()
        return int(row[0])

    async def get_job(
        self, owner_hash: str, project_id: str, job_id: str, include_params: bool = False
    ) -> dict | None:
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM playground_jobs WHERE id = ? AND owner_hash = ? AND project_id = ?",
                (job_id, owner_hash, project_id),
            )
            row = await cursor.fetchone()
        return self._job(row, include_params=include_params) if row else None

    async def delete_job(self, owner_hash: str, project_id: str, job_id: str) -> bool:
        """Xoá hẳn job (chỉ job đã kết thúc — caller kiểm tra status trước)."""
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM playground_jobs WHERE id = ? AND owner_hash = ? AND project_id = ?",
                (job_id, owner_hash, project_id),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def list_jobs_for_project(
        self, owner_hash: str, project_id: str, limit: int = 20
    ) -> list[dict]:
        """Job gần nhất của project, mới nhất trước (tối đa ``limit``).

        Dùng để frontend khôi phục queue sau F5: job running giữ nguyên trong
        bảng; job done/error cũ hơn 24h đã bị create_job dọn định kỳ nên không
        cần lọc thêm. Khớp index (owner_hash, project_id, created_at DESC).
        """
        await self._initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM playground_jobs WHERE owner_hash = ? AND project_id = ? "
                "ORDER BY created_at DESC, id LIMIT ?",
                (owner_hash, project_id, max(1, int(limit))),
            )
            rows = await cursor.fetchall()
        return [self._job(row) for row in rows]

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        error: str | None = None,
        results: list | None = None,
        owner_hash: str | None = None,
        project_id: str | None = None,
    ) -> dict | None:
        await self._initialize()
        now = int(time.time() * 1000)
        # owner/project guard tùy chọn nhưng caller nội bộ luôn nên truyền,
        # để một job_id không bao giờ bị ghi đè chéo giữa các owner.
        where = "WHERE id = ?"
        params: list = [job_id]
        if owner_hash is not None:
            where += " AND owner_hash = ?"
            params.append(owner_hash)
        if project_id is not None:
            where += " AND project_id = ?"
            params.append(project_id)
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE playground_jobs SET status = ?, error = ?, results_json = ?, updated_at = ? "
                + where,
                (status, error, json.dumps(results or []), now, *params),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return None
            cursor = await db.execute(
                "SELECT * FROM playground_jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
        return self._job(row) if row else None

    async def append_results(
        self, owner_hash: str, project_id: str, items: list
    ) -> dict | None:
        """Append atomic vào project.results (một connection duy nhất, có kiểm tra owner)."""
        await self._initialize()
        now = int(time.time() * 1000)
        async with self._connect() as db:
            # BEGIN IMMEDIATE giữ khoá ghi từ lúc đọc để read-modify-write là nguyên tố.
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT results_json FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return None
            merged = json.loads(row["results_json"])
            merged.extend(items)
            await db.execute(
                "UPDATE playground_projects SET results_json = ?, "
                "updated_at = MAX(?, updated_at + 1) WHERE id = ? AND owner_hash = ?",
                (json.dumps(merged), now, project_id, owner_hash),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM playground_projects WHERE id = ? AND owner_hash = ?",
                (project_id, owner_hash),
            )
            row = await cursor.fetchone()
        return self._project(row) if row else None
