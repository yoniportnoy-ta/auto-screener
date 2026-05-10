"""Server-rendered UI route — serves the recruiter scan page."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # Starlette ≥0.27 expects (request, name, context) positionally; the older
    # (name, context_with_request) form raises "unhashable type: 'dict'".
    return templates.TemplateResponse(request, "index.html", {"title": "Auto Screener"})
