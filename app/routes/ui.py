"""Server-rendered UI route — serves the recruiter scan page."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from fastapi.templating import Jinja2Templates

router = APIRouter()

templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# chrome-extension/ lives at the repo root, two levels up from app/routes/.
_EXTENSION_DIR = Path(__file__).parent.parent.parent / "chrome-extension"


def _extension_version() -> str:
    try:
        return json.loads((_EXTENSION_DIR / "manifest.json").read_text()).get("version", "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # Starlette ≥0.27 expects (request, name, context) positionally; the older
    # (name, context_with_request) form raises "unhashable type: 'dict'".
    return templates.TemplateResponse(
        request, "index.html",
        {"title": "Auto Screener", "extensionVersion": _extension_version()},
    )


@router.get("/extension.zip")
def extension_zip() -> StreamingResponse:
    """Zip up the chrome-extension/ folder so recruiters can install via
    Load Unpacked without cloning the repo.

    Streamed in-memory; the folder is small (~20KB) so no need for tempfiles.
    """
    if not _EXTENSION_DIR.is_dir():
        raise HTTPException(500, "chrome-extension folder not found on server")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in _EXTENSION_DIR.rglob("*"):
            if path.is_file() and ".DS_Store" not in path.name:
                # Store paths relative to chrome-extension/ so manifest.json
                # ends up at the zip's root (required by Chrome).
                zf.write(path, arcname=str(path.relative_to(_EXTENSION_DIR)))
    buf.seek(0)
    version = _extension_version()
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="auto-screener-extension-{version}.zip"'},
    )


@router.get("/extension/version")
def extension_version() -> dict:
    """Plain-JSON helper the popup or page-side detection JS can hit to compare
    against the running extension's manifest version."""
    return {"version": _extension_version()}
