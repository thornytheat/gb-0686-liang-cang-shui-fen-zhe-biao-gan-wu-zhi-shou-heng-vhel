const feedback=document.querySelector('#feedback');
const STATUS_TAG={accepted:['ok','已入仓锁单'],rejected:['bad','退收'],pending_recheck:['wait','待复验'],superseded:['bad','已作废']};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,opts={}){
  const reply=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
  const data=await reply.json().catch(()=>({}));
  if(!reply.ok)throw new Error(data.message||'请求失败');
  return data;
}
// ---- 抽样点 ----
const pointsBox=document.querySelector('#points');
function addPoint(m='',i=''){
  const row=document.createElement('div');row.className='point-row';
  row.innerHTML=`<input placeholder="水分%" value="${esc(m)}" data-k="moisture" inputmode="decimal">
    <input placeholder="杂质%" value="${esc(i)}" data-k="impurity" inputmode="decimal">
    <button type="button" class="btn">删</button>`;
  row.querySelector('button').onclick=()=>{if(pointsBox.children.length>1)row.remove();};
  pointsBox.appendChild(row);
}
document.querySelector('#add-point').onclick=()=>addPoint();
addPoint(14.8,1.2);addPoint(16.9,3.4);addPoint(14.2,0.9);
function readPoints(){
  return [...pointsBox.children].map(r=>({
    moisture:r.querySelector('[data-k=moisture]').value,
    impurity:r.querySelector('[data-k=impurity]').value}));
}
// ---- 合同 ----
document.querySelector('#contract-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target;
  const bands=[...f.bands_text.value.trim().split('\n')].filter(Boolean).map(line=>{
    const [grade,max_moisture,max_impurity,price]=line.split(',').map(v=>v.trim());
    return {grade,max_moisture,max_impurity,price};
  });
  try{
    const r=await api('/api/grading/contracts',{method:'POST',body:JSON.stringify({
      contract_id:f.contract_id.value,grain:f.grain.value,
      standard_moisture:f.standard_moisture.value,basis:f.basis.value,
      max_moisture:f.max_moisture.value,max_impurity:f.max_impurity.value,bands})});
    feedback.textContent=(r.created?'合同已登记：':'合同已存在：')+r.contract.contract_id+'（指纹 '+r.contract.fingerprint.slice(0,12)+'…）';
    refreshAll();
  }catch(err){feedback.textContent=err.message;}
});
// ---- 仓 ----
document.querySelector('#bin-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target;
  try{
    const r=await api('/api/grading/bins',{method:'POST',body:JSON.stringify({
      bin_id:f.bin_id.value,grain:f.grain.value,
      capacity_kg:f.capacity_kg.value||null})});
    feedback.textContent=(r.created?'仓已登记：':'仓已存在：')+r.bin.bin_id;
    refreshAll();
  }catch(err){feedback.textContent=err.message;}
});
// ---- 到车 ----
document.querySelector('#arrive-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target;
  try{
    const r=await api('/api/grading/trucks/arrive',{method:'POST',body:JSON.stringify({
      contract_id:f.contract_id.value,truck_id:f.truck_id.value,bin_id:f.bin_id.value,
      net_kg:f.net_kg.value,points:readPoints()})});
    const c=r.conclusion;
    feedback.textContent=(r.mode==='idempotent_retry'?'同化验幂等返回，未重复计价：':'已出结论：')+
      `车 ${c.truck_id} ${c.status} 等级 ${c.grade??'等外'} 折标 ${c.standard_kg}kg 应付 ${c.amount} 元（结论 ${c.conclusion_id}）`;
    refreshAll();
  }catch(err){feedback.textContent=err.message;}
});
// ---- 渲染 ----
function tag(status){const [cls,txt]=STATUS_TAG[status]||['',status];return `<span class="tag ${cls}">${txt}</span>`;}
function renderTrucks(rows){
  const tb=document.querySelector('#truck-table tbody');
  if(!rows.length){tb.innerHTML='<tr><td colspan="10">暂无车结论。</td></tr>';return;}
  tb.innerHTML=rows.map(c=>`<tr>
    <td>${esc(c.truck_id)}</td><td>${esc(c.bin_id)}</td>
    <td>v${c.version} ${tag(c.status)}</td>
    <td>${esc(c.moisture)}</td><td>${esc(c.impurity)}</td>
    <td>${esc(c.grade??'等外')}${c.price?`<div class="mini">${esc(c.price)}/kg</div>`:''}</td>
    <td class="num">${esc(c.net_kg)}</td><td class="num">${esc(c.standard_kg)}</td>
    <td class="num">${esc(c.amount)}<div class="mini">未舍入 ${esc(c.unrounded_amount)}</div></td>
    <td><div class="hashline">${esc(c.conclusion_id)}</div><div class="mini">${esc(c.basis_detail)}${c.supersedes?`<br>作废 ${esc(c.supersedes)}`:''}${c.reject_reason?`<br>${esc(c.reject_reason)}`:''}</div></td>
  </tr>`).join('');
}
function renderBins(bins){
  const box=document.querySelector('#bin-box');
  if(!bins.length){box.innerHTML='<p style="padding:var(--pad)">暂无仓批。</p>';return;}
  box.innerHTML=bins.map(b=>`<div class="verdict-line">
    <b>${esc(b.bin_id)}</b> ${tag(b.grade?'accepted':'pending_recheck')} 仓批等级 <b>${esc(b.grade??'等外')}</b>
    <span class="mini">（最差车等级 ${esc(b.worst_truck_grade??'—')} · 定等版本 v${b.grade_version}）</span><br>
    <span class="mini">混仓水 ${esc(b.moisture)}% / 杂 ${esc(b.impurity)}% · 在仓 ${b.truck_count} 车
    · 净重 ${esc(b.net_kg)}kg · 折标 ${esc(b.standard_kg)}kg
    · 容量 ${esc(b.capacity_kg??'—')}kg · <b>余量 ${esc(b.remaining_kg??'—')}kg</b></span>
    <div class="hashline">车次：${esc((b.truck_ids||[]).join(', '))}</div>
    ${(b.mix_history||[]).map(h=>`<div class="mini">v${h.seq} ${h.event_type==='BIN_MIX_INVALID'?'⚠ 原仓批定等失效重算':h.event_type==='BIN_MIX_OPEN'?'首车建立仓批定等':'等级维持'}：${esc(h.payload.old_grade??'—')} → ${esc(h.payload.new_grade??'—')}（车 ${esc(h.payload.truck_id)}）</div>`).join('')}
  </div>`).join('');
}
function renderPending(rows){
  const box=document.querySelector('#pending-box');
  document.querySelector('#pending-count').textContent=rows.length+' 车';
  if(!rows.length){box.innerHTML='<p style="padding:var(--pad)">没有待复验车次。</p>';return;}
  box.innerHTML='<table class="ledger"><thead><tr><th>车号</th><th>仓</th><th>拒收原因</th><th>原结论</th><th>新化验（水/杂，逗号分隔多组）</th><th></th></tr></thead><tbody>'+
    rows.map(c=>`<tr><td>${esc(c.truck_id)}</td><td>${esc(c.bin_id)}</td><td>${esc(c.reject_reason)}</td>
      <td><div class="hashline">${esc(c.conclusion_id)}</div></td>
      <td><input id="rc-${esc(c.truck_id)}" value="${esc(c.moisture)},${esc(c.impurity)}" style="width:220px"></td>
      <td><button class="btn primary" data-truck="${esc(c.truck_id)}" data-contract="${esc(c.contract_id)}"
        data-bin="${esc(c.bin_id)}" data-net="${esc(c.net_kg)}">复验</button></td></tr>`).join('')+'</tbody></table>';
  box.querySelectorAll('button[data-truck]').forEach(btn=>btn.onclick=async()=>{
    const points=btn.previousElementSibling.value.split(';').flatMap(part=>{
      const [m,i]=part.split(',').map(v=>v.trim());return m?[{moisture:m,impurity:i}]:[];});
    try{
      const r=await api('/api/grading/trucks/recheck',{method:'POST',body:JSON.stringify({
        contract_id:btn.dataset.contract,truck_id:btn.dataset.truck,bin_id:btn.dataset.bin,
        net_kg:btn.dataset.net,points})});
      feedback.textContent=(r.mode==='idempotent_retry'?'同化验重复复验，幂等不计价：':'复验完成：')+
        `车 ${r.conclusion.truck_id} → ${r.conclusion.status} 等级 ${r.conclusion.grade??'等外'} 应付 ${r.conclusion.amount} 元`;
      refreshAll();
    }catch(err){feedback.textContent=err.message;}
  });
}
function renderEvents(events){
  document.querySelector('#event-count').textContent=events.length+' 条';
  document.querySelector('#event-table').querySelector('tbody').innerHTML=events.map(ev=>
    `<tr><td>${ev.seq}</td><td>${esc(ev.event_type)}</td><td>${esc(ev.truck_id??'')}</td>
     <td>${esc(ev.bin_id??'')}</td><td><div class="hashline">${esc(JSON.stringify(ev.payload))}</div></td>
     <td>${esc(ev.created.slice(0,19))}</td></tr>`).join('')||'<tr><td colspan="6">暂无事件。</td></tr>';
}
async function refreshAll(){
  try{
    const [pending,bins,ledger]=await Promise.all([
      api('/api/grading/pending'),api('/api/grading/bins'),api('/api/grading/ledger')]);
    renderPending(pending.trucks);
    const binViews=[];
    for(const b of bins.bins){
      try{binViews.push((await api('/api/grading/bins/'+encodeURIComponent(b.bin_id))).bin);}catch(_){}
    }
    renderBins(binViews);
    renderEvents(ledger.events);
    document.querySelector('#kpi-pending').textContent=pending.count;
    document.querySelector('#kpi-bins').textContent=bins.bins.length;
    document.querySelector('#kpi-events').textContent=ledger.events.length;
    const contractId=document.querySelector('#arrive-form').contract_id.value.trim();
    if(contractId){
      try{
        const batch=await api('/api/grading/batches/'+encodeURIComponent(contractId));
        renderTrucks(batch.truck_conclusions);
        document.querySelector('#kpi-pay').textContent=batch.summary.payable_total;
        document.querySelector('#kpi-trucks').textContent=batch.summary.accepted;
      }catch(_){renderTrucks([]);}
    }
    document.querySelector('#foot-ts').textContent=new Date().toISOString().slice(0,19)+' UTC';
  }catch(err){feedback.textContent=err.message;}
}
document.querySelector('#refresh').onclick=refreshAll;
document.querySelector('#print')?.addEventListener('click',()=>window.print());
refreshAll();
