from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.database import get_db
from src.middlewares.auth import admin_only, can_write, current_user
from src.models import SequentialStop, SequentialStudy
from src.schemas.sequential import BatchInput, CorrectionInput, ReevaluateInput, StudyInput
from src.services import sequential
from src.services.audit import record

router = APIRouter(prefix='/sequential')


def _commit_or_conflict(db, message):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, message)


@router.get('/registry')
def registry(db=Depends(get_db), u=Depends(current_user)):
    studies = list(db.scalars(select(SequentialStudy).order_by(SequentialStudy.id.desc())))
    stops = {stop.study_id: stop for stop in db.scalars(select(SequentialStop))}
    return {
        'registration_rule': (
            '登记一旦保存即锁定共败假设、前项/后项、纳入规则、p0/p1、误报上限α与停止线1/α；'
            '登记不可修改或删除，只能另建新登记。'
        ),
        'method': (
            '每批前项全失败的事务为一个单位，后项全失败计1。累积E值在零假设下为非负鞅，'
            'Ville不等式保证任意时点按E≥1/α停止的误报概率不超过α，可随时停止而结论仍有效。'
        ),
        'items': [{'key': key, 'label': sequential.name_of(key)} for key in sequential.ITEMS],
        'studies': [sequential.study_summary(study, stops.get(study.id)) for study in studies],
    }


@router.post('', status_code=201)
def register(data: StudyInput, db=Depends(get_db), u=Depends(can_write)):
    study = sequential.create_study(db, data, u)
    _commit_or_conflict(db, '该连续检验登记名已被占用，请刷新后重试')
    return sequential.study_detail(db, study.id)


@router.get('/{study_id}')
def detail(study_id: int, db=Depends(get_db), u=Depends(current_user)):
    return sequential.study_detail(db, study_id)


@router.post('/{study_id}/batches', status_code=201)
def ingest(study_id: int, data: BatchInput, db=Depends(get_db), u=Depends(can_write)):
    study, mutation, replay = sequential.ingest_batch(db, study_id, data, u)
    if not replay:
        record(db, u.email, 'sequential.batch.ingest', {
            'id': study.id, 'batch_key': data.batch_key, 'ledger_version': study.ledger_version,
        })
    _commit_or_conflict(db, '该请求键已被并发使用，请用新的幂等键重试')
    sequential.enqueue(study.id)
    result = sequential.study_detail(db, study_id)
    result['replayed'] = replay
    result['mutation_id'] = mutation.id
    return result


@router.post('/{study_id}/corrections', status_code=201)
def correct(study_id: int, data: CorrectionInput, db=Depends(get_db), u=Depends(can_write)):
    study, mutation, replay = sequential.correct_batch(db, study_id, data, u)
    if not replay:
        record(db, u.email, 'sequential.batch.correct', {
            'id': study.id, 'batch_key': data.batch_key, 'action': data.action,
            'reason': data.reason, 'ledger_version': study.ledger_version,
        })
    _commit_or_conflict(db, '该请求键已被并发使用，请用新的幂等键重试')
    sequential.enqueue(study.id)
    result = sequential.study_detail(db, study_id)
    result['replayed'] = replay
    result['mutation_id'] = mutation.id
    return result


@router.post('/{study_id}/reevaluate')
def reevaluate(study_id: int, data: ReevaluateInput, db=Depends(get_db), u=Depends(can_write)):
    job, replay = sequential.enqueue_reevaluation(db, study_id, data.request_key, u)
    db.commit()
    if not replay:
        sequential.enqueue(study_id)
    return {'job_id': job.id, 'target_version': job.target_version, 'replayed': replay}


@router.post('/{study_id}/close')
def close(study_id: int, db=Depends(get_db), u=Depends(admin_only)):
    study = sequential.close_study(db, study_id, u)
    db.commit()
    sequential.enqueue(study.id)
    return sequential.study_detail(db, study_id)


@router.get('/{study_id}/stop')
def stop(study_id: int, db=Depends(get_db), u=Depends(current_user)):
    study = db.get(SequentialStudy, study_id)
    if not study:
        raise HTTPException(404, '连续检验登记不存在')
    original = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study_id))
    if not original:
        raise HTTPException(404, '该登记尚未达到停止线，没有停止原件')
    return sequential.stop_dict(original)
