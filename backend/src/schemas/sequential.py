from pydantic import BaseModel, Field


class StudyInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    hypothesis: str = Field(min_length=10, max_length=2000)
    antecedent: list[str] = Field(min_length=1, max_length=3)
    consequent: list[str] = Field(min_length=1, max_length=2)
    p0: float = 0.5
    alt_rate: float = 0.8
    alpha: float = 0.05
    inclusion: dict = Field(default_factory=dict)


class BatchInput(BaseModel):
    batch_key: str = Field(min_length=1, max_length=160)
    labels: dict
    final_recheck: bool = True
    source: str = Field(default='manual.json', max_length=200)
    request_key: str = Field(min_length=8, max_length=120)


class CorrectionInput(BaseModel):
    batch_key: str = Field(min_length=1, max_length=160)
    action: str
    reason: str = Field(min_length=4, max_length=500)
    labels: dict | None = None
    final_recheck: bool | None = None
    request_key: str = Field(min_length=8, max_length=120)


class ReevaluateInput(BaseModel):
    request_key: str = Field(min_length=8, max_length=120)
