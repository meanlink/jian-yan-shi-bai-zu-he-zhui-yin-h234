"""预登记连续检验：登记、批次账本、更正、随时可停的证据与冻结。

统计口径（开检前随登记锁定）：
- 每一批"前项全部失败"的复检事务是一个单位；
- 单位中"后项全部失败"计 1，否则计 0；
- H0: P(后项共败|前项共现)=p0；H1: =alt_rate（预登记备择）；
- 逐批累积似然比 E = Π [p1^X (1-p1)^(1-X)] / [p0^X (1-p0)^(1-X)]。
E 在 H0 下是非负鞅，Ville 不等式给出：若每次观察都按 E≥1/α 停止，
误报概率不超过 α。因此随时停止仍有效，不需要固定样本量。
"""
import math
import time

from fastapi import HTTPException
from sqlalchemy import select

from src.models import (
    SequentialEvalJob,
    SequentialMutation,
    SequentialObservation,
    SequentialStop,
    SequentialStudy,
)
from src.services.audit import record
from src.services.business import ITEMS
from src.utils.serialization import checksum

ITEMS_SET = set(ITEMS)
MAX_TRAJECTORY = 1500
STATUS_ACTIVE = 'active'
STATUS_STOPPED = 'stopped'
STATUS_ENDED = 'ended'


def name_of(key):
    from src.services.business import NAMES
    return NAMES.get(key, key)


def enqueue(study_id):
    """通知 worker 立刻处理；Redis 不可用时 worker 的 SQL 轮询兜底。"""
    try:
        from redis import Redis
        from src.config.settings import REDIS_URL
        client = Redis.from_url(REDIS_URL, socket_timeout=1)
        client.rpush('sequential:queue', study_id)
        client.close()
    except Exception:
        pass


def _now():
    return time.time()


def _finite(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, f'{label}须为有效数值')
    if not math.isfinite(number):
        raise HTTPException(422, f'{label}须为有限数值')
    return number


def _item_list(value, label, limit):
    if not isinstance(value, list) or not value:
        raise HTTPException(422, f'{label}至少选择一项')
    items = []
    for key in value:
        if key not in ITEMS_SET:
            raise HTTPException(422, f'{label}包含未知检验项：{key}')
        if key not in items:
            items.append(key)
    if len(items) > limit:
        raise HTTPException(422, f'{label}最多选择{limit}项')
    return items


def parse_labels(raw):
    if not isinstance(raw, dict):
        raise HTTPException(422, '检验标记须为五项0/1对象')
    extra = sorted(set(raw) - ITEMS_SET)
    missing = [key for key in ITEMS if key not in raw]
    if extra:
        raise HTTPException(422, '包含未知检验项：' + '、'.join(extra))
    if missing:
        raise HTTPException(422, '五项检验标记须完整，缺少：' + '、'.join(missing))
    labels = {}
    for key in ITEMS:
        value = raw[key]
        if isinstance(value, bool):
            raise HTTPException(422, '检验标记仅接受0(通过)或1(失败)')
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(422, '检验标记仅接受0(通过)或1(失败)')
        if number not in (0.0, 1.0):
            raise HTTPException(422, '检验标记仅接受0(通过)或1(失败)')
        labels[key] = int(number)
    return labels


def _clean_batch_key(value):
    key = str(value or '').strip()
    if not key or len(key) > 160:
        raise HTTPException(422, '批次标识去除空格后须为1~160个字符')
    return key


def _clean_request_key(value):
    key = str(value or '').strip()
    if len(key) < 8 or len(key) > 120:
        raise HTTPException(422, '请求幂等键须为8~120个字符，用于重试时不重复纳入')
    return key


def _clean_reason(value, required=True):
    text = str(value or '').strip()
    if required and len(text) < 4:
        raise HTTPException(422, '更正原因至少需要四个字符')
    if len(text) > 500:
        raise HTTPException(422, '更正原因最多500个字符')
    return text


def _inclusion(value):
    if not isinstance(value, dict):
        raise HTTPException(422, '纳入规则须为对象')
    allowed = {'final_recheck_only', 'excluded_batches', 'min_units_before_stop', 'max_batches'}
    extra = set(value) - allowed
    if extra:
        raise HTTPException(422, '纳入规则包含未知字段：' + '、'.join(sorted(extra)))
    final_only = value.get('final_recheck_only', True)
    if not isinstance(final_only, bool):
        raise HTTPException(422, 'final_recheck_only 须为布尔值')
    excluded = value.get('excluded_batches', [])
    if not isinstance(excluded, list) or len(excluded) > 200:
        raise HTTPException(422, '预登记排除批次须为不超过200条的数组')
    clean_excluded = []
    for item in excluded:
        key = _clean_batch_key(item)
        if key not in clean_excluded:
            clean_excluded.append(key)
    try:
        min_units = int(value.get('min_units_before_stop', 8))
        max_batches = int(value.get('max_batches', 0))
    except (TypeError, ValueError):
        raise HTTPException(422, '停止前最少单位数与纳入上限须为整数')
    if not 1 <= min_units <= 10000:
        raise HTTPException(422, '停止前最少前项共现单位数须在1~10000之间')
    if not 0 <= max_batches <= 100000:
        raise HTTPException(422, '纳入批次上限须为0(不限)或1~100000')
    if max_batches and max_batches < min_units:
        raise HTTPException(422, '纳入批次上限不应小于停止前最少单位数')
    return {
        'final_recheck_only': final_only,
        'excluded_batches': clean_excluded,
        'min_units_before_stop': min_units,
        'max_batches': max_batches,
    }


def _registration_payload(name, hypothesis, antecedent, consequent, p0, alt_rate, alpha, inclusion):
    return {
        'name': name,
        'hypothesis': hypothesis,
        'antecedent': antecedent,
        'consequent': consequent,
        'p0': p0,
        'alt_rate': alt_rate,
        'alpha': alpha,
        'inclusion': inclusion,
    }


def create_study(db, data, user):
    name = str(data.name or '').strip()
    if len(name) < 2 or len(name) > 160:
        raise HTTPException(422, '登记名称去除空格后须为2~160个字符')
    if db.scalar(select(SequentialStudy).where(SequentialStudy.name == name)):
        raise HTTPException(409, '该连续检验登记名已存在；预登记不可覆盖，请换一个名称')
    statement = str(data.hypothesis or '').strip()
    if len(statement) < 10 or len(statement) > 2000:
        raise HTTPException(422, '共败假设须为10~2000个字符，并在开检前锁定')
    antecedent = _item_list(data.antecedent, '前项A', 3)
    consequent = _item_list(data.consequent, '后项B', 2)
    if set(antecedent) & set(consequent):
        raise HTTPException(422, '前项与后项不能包含同一检验项')
    p0 = _finite(data.p0, '零假设共败率 p0')
    alt_rate = _finite(data.alt_rate, '备择共败率 p1')
    alpha = _finite(data.alpha, '误报上限 α')
    if not 0.01 <= p0 <= 0.95:
        raise HTTPException(422, '零假设共败率 p0 须在0.01~0.95之间')
    if not p0 + 0.005 <= alt_rate <= 0.999:
        raise HTTPException(422, '备择共败率 p1 须大于 p0 且不超过0.999')
    if not 0.001 <= alpha <= 0.25:
        raise HTTPException(422, '误报上限 α 须在0.001~0.25之间')
    inclusion = _inclusion(data.inclusion or {})
    payload = _registration_payload(name, statement, antecedent, consequent, p0, alt_rate, alpha, inclusion)
    study = SequentialStudy(
        name=name,
        hypothesis={
            'statement': statement,
            'antecedent': antecedent,
            'consequent': consequent,
            'null_rate': p0,
            'alt_rate': alt_rate,
        },
        inclusion=inclusion,
        alpha=alpha,
        p0=p0,
        alt_rate=alt_rate,
        registration_checksum=checksum(payload),
        status=STATUS_ACTIVE,
        locked_at=_now(),
        created_by=user.id,
        ledger_version=0,
        evidence_version=0,
        evidence=_empty_evidence(),
        conclusion='',
    )
    db.add(study)
    db.flush()
    record(db, user.email, 'sequential.register', {
        'id': study.id,
        'name': name,
        'checksum': study.registration_checksum,
        'alpha': alpha,
    })
    return study


def _empty_evidence():
    return {
        'n': 0, 's': 0, 'e_value': 1.0, 'log_e': 0.0, 'anytime_p': 1.0,
        'stop_ready': False, 'first_crossing_sequence': None,
        'trajectory': [], 'trajectory_truncated': False,
        'counts': {'active': 0, 'eligible': 0, 'excluded': 0, 'units': 0, 'pending_review': 0, 'enrolled_batches': 0},
        'computed_at': None, 'ledger_version': 0,
    }


def _locked_study(db, study_id):
    study = db.execute(
        select(SequentialStudy)
        .where(SequentialStudy.id == study_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not study:
        raise HTTPException(404, '连续检验登记不存在')
    return study


def _latest_observation(db, study_id, batch_key):
    return db.scalar(
        select(SequentialObservation)
        .where(SequentialObservation.study_id == study_id, SequentialObservation.batch_key == batch_key)
        .order_by(SequentialObservation.revision.desc(), SequentialObservation.id.desc())
    )


def _next_revision(db, study_id, batch_key):
    current = db.scalar(
        select(SequentialObservation.revision)
        .where(SequentialObservation.study_id == study_id, SequentialObservation.batch_key == batch_key)
        .order_by(SequentialObservation.revision.desc())
    )
    return int(current or 0) + 1


def _next_sequence(db, study_id):
    current = db.scalar(
        select(SequentialObservation.sequence)
        .where(SequentialObservation.study_id == study_id)
        .order_by(SequentialObservation.sequence.desc())
    )
    return int(current or 0) + 1


def _evaluate_batch(study, labels, final_recheck, batch_key):
    inclusion = study.inclusion or {}
    if inclusion.get('final_recheck_only', True) and not final_recheck:
        return False, '非最终复核，按预登记纳入规则排除', False, False, None
    if batch_key in (inclusion.get('excluded_batches') or []):
        return False, '预登记排除批次', False, False, None
    antecedent = study.hypothesis.get('antecedent', [])
    consequent = study.hypothesis.get('consequent', [])
    antecedent_present = all(labels[key] == 1 for key in antecedent)
    consequent_present = all(labels[key] == 1 for key in consequent)
    outcome = None
    if antecedent_present:
        outcome = 1 if consequent_present else 0
    return True, '', antecedent_present, consequent_present, outcome


def _mutation_replay(db, study_id, request_key):
    return db.scalar(
        select(SequentialMutation).where(
            SequentialMutation.study_id == study_id,
            SequentialMutation.request_key == request_key,
        )
    )


def _new_mutation(db, study, kind, request_key, detail, actor):
    mutation = SequentialMutation(
        study_id=study.id,
        request_key=request_key,
        kind=kind,
        ledger_version=study.ledger_version,
        detail=detail,
        actor=actor,
        at=_now(),
    )
    db.add(mutation)
    db.flush()
    return mutation


def _ensure_eval_job(db, study, version):
    job = db.scalar(
        select(SequentialEvalJob).where(
            SequentialEvalJob.study_id == study.id,
            SequentialEvalJob.target_version == version,
        )
    )
    if not job:
        now = _now()
        job = SequentialEvalJob(
            study_id=study.id, target_version=version,
            status='queued', attempts=0, note='', created_at=now, updated_at=now,
        )
        db.add(job)
        db.flush()
    return job


def _new_observation(db, study, batch_key, revision, sequence, action, state, eligible,
                     final_recheck, exclusion_reason, pending_reason, labels,
                     antecedent_present, consequent_present, outcome, source,
                     applied_version, actor):
    now = _now()
    observation = SequentialObservation(
        study_id=study.id,
        batch_key=batch_key,
        revision=revision,
        sequence=sequence,
        action=action,
        state=state,
        eligible=eligible,
        final_recheck=final_recheck,
        exclusion_reason=exclusion_reason,
        pending_reason=pending_reason,
        labels=labels,
        antecedent_present=antecedent_present,
        consequent_present=consequent_present,
        outcome=outcome,
        source=source,
        applied_version=applied_version,
        recorded_by=actor,
        recorded_at=now,
        updated_at=now,
    )
    db.add(observation)
    db.flush()
    return observation


def ingest_batch(db, study_id, data, user):
    """收数：新批次进入账本。停止后新批只作为待复核记录，不进入证据。"""
    study = _locked_study(db, study_id)
    batch_key = _clean_batch_key(data.batch_key)
    request_key = _clean_request_key(data.request_key)
    replay = _mutation_replay(db, study_id, request_key)
    if replay:
        return study, replay, True
    if study.status == STATUS_ENDED:
        raise HTTPException(409, '登记已结束纳入，不再接收新批次；如需继续请另行预登记')
    if _latest_observation(db, study_id, batch_key):
        raise HTTPException(409, '该批次已在账本中；补报或撤回请提交"更正"并写明原因')
    labels = parse_labels(data.labels)
    final_recheck = bool(data.final_recheck)
    sequence = _next_sequence(db, study_id)
    eligible, reason, antecedent_present, consequent_present, outcome = _evaluate_batch(
        study, labels, final_recheck, batch_key
    )
    if study.status == STATUS_STOPPED:
        # 冻结后不推进账本、不新建评估任务，只留待复核记录
        version = study.ledger_version
        observation = _new_observation(
            db, study, batch_key, 1, sequence, 'ingest', 'pending_review', eligible,
            final_recheck, reason, f'停止后新批，仅待复核（登记停止于 v{study.evidence_version}）', labels,
            antecedent_present, consequent_present, outcome, str(data.source or 'manual.json')[:200],
            study.evidence_version, user.email,
        )
        kind = 'ingest_pending'
    else:
        study.ledger_version += 1
        version = study.ledger_version
        observation = _new_observation(
            db, study, batch_key, 1, sequence, 'ingest', 'active', eligible,
            final_recheck, reason, '', labels,
            antecedent_present, consequent_present, outcome, str(data.source or 'manual.json')[:200],
            version, user.email,
        )
        kind = 'ingest'
        _ensure_eval_job(db, study, version)
    mutation = _new_mutation(db, study, kind, request_key, {
        'batch_key': batch_key, 'observation_id': observation.id,
        'state': observation.state, 'ledger_version': version,
    }, user.email)
    return study, mutation, False


def correct_batch(db, study_id, data, user):
    """更正：补报/撤回先让旧版失效，再按当前账本重建后续证据。"""
    study = _locked_study(db, study_id)
    batch_key = _clean_batch_key(data.batch_key)
    request_key = _clean_request_key(data.request_key)
    replay = _mutation_replay(db, study_id, request_key)
    if replay:
        return study, replay, True
    if data.action not in ('supplement', 'withdraw'):
        raise HTTPException(422, '更正动作仅支持补报(supplement)或撤回(withdraw)')
    reason = _clean_reason(data.reason)
    latest = _latest_observation(db, study_id, batch_key)
    if not latest:
        raise HTTPException(404, '该批次不在账本中，无法更正')
    if latest.state == 'superseded':
        raise HTTPException(409, '批次版本状态异常，请刷新后重试')
    if latest.state == 'pending_review':
        raise HTTPException(409, '停止后的更正已在待复核区，不能再次叠加；请等待复核')
    if data.action == 'withdraw' and latest.state != 'active':
        raise HTTPException(409, '只有当前生效批次可以撤回')
    if data.action == 'supplement' and latest.state not in ('active', 'withdrawn'):
        raise HTTPException(409, '该批次当前不可补报')
    if study.status == STATUS_ENDED and latest.state == 'withdrawn':
        raise HTTPException(409, '登记已结束纳入，不再恢复批次')

    if data.action == 'supplement':
        if not data.labels:
            raise HTTPException(422, '补报须至少提交一项新的检验标记')
        unknown = sorted(set(data.labels) - ITEMS_SET)
        if unknown:
            raise HTTPException(422, '包含未知检验项：' + '、'.join(unknown))
        # 未提交的检验项沿用该批原值，整体仍须构成合法的五项0/1记录
        labels = parse_labels({**(latest.labels or {}), **data.labels})
        final_recheck = latest.final_recheck
        if data.final_recheck is not None:
            final_recheck = bool(data.final_recheck)
    else:
        labels = dict(latest.labels or {})
        final_recheck = latest.final_recheck

    revision = _next_revision(db, study_id, batch_key)
    sequence = latest.sequence
    eligible, exclusion_reason, antecedent_present, consequent_present, outcome = _evaluate_batch(
        study, labels, final_recheck, batch_key
    )
    source = latest.source

    if study.status == STATUS_STOPPED:
        # 冻结后旧版保持生效，原件样本不变；只追加一条待复核记录，不推进账本
        version = study.ledger_version
        observation = _new_observation(
            db, study, batch_key, revision, sequence,
            'supplement' if data.action == 'supplement' else 'withdraw',
            'pending_review', eligible, final_recheck, exclusion_reason,
            f'停止后{("补报" if data.action == "supplement" else "撤回")}，仅待复核：{reason}',
            labels, antecedent_present, consequent_present, outcome, source,
            study.evidence_version, user.email,
        )
        kind = 'correction_pending'
    else:
        study.ledger_version += 1
        version = study.ledger_version
        latest.state = 'superseded'
        latest.updated_at = _now()
        state = 'active' if data.action == 'supplement' else 'withdrawn'
        observation = _new_observation(
            db, study, batch_key, revision, sequence, data.action, state,
            eligible if state == 'active' else False, final_recheck,
            exclusion_reason, '', labels,
            antecedent_present, consequent_present, outcome, source, version, user.email,
        )
        kind = 'correction'
        _ensure_eval_job(db, study, version)
    mutation = _new_mutation(db, study, kind, request_key, {
        'batch_key': batch_key, 'action': data.action, 'reason': reason,
        'observation_id': observation.id, 'state': observation.state,
        'ledger_version': version,
    }, user.email)
    return study, mutation, False


def close_study(db, study_id, user):
    """负责人可提前结束纳入，不产生停止结论。

    结束前先按当前账本重算；若已达到停止线，停止优先于结束。
    """
    study = _locked_study(db, study_id)
    if study.status == STATUS_STOPPED:
        raise HTTPException(409, '登记已达到停止线并冻结，不能改为结束')
    if study.status == STATUS_ENDED:
        return study
    evidence = recompute_evidence(db, study)
    study.evidence = evidence
    study.evidence_version = study.ledger_version
    if evidence['stop_ready']:
        freeze_study(db, study, evidence)
        db.commit()
        raise HTTPException(409, '当前证据已达停止线，登记已冻结为停止结论，不能改为结束')
    study.ledger_version += 1
    version = study.ledger_version
    study.status = STATUS_ENDED
    study.conclusion = '登记由负责人提前结束纳入；未达到停止线，当前证据不构成预登记结论。'
    record(db, user.email, 'sequential.close', {'id': study.id, 'ledger_version': version})
    _ensure_eval_job(db, study, version)
    return study


def _counts(db, study_id):
    observations = list(db.scalars(
        select(SequentialObservation).where(SequentialObservation.study_id == study_id)
    ))
    active = [row for row in observations if row.state == 'active']
    pending = [row for row in observations if row.state == 'pending_review']
    return {
        'active': len(active),
        'eligible': sum(1 for row in active if row.eligible),
        'excluded': sum(1 for row in active if not row.eligible),
        'units': sum(1 for row in active if row.eligible and row.antecedent_present),
        'pending_review': len(pending),
        'enrolled_batches': len({row.batch_key for row in active}),
        'withdrawn': sum(1 for row in observations if row.state == 'withdrawn'),
        'superseded': sum(1 for row in observations if row.state == 'superseded'),
    }


def recompute_evidence(db, study, until_sequence=None):
    """按当前生效账本重建证据曲线；更正后的批次在原序号位置重新计入。

    每次都从生效账本全量重算，避免多个收数合并成一次评估时读到过期前缀。
    until_sequence 用于冻结停止原件：只累计到首次越过停止线的那一批。
    """
    active = list(db.scalars(
        select(SequentialObservation)
        .where(
            SequentialObservation.study_id == study.id,
            SequentialObservation.state == 'active',
        )
        .order_by(SequentialObservation.sequence, SequentialObservation.revision)
    ))
    if until_sequence is not None:
        active = [row for row in active if row.sequence <= until_sequence]
    alpha = float(study.alpha)
    p0 = float(study.p0)
    p1 = float(study.alt_rate)
    log_threshold = -math.log(alpha)
    log_step_up = math.log(p1 / p0)
    log_step_down = math.log((1 - p1) / (1 - p0))
    min_units = int((study.inclusion or {}).get('min_units_before_stop', 8))
    max_batches = int((study.inclusion or {}).get('max_batches', 0))

    n = 0
    s = 0
    log_e = 0.0
    trajectory = []
    first_crossing = None
    for observation in active:
        if observation.eligible and observation.antecedent_present:
            x = 1 if observation.outcome == 1 else 0
            n += 1
            s += x
            log_e += log_step_up if x else log_step_down
        row = {
            'sequence': observation.sequence,
            'batch_key': observation.batch_key,
            'revision': observation.revision,
            'labels': observation.labels,
            'eligible': observation.eligible,
            'exclusion_reason': observation.exclusion_reason,
            'antecedent_present': observation.antecedent_present,
            'consequent_present': observation.consequent_present,
            'outcome': observation.outcome,
            'recorded_at': observation.recorded_at,
            'n': n, 's': s, 'e_value': math.exp(log_e) if log_e < 709 else 1e308,
        }
        if first_crossing is None and n >= min_units and log_e >= log_threshold:
            first_crossing = observation.sequence
        trajectory.append(row)

    e_value = math.exp(log_e) if log_e < 709 else 1e308
    anytime_p = 1.0 if log_e <= 0 else min(1.0, math.exp(-log_e))
    stop_ready = bool(n >= min_units and log_e >= log_threshold)
    evidence = {
        'n': n,
        's': s,
        'e_value': e_value,
        'log_e': log_e,
        'anytime_p': anytime_p,
        'alpha': alpha,
        'threshold': 1.0 / alpha,
        'p0': p0,
        'alt_rate': p1,
        'stop_ready': stop_ready,
        'first_crossing_sequence': first_crossing,
        'min_units_before_stop': min_units,
        'max_batches': max_batches,
        'enrollment_complete': bool(max_batches and len({row.batch_key for row in active}) >= max_batches),
        'counts': _counts(db, study.id),
        'ledger_version': study.ledger_version,
        'computed_at': _now(),
        'trajectory': trajectory[-MAX_TRAJECTORY:],
        'trajectory_truncated': len(trajectory) > MAX_TRAJECTORY,
        'method': (
            'E=Π[p1^X(1-p1)^(1-X)]/[p0^X(1-p0)^(1-X)]，X为前项共现批次中后项是否共败。'
            'H0下E为非负鞅，Ville不等式保证任意观察时点按E≥1/α停止的误报概率≤α。'
        ),
    }
    return evidence


def _stop_snapshot(db, study, evidence):
    crossing = evidence.get('first_crossing_sequence')
    observations = list(db.scalars(
        select(SequentialObservation)
        .where(
            SequentialObservation.study_id == study.id,
            SequentialObservation.state == 'active',
        )
        .order_by(SequentialObservation.sequence)
    ))
    if crossing is not None:
        observations = [row for row in observations if row.sequence <= crossing]
    sample = [{
        'sequence': row.sequence,
        'batch_key': row.batch_key,
        'revision': row.revision,
        'labels': row.labels,
        'eligible': row.eligible,
        'antecedent_present': row.antecedent_present,
        'consequent_present': row.consequent_present,
        'outcome': row.outcome,
        'recorded_at': row.recorded_at,
    } for row in observations if row.eligible and row.antecedent_present]
    ledger = [{
        'sequence': row.sequence,
        'batch_key': row.batch_key,
        'revision': row.revision,
        'state': row.state,
        'eligible': row.eligible,
        'labels': row.labels,
        'recorded_at': row.recorded_at,
        'applied_version': row.applied_version,
    } for row in observations]
    last = observations[-1] if observations else None
    payload = {
        'study_id': study.id,
        'registration_checksum': study.registration_checksum,
        'evidence_version': study.evidence_version,
        'ledger_version': study.ledger_version,
        'alpha': study.alpha,
        'threshold': evidence['threshold'],
        'stop_sequence': last.sequence if last else 0,
        'stop_batch_key': last.batch_key if last else '',
        'n': len(sample),
        's': sum(1 for row in sample if row['outcome'] == 1),
        'e_value': evidence['e_value'],
        'sample': sample,
        'ledger': ledger,
    }
    return payload, sample, (last.sequence if last else 0), (last.batch_key if last else '')


def _stop_metrics(evidence, sample):
    """冻结口径按停止点重算，避免把停止线之后到达的批次算进原件。"""
    n = len(sample)
    s = sum(1 for row in sample if row['outcome'] == 1)
    p0 = float(evidence['p0'])
    p1 = float(evidence['alt_rate'])
    log_e = s * math.log(p1 / p0) + (n - s) * math.log((1 - p1) / (1 - p0))
    return n, s, math.exp(log_e) if log_e < 709 else 1e308


def freeze_study(db, study, evidence):
    """达到停止线时写入不可变的停止原件；重复调用返回既有原件。"""
    existing = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    if existing:
        return existing, False
    payload, sample, stop_sequence, stop_batch_key = _stop_snapshot(db, study, evidence)
    n, s, e_value = _stop_metrics(evidence, sample)
    digest = checksum(payload)
    conclusion = (
        f"在预登记口径下（p0={study.p0:g}，p1={study.alt_rate:g}，α={study.alpha:g}），"
        f"截至第{stop_sequence}批累计{n}个前项共现批次中{s}批出现后项共败；"
        f"E={e_value:.4g}，达到1/α={1 / study.alpha:.4g}的停止线。"
        f"自第{stop_sequence}批（{stop_batch_key}）冻结样本与结论；此后更正仅待复核，不改写本结论。"
    )
    stop = SequentialStop(
        study_id=study.id,
        evidence_version=study.evidence_version,
        ledger_version=study.ledger_version,
        stop_sequence=stop_sequence,
        stop_batch_key=stop_batch_key,
        stop_reason='e_value_threshold',
        batch_count=len({row['batch_key'] for row in payload['ledger']}),
        unit_count=n,
        success_count=s,
        e_value=e_value,
        alpha=study.alpha,
        threshold=evidence['threshold'],
        conclusion=conclusion,
        sample=sample,
        ledger=payload['ledger'],
        checksum=digest,
        frozen_at=_now(),
        frozen_by='worker',
    )
    db.add(stop)
    db.flush()
    study.status = STATUS_STOPPED
    study.stopping_version = study.evidence_version
    study.stopped_at = stop.frozen_at
    study.stop_sequence = stop_sequence
    study.stop_batch_key = stop_batch_key
    study.stop_reason = stop.stop_reason
    study.conclusion = conclusion
    return stop, True


def _end_conclusion(study, evidence):
    return (
        f"已达到预登记纳入上限{evidence['max_batches']}批，停止纳入；"
        f"当前前项共现{evidence['n']}个单位、后项共败{evidence['s']}个，E={evidence['e_value']:.4g}，"
        f"未达到1/α={1 / study.alpha:.4g}的停止线。证据不构成预登记结论。"
    )


def apply_outcome(db, study, evidence):
    """把一次同版证据落到登记上；停止优先于结束，已结束的登记不再改变状态。"""
    if study.status != STATUS_ACTIVE:
        return None, False
    if evidence['stop_ready']:
        crossing = evidence.get('first_crossing_sequence')
        if crossing is not None:
            evidence = recompute_evidence(db, study, until_sequence=crossing)
            study.evidence = evidence
        stop, created = freeze_study(db, study, evidence)
        if created:
            record(db, 'worker', 'sequential.stop', {
                'id': study.id,
                'stop_sequence': stop.stop_sequence,
                'e_value': stop.e_value,
                'checksum': stop.checksum,
                'evidence_version': stop.evidence_version,
            })
        return stop, created
    if evidence['enrollment_complete']:
        study.status = STATUS_ENDED
        study.conclusion = _end_conclusion(study, evidence)
        record(db, 'worker', 'sequential.end', {
            'id': study.id, 'ledger_version': study.ledger_version, 'e_value': evidence['e_value'],
        })
    return None, False


def study_summary(study, stop=None):
    evidence = study.evidence or _empty_evidence()
    return {
        'id': study.id,
        'name': study.name,
        'status': study.status,
        'locked_at': study.locked_at,
        'registration_checksum': study.registration_checksum,
        'p0': study.p0,
        'alt_rate': study.alt_rate,
        'alpha': study.alpha,
        'threshold': 1.0 / study.alpha,
        'hypothesis': study.hypothesis,
        'inclusion': study.inclusion,
        'ledger_version': study.ledger_version,
        'evidence_version': study.evidence_version,
        'n': evidence.get('n', 0),
        's': evidence.get('s', 0),
        'e_value': evidence.get('e_value', 1.0),
        'anytime_p': evidence.get('anytime_p', 1.0),
        'stop_ready': evidence.get('stop_ready', False),
        'stop_sequence': study.stop_sequence,
        'stop_batch_key': study.stop_batch_key,
        'stopped_at': study.stopped_at,
        'stopping_version': study.stopping_version,
        'stop_reason': study.stop_reason,
        'conclusion': study.conclusion,
        'has_stop': stop is not None,
        'stop_checksum': stop.checksum if stop else None,
        'counts': evidence.get('counts', _empty_evidence()['counts']),
    }


def stop_dict(stop):
    if not stop:
        return None
    return {
        'id': stop.id,
        'evidence_version': stop.evidence_version,
        'ledger_version': stop.ledger_version,
        'stop_sequence': stop.stop_sequence,
        'stop_batch_key': stop.stop_batch_key,
        'stop_reason': stop.stop_reason,
        'batch_count': stop.batch_count,
        'unit_count': stop.unit_count,
        'success_count': stop.success_count,
        'e_value': stop.e_value,
        'alpha': stop.alpha,
        'threshold': stop.threshold,
        'conclusion': stop.conclusion,
        'checksum': stop.checksum,
        'frozen_at': stop.frozen_at,
        'sample': stop.sample,
    }


def observation_dict(observation):
    return {
        'id': observation.id,
        'batch_key': observation.batch_key,
        'revision': observation.revision,
        'sequence': observation.sequence,
        'action': observation.action,
        'state': observation.state,
        'eligible': observation.eligible,
        'exclusion_reason': observation.exclusion_reason,
        'pending_reason': observation.pending_reason,
        'labels': observation.labels,
        'antecedent_present': observation.antecedent_present,
        'consequent_present': observation.consequent_present,
        'outcome': observation.outcome,
        'source': observation.source,
        'applied_version': observation.applied_version,
        'recorded_by': observation.recorded_by,
        'recorded_at': observation.recorded_at,
        'updated_at': observation.updated_at,
    }


def study_detail(db, study_id):
    study = db.get(SequentialStudy, study_id)
    if not study:
        raise HTTPException(404, '连续检验登记不存在')
    stop = db.scalar(select(SequentialStop).where(SequentialStop.study_id == study.id))
    active = list(db.scalars(
        select(SequentialObservation)
        .where(
            SequentialObservation.study_id == study.id,
            SequentialObservation.state == 'active',
        )
        .order_by(SequentialObservation.sequence, SequentialObservation.revision)
    ))
    pending = list(db.scalars(
        select(SequentialObservation)
        .where(
            SequentialObservation.study_id == study.id,
            SequentialObservation.state == 'pending_review',
        )
        .order_by(SequentialObservation.sequence, SequentialObservation.revision)
    ))
    recent_mutations = list(db.scalars(
        select(SequentialMutation)
        .where(SequentialMutation.study_id == study.id)
        .order_by(SequentialMutation.id.desc())
        .limit(20)
    ))
    latest_job = db.scalar(
        select(SequentialEvalJob)
        .where(SequentialEvalJob.study_id == study.id)
        .order_by(SequentialEvalJob.id.desc())
    )
    evidence = study.evidence or _empty_evidence()
    result = study_summary(study, stop)
    result['counts'] = _counts(db, study.id)
    result.update({
        'evidence': evidence,
        'trajectory': evidence.get('trajectory', []),
        'stop': stop_dict(stop),
        'active_batches': [observation_dict(row) for row in active],
        'pending_review': [observation_dict(row) for row in pending],
        'mutations': [{
            'id': row.id, 'kind': row.kind, 'ledger_version': row.ledger_version,
            'detail': row.detail, 'actor': row.actor, 'at': row.at,
        } for row in recent_mutations],
        'evaluation': {
            'status': latest_job.status if latest_job else 'none',
            'target_version': latest_job.target_version if latest_job else 0,
            'note': latest_job.note if latest_job else '',
        },
        'can_ingest': study.status == STATUS_ACTIVE,
        'can_correct': study.status in (STATUS_ACTIVE, STATUS_ENDED, STATUS_STOPPED),
    })
    return result


def evaluate_study_version(db, study, job):
    """按 job 记录的账本版本重放。调用方须已锁定 study 行。

    返回 'stale' | 'frozen' | 'done'：
    - stale: 账本已前进，本次旧计算作废，不写证据；
    - frozen: 登记已停止，任何晚到计算都不改写停止原件；
    - done: 已按同版账本写入证据并可能触发停止。
    """
    if study.status == STATUS_STOPPED:
        return 'frozen'
    if job.target_version != study.ledger_version:
        return 'stale'
    evidence = recompute_evidence(db, study)
    study.evidence = evidence
    study.evidence_version = job.target_version
    apply_outcome(db, study, evidence)
    return 'done'


def enqueue_reevaluation(db, study_id, request_key, user):
    """手动重算也使用幂等键：重复提交不推进账本、不新建任务。"""
    study = _locked_study(db, study_id)
    key = _clean_request_key(request_key)
    replay = _mutation_replay(db, study_id, key)
    if replay:
        job = db.scalar(
            select(SequentialEvalJob).where(
                SequentialEvalJob.study_id == study.id,
                SequentialEvalJob.target_version == replay.ledger_version,
            )
        )
        return job, True
    if study.status == STATUS_STOPPED:
        raise HTTPException(409, '登记已冻结，重新计算不会改写停止原件')
    study.ledger_version += 1
    job = _ensure_eval_job(db, study, study.ledger_version)
    _new_mutation(db, study, 'reevaluate', key, {'job_id': job.id}, user.email)
    record(db, user.email, 'sequential.reevaluate', {'id': study.id, 'ledger_version': study.ledger_version})
    return job, False
