from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Word(BaseModel):
    """单词级别的时间戳数据结构。"""

    text: str
    start: float
    end: float
    conf: Optional[float] = Field(default=None, description="置信度，可为空")


class Segment(BaseModel):
    """句段结构，包含原始文本和单词列表。"""

    start: float
    end: float
    text: str
    words: List[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    """统一的转录结构。"""

    segments: List[Segment] = Field(default_factory=list)
    language: str = Field(default="auto")
