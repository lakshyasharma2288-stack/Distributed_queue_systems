import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ImageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_type: str
    size_bytes: int
    width: int
    height: int
    created_at: datetime
