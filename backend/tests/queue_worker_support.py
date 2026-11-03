"""Test-only worker adapter: real Celery process, free provider, controlled crash window."""

import os
import time
from pathlib import Path

from arclight.providers.media_submission import MockMediaSubmissionProvider
from arclight.workers import tasks
from arclight.workers.celery_app import celery_app


class ControlledProvider(MockMediaSubmissionProvider):
    def submit(self, request):
        control = Path(os.environ["ARCLIGHT_TEST_WORKER_CONTROL"])
        if request.snapshot["context"]["generation"]["context"].get("block_worker"):
            (control / str(request.job_id)).write_text(str(os.getpid()), encoding="utf-8")
            deadline = time.monotonic() + 50
            while not (control / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        return super().submit(request)


tasks.get_media_submission_provider = lambda name, settings: ControlledProvider()
app = celery_app
