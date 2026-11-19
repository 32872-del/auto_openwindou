'use strict';
const $=s=>document.querySelector(s),$$=s=>Array.from(document.querySelectorAll(s));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={page:'work',kind:'desktop',key:'octo',catalog:[],doc:null,plan:null,proxies:null,dirty:false,rows:[],offset:0,total:0,cursor:0,selected:new Set(),query:'',drafts:{},generation:0,system:{},demo:false,preferences:{create:{},workspace:{},paste:{},panels:{}}};
let toastTimer,dialogTimer,screenURL;
let preferenceTimer;
const pendingPreferences={};
let preferenceSaving=Promise.resolve();
function flushPreferences(){
 clearTimeout(preferenceTimer);
 if(!Object.keys(pendingPreferences).length)return preferenceSaving;
 const changes=structuredClone(pendingPreferences);
 Object.keys(pendingPreferences).forEach(key=>delete pendingPreferences[key]);
 preferenceSaving=preferenceSaving.then(async()=>{
  try{const saved=await api('/ui/preferences','PUT',changes);state.preferences.revision=saved.revision}
  catch(error){
   for(const [section,values] of Object.entries(changes))pendingPreferences[section]={...values,...pendingPreferences[section]};
   console.warn('界面偏好未保存：'+error.message);
  }
 });
 return preferenceSaving;
}
function remember(section,changes,immediate=false){
 state.preferences[section]={...(state.preferences[section]||{}),...changes};
 pendingPreferences[section]={...(pendingPreferences[section]||{}),...changes};
 clearTimeout(preferenceTimer);
 if(immediate)return flushPreferences();
 preferenceTimer=setTimeout(flushPreferences,350);
}
function toast(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').classList.add('show');toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),5000)}
async function api(path,method='GET',body){if(state.demo&&(path.startsWith('/runs')||path.startsWith('/run-batch/')))return demoRequest(path,method,body);const control=new AbortController(),timer=setTimeout(()=>control.abort(),240000);try{const res=await fetch('/api'+path,{method,signal:control.signal,headers:{'X-Window-Token':window.API_TOKEN,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});let data;try{data=await res.json()}catch{throw Error('本机服务未返回有效内容（HTTP '+res.status+'）')}if(!res.ok){const error=Error(data.message||JSON.stringify(data.detail||data));Object.assign(error,{code:data.code,status:res.status,provider:data.provider,label:data.label});throw error;}return data}catch(e){if(e.name==='AbortError')throw Error('请求 '+path+' 等待超过 240 秒，请查看日志确认处理结果后再操作');throw e}finally{clearTimeout(timer)}}
async function act(fn,button){if(button)button.disabled=true;try{for(let attempt=0;attempt<15;attempt++){try{return await fn()}catch(e){if(e.code==='PROGRAM_REQUIRED'&&await requestProgram(e))continue;throw e}}}catch(e){toast(e.message);console.error(e)}finally{if(button?.isConnected)button.disabled=false}}
function head(title,note,action=''){return `<div class="page-head"><div><h1>${title}</h1><p>${note}</p></div>${action}</div>`}
let dialogOpener=null;
function openDialog(title,html,wide=false){clearInterval(dialogTimer);closeHelp();if($('#overlay').classList.contains('hidden'))dialogOpener=document.activeElement;$('.drawer').classList.toggle('wide',wide);$('#dialog-title').textContent=title;$('#dialog-body').oninput=null;$('#dialog-body').onchange=null;$('#dialog-body').innerHTML=html;$('#overlay').classList.remove('hidden');$('#dialog-body').scrollTop=0;$('#dialog-body').classList.toggle('catalog-body',!!$('#dialog-body > .catalog-dialog'));$('#close-dialog').focus({preventScroll:true})}
function closeDialog(){clearInterval(dialogTimer);if(screenURL)URL.revokeObjectURL(screenURL);$('#overlay').classList.add('hidden');closeHelp();if(dialogOpener?.isConnected)dialogOpener.focus({preventScroll:true})}
$('#close-dialog').onclick=closeDialog;$('#overlay').onclick=e=>{if(e.target===$('#overlay'))closeDialog()};document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDialog()});
function stash(){if(state.page==='environment'&&state.doc&&state.editId&&$('#provider-form'))state.drafts[state.editId]={doc:structuredClone(state.doc),plan:structuredClone(state.plan),dirty:state.dirty}}
async function page(name){stash();if(name!=='work')state.demo=false;state.page=name;state.generation++;await remember('workspace',{page:name,kind:state.kind,key:state.key},true);window.scrollTo(0,0);$$('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===name));$('#breadcrumb').textContent={work:'创建与运行',environment:'环境方案',proxy:'代理来源',settings:'系统设置',records:'检测记录'}[name];await ({environment:environment,work:work,proxy:proxyPage,settings:settingsPage,records:recordsPage})[name]()}
$$('nav button').forEach(b=>b.onclick=()=>act(()=>page(b.dataset.page)));$('#refresh').onclick=()=>act(()=>page(state.page));
function types(){return `<div class="tabs">${[['desktop','电脑浏览器'],['phone','云手机']].map(([k,t])=>`<button data-kind="${k}" class="${state.kind===k?'active':''}">${t}</button>`).join('')}</div>`}
async function environment(){
 const generation=state.generation;state.proxies=await api('/proxies');if(generation!==state.generation)return;
 const list=state.catalog.filter(p=>p.family===(state.kind==='desktop'?'desktop':'cloud'));if(!list.some(p=>p.key===state.key))state.key=list[0].key;
 $('#page').innerHTML=head('环境方案','选择厂商，配置系统、设备与代理范围。创建时使用已保存参数。',state.kind==='desktop'?'<button id="edit-weights">电脑浏览器随机比例</button>':'')+types()+`<div class="layout"><div class="vendors">${list.map(p=>`<button class="vendor ${p.key===state.key?'active':''}" data-provider="${p.key}"><i>${esc(p.name.substring(0,2))}</i><span><strong>${esc(p.name)}</strong><small>${p.family==='cloud'?'设备与连接独立配置':'本厂商专属参数'}</small></span></button>`).join('')}</div><section class="card" id="provider-panel"><div class="empty">正在载入配置…</div></section></div>`;
 $$('[data-kind]').forEach(b=>b.onclick=()=>act(async()=>{stash();state.kind=b.dataset.kind;const choices=state.catalog.filter(p=>p.family===(state.kind==='desktop'?'desktop':'cloud'));if(!choices.some(p=>p.key===state.key))state.key=choices[0].key;await remember('workspace',{kind:state.kind,key:state.key},true);state.generation++;await environment()}));
 if($('#edit-weights'))$('#edit-weights').onclick=()=>act(weightDialog);
 $$('[data-provider]').forEach(b=>b.onclick=()=>act(async()=>{stash();state.key=b.dataset.provider;await remember('workspace',{kind:state.kind,key:state.key},true);state.generation++;await environment()}));
 const key=state.key,kind=state.kind;const draft=state.drafts[kind+':'+key];
 const [doc,plan]=draft?[draft.doc,draft.plan]:await Promise.all([api('/providers/'+key),api('/plans/'+kind+'/'+key)]);
 if(generation!==state.generation||key!==state.key||kind!==state.kind)return;
 state.doc=doc;state.plan=plan;state.dirty=!!draft?.dirty;renderProvider();
}
function catalogCacheNote(result){const c=result.cache;if(!c)return '';return (c.hit?'本地缓存':'已更新缓存')+(c.saved_at?' · '+new Date(c.saved_at).toLocaleString():'')+(c.refresh_error?'。刷新未完成，'+(c.hit?'保留旧列表。':'')+c.refresh_error:'')}
function help(text){return text?`<button type="button" class="help" data-help="${esc(text)}" aria-label="查看说明" aria-expanded="false">?</button>`:''}
let activeHelp=null;
function closeHelp(){if(activeHelp)activeHelp.setAttribute('aria-expanded','false');activeHelp=null;$('#help-popover')?.remove()}
function showHelp(button){
 if(activeHelp===button){closeHelp();return}closeHelp();activeHelp=button;button.setAttribute('aria-expanded','true');
 const box=document.createElement('div');box.id='help-popover';box.setAttribute('role','tooltip');box.textContent=button.dataset.help;
 Object.assign(box.style,{position:'fixed',zIndex:'10000',maxWidth:'min(360px, calc(100vw - 24px))',padding:'12px 16px',background:'#fff',color:'#253d34',border:'1px solid #c9d9d0',borderRadius:'8px',boxShadow:'0 5px 24px #0002',fontSize:'13px',lineHeight:'1.7',whiteSpace:'pre-wrap'});document.body.appendChild(box);
 positionHelp();
}
function positionHelp(){
 const box=$('#help-popover');if(!box||!activeHelp)return;
 if(!activeHelp.isConnected){closeHelp();return}
 const r=activeHelp.getBoundingClientRect(),w=box.offsetWidth,h=box.offsetHeight;
 if(r.bottom<0||r.top>innerHeight){closeHelp();return}
 box.style.left=Math.max(12,Math.min(r.left,innerWidth-w-12))+'px';box.style.top=Math.max(12,Math.min(innerHeight-h-12,r.bottom+h+18<innerHeight?r.bottom+8:r.top-h-8))+'px';
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-help]');if(b){e.preventDefault();e.stopPropagation();showHelp(b)}else if(!e.target.closest('#help-popover'))closeHelp()},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&activeHelp){e.preventDefault();e.stopImmediatePropagation();closeHelp()}},true);
window.addEventListener('resize',positionHelp);document.addEventListener('scroll',e=>{if(!e.target.closest?.('#help-popover'))positionHelp()},true);
function field(f,v){
 const name=`<span class="field-name">${esc(f.label)}${help(f.hint)}</span>`;let input='';
	 if(choiceEnabled(f))return choiceField(f,v);
 if(f.type==='multi')return `<div class="wide-field"><label>${name}</label><div class="chips" style="margin-top:7px">${f.options.map(([value,label])=>`<label class="chip"><input type="checkbox" data-array="${f.key}" value="${esc(value)}" ${v.includes(value)?'checked':''}>${esc(label)}</label>`).join('')}</div></div>`;
 if(f.type==='weights')return weightsField(f,v);
 if(f.type==='bool')return `<label><span class="field-name">${esc(f.label)}${help(f.hint)}</span><div class="switch"><span>启用</span><input type="checkbox" data-field="${f.key}" ${v?'checked':''}></div></label>`;
 if(f.type==='select')input=`<select data-field="${f.key}">${f.options.map(([value,label])=>`<option value="${esc(value)}" ${value===v?'selected':''}>${esc(label)}</option>`).join('')}</select>`;
 else if(f.type==='list')input=`<textarea rows="2" data-field="${f.key}" placeholder="${esc(f.hint)}">${esc(v.join(', '))}</textarea>`;
 else input=`<input data-field="${f.key}" type="${f.type==='number'?'number':'text'}" ${f.type==='number'?`min="${f.minimum}" max="${f.maximum}"`:''} value="${esc(v)}" autocomplete="off" placeholder="${esc(f.default||'未填写')}">`;
 return `<label class="${['manager_path','browser_path','api_url','remote_api_url','fingerprint_directory','core_root'].includes(f.key)?'wide-field':''}">${name}${input}</label>`;
}
function markDirty(){state.dirty=true;state.editRevision=(state.editRevision||0)+1;const el=$('#save-note');if(el)el.textContent='尚未保存'}
function panelOpen(key,index){return state.preferences.panels?.[key] ?? false}
function simplifyEnvironment(){
 const host=$('#provider-form'),advanced=document.createElement('details');
 advanced.className='environment-advanced';advanced.innerHTML='<summary>高级环境选项（系统、设备筛选等）</summary><p class="note">不展开也会保留原有设置；粘贴新云手机资料固定使用全部空闲设备。</p>';
 const groups=Array.from(host.querySelectorAll(':scope > details[data-panel]'));
 for(const group of groups){
  const name=group.querySelector('summary').textContent;
  if(name==='连接'){group.open=true;group.classList.add('environment-common');group.querySelector('summary').textContent='常用 · 连接设置'}
  else advanced.append(group);
 }
 if(advanced.querySelector('details'))host.append(advanced);
}
function renderProvider(){
 state.editId=state.kind+':'+state.key;
 const p=state.catalog.find(p=>p.key===state.key),groups=[...new Set(p.fields.filter(f=>f.group!=='兼容').map(f=>f.group))];
 $('#provider-panel').innerHTML=`<div class="form-head"><div><h2>${esc(p.name)}</h2><small>${state.kind==='desktop'?'电脑环境配置':'API 管理已有设备 · 关闭窗口后自动关机'}</small></div><div class="flex"><span id="save-note" class="save-note">${state.dirty?'尚未保存':'已载入'}</span><button id="save-provider" class="primary">保存配置</button></div></div><div class="form-body" id="provider-form"><p class="note" style="padding-top:15px">${esc(p.note)}</p>${groups.map((g,i)=>`<details data-panel="${esc(state.key+':'+g)}" ${panelOpen(state.key+':'+g,i)?'open':''}><summary>${esc(g)}</summary><div class="fields">${p.fields.filter(f=>f.group===g).map(f=>field(f,state.doc.settings[f.key])).join('')}</div>${g==='连接'?`<div class="flex" style="margin-top:16px"><button id="probe">${p.family==='cloud'?'扫描设备':'测试连接'}</button><small>只查询状态，不创建环境</small></div><div id="probe-result"></div>`:''}${g==='设备'?'<div id="devices"></div>':''}</details>`).join('')}
	 <details open><summary>本方案的启动与代理</summary><div class="fields">${state.kind==='desktop'?`<label class="wide-field">默认打开的网页<input id="plan-url" value="${esc(state.plan.settings.url)}"></label>`:''}${state.kind!=='desktop'?`<p class="note wide-field">使用已有云手机。自动启动时不弹出控制画面；列表删除或退出软件时实际关机，不删除设备。</p>`:`<p class="note wide-field">浏览器在当前桌面启动；窗口关闭后自动保留环境，删除操作移入回收站。</p>`}
 <label class="wide-field">代理使用方式<select id="plan-proxy-mode">${proxyModeOptions(state.plan.settings.proxy_mode)}</select></label><div class="wide-field" id="plan-proxy-options"><span class="field-name">指定代理来源${help('一个固定使用；多个按来源权重随机。继承和直连请用上方选择。')}</span><div class="chips" style="margin-top:8px">${state.proxies.sources.map(s=>`<label class="chip"><input data-source="${esc(s.id)}" type="checkbox" ${state.plan.settings.proxy_sources.includes(s.id)?'checked':''}>${esc(s.name)}${s.enabled?'':'（停用）'}</label>`).join('')||'<small>尚未配置代理来源。可先到“代理来源”添加。</small>'}</div></div></div></details>
 <p class="note">厂商参数与方案保存到通用配置 / 共用配置.json。本机程序路径保存在专属配置 / 本机设置。</p></div>`;
 $$('[data-field]').forEach(el=>el.oninput=()=>{const f=p.fields.find(f=>f.key===el.dataset.field);state.doc.settings[f.key]=f.type==='bool'?el.checked:f.type==='number'?Number(el.value):f.type==='list'?el.value.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean):el.value;markDirty();syncEnvironmentFields()});
 $$('[data-array]').forEach(el=>el.onchange=()=>{state.doc.settings[el.dataset.array]=$$(`[data-array="${el.dataset.array}"]:checked`).map(n=>n.value);markDirty();syncEnvironmentFields()});
 for(const [id,key] of [['plan-url','url'],['plan-package','package'],['plan-label','launch_label'],['plan-apk','apk_path']]){const el=$('#'+id);if(el)el.oninput=()=>{state.plan.settings[key]=el.value;markDirty()}}
 $$('[data-source]').forEach(el=>el.onchange=()=>{state.plan.settings.proxy_sources=$$('[data-source]:checked').map(n=>n.dataset.source);markDirty()});
	 bindChoiceFields();
	 $$('[data-panel]').forEach(el=>el.ontoggle=()=>remember('panels',{[el.dataset.panel]:el.open}));
	 bindEnvironmentExtras();
 simplifyEnvironment();
 $('#save-provider').onclick=e=>act(saveProvider,e.target);
 if($('#probe'))$('#probe').onclick=e=>act(async()=>{const editId=state.editId,key=state.key;const result=await api('/providers/'+key+'/probe','POST',{settings:state.doc.settings});if(state.editId!==editId||!$('#probe-result')){toast(result.message);return}$('#probe-result').innerHTML=`<div class="result-box ${result.ok?'':'error'}">${esc(result.message)}</div>`;if(result.devices)renderDevices(result.devices)},e.target);
}
async function saveProvider(){
 const key=state.key,kind=state.kind,editId=state.editId,plan=structuredClone(state.plan),revision=state.editRevision||0;
 const doc=await api('/providers/'+key,'PUT',structuredClone(state.doc));
 const updateDoc=current=>{current.revision=doc.revision;if('common_revision' in doc)current.common_revision=doc.common_revision};
 if(state.editId===editId){if((state.editRevision||0)===revision)state.doc=doc;else updateDoc(state.doc)}
 else if(state.drafts[editId])updateDoc(state.drafts[editId].doc);
 let savedPlan;
 try{savedPlan=await api('/plans/'+kind+'/'+key,'PUT',plan)}catch(e){if(state.editId===editId)markDirty();throw Error('厂商配置已保存，启动方案未保存：'+e.message)}
 if(state.editId===editId&&(state.editRevision||0)===revision)delete state.drafts[kind+':'+key];
 // 同一厂商的连接是两类云流程共用的，清理另一个未编辑的缓存。
 for(const [id,draft] of Object.entries(state.drafts))if(id.endsWith(':'+key)&&!draft.dirty)delete state.drafts[id];
 if(state.editId===editId){if((state.editRevision||0)===revision){state.plan=savedPlan;state.dirty=false;if($('#save-note'))$('#save-note').textContent='已保存'}else{state.plan.revision=savedPlan.revision;if('common_revision' in savedPlan)state.plan.common_revision=savedPlan.common_revision;markDirty()}}
 else if(state.drafts[editId]){state.drafts[editId].plan.revision=savedPlan.revision;if('common_revision' in savedPlan)state.drafts[editId].plan.common_revision=savedPlan.common_revision}
 toast('已保存 '+state.catalog.find(p=>p.key===key).name+' 的配置'+(state.editId===editId&&state.dirty?'；保存期间的新修改仍保留，请再次保存':''));
}
function renderDevices(devices){return renderCloudDevices(devices)}

$('#logs').onclick=()=>act(async()=>{const logs=await api('/logs');openDialog('操作日志',logs.length?logs.map(l=>{const who=[l.provider,l.operation,l.name,l.run?'窗口 '+String(l.run).slice(0,8):''].filter(Boolean).join(' · ');return `<p><small>${esc(l.time)} ${esc(who)}</small></p><pre>${esc(l.message)}${l.stage?'\n阶段：'+esc(l.stage):''}${l.sitekey?'\n站点公钥：'+esc(l.sitekey):''}${l.traceback?'\n'+esc(l.traceback):''}</pre>`}).join(''):'<p class="muted">暂无日志</p>',true)});
$('#feature-list').onclick=()=>act(async()=>openDialog('功能清单与验收状态',`<pre class="feature-text">${esc((await api('/features')).text)}</pre>`,true));
async function poll(){if(state.page==='work'&&!state.demo){try{const data=await api('/events?after='+state.cursor);if(state.page==='work')await applyRunEvents(data)}catch(e){console.warn(e.message)}}setTimeout(poll,2000)}
(async()=>{try{const data=await api('/bootstrap');state.catalog=data.providers;state.system=data.system;state.preferences=data.preferences||state.preferences;const saved=state.preferences.workspace||{};if(['desktop','phone'].includes(saved.kind))state.kind=saved.kind;if(state.catalog.some(p=>p.key===saved.key))state.key=saved.key;await applyAlias(data.system.alias);await page(['work','environment','proxy','records','settings'].includes(saved.page)?saved.page:'work');if(data.system.operator_required)operatorDialog();poll()}catch(e){$('#page').innerHTML=`<div class="card empty error">${esc(e.message)}</div>`}})();
