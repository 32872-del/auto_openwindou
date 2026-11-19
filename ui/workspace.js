const statusNames={cancelled:'已取消',queued:'排队中',creating:'正在创建',running:'运行中',closing:'清理中',failed:'需处理',cleanup_failed:'清理失败',closed:'已清理',stopped:'已停止'};
const copyHeaders=['序号','窗口名称','启动编号','类型','厂商','系统','代理来源','代理或浏览器检测IP','IP协议','延迟(ms)','状态','IP读数来源','手机Chrome检测结果','手机Chrome逐线路读数'];
const typeName=k=>({desktop:'电脑浏览器',phone:'云手机',app:'云手机'}[k]);
function runQuery(){return new URLSearchParams({offset:state.offset,query:state.query,category:state.category||'all',kind:state.filterKind||'all',descending:!!state.descending})}
async function work(){
 const generation=state.generation,value=await api('/runs?'+runQuery());if(generation!==state.generation)return;
 state.rows=value.rows;state.total=value.total;state.cursor=value.cursor;
 $('#page').innerHTML=head('创建与运行','创建、使用、关闭和保留，都在这张表里。','<div class="flex"><button id="toggle-demo">'+(state.demo?'退出演示':'查看模拟记录')+'</button><button id="paste-table">粘贴邮箱</button><button class="primary" id="create">＋ 创建窗口</button></div>')+
 `${state.demo?'<div class="noticebox demo-banner">演示记录只保存在当前页面内存。按钮操作只改变模拟状态，不会创建、关闭或删除真实资源。</div>':''}<div class="metrics">${[['total','全部窗口'],['running','运行中'],['pending','排队 / 创建'],['failed','需处理']].map(([k,label])=>`<div class="metric"><strong data-stat="${k}">${value.stats[k]}</strong><span>${label}</span></div>`).join('')}<div class="info">存活监测：进程</div></div>
 <section class="card"><div class="filters"><div class="tabs">${[['all','全部窗口'],['kept','已保留'],['trash','回收站']].map(([k,label])=>`<button data-list="${k}" class="${(state.category||'all')===k?'active':''}">${label}</button>`).join('')}</div><div class="grow"></div><select id="type-filter" aria-label="按环境类型筛选"><option value="all">全部类型</option><option value="desktop">电脑浏览器</option><option value="phone">云手机</option></select><input id="search" aria-label="搜索窗口" placeholder="名称、IP、编号、错误…" value="${esc(state.query)}"></div>
	 <div class="toolbar"><span id="selection-note" class="muted"></span>${state.category==='trash'?'<button id="restore-selected">恢复选中</button><button class="danger" id="purge-selected">彻底删除选中</button>':'<button id="copy-selected">复制</button><button id="open-selected">打开选中</button><button id="close-selected">关闭选中</button><button class="danger" id="trash-selected">删除选中</button>'}<div class="grow"></div><button id="export">导出表格</button><button id="work-logs">日志</button></div>
 <div class="table-wrap"><table><thead><tr><th><input id="all-rows" type="checkbox" aria-label="全选"></th><th><button id="sort-number" class="link">序号 ↕</button></th><th>窗口名称 / 启动编号</th><th>类型 / 厂商</th><th>系统</th><th>代理来源</th><th>检测 IP</th><th>延迟</th><th>状态</th><th>操作</th></tr></thead><tbody id="rows"></tbody></table></div>
 <div class="foot"><span>双击复制单元格 · 框选后 Ctrl+C 粘贴到 Excel</span><div><button id="prev">上一页</button>　<span id="page-count"></span>　<button id="next">下一页</button></div></div></section>
	 <p class="sectionnote">浏览器关闭后按创建时的选项保留或删除，邮箱固定环境始终保留。删除记录先移入回收站；普通记录 30 天后自动清理，邮箱固定环境需手动彻底删除。云手机通过厂商接口启动，关闭时关机。</p>`;
	 renderRows();pagination();
 $('#toggle-demo').onclick=()=>act(async()=>{
  if(!state.demo)resetDemo();
  state.demo=!state.demo;state.generation++;state.offset=0;state.selected.clear();await work();
 });
 $('#create').disabled=state.demo;$('#paste-table').disabled=state.demo;
 $('#create').onclick=()=>act(async()=>{state.proxies=await api('/proxies');openCreate()});$('#paste-table').onclick=()=>openPaste(false);
	 if($('#copy-selected'))$('#copy-selected').onclick=()=>act(()=>copyRows());$('#export').onclick=exportRows;
 $('#search').oninput=e=>{state.query=e.target.value;clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>act(async()=>{state.offset=0;await updateRunPage()}),350)};
 $('#all-rows').onchange=e=>{state.rows.forEach(r=>e.target.checked?state.selected.add(r.id):state.selected.delete(r.id));renderRows()};
	 if($('#open-selected'))$('#open-selected').onclick=e=>act(()=>batchOpen(),e.target);
	 if($('#close-selected'))$('#close-selected').onclick=e=>act(()=>batchClose(),e.target);
	 if($('#trash-selected'))$('#trash-selected').onclick=e=>act(()=>deleteRuns(chosenRows(true)),e.target);
	 if($('#restore-selected'))$('#restore-selected').onclick=e=>act(()=>batchRunAction('restore'),e.target);
	 if($('#purge-selected'))$('#purge-selected').onclick=()=>deleteRuns(chosenRows(true),'purge');
	 $('#work-logs').onclick=()=>$('#logs').click();
 $$('[data-list]').forEach(el=>el.onclick=()=>act(async()=>{state.category=el.dataset.list;state.offset=0;state.selected.clear();await work()}));
 $('#type-filter').value=state.filterKind||'all';$('#type-filter').onchange=e=>act(async()=>{state.filterKind=e.target.value;state.offset=0;state.selected.clear();await updateRunPage()});
 $('#sort-number').onclick=()=>act(async()=>{state.descending=!state.descending;await updateRunPage()});
 $('#prev').onclick=()=>act(async()=>{state.offset=Math.max(0,state.offset-100);state.selected.clear();await updateRunPage()});$('#next').onclick=()=>act(async()=>{state.offset+=100;state.selected.clear();await updateRunPage()});
}
function pagination(){$('#page-count').textContent=`${state.total?state.offset+1:0}–${Math.min(state.offset+state.rows.length,state.total)} / ${state.total}`;$('#prev').disabled=state.offset===0;$('#next').disabled=state.offset+100>=state.total}
async function updateRunPage(){const generation=state.generation,query=runQuery().toString(),value=await api('/runs?'+query);if(generation!==state.generation||state.page!=='work'||query!==runQuery().toString())return;state.rows=value.rows;state.total=value.total;state.cursor=value.cursor;$$('[data-stat]').forEach(el=>el.textContent=value.stats[el.dataset.stat]);renderRows();pagination()}
function visibleRows(){return state.rows}
function chromeReadingStatus(r){
 const report=r.chrome_egress;
 if(!report?.status)return r.status==='failed'?'failed_unread':'unread';
 let status=report.status;
 if(status==='verified'||status==='mismatch'){
  const expected=report.expected_ip,probes=report.probes||[];
  const conflicts=samples=>{
   const ips=[...new Set([expected,...samples.map(p=>p.ip||p.previous_ip)].filter(Boolean))];
   return [false,true].some(ipv6=>ips.filter(ip=>ip.includes(':')===ipv6).length>1);
  };
  const manual=probes.filter(p=>(p.ip?p.automatic:(p.previous_automatic??p.automatic))!==true);
  // Older records may carry a positive status alongside contradictory or incomplete evidence.
  if(status==='verified'&&report.method!=='chrome_page')status='unavailable';
  else if(conflicts(manual))status='mismatch';
  else if(conflicts(probes))status='cached';
  else if(status==='verified'&&(!expected||report.last_read_error||report.error||probes.some(p=>p.error||(p.ip&&p.ip!==expected))))status='partial';
  else if(status==='verified'){
   const required=[['ip111.cn','ip111 国内线路'],['ip111.cn','ip111 国外线路'],['ip111.cn','ip111 谷歌线路'],
    ['ip.sb',expected.includes(':')?'IP.SB IPv6':'IP.SB IPv4']];
   const complete=required.every(([page,site])=>probes.some(p=>p.page_host===page&&p.site===site&&
    p.automatic===false&&p.ip===expected&&p.checked_at>0&&p.checked_at<=report.checked_at&&
    Number(report.expires_at)<=Number(p.checked_at)+120));
   if(!complete||!r.vendor_proxy?.ip||r.vendor_proxy.ip!==expected)status='partial';
  }
 }
 const sameLaunch=report.launch&&report.launch===r.launch;
 if(!sameLaunch)return status==='mismatch'?'past_mismatch':'past';
 if(status==='verified'&&(r.status!=='running'||!(Number(report.expires_at)*1000>Date.now())))return 'expired';
 return status;
}
const chromeReadingLabels={
 unread:'', verified:'两站已读线路与代理一致', mismatch:'页面出口不一致',
 failed_unread:'',cached:'开机页面可能保留旧 IP，请刷新后再读取',
 partial:'页面读数待补全', unavailable:'', expired:'上次读数已过期，请重新读取',
 past:'历史页面读数，本次待复测', past_mismatch:'历史页面出口不一致，本次待复测'
};
function chromeTime(seconds){
 const instant=new Date(Number(seconds)*1000);
 return seconds&&Number.isFinite(instant.getTime())?instant.toLocaleString('zh-CN',{hour12:false}):'';
}
function previousChromeReading(probe){
 if(probe.ip||!probe.previous_ip)return '';
 return ['上次读到',probe.previous_ip,chromeTime(probe.previous_checked_at)].filter(Boolean).join(' ');
}
function runIPSource(r){
 if(r.kind==='desktop')return '';
 if(r.vendor_proxy?.ip)return '开机前厂商检测';
 if(r.preflight_ip)return '本机代理预检';
 return r.ip?'记录中的代理 IP（来源未记录）':'';
}
function hasChromePageReadings(r){
 return r.chrome_egress?.method==='chrome_page'&&(r.chrome_egress.probes||[]).some(p=>p.ip||p.previous_ip);
}
function runIPCell(r){
 if(r.kind==='desktop')return esc(r.ip||'—');
 const source=runIPSource(r);
 if(r.provider!=='duoplus'&&!r.chrome_egress?.status)return esc(r.ip||'—')+(source?`<small>${esc(source)}</small>`:'');
 const status=chromeReadingStatus(r),chrome=r.chrome_egress||{};
 const hasReadings=hasChromePageReadings(r),label=hasReadings?(chromeReadingLabels[status]||''):'';
 const labels=label?[`<strong>Chrome · ${esc(label)}</strong>`]:[];
 const probes=hasReadings?(chrome.probes||[]):[];
 const ips=[...new Set(probes.map(p=>p.ip).filter(Boolean))];
 if(ips.length)labels.push(`<small>页面读数 · ${esc(ips.join(' / '))}</small>`);
 const prior=[...new Set(probes.map(previousChromeReading).filter(Boolean))];
 for(const reading of prior)labels.push(`<small>${esc(reading)}</small>`);
 const reference=r.vendor_proxy?.ip||r.preflight_ip||r.ip;
 if(reference)labels.push(`<small>代理参考 · ${esc(reference)} · ${esc(source)}</small>`);

 return labels.join('');
}
function phoneEgressResults(r){
 const chrome=r.chrome_egress||{},status=chromeReadingStatus(r);
 if(!hasChromePageReadings(r)){
  const reference=r.vendor_proxy?.ip||r.preflight_ip||r.ip;
  return reference?`<section class="noticebox egress-neutral">代理参考 · ${esc(reference)} · ${esc(runIPSource(r))}</section>`:'';
 }
 const switches={confirmed:'厂商已确认启用',unconfirmed:'厂商未确认启用',rejected:'厂商拒绝启用'};
 function probeList(probes){
  if(!probes?.length)return '';
  return `<ul>${probes.map(p=>{
   const hosts=[p.page_host,p.host!==p.page_host?p.host:''].filter(Boolean).join(' → ');
   const context=[hosts,chromeTime(p.checked_at),p.automatic?'开机时读取已有页面，可能为缓存':'',previousChromeReading(p)].filter(Boolean).join(' · ');
   return `<li>${esc(p.site||p.host)} · <b>${esc(p.ip||p.error||'未取得 IP')}</b>${context?`<small>${esc(context)}</small>`:''}</li>`;
  }).join('')}</ul>`;
 }
 function evidence(report){
  return `${report.expected_ip?`<p>比对的代理出口　${esc(report.expected_ip)}</p>`:''}
   ${switches[report.proxy_switch]?`<p>设备代理开关　${switches[report.proxy_switch]}</p>`:''}
   ${probeList(report.probes)}${report.error?`<p>${esc(report.error)}</p>`:''}
   `;
 }
 const mismatch=['mismatch','past_mismatch'].includes(status);
 const chromeClass=mismatch?'warningbox':status==='verified'?'':'egress-neutral';
 const stamp=[chrome.page_host,chromeTime(chrome.checked_at)].filter(Boolean).join(' · ');
 return `<section class="noticebox ${chromeClass}">
  <strong>手机 Chrome 检测页${chromeReadingLabels[status]?' · '+esc(chromeReadingLabels[status]):''}</strong>
  ${stamp?`<p>最近读取　${esc(stamp)}</p>`:''}
  ${evidence(status==='cached'&&chrome.status!=='cached'?{...chrome,error:''}:chrome)}
  ${chrome.last_read_error&&chrome.last_read_error!==chrome.error?`<p>最近一次读取未完成　${esc(chrome.last_read_error)}</p>`:''}
  ${mismatch?'<p>在同一次开机中分别刷新两个检测页，等各线路显示 IP 后逐页读取，以便核对差异。</p>':''}
 </section>`;
}
function rowValues(r){
 const chrome=r.chrome_egress||{},status=chromeReadingStatus(r);
 const readings=(hasChromePageReadings(r)?(chrome.probes||[]):[]).map(p=>[p.site||p.host,p.ip||p.error||'未取得 IP',chromeTime(p.checked_at),p.automatic?'开机时读取已有页面':'',previousChromeReading(p)].filter(Boolean).join(' '));
 return [r.number,r.name,r.launch,typeName(r.kind),r.label,r.os||'',r.source||'',r.ip||'',r.protocol||'',r.latency??'',r.trashed?'已回收':statusNames[r.status]||r.status,runIPSource(r),r.provider==='duoplus'&&hasChromePageReadings(r)?(chromeReadingLabels[status]||''):'',readings.join(' / ')];
}
let rowEgressTimer;
function renderRows(){
 notifyProxyCityUnavailable(state.rows);
 if(rowEgressTimer){clearTimeout(rowEgressTimer);rowEgressTimer=null}
 const current=state.rows.filter(r=>chromeReadingStatus(r)==='verified');
 if(current.length){
  const next=Math.min(...current.map(r=>r.chrome_egress.expires_at))*1000;
  rowEgressTimer=setTimeout(()=>{if(state.page==='work'&&$('#rows'))renderRows()},Math.max(1,next-Date.now()+1));
 }
	 const html=state.rows.map(r=>`<tr data-id="${r.id}"><td><input type="checkbox" data-check="${r.id}" ${state.selected.has(r.id)?'checked':''}></td><td data-col="0">${r.number}</td><td data-col="1">${esc(r.name)}<small class="mono">${esc(r.launch)}</small></td><td data-col="2">${esc(r.label)}<small>${typeName(r.kind)}</small></td><td data-col="3">${esc(r.os||'—')}</td><td data-col="4">${esc(r.source||'—')}</td><td data-col="5">${runIPCell(r)}</td><td data-col="6">${r.latency!=null?r.latency+' ms':'—'}</td><td data-col="7"><span class="status ${r.status.includes('failed')?'bad':''}" title="${esc(r.cleanup_error||r.error||'')}">${r.trashed?'已回收':statusNames[r.status]||r.status}</span><small>${esc(r.stage||'')}${r.keep?' · 已保留':''}</small>${r.viewer_status?`<small title="${esc(r.viewer_error||'')}">${esc(r.viewer_status)}</small>`:''}${r.tls_status?`<small title="${esc(r.tls_error||'')}">TLS · ${esc(r.tls_status)}</small>`:''}</td><td class="nowrap"><div class="rowactions">${r.trashed?`<button data-restore="${r.id}">恢复</button><button class="danger" data-purge="${r.id}">彻底删除</button>`:`<button data-copy="${r.id}">复制</button><button data-detail="${r.id}">详细</button><button data-edit="${r.id}" ${state.demo||state.runActions?.has(r.id)?'disabled':''} title="关闭并清理完成后可修改资料与下次启动设置">编辑</button><button class="v2" data-v2="${r.id}" ${r.kind!=='desktop'||r.provider==='ruyi'||r.status!=='running'||queryBusy(r)?'disabled title="仅支持运行中的兼容网页环境"':''}>${esc(r.v2||'V2')}</button><button data-open="${r.id}" ${state.runActions?.has(r.id)||!canOpenRun(r)?'disabled':''}>打开</button><button data-retry="${r.id}" ${state.runActions?.has(r.id)?'disabled':''}>${state.runActions?.has(r.id)?'处理中…':'重试'}</button><button class="danger" data-trash="${r.id}" ${state.runActions?.has(r.id)?'disabled':''}>删除</button>`}</div></td></tr>`).join('')||'<tr><td colspan="10"><div class="empty"><strong>没有符合条件的窗口</strong>可调整筛选，或点击右上角创建。</div></td></tr>';
 patchRowsHTML(html);
 if(typeof checkProgramRequests==='function')void checkProgramRequests(state.rows);
 $$('[data-check]').forEach(el=>el.onchange=()=>{el.checked?state.selected.add(el.dataset.check):state.selected.delete(el.dataset.check);selectionNote()});selectionNote();
	 $$('[data-copy]').forEach(el=>el.onclick=()=>act(()=>copy(formatBrief(state.rows.find(r=>r.id===el.dataset.copy)))));$$('[data-detail]').forEach(el=>el.onclick=()=>act(()=>detail(el.dataset.detail)));
	 $$('[data-edit]').forEach(el=>el.onclick=e=>act(()=>editRun(el.dataset.edit),e.currentTarget));
	 $$('[data-open]').forEach(el=>el.onclick=e=>act(()=>openOne(el.dataset.open),e.target));
	 $$('[data-retry]').forEach(el=>el.onclick=e=>act(()=>retryOrOpen(el.dataset.retry),e.target));
	 $$('[data-trash]').forEach(el=>el.onclick=e=>act(()=>deleteRuns(state.rows.filter(r=>r.id===el.dataset.trash)),e.target));
	 $$('[data-restore]').forEach(el=>el.onclick=e=>act(()=>batchRunAction('restore',[el.dataset.restore]),e.target));$$('[data-purge]').forEach(el=>el.onclick=()=>deleteRuns(state.rows.filter(r=>r.id===el.dataset.purge),'purge'));
 $$('[data-v2]').forEach(el=>el.onclick=()=>act(async()=>{const result=await api('/runs/'+el.dataset.v2+'/v2','POST',{});toast(result.message)},el));
 $$('td[data-col]').forEach(td=>td.ondblclick=()=>{if(state.system.double_click_copy)act(()=>copy(td.innerText))});bindCells();
}
async function copy(value){try{if(window.pywebview?.api?.write_clipboard){const r=await window.pywebview.api.write_clipboard(value);if(r.error)throw Error(r.error)}else await navigator.clipboard.writeText(value);toast('已复制')}catch{openDialog('复制内容',`<textarea rows="14" id="manual-copy">${esc(value)}</textarea><p class="note">请按 Ctrl+C 复制</p>`);$('#manual-copy').select()}}
function chosenRows(requireSelection=false){return state.rows.filter(r=>state.selected.has(r.id)||(!requireSelection&&!state.selected.size))}
function formatBrief(r){
 const loc=r.location||{},kind=r.kind==='desktop'?'浏':'云';
 const provider={duoplus:'duo',geelark:'gee',octo:'octo',morelogin:'more',bitbrowser:'bit',adspower:'ads',undetectable:'u',helix:'helix',ruyi:'ruyi',kameleo:'kam',hidemium:'hid',vmlogin:'vm',hub:'hub',chrome:'chrome'}[r.provider]||r.provider;
 const rawSystem=String(r.device_system||r.os||r.planned_system||'').trim();
 const version=(rawSystem.match(/\d+(?:\.\d+)?/)||[])[0]||'';
 const brand=String(r.device_brand||'').trim();
 const brandShort={samsung:'三星',oneplus:'一加',google:'谷歌',xiaomi:'小米',huawei:'华为',oppo:'OPPO',vivo:'vivo',honor:'荣耀',motorola:'摩托'}[brand.toLowerCase()]||brand;
 const system=r.kind==='desktop'?(rawSystem.toLowerCase().includes('mac')?'mac':rawSystem.toLowerCase().includes('win')?'win':rawSystem.toLowerCase().includes('android')?'安卓':rawSystem.toLowerCase().replace(/\s+/g,''))+(version||''):(brandShort||'安卓')+(version||'');
 const protocol=String(r.protocol||r.ip_family||r.requested_ip_family||'').toLowerCase();
 const ip=protocol.includes('ipv4')&&protocol.includes('ipv6')||protocol==='dual'?'ipv46':protocol.includes('ipv6')?'ipv6':protocol.includes('ipv4')?'ipv4':'';
 const source=String(r.source||'').trim();
 const proxy=/boltproxy|bolt/i.test(source)?'bol':/niceproxy|9proxy/i.test(source)?'9p':source==='直连'?'直':source==='待取得代理'?'待代理':source.replace(/[^\p{L}\p{N}]/gu,'').slice(0,6);
 const level=r.proxy_level||(/州代理/.test(source)?'state':/国家代理/.test(source)?'country':loc.city?'city':loc.state?'state':loc.country?'country':'');
 const region=level==='state'?'州':level==='city'?'城市':level==='country'?'国家':'';
 return [kind,provider,system,ip,proxy,region,r.launch].filter(Boolean).join('+');
}
function tableText(rows){return [copyHeaders.join('\t'),...rows.map(r=>rowValues(r).map(x=>String(x??'').replace(/[\r\n\t]/g,' ')).join('\t'))].join('\r\n')}
async function copyRows(){const data=chosenRows();if(!data.length)return toast('没有可复制的行');await copy(data.map(formatBrief).join('\n'));if($('#overlay').classList.contains('hidden'))toast('已复制 '+data.length+' 条'+(state.selected.size?'选中记录':'当前页筛选记录'))}
function selectionNote(){const n=state.rows.filter(r=>state.selected.has(r.id)).length;if($('#selection-note'))$('#selection-note').textContent=`已选 ${n} 项`;const all=$('#all-rows');if(all){all.checked=n>0&&n===state.rows.length;all.indeterminate=n>0&&n<state.rows.length}}
function canOpenRun(r){return !r.trashed&&(r.kind!=='desktop'||r.keep||r.account_email)&&!['closed','closing','cleanup_failed'].includes(r.status)}
async function openOne(id){return withRunActions([id],async()=>{const result=await api('/runs/'+id+'/resume','POST',{});await updateRunPage();toast(result.message)})}
async function batchOpen(){
 const rows=chosenRows(true);if(!rows.length)throw Error('请先勾选当前页需要打开的记录');
 return withRunActions(rows.map(r=>r.id),async()=>{
  const result=await api('/run-batch/open','POST',{ids:rows.map(r=>r.id)});
  const errors=(result.results||[]).filter(r=>!r.ok);
  await updateRunPage();
  if(errors.length)openDialog('批量打开结果',`<p>${esc(result.message)}</p><pre>${esc(errors.map(r=>(rows.find(x=>x.id===r.id)?.name||r.id)+'：'+r.error).join('\n'))}</pre>`);
  else toast(result.message);
 });
}
async function batchClose(){const rows=chosenRows(true);if(!rows.length)throw Error('请先勾选当前页需要关闭的记录');const errors=[];for(const r of rows){try{await api('/runs/'+r.id+'/close','POST',{})}catch(e){errors.push(r.name+'：'+e.message)}}if(errors.length)openDialog('批量关闭结果',`<p>已请求关闭 ${rows.length-errors.length} 条，${errors.length} 条未完成。</p><pre>${esc(errors.join('\n'))}</pre>`);else toast(`已请求关闭 ${rows.length} 条`);await updateRunPage()}
async function batchRunAction(action,ids=null){const chosen=ids||chosenRows(true).map(r=>r.id);if(!chosen.length)throw Error('请先勾选当前页需要操作的记录');return withRunActions(chosen,async()=>{const result=await api('/run-batch/'+action,'POST',{ids:chosen});chosen.forEach(id=>state.selected.delete(id));await updateRunPage();toast(result.message)})}
async function retryOrOpen(id){return withRunActions([id],async()=>{const result=await api('/runs/'+id+'/retry','POST',{});await updateRunPage();toast(result.message||('已排队重试 '+result.ids.length+' 条，将重新取得并检测代理'))})}
async function withRunActions(ids,fn){state.runActions??=new Set();if(ids.some(id=>state.runActions.has(id)))throw Error('该记录正在处理，请稍候');ids.forEach(id=>state.runActions.add(id));renderRows();try{return await fn()}finally{ids.forEach(id=>state.runActions.delete(id));if(state.page==='work')renderRows()}}
function download(name,text,type='text/tab-separated-values;charset=utf-8'){const url=URL.createObjectURL(new Blob([text],{type})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function exportRows(){download('窗口记录.tsv','\ufeff'+tableText(chosenRows()))}
let cellStart=null,cellEnd=null,drag=false;
function bindCells(){$$('td[data-col]').forEach(td=>{td.onpointerdown=e=>{if(e.button!==0)return;cellStart=td;cellEnd=td;drag=true;markCells()};td.onpointerenter=()=>{if(drag){cellEnd=td;markCells()}}})}
function marked(){if(!cellStart?.isConnected||!cellEnd?.isConnected)return [];const rs=$$('#rows tr'),a=rs.indexOf(cellStart.parentElement),b=rs.indexOf(cellEnd.parentElement),x=+cellStart.dataset.col,y=+cellEnd.dataset.col;return rs.slice(Math.min(a,b),Math.max(a,b)+1).map(tr=>Array.from(tr.querySelectorAll('td[data-col]')).filter(td=>+td.dataset.col>=Math.min(x,y)&&+td.dataset.col<=Math.max(x,y)))}
function markCells(){$$('.cell-selected').forEach(td=>td.classList.remove('cell-selected'));marked().flat().forEach(td=>td.classList.add('cell-selected'))}
document.addEventListener('pointerup',()=>drag=false);document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='c'&&!/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)&&marked().length&&$('#overlay').classList.contains('hidden')){e.preventDefault();act(()=>copy(marked().map(r=>r.map(td=>td.innerText.replace(/\n/g,' ')).join('\t')).join('\n')))}});
function purgeDialog(ids){if(!ids.length)return toast('请先勾选回收站记录');openDialog('彻底删除',`<div class="stack"><p>将彻底删除 ${ids.length} 条回收记录及其仍保留的浏览器环境。此操作无法恢复。</p><div class="flex"><button id="cancel-purge">取消</button><button class="danger" id="confirm-purge">彻底删除</button><button id="purge-local">仅移除本地记录</button></div></div>`);$('#cancel-purge').onclick=closeDialog;$('#purge-local').onclick=()=>removeLocalDialog(ids);$('#confirm-purge').onclick=e=>act(async()=>{const result=await api('/run-batch/purge','POST',{ids});ids.forEach(id=>state.selected.delete(id));closeDialog();await updateRunPage();toast(result.message)},e.target)}
async function detail(id){
 const r=state.rows.find(r=>r.id===id),loc=r.location||{};
 const canRemoveLocal=r.provider==='octo'||!['queued','creating','running','closing'].includes(r.status);
	 openDialog(r.name,`<div class="stack">
  <div class="flex"><button id="copy-diagnostic">复制诊断</button>${canRemoveLocal?'<button class="danger" id="remove-local">强制删除本地记录</button>':''}</div>
  ${r.provider==='duoplus'?`<div class="stack" id="phone-egress">${!state.demo&&!r.trashed&&r.status==='running'?'<div><button id="read-phone-egress">读取手机 Chrome 检测页</button><small>在同一台手机的 Chrome 分别刷新 ip111.cn 和 ip.sb，等 IP 显示后，每页各点一次读取。两次读取请在 2 分钟内完成。</small></div>':''}<div id="egress-results">${phoneEgressResults(r)}</div></div>`:''}
  ${r.error||r.cleanup_error?`<div class="noticebox warningbox"><strong>本次未完成的原因</strong><p>启动：${esc(r.error||'无')}</p>${r.cleanup_error?`<p>清理：${esc(r.cleanup_error)}</p>`:''}<small>失败阶段：${esc(r.stage||'未记录')}</small></div>`:`<div class="noticebox">当前：${statusNames[r.status]} · ${r.kind!=='desktop'?'关闭时关机，保留云手机数据':r.keep?'关闭后保留环境':'关闭后自动删除浏览器环境'}</div>`}<dl class="details-grid">${[['绑定邮箱',r.account_email||r.input_data?.email||'未绑定'],['资料名称',r.input_data?.name||'未填写'],['名',r.input_data?.firstName||'未填写'],['姓',r.input_data?.lastName||'未填写'],['电话',r.input_data?.phone||'未填写'],['云手机画面',r.viewer_error||r.viewer_status||'未打开'],['短任务连接',r.connection_phase||'已断开'],['代理位置提示',r.proxy_warning||'无'],['启动编号',r.launch],['类型',typeName(r.kind)],['厂商 / 系统',r.label+' / '+(r.os||'待创建')],['代理来源',r.source],[r.kind==='desktop'?'实际出口':'代理检测 IP',[r.ip,r.protocol].filter(Boolean).join(' · ')||'未检测'],['目标位置',[loc.country,loc.state,loc.city,loc.zip].filter(Boolean).join(' / ')||'未指定'],['地址',r.address||'未填写'],['备注',r.note||'未填写'],['原始数据行',r.source_row||'手工创建'],['进程绑定',r.pid?`PID ${r.pid} / 创建时间 ${r.created}`:'尚未绑定或已经退出'],['厂商环境ID',r.profile_id||(r.creation_state==='not_requested'||r.creation_state==='rejected'?'未创建':'创建结果待确认')],['创建状态',({not_requested:'未发送创建请求',requesting:'创建请求已发送',created:'已创建',rejected:'厂商明确拒绝创建',uncertain:'结果未知，远端可能已有环境',deleted:'已确认删除'})[r.creation_state]||'旧记录，请结合失败阶段核实'],['代理尝试次数',r.proxy_attempts??'未记录']].map(([a,b])=>`<dt>${a}</dt><dd>${esc(b)}</dd>`).join('')}</dl>${tlsDetails(r)}<h3>检测存档</h3><div id="detail-archives">正在读取…</div><h3>本次执行过程</h3><div id="detail-timeline">正在读取…</div></div>`,true);
 const extras=[];
 if(r.kind!=='desktop'){
  extras.push(['联网模式',({wifi:'Wi-Fi',mobile:'移动网络',keep:'沿用设备设置'})[r.network_mode]||'沿用设备设置']);
  if(r.network_report?.status==='configured'&&r.network_report.vendor_wifi_status===1){
   extras.push(['联网设置核验','厂商 Wi-Fi 仿真开关已确认开启']);
   extras.push(['Android 底层网络',r.network_report.android_summary||'未读取']);
  }
  if(loc.city && typeof r.allow_city_fallback==='boolean'){
   extras.push(['本次城市匹配',r.allow_city_fallback?'城市不可用时允许同州 / 省':'仅限 '+loc.city]);
  }
  if(r.preflight_ip)extras.push(['本机代理预检 IP',r.preflight_ip]);
  if(r.vendor_candidate?.ip){
   const candidate=r.vendor_candidate,geo=candidate.geo||{};
   extras.push(['厂商绑定前检测',[candidate.ip,geo.countryCode,geo.region,geo.city].filter(Boolean).join(' / ')]);
  }
  if(r.vendor_proxy?.ip)extras.push(['开机前厂商检测 IP',r.vendor_proxy.ip]);
 }
 const geo=r.vendor_proxy?.geo||r.geo;
 if(geo){
  const region=[geo.countryCode||geo.country_code||geo.country,geo.region||geo.subdivision,geo.city].filter(Boolean).join(' / ');
  if(region)extras.push([r.vendor_proxy?.ip?'厂商检测出口地区':'本机预检出口地区',region]);
 }
 $('#dialog-body .details-grid').insertAdjacentHTML('beforeend',extras.map(([label,value])=>`<dt>${label}</dt><dd>${esc(value)}</dd>`).join(''));
 let diagnostic=r;
 const resultsPanel=$('#egress-results');
 function renderEgressPanel(){
  if(!resultsPanel?.isConnected)return;
  resultsPanel.innerHTML=phoneEgressResults(diagnostic);
  if(chromeReadingStatus(diagnostic)==='verified'){
   clearTimeout(dialogTimer);
   dialogTimer=setTimeout(renderEgressPanel,Math.max(1,diagnostic.chrome_egress.expires_at*1000-Date.now()+1));
  }
 }
 if(chromeReadingStatus(r)==='verified')renderEgressPanel();
 const readEgress=$('#read-phone-egress');
 if(readEgress)readEgress.onclick=()=>act(async()=>{
  const result=await api('/runs/'+id+'/device-egress','POST',{});
  diagnostic={...diagnostic,chrome_egress:result.chrome_egress};
  if(readEgress.isConnected){
   renderEgressPanel();
   const status=chromeReadingStatus(diagnostic);
   toast(status===result.chrome_egress.status?result.message:'手机 Chrome · '+chromeReadingLabels[status]);
  }
  if(state.page==='work')await updateRunPage();
 },readEgress);
	 $('#copy-diagnostic').onclick=()=>act(()=>copy(JSON.stringify(diagnostic,null,2)));
 if($('#remove-local'))$('#remove-local').onclick=()=>removeLocalDialog([id]);
 try{const result=await api('/runs/'+id+'/history');if($('#detail-timeline'))$('#detail-timeline').innerHTML=`<div class="timeline">${result.events.map(e=>`<div>${esc(e.stage||e.status)}<small>${esc(e.time)} ${e.operation==='cleanup'?'清理':'启动'}：${esc(e.error||'')}</small></div>`).join('')||'暂无过程记录'}</div>`;if($('#detail-archives'))$('#detail-archives').innerHTML=archiveButtons(result.archives||[]);bindArchiveButtons()}catch(e){if($('#detail-timeline'))$('#detail-timeline').textContent=e.message;if($('#detail-archives'))$('#detail-archives').textContent='尚无存档'}
}

function tlsDetails(r){
 if(!r.tls_status)return '';
 return `<div class="noticebox"><strong>TLS 检测 · ${esc(r.tls_status)}</strong>${r.tls_captured_at?`<p>检测时间 ${esc(r.tls_captured_at)}</p>`:''}${r.tls_error?`<p>${esc(r.tls_error)}</p>`:''}</div>`;
}
function queryBusy(r){return !!r.connection_phase&&r.connection_phase!=='已断开'}
function patchRowsHTML(html){
 const body=$('#rows'),template=document.createElement('template');template.innerHTML=`<table><tbody>${html}</tbody></table>`;
 const previous=new Map(Array.from(body.children).filter(r=>r.dataset.id).map(r=>[r.dataset.id,r]));
 const incoming=Array.from(template.content.querySelector('tbody').children),wanted=new Set();
 for(const fresh of incoming){const id=fresh.dataset.id,existing=previous.get(id);wanted.add(id);
  if(existing&&existing.children.length===fresh.children.length){
   Array.from(existing.children).forEach((cell,i)=>{if(cell.innerHTML!==fresh.children[i].innerHTML)cell.innerHTML=fresh.children[i].innerHTML});
   body.append(existing);
  }else body.append(fresh);
 }
 for(const child of Array.from(body.children))if(!wanted.has(child.dataset.id)||(!child.dataset.id&&child!==body.lastElementChild))child.remove();
 markCells();
}
async function applyRunEvents(data){
 notifyProxyCityUnavailable((data.events||[]).map(event=>event.row));
 if(data.reset){await updateRunPage();return}
 const changes=new Map(data.events.map(e=>[e.row.id,e.row]));
 const known=new Map(state.rows.map(r=>[r.id,r]));
 const affectsPage=Array.from(changes.values()).some(r=>!known.has(r.id)||r.deleted||Boolean(r.trashed)!==Boolean(known.get(r.id)?.trashed)||(state.category==='kept'&&r.keep!==known.get(r.id)?.keep));
 if(data.events.length&&(affectsPage||state.query)){await updateRunPage();return}
 if(changes.size){state.rows=state.rows.map(r=>changes.get(r.id)||r);renderRows()}
 if(data.stats)$$('[data-stat]').forEach(el=>el.textContent=data.stats[el.dataset.stat]);
 state.cursor=data.cursor??data.events.at(-1)?.seq??state.cursor;
}

function deleteRuns(rows,normalAction='trash'){
 const ids=rows.map(row=>row.id);
 const octoIds=rows.filter(row=>row.provider==='octo').map(row=>row.id);
 if(octoIds.length)return removeLocalDialog(octoIds,ids,normalAction);
 if(normalAction==='purge')return purgeDialog(ids);
 return batchRunAction('trash',ids);
}

function removeLocalDialog(ids,normalIds,normalAction='trash'){
 const localTarget=ids.length===1?state.rows.find(row=>row.id===ids[0])?.name:'';
 const scope=normalIds?`选中的 ${ids.length} 条 O 浏览器本地记录`:`这 ${ids.length} 条本地记录`;
 const target=localTarget?`${scope}（${esc(localTarget)}）`:scope;
 const octoOnly=ids.every(id=>state.rows.some(row=>row.id===id&&row.provider==='octo'));
 const purge=normalAction==='purge',normalLabel=purge?'彻底删除':'常规删除';
 let normalNote='';
 if(normalIds)normalNote=purge?`将彻底删除选中的 ${normalIds.length} 条回收记录及其仍保留的浏览器环境，此操作无法恢复。`:`常规删除将对选中的 ${normalIds.length} 条记录执行原有清理流程。`;
 openDialog(purge?'彻底删除回收记录':normalIds?'删除窗口记录':'强制删除本地记录',`<div class="stack">
  ${normalNote?`<p>${normalNote}</p>`:''}
  <p>强制删除将移除${target}及关联的邮箱绑定。记录无法恢复，再次添加相同邮箱可能创建新环境。</p>
  <p>选择强制删除本地记录时，厂商端已有的环境和进程会保留。${octoOnly?'O 浏览器客户端离线或登录账号不同时也可使用。':''}</p>
  <div class="flex"><button id="cancel-local">取消</button>${normalIds?`<button id="normal-delete">${normalLabel}</button>`:''}<button class="danger" id="confirm-local">确认强制删除本地记录</button></div>
  <div id="delete-error" class="error" role="alert"></div></div>`);
 $('#cancel-local').onclick=closeDialog;
 const errorBox=$('#delete-error');
 function submit(action,targets,button){
  return act(async()=>{
   errorBox.textContent='';
   try{
    await batchRunAction(action,targets);
    if($('#delete-error')===errorBox)closeDialog();
   }catch(error){
    errorBox.textContent=error.message;
    throw error;
   }
  },button);
 }
 if(normalIds)$('#normal-delete').onclick=e=>submit(normalAction,normalIds,e.currentTarget);
 $('#confirm-local').onclick=e=>submit('remove-local',ids,e.currentTarget);
}
