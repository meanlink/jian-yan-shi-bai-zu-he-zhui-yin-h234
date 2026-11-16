"""预登记连续检验的独立单元测试。

运行：backend/ 下 `python -m pytest ../../test-artifacts/gb-1012/tests/test_sequential.py`
依赖：DATABASE_URL 指向临时 SQLite 或 MySQL；不依赖 Redis（worker 的 SQL 轮询兜底）。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'backend'))

TEST_DB = '/tmp/sequential-tests.db'
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + TEST_DB)
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
if os.environ['DATABASE_URL'] == 'sqlite:///' + TEST_DB and os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from sqlalchemy import select  # noqa: E402

from migrations.initial import migrate  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.models import SequentialEvalJob, SequentialObservation, SequentialStop, User  # noqa: E402
from src.schemas.sequential import BatchInput, CorrectionInput, StudyInput  # noqa: E402
from src.services import sequential  # noqa: E402
from src.sequential_worker import process_job, tick  # noqa: E402
from src.utils.security import hash_password  # noqa: E402


def labels(heat=0, visc=0):
    return {'heat_fail': heat, 'ph_fail': 0, 'viscosity_fail': visc, 'ash_fail': 0, 'color_fail': 0}


@pytest.fixture()
def db():
    migrate()
    with Session() as session:
        admin = session.scalar(select(User).where(User.email == 'test@admin'))
        if not admin:
            admin = User(email='test@admin', name='测试', role='admin', password=hash_password('x'))
            session.add(admin)
            session.commit()
        yield session
        session.rollback()


def ACTOR(db):
    return db.scalar(select(User).where(User.email == 'test@admin'))


def make_study(db, name, min_units=3, alpha=0.05, p0=0.5, alt=0.9, max_batches=0):
    study = sequential.create_study(db, StudyInput(
        name=name,
        hypothesis='制样异常导致热稳定与黏度在同一批复检中共同失败',
        antecedent=['heat_fail'], consequent=['viscosity_fail'],
        p0=p0, alt_rate=alt, alpha=alpha,
        inclusion={'min_units_before_stop': min_units, 'max_batches': max_batches},
    ), ACTOR(db))
    db.commit()
    return study


def ingest(db, study, key, heat=1, visc=1, rk=None):
    sequential.ingest_batch(db, study.id, BatchInput(
        batch_key=key, labels=labels(heat, visc), request_key=rk or f'key-{key}-{heat}{visc}'), ACTOR(db))
    db.commit()
    tick()
    db.refresh(study)


def test_e_value_math_and_stop_line(db):
    study = make_study(db, 'E值口径')
    ingest(db, study, 'M1', 1, 1)
    ingest(db, study, 'M2', 1, 0)
    # E = (p1/p0) * ((1-p1)/(1-p0)) = 1.8 * 0.2 = 0.36
    assert study.evidence['n'] == 2 and study.evidence['s'] == 1
    assert math.isclose(study.evidence['e_value'], 0.36)
    assert math.isclose(study.evidence['threshold'], 20.0)
    assert study.status == 'active'


def test_anytime_valid_stop_freezes_original(db):
    study = make_study(db, '随时停止')
    for i in range(6):
        ingest(db, study, f'F{i}', 1, 1)
    assert study.status == 'stopped'
    stop = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    assert stop is not None
    assert stop.stop_sequence == 6
    assert stop.unit_count == 6 and stop.success_count == 6
    assert stop.e_value >= stop.threshold
    first = stop.checksum
    # 任何晚到的计算都不改写停止原件
    late = SequentialEvalJob(study_id=study.id, target_version=study.ledger_version + 9,
                             status='queued', attempts=0, note='', created_at=1, updated_at=1)
    db.add(late); db.commit()
    process_job(late.id)
    db.refresh(late); db.refresh(study)
    stop_again = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    assert late.status == 'skipped'
    assert stop_again.checksum == first
    assert study.evidence_version == study.stopping_version


def test_stop_freezes_until_first_crossing_only(db):
    # 1.8^n >= 1/0.05=20 在 n=6 首次成立；min_units=3 不推迟停止
    study = make_study(db, '停止点截断', min_units=3)
    for i in range(8):
        ingest(db, study, f'T{i}', 1, 1)
    stop = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    assert study.status == 'stopped'
    assert stop.stop_sequence == 6
    assert stop.unit_count == 6
    assert len(stop.sample) == 6


def test_correction_rebuilds_and_removes_old_contribution(db):
    study = make_study(db, '更正重建')
    ingest(db, study, 'C0', 1, 1)
    ingest(db, study, 'C1', 1, 0)
    assert study.evidence['n'] == 2 and study.evidence['s'] == 1
    sequential.correct_batch(db, study.id, CorrectionInput(
        batch_key='C1', action='supplement', labels=labels(1, 1),
        reason='复核后补报为共同失败', request_key='corr-00001-xyz'), ACTOR(db))
    db.commit(); tick(); db.refresh(study)
    assert study.evidence['n'] == 2 and study.evidence['s'] == 2
    assert math.isclose(study.evidence['e_value'], 1.8 * 1.8)
    seq = sequential.recompute_evidence(db, study)
    assert (seq['n'], seq['s']) == (2, 2)
    assert math.isclose(seq['e_value'], study.evidence['e_value'])


def test_withdraw_removes_unit_and_keeps_others(db):
    study = make_study(db, '撤回单位')
    ingest(db, study, 'W0', 1, 1)
    ingest(db, study, 'W1', 1, 0)
    sequential.correct_batch(db, study.id, CorrectionInput(
        batch_key='W0', action='withdraw', reason='原始记录重复登记',
        request_key='withdraw-01'), ACTOR(db))
    db.commit(); tick(); db.refresh(study)
    assert study.evidence['n'] == 1 and study.evidence['s'] == 0
    latest = db.scalar(select(SequentialObservation).where(
        SequentialObservation.study_id == study.id,
        SequentialObservation.batch_key == 'W0').order_by(SequentialObservation.revision.desc()))
    assert latest.state == 'withdrawn'


def test_partial_supplement_keeps_other_labels(db):
    study = make_study(db, '部分补报')
    ingest(db, study, 'PS0', 1, 0)
    sequential.correct_batch(db, study.id, CorrectionInput(
        batch_key='PS0', action='supplement', labels={'viscosity_fail': 1},
        reason='仅黏度复核值需要更正', request_key='partial-0001'), ACTOR(db))
    db.commit(); tick(); db.refresh(study)
    latest = db.scalar(select(SequentialObservation).where(
        SequentialObservation.study_id == study.id,
        SequentialObservation.batch_key == 'PS0').order_by(SequentialObservation.revision.desc()))
    assert latest.labels == labels(1, 1)
    assert study.evidence['s'] == 1


def test_retry_same_request_key_not_counted_twice(db):
    study = make_study(db, '重试幂等')
    ingest(db, study, 'R0', 1, 1, rk='retry-key-001')
    ingest(db, study, 'R0', 1, 1, rk='retry-key-001')
    active = list(db.scalars(select(SequentialObservation).where(
        SequentialObservation.study_id == study.id,
        SequentialObservation.state == 'active')))
    assert len(active) == 1
    assert study.evidence['n'] == 1


def test_excluded_and_non_final_batches_do_not_enter_evidence(db):
    study = sequential.create_study(db, StudyInput(
        name='纳入规则', hypothesis='预登记排除批次与非最终复核不得进入证据',
        antecedent=['heat_fail'], consequent=['viscosity_fail'], p0=0.5, alt_rate=0.9, alpha=0.05,
        inclusion={'final_recheck_only': True, 'excluded_batches': ['X1'],
                   'min_units_before_stop': 3, 'max_batches': 0},
    ), ACTOR(db))
    db.commit()
    ingest(db, study, 'X1', 1, 1)  # 预登记排除
    sequential.ingest_batch(db, study.id, BatchInput(
        batch_key='NF', labels=labels(1, 1), final_recheck=False, request_key='nonfinal-001'),
        ACTOR(db))
    db.commit(); tick(); db.refresh(study)
    assert study.evidence['n'] == 0
    assert study.evidence['counts']['excluded'] == 2


def test_post_stop_changes_marked_pending_review(db):
    study = make_study(db, '停止后待复核')
    for i in range(6):
        ingest(db, study, f'P{i}', 1, 1)
    stop = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    frozen = (study.evidence['e_value'], stop.checksum)
    sequential.ingest_batch(db, study.id, BatchInput(
        batch_key='LATE', labels=labels(1, 1), request_key='late-000001'),
        ACTOR(db))
    db.commit()
    sequential.correct_batch(db, study.id, CorrectionInput(
        batch_key='P0', action='withdraw', reason='停止后核对发现重复登记',
        request_key='late-000002'), ACTOR(db))
    db.commit(); tick(); db.refresh(study)
    detail = sequential.study_detail(db, study.id)
    assert detail['counts']['pending_review'] == 2
    assert study.evidence['e_value'] == frozen[0]
    again = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    assert again.checksum == frozen[1]
    assert all(row['pending_reason'] for row in detail['pending_review'])


def test_late_old_version_is_stale_and_does_not_write_evidence(db):
    study = make_study(db, '旧计算晚到')
    sequential.ingest_batch(db, study.id, BatchInput(
        batch_key='S0', labels=labels(1, 1), request_key='stale-000001'),
        ACTOR(db))
    db.commit()
    job = db.scalar(select(SequentialEvalJob).where(
        SequentialEvalJob.study_id == study.id, SequentialEvalJob.target_version == 1))
    # 账本前进后旧任务才执行
    sequential.ingest_batch(db, study.id, BatchInput(
        batch_key='S1', labels=labels(1, 1), request_key='stale-000002'),
        ACTOR(db))
    db.commit()
    process_job(job.id)
    db.refresh(job); db.refresh(study)
    assert job.status == 'stale'
    assert study.evidence_version == 0


def test_reevaluate_is_idempotent(db):
    study = make_study(db, '重算幂等')
    actor = ACTOR(db)
    first, replay_a = sequential.enqueue_reevaluation(db, study.id, 'reeval-key-01', actor)
    db.commit()
    second, replay_b = sequential.enqueue_reevaluation(db, study.id, 'reeval-key-01', actor)
    db.commit()
    db.refresh(study)
    assert (replay_a, replay_b) == (False, True)
    assert first.id == second.id
    assert study.ledger_version == 1


def test_close_refuses_when_stop_line_reached(db):
    study = make_study(db, '停止优先于结束', min_units=3)
    for i in range(6):
        ingest(db, study, f'X{i}', 1, 1)
    db.refresh(study)
    assert study.status == 'stopped'
    with pytest.raises(Exception):
        sequential.close_study(db, study.id, ACTOR(db))


def test_max_batches_ends_enrollment(db):
    study = make_study(db, '纳入上限', min_units=2, max_batches=2)
    ingest(db, study, 'E0', 1, 1)
    ingest(db, study, 'E1', 1, 0)
    assert study.status == 'ended'
    assert study.evidence_version == 2
    detail = sequential.study_detail(db, study.id)
    assert detail['can_ingest'] is False
