from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from lawyer_rag.config import Settings


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _uuid(value: str) -> str:
        return str(uuid.UUID(value))

    def document_dir(self, matter_id: str, document_id: str) -> Path:
        return (
            self.settings.data_dir
            / "matters"
            / self._uuid(matter_id)
            / "documents"
            / self._uuid(document_id)
        )

    def original_path(self, matter_id: str, document_id: str) -> Path:
        return self.document_dir(matter_id, document_id) / "original.pdf"

    def ocr_path(self, matter_id: str, document_id: str) -> Path:
        return self.document_dir(matter_id, document_id) / "ocr.pdf"

    def sidecar_path(self, matter_id: str, document_id: str) -> Path:
        return self.document_dir(matter_id, document_id) / "ocr.txt"

    def preview_path(self, matter_id: str, document_id: str, page: int) -> Path:
        return self.document_dir(matter_id, document_id) / "previews" / f"page-{page}.png"

    def save_upload(self, stream: BinaryIO, matter_id: str, document_id: str) -> tuple[Path, str, int]:
        directory = self.document_dir(matter_id, document_id)
        directory.mkdir(parents=True, exist_ok=False)
        destination = self.original_path(matter_id, document_id)
        digest = hashlib.sha256()
        size = 0

        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_upload_bytes:
                    temporary.close()
                    temporary_path.unlink(missing_ok=True)
                    shutil.rmtree(directory, ignore_errors=True)
                    raise ValueError("PDF exceeds the configured upload size limit")
                digest.update(chunk)
                temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())

        os.replace(temporary_path, destination)
        return destination, digest.hexdigest(), size

    def remove_document_dir(self, matter_id: str, document_id: str) -> None:
        shutil.rmtree(self.document_dir(matter_id, document_id), ignore_errors=True)

    def render_preview(self, matter_id: str, document_id: str, page: int) -> Path:
        if page < 1:
            raise ValueError("Page numbers start at 1")
        preview = self.preview_path(matter_id, document_id, page)
        if preview.exists():
            return preview
        preview.parent.mkdir(parents=True, exist_ok=True)
        source = self.ocr_path(matter_id, document_id)
        if not source.exists():
            source = self.original_path(matter_id, document_id)
        prefix = preview.with_suffix("")
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-png",
                "-scale-to",
                "1600",
                str(source),
                str(prefix),
            ],
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return preview
