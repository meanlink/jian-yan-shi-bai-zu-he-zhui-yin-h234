import {sequential} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const ITEM_LABELS = {
  heat_fail:'热稳定失败', ph_fail:'pH偏离', viscosity_fail:'黏度失败', ash_fail:'灰分失败', color_fail:'色差失败'
};
const STATUS_LABELS = {active:'纳入中',stopped:'已停止并冻结',ended:'已结束纳入'};
const STATE_LABELS = {active:'生效',superseded:'已失效',withdrawn:'已撤回',pending_review:'待复核'};
const ACTION_LABELS = {ingest:'收数',supplement:'补报',withdraw:'撤回'};
const KIND_LABELS = {ingest:'收数',ingest_pending:'停止后收数（待复核）',correction:'更正',correction_pending:'停止后更正（待复核）',reevaluate:'手动重算'};

function requestKey(prefix){
  return prefix+'-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
}

function labelsText(labels){
  if(!labels)return '—';
  return Object.keys(ITEM_LABELS).map(k=>esc(ITEM_LABELS[k])+'='+(labels[k]??'?')).join(' · ');
}

function listRow(s){
  const badge=s.status==='stopped'?'stopped':s.status==='ended'?'ended':'';
  return '<button class="study-item '+(s.id==workspace.selectedStudy?'selected':'')+'" data-study="'+s.id+'">'
    +'<div><strong>'+esc(s.name)+'</strong><span class="study-status '+badge+'">'+esc(STATUS_LABELS[s.status]||s.status)+'</span></div>'
    +'<p>'+esc((s.hypothesis?.antecedent||[]).map(k=>ITEM_LABELS[k]||k).join(' + '))+' → '+esc((s.hypothesis?.consequent||[]).map(k=>ITEM_LABELS[k]||k).join(' + '))+'</p>'
    +'<small>单位 '+s.n+' · 共败 '+s.s+' · E='+fmt(s.e_value)+' · 停止线 '+fmt(s.threshold)+'</small></button>';
}

function registryPanel(registry){
  const isAdmin=session.user.role==='admin',canWrite=session.user.role!=='viewer';
  const itemOptions=registry.items.map(i=>'<option value="'+esc(i.key)+'">'+esc(i.label)+'</option>').join('');
  const newForm='<details class="study-form"><summary>预登记新的连续检验</summary>'
    +'<p class="hint">'+esc(registry.registration_rule)+'</p>'
    +'<form id="study-form">'
    +'<label>登记名称<input name="name" required minlength="2" maxlength="160" placeholder="例如：制样异常共败 · 11月复检"></label>'
    +'<label>共败假设（开检前锁定）<textarea name="hypothesis" rows="3" required minlength="10" maxlength="2000" placeholder="例如：制样异常会使热稳定与黏度在同批复检中同时失败"></textarea></label>'
    +'<div class="parameter-grid"><label>前项A（可多选）<select name="antecedent" multiple size="5" required>'+itemOptions+'</select></label>'
    +'<label>后项B（可多选）<select name="consequent" multiple size="5" required>'+itemOptions+'</select></label></div>'
    +'<div class="parameter-grid"><label>零假设共败率 p0<input type="number" step="0.01" min="0.01" max="0.95" name="p0" value="0.5" required></label>'
    +'<label>备择共败率 p1<input type="number" step="0.01" min="0.01" max="0.99" name="alt_rate" value="0.8" required></label></div>'
    +'<div class="parameter-grid"><label>误报上限 α<input type="number" step="0.001" min="0.001" max="0.25" name="alpha" value="0.05" required></label>'
    +'<label>停止前最少单位数<input type="number" step="1" min="1" max="10000" name="min_units_before_stop" value="8" required></label></div>'
    +'<label>预登记排除批次（逗号分隔，可留空）<input name="excluded_batches" placeholder="重复登记或已知异常的批次标识"></label>'
    +'<button type="submit" class="primary" '+(canWrite?'':'disabled')+'>锁定登记并开检 ↗</button>'
    +'</form></details>';
  return '<section class="panel registry-panel"><div class="section-title"><h2>预登记台账</h2><span>'+registry.studies.length+' 项登记</span></div>'
    +'<div class="study-list">'+(registry.studies.map(listRow).join('')||'<p class="empty">尚无预登记。开检前先锁定假设、纳入规则与误报上限。</p>')+'</div>'
    +newForm
    +'<div class="method-sheet compact"><h3>可随时停止仍有效的证据</h3><p>'+esc(registry.method)+'</p></div>'
    +'<p class="hint">'+(isAdmin?'可收数、更正、重新计算与结束纳入。':'可收数与更正；结束登记需业务负责人。')+' 停止线由 worker 在同版账本上判定。</p></section>';
}

function evidencePanel(study){
  const e=study.evidence||{},trajectory=study.trajectory||[];
  const crossing=study.stop?study.stop.stop_sequence:e.first_crossing_sequence;
  const progress=Math.min(1,e.log_e>0?e.log_e/Math.log(e.threshold||20):0);
  const chart=trajectory.length?'<div class="evidence-chart" role="img" aria-label="逐批证据轨迹">'
    +trajectory.map((row,i)=>{
      const height=Math.max(2,Math.min(100,row.log_e>0?row.log_e/Math.log(e.threshold||20)*100:2));
      const cls=row.sequence===crossing?'cross':'';
      return '<span class="evidence-bar '+cls+'" title="第'+row.sequence+'批 '+esc(row.batch_key)+' · E='+fmt(row.e_value)+'"><i style="height:'+height+'%"></i><b>'+(row.sequence===crossing?'✕':'')+'</b></span>';
    }).join('')+'</div>':'<p class="empty">尚无前项共现单位，E 值保持 1。</p>';
  return '<section class="panel"><div class="section-title"><h2>逐批证据</h2><span>证据 v'+study.evidence_version+' · 账本 v'+study.ledger_version+'</span></div>'
    +'<div class="metrics evidence-metrics">'
    +'<div><span>前项共现单位</span><strong>'+fmt(e.n)+'</strong></div>'
    +'<div><span>后项共败</span><strong>'+fmt(e.s)+'</strong></div>'
    +'<div><span>E 值</span><strong>'+fmt(e.e_value)+'</strong></div>'
    +'<div><span>停止线 1/α</span><strong>'+fmt(e.threshold)+'</strong></div>'
    +'<div><span>任意时点 p</span><strong>'+fmt(e.anytime_p)+'</strong></div>'
    +'</div>'
    +'<div class="evidence-progress"><span style="width:'+Math.round(progress*100)+'%"></span></div>'
    +chart
    +'<p class="hint">'+esc(e.method||'')+'</p>'
    +'<div class="section-title"><h2>批次账本</h2><span>'+study.active_batches.length+' 版生效</span></div>'
    +table([
      {key:'sequence',label:'序号'},{key:'batch_key',label:'批次'},{key:'revision',label:'版本'},
      {key:'outcome_text',label:'单位结果'},{key:'antecedent_text',label:'前项'},{key:'consequent_text',label:'后项'},
      {key:'state_text',label:'状态'},{key:'recorded_at_text',label:'记录时间'}
    ],study.active_batches.map(r=>({
      ...r,
      outcome_text:r.outcome===null||r.outcome===undefined?'不构成单位':(r.outcome===1?'后项共败':'后项未共败'),
      antecedent_text:r.antecedent_present?'出现':'未出现',
      consequent_text:r.consequent_present?'出现':'未出现',
      state_text:r.eligible?'纳入':(r.exclusion_reason||'排除'),
      recorded_at_text:date(r.recorded_at)
    })),'暂无批次。先在上方收数。')
    +'</section>';
}

function stopPanel(study){
  if(study.stop){
    const s=study.stop;
    return '<section class="panel stop-panel"><div class="section-title"><h2>停止原件（已冻结）</h2><span>'+date(s.frozen_at)+'</span></div>'
      +'<h3 class="stop-conclusion">'+esc(s.conclusion)+'</h3>'
      +'<div class="metrics evidence-metrics"><div><span>停止批次序号</span><strong>'+fmt(s.stop_sequence)+'</strong></div>'
      +'<div><span>冻结样本单位</span><strong>'+fmt(s.unit_count)+'</strong></div>'
      +'<div><span>共败单位</span><strong>'+fmt(s.success_count)+'</strong></div>'
      +'<div><span>停止时 E</span><strong>'+fmt(s.e_value)+'</strong></div>'
      +'<div><span>证据版本</span><strong>v'+s.evidence_version+'</strong></div></div>'
      +'<p class="hint">停止批次 '+esc(s.stop_batch_key)+' · 原件指纹 SHA256 '+esc(s.checksum.slice(0,32))+'… · 冻结后收数或更正只进入待复核，不改写本结论。</p>'
      +'<p class="hint">冻结样本清单：'+s.sample.map(row=>esc(row.batch_key)+'(第'+row.sequence+'批)').join('、')+'</p></section>';
  }
  if(study.status==='ended'){
    return '<section class="panel"><div class="section-title"><h2>未达到停止线</h2></div><p>'+esc(study.conclusion||'登记已结束纳入，未达到停止线。')+'</p></section>';
  }
  return '<section class="panel"><div class="section-title"><h2>停止条件</h2><span>尚未冻结</span></div>'
    +'<p class="hint">α='+fmt(study.alpha)+'，停止线 E≥'+fmt(study.threshold)+'，且至少 '+fmt(study.inclusion?.min_units_before_stop)+' 个前项共现单位。存在登记内排除批次 '+((study.inclusion?.excluded_batches||[]).length||0)+' 个。达到停止线时 worker 冻结样本与结论。</p>'
    +(study.evidence?.stop_ready?'<p class="warning">E 值已达停止线，等待 worker 冻结或点击重新计算。</p>':'')+'</section>';
}

function dataPanel(study){
  return '<div class="two-columns sequential-actions">'
    +'<section class="panel"><div class="section-title"><h2>收数</h2><span>逐批接入</span></div>'
    +'<form id="ingest-form"><label>批次标识<input name="batch_key" required maxlength="160" placeholder="例如 HP-R241"></label>'
    +'<div class="label-grid">'+Object.keys(ITEM_LABELS).map((k,i)=>'<label>'+esc(ITEM_LABELS[k])+'<select name="label-'+k+'"><option value="0">0 通过</option><option value="1">1 失败</option></select></label>').join('')+'</div>'
    +'<label class="inline"><input type="checkbox" name="final_recheck" checked> 该批只保留最终复核结果</label>'
    +'<button type="submit" class="primary" '+(study.status==='active'?'':'disabled')+'>纳入本批批次</button>'
    +'<p class="hint">'+(study.status==='active'?'录入后由 worker 在下一版账本上重建证据。':study.status==='stopped'?'登记已冻结：新批只记入待复核，不进入证据。':'登记已结束纳入，不再收数。')+'</p></form></section>'
    +'<section class="panel"><div class="section-title"><h2>更正</h2><span>补报 / 撤回</span></div>'
    +'<form id="correct-form"><label>批次标识<select name="batch_key" required>'+(study.active_batches.map(r=>'<option value="'+esc(r.batch_key)+'">'+esc(r.batch_key)+' · v'+r.revision+'</option>').join('')||'<option value="">暂无批次</option>')+'</select></label>'
    +'<label>更正动作<select name="action"><option value="supplement">补报（替换为新的最终复核）</option><option value="withdraw">撤回（移出证据）</option></select></label>'
    +'<div class="label-grid">'+Object.keys(ITEM_LABELS).map(k=>'<label>'+esc(ITEM_LABELS[k])+'<select name="label-'+k+'"><option value="">沿用原值</option><option value="0">0 通过</option><option value="1">1 失败</option></select></label>').join('')+'</div>'
    +'<label>更正原因（必填）<textarea name="reason" rows="2" required minlength="4" maxlength="500" placeholder="例如：原始记录重复登记，撤回该批"></textarea></label>'
    +'<button type="submit" class="primary" '+(!study.active_batches.length?'disabled':'')+'>提交更正并重建证据</button>'
    +'<p class="hint">更正先生效新版本、旧版标记失效，再按当前账本重建后续证据；撤回的批次保留失效版本备查。'+(study.status==='stopped'?'停止后更正只进入待复核并写明原因，不改写停止原件。':'')+'</p></form></section>'
    +'</div>';
}

function pendingPanel(study){
  if(!study.pending_review.length)return '';
  return '<section class="panel"><div class="section-title"><h2>待复核更正</h2><span>停止后到达，未进入证据</span></div>'
    +table([{key:'sequence',label:'序号'},{key:'batch_key',label:'批次'},{key:'action_text',label:'动作'},{key:'pending_reason',label:'复核原因'},{key:'recorded_at_text',label:'时间'}],
      study.pending_review.map(r=>({...r,action_text:ACTION_LABELS[r.action]||r.action,recorded_at_text:date(r.recorded_at)})),'')
    +'</section>';
}

function auditPanel(study){
  if(!study.mutations.length)return '';
  return '<section class="panel"><div class="section-title"><h2>登记操作轨迹</h2><span>幂等键防重复纳入</span></div>'
    +table([{key:'kind_text',label:'操作'},{key:'ledger_version_text',label:'账本版本'},{key:'detail_text',label:'内容'},{key:'actor',label:'操作者'},{key:'at_text',label:'时间'}],
      study.mutations.map(r=>({
        kind_text:KIND_LABELS[r.kind]||r.kind,ledger_version_text:'v'+r.ledger_version,
        detail_text:(r.detail?.batch_key||'')+(r.detail?.reason?' · '+r.detail.reason:''),
        actor:r.actor,at_text:date(r.at)
      })),'')
    +'</section>';
}

function detailPanel(study){
  const canWrite=session.user.role!=='viewer',isAdmin=session.user.role==='admin';
  const hypothesis=study.hypothesis||{};
  return '<section class="study-head"><div>'
    +'<p class="eyebrow">PREREGISTERED SEQUENTIAL TEST</p><h2>'+esc(study.name)+'</h2>'
    +'<p class="study-hypothesis">'+esc(hypothesis.statement||'')+'</p>'
    +'<div class="study-facts"><span>前项 '+esc((hypothesis.antecedent||[]).map(k=>ITEM_LABELS[k]||k).join(' + '))+'</span>'
    +'<span>后项 '+esc((hypothesis.consequent||[]).map(k=>ITEM_LABELS[k]||k).join(' + '))+'</span>'
    +'<span>p0='+fmt(study.p0)+' · p1='+fmt(study.alt_rate)+' · α='+fmt(study.alpha)+'</span>'
    +'<span>登记指纹 '+esc(study.registration_checksum.slice(0,20))+'…</span>'
    +'<span>锁定于 '+date(study.locked_at)+'</span></div></div>'
    +'<div class="study-actions"><span class="study-status '+(study.status==='stopped'?'stopped':'')+'">'+esc(STATUS_LABELS[study.status]||study.status)+'</span>'
    +'<button class="button" id="reevaluate-study" '+(!canWrite||study.status==='stopped'?'disabled':'')+'>按当前账本重算 ↻</button>'
    +(isAdmin&&study.status==='active'?'<button class="button" id="close-study">结束纳入</button>':'')
    +'</div></section>'
    +stopPanel(study)+evidencePanel(study)+dataPanel(study)+pendingPanel(study)+auditPanel(study)
    +'<p class="hint">证据评估：'+esc(study.evaluation.status)+'（目标 v'+study.evaluation.target_version+'）'+esc(study.evaluation.note?' · '+study.evaluation.note:'')+'</p>';
}

export async function sequentialPage(id){
  const registry=await sequential.registry();
  const wanted=id?Number(id):(registry.studies[0]?.id||null);
  workspace.selectedStudy=wanted&&registry.studies.some(s=>s.id===wanted)?wanted:null;
  const study=workspace.selectedStudy?await sequential.get(workspace.selectedStudy):null;
  return heading('04 / SEQUENTIAL EVIDENCE','预登记连续检验','开检前锁定共败假设、纳入规则与误报上限，逐批累积可随时停止仍有效的证据；达到停止线即冻结样本与结论。')
    +(study?detailPanel(study)+registryPanel(registry):registryPanel(registry));
}

export function bindSequential(refresh){
  document.querySelectorAll('[data-study]').forEach(button=>button.onclick=()=>{location.hash='#/sequential/'+button.dataset.study;});
  const form=document.querySelector('#study-form');
  if(form)submit(form,async f=>{
    const antecedent=[...f.getAll('antecedent')],consequent=[...f.getAll('consequent')];
    const excluded=(f.get('excluded_batches')||'').split(',').map(v=>v.trim()).filter(Boolean);
    const study=await sequential.register({
      name:f.get('name'),hypothesis:f.get('hypothesis'),antecedent,consequent,
      p0:Number(f.get('p0')),alt_rate:Number(f.get('alt_rate')),alpha:Number(f.get('alpha')),
      inclusion:{final_recheck_only:true,excluded_batches:excluded,min_units_before_stop:Number(f.get('min_units_before_stop')),max_batches:0}
    });
    notify('登记已锁定，开检不可再改口径');
    location.hash='#/sequential/'+study.id;
  });
  const ingest=document.querySelector('#ingest-form');
  if(ingest)submit(ingest,async f=>{
    const labels={};for(const k of Object.keys(ITEM_LABELS))labels[k]=Number(f.get('label-'+k)||0);
    const study=await sequential.ingest(workspace.selectedStudy,{batch_key:f.get('batch_key'),labels,final_recheck:f.get('final_recheck')==='on',request_key:requestKey('ingest')});
    notify(study.status==='stopped'?'登记已冻结：本批只记入待复核':'批次已纳入，等待重建证据');
    await refresh();
  });
  const correct=document.querySelector('#correct-form');
  if(correct)submit(correct,async f=>{
    const action=f.get('action'),labels={};let hasLabels=false;
    for(const k of Object.keys(ITEM_LABELS)){const v=f.get('label-'+k);if(v!==''){labels[k]=Number(v);hasLabels=true;}}
    if(action==='supplement'&&!hasLabels)throw new Error('补报至少需填写一项新的检验标记，其余沿用原值');
    const payload={batch_key:f.get('batch_key'),action,reason:f.get('reason'),request_key:requestKey('correct')};
    if(action==='supplement')payload.labels=labels;
    const study=await sequential.correct(workspace.selectedStudy,payload);
    notify(study.status==='stopped'?'更正已记入待复核，停止原件不变':'更正已生效，后续证据已重建');
    await refresh();
  });
  document.querySelector('#reevaluate-study')?.addEventListener('click',async e=>{
    e.target.disabled=true;
    try{await sequential.reevaluate(workspace.selectedStudy,{request_key:requestKey('reeval')});notify('已提交重算，稍后刷新查看');await refresh();}
    catch(error){notify(error.message,true);e.target.disabled=false;}
  });
  document.querySelector('#close-study')?.addEventListener('click',async e=>{
    if(!confirm('结束纳入后不再接收新批次，且不会产生停止结论。继续？'))return;
    e.target.disabled=true;
    try{await sequential.close(workspace.selectedStudy);notify('登记已结束纳入');await refresh();}
    catch(error){notify(error.message,true);e.target.disabled=false;}
  });
}
