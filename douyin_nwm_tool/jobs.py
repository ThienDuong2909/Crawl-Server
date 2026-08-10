import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from .downloader import DownloadService

JobStatus = Literal["queued", "running", "success", "failed"]


@dataclass
class Job:
    id: str
    type: str
    url: str
    status: JobStatus = "queued"
    progress: int = 0
    message: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "url": self.url,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, downloader: DownloadService | Any | None = None):
        self.downloader = downloader or DownloadService()
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def create_download_job(self, url: str, start: bool = True) -> Job:
        job = self.create_download_job_record(url)
        if start:
            task = asyncio.create_task(self.run_download_job(job.id))
            self.tasks[job.id] = task
        return job

    def create_download_job_record(self, url: str) -> Job:
        job = Job(id=uuid.uuid4().hex, type="download", url=url)
        self.jobs[job.id] = job
        return job

    async def run_download_job(self, job_id: str) -> None:
        job = self.get(job_id)
        await self._run_download(job)

    async def _run_download(self, job: Job) -> None:
        try:
            self._update(job, status="running", progress=5, message="Parsing Douyin URL")
            result = await self.downloader.download(job.url, progress_callback=lambda event: self._on_download_progress(job, event))
            self._update(
                job,
                status="success",
                progress=100,
                message="Completed",
                result={
                    "video_id": result.video_id,
                    "file_path": str(result.file_path),
                    "source_url": result.source_url,
                    "bytes_written": result.bytes_written,
                },
            )
        except Exception as exc:
            self._update(job, status="failed", progress=100, message="Failed", error=str(exc))

    def _update(self, job: Job, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(job, key, value)
        job.updated_at = time.time()

    def _on_download_progress(self, job: Job, event: dict[str, Any]) -> None:
        phase = event.get("phase")
        progress = int(event.get("progress") or job.progress)
        bytes_written = event.get("bytes_written")
        total_bytes = event.get("total_bytes")
        message = "Parsing Douyin URL"
        if phase == "downloading":
            if total_bytes:
                message = f"Downloading {bytes_written}/{total_bytes} bytes"
            else:
                message = f"Downloading {bytes_written or 0} bytes"
        elif phase == "completed":
            message = "Completed"
        self._update(job, status="running", progress=progress, message=message)

    def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def list_jobs(self, limit: int = 100) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    async def wait(self, job_id: str, timeout: float | None = None) -> Job:
        task = self.tasks.get(job_id)
        if task:
            await asyncio.wait_for(task, timeout=timeout)
        return self.get(job_id)
