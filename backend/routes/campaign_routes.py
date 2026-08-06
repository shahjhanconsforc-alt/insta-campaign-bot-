from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend import db
from backend.automation import campaign, media

router = APIRouter(prefix="/api/campaign", tags=["campaign"])


class CampaignPlanBody(BaseModel):
    message: str = ""
    limit: int = Field(default=25, ge=1, le=200)
    save_draft: bool = True


@router.post("/plan")
async def plan(body: CampaignPlanBody):
    try:
        return campaign.build_plan(body.message, limit=body.limit, save_draft=body.save_draft)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


# ─── SMS drafts (the sms1/sms2/… tabs) ───────────────────────────


class SmsDraft(BaseModel):
    id: str                       # "sms1", "sms2", … (positional, stable)
    text: str = ""
    name: str = ""                # the tab's display name; "" falls back to the id
    disabled: bool = False        # a disabled draft is kept but never sent


class SmsDraftsBody(BaseModel):
    drafts: list[SmsDraft]


# ─── Campaign media (images attached in the panel) ───────────────


class MediaFile(BaseModel):
    name: str = "image"
    type: str = ""
    data: str = ""                # base64, optionally with a data: URI prefix


class MediaUploadBody(BaseModel):
    files: list[MediaFile]


@router.post("/media")
async def upload_media(body: MediaUploadBody):
    """Save the images the panel attached and return their stored filenames.

    Those filenames go back to the panel and are sent with /automation/search-
    campaign, where the browser step resolves them to real paths and uploads
    them into each DM."""
    from services.storage.minio_client import MinioUnavailable

    try:
        saved = media.save_uploads([f.model_dump() for f in body.files])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc
    return {"media": saved}


@router.get("/media")
async def list_media():
    """Every image in MinIO — what the panel's media section renders."""
    from services.storage.minio_client import MinioUnavailable

    try:
        return {"media": media.listing()}
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.get("/media/{name}")
async def get_media(name: str):
    """The bytes of one stored image (fetched from MinIO), for the <img> preview."""
    from services.storage.minio_client import MinioUnavailable

    try:
        return FileResponse(media.path_of(name))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.delete("/media/{name}")
async def delete_media(name: str):
    """Drop one stored image from MinIO — the ✕ on its thumbnail."""
    from services.storage.minio_client import MinioUnavailable

    try:
        return media.delete(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.get("/sms")
async def get_sms():
    """The saved message drafts (from MinIO), loaded when the panel opens."""
    from services.storage.minio_client import MinioUnavailable

    try:
        return {"drafts": db.get_sms_drafts()}
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.put("/sms")
async def save_sms(body: SmsDraftsBody):
    """Persist the message drafts to MinIO, each under its sms id."""
    from services.storage.minio_client import MinioUnavailable

    drafts = [d.model_dump() for d in body.drafts]
    try:
        return {"drafts": db.save_sms_drafts(drafts)}
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc
