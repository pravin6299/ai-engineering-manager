from pydantic import BaseModel, HttpUrl
from typing import List, Optional

class ClassInfo(BaseModel):
    class_id: int
    class_name: str
    grade: Optional[float] = None

class ScheduleEntry(BaseModel):
    day_of_week: str
    start_time: str
    end_time: str
    class_id: int
    class_name: Optional[str] = None

class DashboardResponse(BaseModel):
    name: str
    photo_url: Optional[HttpUrl] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    classes: List[ClassInfo]
    schedule: List[ScheduleEntry]