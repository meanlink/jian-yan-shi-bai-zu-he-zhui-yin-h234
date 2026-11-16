import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, SequentialStudy
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum


class _SeedUser:
    def __init__(self, user):
        self.id = user.id
        self.email = user.email


def _seed_sequential(db, records, admin):
    """用同一的240批复检记录建立一条已冻结的预登记连续检验示例。"""
    from src.schemas.sequential import BatchInput, CorrectionInput, StudyInput
    from src.services import sequential
    from src.sequential_worker import tick

    if db.scalar(select(SequentialStudy).where(SequentialStudy.name == '制样共败 · 连续检验示例')):
        return
    actor = _SeedUser(admin)
    study = sequential.create_study(db, StudyInput(
        name='制样共败 · 连续检验示例',
        hypothesis='制样异常会让热稳定与黏度在同一批复检中同时失败，复核小组先按此假设追查制样记录。',
        antecedent=['heat_fail'],
        consequent=['viscosity_fail'],
        p0=0.5,
        alt_rate=0.9,
        alpha=0.05,
        inclusion={'final_recheck_only': True, 'excluded_batches': ['HP-R007'],
                   'min_units_before_stop': 5, 'max_batches': 0},
    ), actor)
    db.commit()
    for index, row in enumerate(records[:60], 1):
        sequential.ingest_batch(db, study.id, BatchInput(
            batch_key=row['batch_id'],
            labels={key: row[key] for key in sequential.ITEMS},
            final_recheck=True,
            source='assay_rechecks_202611.csv',
            request_key=f'seed-ingest-{index:06d}',
        ), actor)
        db.commit()
        tick()
        db.refresh(study)
        if study.status == 'stopped':
            # 停止后再到达的一批和一条更正，演示只标记待复核
            sequential.ingest_batch(db, study.id, BatchInput(
                batch_key='HP-R241',
                labels={key: records[239][key] for key in sequential.ITEMS},
                final_recheck=True,
                source='assay_rechecks_202611.csv',
                request_key='seed-post-ingest-01',
            ), actor)
            db.commit()
            sequential.correct_batch(db, study.id, CorrectionInput(
                batch_key=row['batch_id'],
                action='withdraw',
                reason='停止后核对发现该批为同批复检重复登记',
                request_key='seed-post-correct-01',
            ), actor)
            db.commit()
            tick()
            break


def seed():
    migrate()
    with Session() as db:
        for role, name in [('admin', '林知 · 业务负责人'), ('analyst', '陈舟 · 分析员'), ('viewer', '周禾 · 观察员')]:
            email = role + '@demo.local'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, name=name, role=role, password=hash_password('Demo123!')))
        db.flush()
        m = metadata()
        d = db.scalar(select(Dataset).where(Dataset.name == m['dataset_name']))
        if not d:
            rows = seed_records()
            d = Dataset(name=m['dataset_name'], source=m['short'] + '-202611.csv', records=rows, checksum=checksum(rows), created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name == '基线复核 · ' + m['name'])):
            params = {p['key']: p['default'] for p in m.get('parameters', [])}
            result = analyze(d.records, params)
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            admin = db.scalar(select(User).where(User.role == 'admin'))
            j = Job(name='基线复核 · ' + m['name'], dataset_id=d.id, params=params, status='completed', result=result, created_by=admin.id, created_at=time.time() - 3600, finished_at=time.time() - 3595, approved=True)
            db.add(j); db.flush()
            r = Report(name='每日业务复核简报', job_id=j.id, destination='业务复核组 · 本地收件箱', interval_seconds=86400, next_run=time.time() + 86400, enabled=True)
            db.add(r); db.flush(); deliver(db, r)
            db.add(Audit(actor='seed', action='demo.baseline', detail={'dataset': d.id}, at=time.time()))
        db.commit()
        admin = db.scalar(select(User).where(User.role == 'admin'))
        _seed_sequential(db, d.records, admin)
        counts = {model.__tablename__: db.scalar(select(func.count()).select_from(model)) for model in [User, Dataset, Job, Report, Delivery, Audit, SequentialStudy]}
        print(json.dumps(counts, ensure_ascii=False))
        return counts


if __name__ == '__main__':
    seed()
