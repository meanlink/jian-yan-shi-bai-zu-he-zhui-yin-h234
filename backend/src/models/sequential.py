"""预登记连续检验：登记、逐批账本、评估任务。"""
from sqlalchemy import String, JSON, Double, Integer, ForeignKey, Boolean, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class SequentialStudy(Base):
    """开检前锁定口径的连续检验登记。冻结后 evidence/freeze 不再被改写。"""
    __tablename__ = 'sequential_studies'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    hypothesis: Mapped[dict] = mapped_column(JSON, default=dict)
    inclusion: Mapped[dict] = mapped_column(JSON, default=dict)
    alpha: Mapped[float] = mapped_column(Double)
    p0: Mapped[float] = mapped_column(Double)
    alt_rate: Mapped[float] = mapped_column(Double)
    registration_checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='active')  # active/stopped/ended
    locked_at: Mapped[float] = mapped_column(Double)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    ledger_version: Mapped[int] = mapped_column(Integer, default=0)
    evidence_version: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    stopping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stopped_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    stop_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_batch_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    conclusion: Mapped[str] = mapped_column(String(2000), default='')
    freeze: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SequentialObservation(Base):
    """批次账本的一版记录。同批补报/撤回新增版本，旧版标记 superseded。"""
    __tablename__ = 'sequential_observations'
    __table_args__ = (
        UniqueConstraint('study_id', 'batch_key', 'revision', name='uq_sequential_observation_version'),
        Index('ix_sequential_observation_active', 'study_id', 'state', 'sequence'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey('sequential_studies.id'))
    batch_key: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    sequence: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20), default='ingest')  # ingest/supplement/withdraw
    state: Mapped[str] = mapped_column(String(20), default='active')  # active/superseded/withdrawn/pending_review
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    final_recheck: Mapped[bool] = mapped_column(Boolean, default=True)
    exclusion_reason: Mapped[str] = mapped_column(String(300), default='')
    pending_reason: Mapped[str] = mapped_column(String(300), default='')
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    antecedent_present: Mapped[bool] = mapped_column(Boolean, default=False)
    consequent_present: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(200), default='manual.json')
    applied_version: Mapped[int] = mapped_column(Integer, default=0)
    recorded_by: Mapped[str] = mapped_column(String(150), default='')
    recorded_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class SequentialMutation(Base):
    """一次收数/更正操作；request_key 让网络重试不会重复纳入。"""
    __tablename__ = 'sequential_mutations'
    __table_args__ = (UniqueConstraint('study_id', 'request_key', name='uq_sequential_mutation_request'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey('sequential_studies.id'))
    request_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    ledger_version: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(150))
    at: Mapped[float] = mapped_column(Double)


class SequentialStop(Base):
    """达到停止线时冻结的停止原件；此后更正只进入待复核，不改写本记录。"""
    __tablename__ = 'sequential_stops'
    __table_args__ = (UniqueConstraint('study_id', name='uq_sequential_stop_study'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey('sequential_studies.id'))
    evidence_version: Mapped[int]
    ledger_version: Mapped[int]
    stop_sequence: Mapped[int]
    stop_batch_key: Mapped[str] = mapped_column(String(160))
    stop_reason: Mapped[str] = mapped_column(String(40))
    batch_count: Mapped[int]
    unit_count: Mapped[int]
    success_count: Mapped[int]
    e_value: Mapped[float] = mapped_column(Double)
    alpha: Mapped[float] = mapped_column(Double)
    threshold: Mapped[float] = mapped_column(Double)
    conclusion: Mapped[str] = mapped_column(String(2000))
    sample: Mapped[list] = mapped_column(JSON, default=list)
    ledger: Mapped[list] = mapped_column(JSON, default=list)
    checksum: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[float] = mapped_column(Double)
    frozen_by: Mapped[str] = mapped_column(String(150), default='worker')


class SequentialEvalJob(Base):
    """按账本版本评估证据。旧版本晚到时只标记 stale，不覆盖停止原件。"""
    __tablename__ = 'sequential_eval_jobs'
    __table_args__ = (UniqueConstraint('study_id', 'target_version', name='uq_sequential_eval_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey('sequential_studies.id'))
    target_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='queued')  # queued/running/done/stale/skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
