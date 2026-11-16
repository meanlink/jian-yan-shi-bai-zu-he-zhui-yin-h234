"""预登记连续检验 worker：按账本版本处理评估任务。

保证：
- 同版：只处理 target_version == 当前 ledger_version 的任务；
- 旧计算晚到：标记 stale，不写证据；
- 到达停止线：冻结样本与结论，之后任务只标记 skipped，原件不变；
- 中断恢复：queued/running 任务按 SQL 重新拾取，重试不重复纳入；
- 并发收数：task 行加锁，账本版本比较失败即放弃本次写入。
"""
import logging
import time

from redis import Redis
from sqlalchemy import select

from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import SequentialEvalJob, SequentialStudy
from src.services import sequential

QUEUE = 'sequential:queue'
MAX_ATTEMPTS = 3


def _note(db, job, status, note):
    job.status = status
    job.note = note[:500]
    job.updated_at = time.time()


def process_job(job_id):
    with Session() as db:
        job = db.get(SequentialEvalJob, job_id)
        if not job or job.status in ('done', 'stale', 'skipped'):
            return
        study = db.execute(
            select(SequentialStudy).where(SequentialStudy.id == job.study_id).with_for_update()
        ).scalar_one_or_none()
        if not study:
            _note(db, job, 'skipped', '登记不存在')
            db.commit()
            return
        job.status = 'running'
        job.attempts += 1
        job.updated_at = time.time()
        db.flush()
        try:
            outcome = sequential.evaluate_study_version(db, study, job)
        except Exception as error:  # pragma: no cover - defensive path
            logging.exception('Sequential evaluation failed')
            job.status = 'queued' if job.attempts < MAX_ATTEMPTS else 'failed'
            job.note = f'计算失败：{error}'[:500]
            job.updated_at = time.time()
            db.commit()
            return
        if outcome == 'stale':
            _note(db, job, 'stale', f'账本已前进到 v{study.ledger_version}，本任务按 v{job.target_version} 作废，不写证据')
        elif outcome == 'frozen':
            _note(db, job, 'skipped', f'登记已停止并冻结于 v{study.stopping_version}，晚到计算不改写停止原件')
        elif outcome == 'done':
            _note(db, job, 'done', f'按 v{job.target_version} 重建证据；E={study.evidence.get("e_value", 1):.4g}')
        db.commit()


def tick():
    with Session() as db:
        pending = list(db.scalars(
            select(SequentialEvalJob.id)
            .where(SequentialEvalJob.status.in_(['queued', 'running']))
            .order_by(SequentialEvalJob.id)
            .limit(6)
        ))
    for job_id in pending:
        process_job(job_id)


def drain(client):
    """把 Redis 通知转成一次 tick；队列只是加速，不作为唯一事实来源。"""
    while True:
        item = client.lpop(QUEUE)
        if not item:
            return


def main():
    client = Redis.from_url(REDIS_URL, socket_timeout=2)
    while True:
        try:
            item = client.blpop(QUEUE, timeout=1)
            if item:
                drain(client)
            tick()
        except Exception:
            logging.exception('Sequential worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
