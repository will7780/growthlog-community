"""
Attachment request and response schemas.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class AttachmentResponse(BaseModel):
    """Attachment metadata returned to the frontend."""

    id: int
    entry_id: int
    user_id: int
    original_filename: str
    mime_type: str
    file_ext: str
    file_size: int
    status: str
    page_count: Optional[int] = None
    slide_count: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    # Image OCR visibility (aggregated from chunk metadata; optional).
    ocr_status: Optional[str] = None
    ocr_provider: Optional[str] = None
    extraction_methods: Optional[List[str]] = None
    ocr_reindex_required: bool = False

    model_config = ConfigDict(from_attributes=True)
