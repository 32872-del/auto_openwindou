function proxyModeOptions(value,kind=state.kind){return [['inherit','继承上一级'],['selected','指定代理来源'],['direct',kind==='phone'?'保留云手机现有代理':'直连，不使用代理']].map(([k,t])=>`<option value="${k}" ${value===k?'selected':''}>${t}</option>`).join('')}
function weightsKeys(f){const v=state.doc.settings;if(f.key==='system_weights')return (f.options||[]).filter(([k])=>v.systems.includes(k));if(f.key==='device_brand_weights')return v.device_brands.map(k=>[k,k]);if(f.key==='device_weights')return v.device_ids.map(k=>[k,k]);if(f.key==='android_brand_weights')return v.android_brands.map(k=>[k,k]);return f.options||[]}
function weightsField(f,value){return `<div class="wide-field" data-weights="${f.key}"><span class="field-name">${esc(f.label)}${help(f.hint)}</span><div class="weight-options">${weightsKeys(f).map(([key,label])=>`<label class="weight-row"><span>${esc(label)}</span><input type="number" min="0" max="1000" data-weight-field="${f.key}" data-weight-key="${esc(key)}" value="${value[key]??1}"></label>`).join('')||'<small>先选择上方范围；未填写的选项按 1。</small>'}</div></div>`}
function bindWeightInputs(){$$('[data-weight-field]').forEach(el=>el.oninput=()=>{state.doc.settings[el.dataset.weightField][el.dataset.weightKey]=Number(el.value);markDirty()})}
function syncEnvironmentFields(){
 const values=state.doc.settings,p=state.catalog.find(p=>p.key===state.key);
 for(const f of p.fields.filter(f=>f.type==='weights')){const node=$(`[data-weights="${f.key}"]`);if(node)node.outerHTML=weightsField(f,values[f.key])}
 bindWeightInputs();
 const show=(key,visible)=>{const el=$(`[data-field="${key}"]`)||$(`[data-array="${key}"]`)||$(`[data-weights="${key}"]`);if(el)(el.closest('.wide-field')||el.closest('label')).hidden=!visible};
 show('fixed_system',values.system_mode==='fixed');show('systems',values.system_mode!=='fixed');show('system_weights',values.system_mode==='weighted');
 show('android_brands',values.android_mode==='brands');show('android_brand_weights',values.android_mode==='brands');show('android_model',values.android_mode==='fixed');show('android_version',values.android_mode==='fixed');
 show('android_models',values.android_mode==='models');show('android_versions',values.android_mode!=='fixed');
 show('device_weights',values.device_mode==='weighted');
 const choiceVisible={
  systems:values.system_mode!=='fixed',
  android_brands:values.android_mode==='brands',
  android_models:values.android_mode==='models',
  android_versions:values.android_mode!=='fixed',
  android_model:values.android_mode==='fixed',
  android_version:values.android_mode==='fixed',
  device_ids:values.device_selection==='selected'
 };
 $$('[data-choice-container]').forEach(el=>{
  if(Object.hasOwn(choiceVisible,el.dataset.choiceContainer))el.hidden=choiceVisible[el.dataset.choiceContainer]===false;
 });
 if($('#plan-proxy-options'))$('#plan-proxy-options').hidden=$('#plan-proxy-mode').value!=='selected';
}
function bindEnvironmentExtras(){
 bindCloudCatalog();
 bindProgramProbe();
 bindVendorCatalogs();
 bindProxyScopeRefresh('plan',state.plan.settings.proxy_sources);
 $('#plan-proxy-mode').onchange=e=>{state.plan.settings.proxy_mode=e.target.value;markDirty();syncEnvironmentFields()};
 syncEnvironmentFields();
 if(['octo','hub'].includes(state.key)){
  const input=$('[data-field="android_model"]');if(input){input.readOnly=true;input.placeholder='从型号库选择';const b=document.createElement('button');b.textContent='选择 Android 型号 / 品牌';b.type='button';input.closest('details').appendChild(b);b.onclick=e=>act(()=>modelDialog(false),e.target)}
 }
}
function bindVendorCatalogs(){
 const catalogFields={templates:['fingerprint_template','选择完整模板'],configs:['config_ids','选择账号配置库'],fingerprints:['fingerprint_ids','选择指纹库'],versions:['chrome_versions','选择内核版本'],groups:['group','选择分组']};
 const supported={ruyi:['templates'],undetectable:['configs','groups'],kameleo:['versions','fingerprints'],morelogin:['versions','groups'],octo:['groups'],hub:['versions'],adspower:['groups'],bitbrowser:['groups'],vmlogin:['groups'],hidemium:['groups'],helix:['groups']};
 for(const section of supported[state.key]||[]){const [field,title]=catalogFields[section],input=$(`[data-field="${field}"]`);if(!input)continue;const b=document.createElement('button');b.type='button';b.textContent=title;b.dataset.catalogSection=section;input.after(b);b.onclick=e=>act(()=>vendorCatalogDialog(section,field,title),e.target)}
}
async function vendorCatalogDialog(section,field,title){
 const key=state.key,id=state.editId,settings=structuredClone(state.doc.settings),many=!['groups','templates'].includes(section);
 const selected=new Set(many?settings[field]:settings[field]?[settings[field]]:[]);let rows=[],filters={};
 if(section==='fingerprints')filters={system:settings.fixed_system||'Win/Win11',product:settings.browser_product,version:settings.chrome_versions[0]||''};
 openDialog(title,`<div class="catalog-dialog">${section==='fingerprints'?`<div class="row"><label>系统<select id="catalog-os">${[['Win/Win10','Windows 10'],['Win/Win11','Windows 11'],['Mac/Intel','macOS'],['Android','Android']].map(([v,t])=>`<option value="${v}" ${v===filters.system?'selected':''}>${t}</option>`).join('')}</select></label><label>主版本<input id="catalog-version" value="${esc(filters.version)}" placeholder="留空不限版本"></label></div>`:''}<div class="flex"><input id="catalog-search" placeholder="筛选名称、系统或型号"><button id="catalog-refresh">从厂商刷新</button></div><p id="catalog-note" class="note">正在查询当前填写的连接…</p><div id="catalog-items" class="device-list catalog-list"></div><div class="flex"><button id="catalog-clear">清空指定范围</button><button class="primary" id="catalog-apply">使用勾选项</button></div></div>`,true);
 const host=$('#catalog-items'),note=$('#catalog-note');
 const render=()=>{const q=$('#catalog-search').value.toLowerCase();host.innerHTML=rows.filter(r=>(r.id+' '+r.label+' '+r.detail).toLowerCase().includes(q)).map(r=>`<label class="inline"><input type="${many?'checkbox':'radio'}" name="vendor-catalog" data-catalog-id="${esc(r.id)}" ${selected.has(r.id)?'checked':''}><span><strong>${esc(r.label)}</strong><small>${esc(r.detail||r.id)}</small></span></label>`).join('')||'<p>没有匹配项，请调整筛选或检查厂商账号权限。</p>';host.querySelectorAll('input').forEach(el=>el.onchange=()=>{if(!many)selected.clear();if(el.checked)selected.add(el.dataset.catalogId);else selected.delete(el.dataset.catalogId)})};
 const load=async(refresh=false)=>{if(section==='fingerprints'){filters.system=$('#catalog-os').value;filters.version=$('#catalog-version').value.trim()}note.textContent='正在读取厂商列表…';try{const data=await api('/providers/'+key+'/catalog/'+section,'POST',{settings:structuredClone(state.doc.settings),filters,refresh});if(!host.isConnected||id!==state.editId)return;rows=data.items;note.textContent=data.source+' · '+data.total+' 项'+(data.partial?'（部分结果）':'')+'。'+catalogCacheNote(data)+'。'+(data.note||'');render()}catch(e){if(note.isConnected)note.textContent=e.message;throw e}};
 $('#catalog-refresh').onclick=e=>act(()=>load(true),e.target);$('#catalog-search').oninput=render;
 $('#catalog-clear').onclick=()=>{selected.clear();render()};
 $('#catalog-apply').onclick=()=>{if(id!==state.editId)return closeDialog();state.doc.settings[field]=many?[...selected]:[...selected][0]||'';if(section==='templates')state.doc.settings.fingerprint_mode=selected.size?'fixed':'random';if(section==='configs'&&selected.size)state.doc.settings.fp_source='pool';if(section==='fingerprints'&&selected.size){state.doc.settings.system_mode='fixed';state.doc.settings.fixed_system=filters.system;state.doc.settings.browser_product=filters.product}markDirty();closeDialog();renderProvider()};
 await load();
}
function bindProgramProbe(){
 const key=state.key,field=['chrome','ruyi'].includes(key)?'browser_path':'manager_path',input=$(`[data-field="${field}"]`);
 if(!input)return;
 const bar=document.createElement('div');bar.className='flex';bar.style.marginTop='8px';bar.innerHTML='<button type="button" id="find-program">探测程序</button><button type="button" id="choose-program">选择 EXE</button>';input.after(bar);
 const run=async(candidate_id='')=>{const id=state.editId,revision=state.editRevision,settings=structuredClone(state.doc.settings);const result=await api('/providers/'+key+'/programs','POST',{settings,candidate_id});if(id!==state.editId)return;if(revision!==state.editRevision){toast('探测期间配置已修改，请重新探测；已保留新输入');return;}
   const select=candidate=>{state.doc.settings[field]=candidate.path;markDirty();input.value=candidate.path;toast('已选择 '+candidate.path+'，保存配置后使用')};
   if(result.selected){select(result.selected);return}
   openDialog('选择本机程序',`<div class="stack"><p>${esc(result.message)}</p>${result.candidates.map(c=>`<div class="card card-body"><strong>${esc(c.productName)} ${esc(c.version)}</strong><p>${esc(c.path)}</p><small>${esc(c.reason||c.publisher)}</small><button data-program="${esc(c.candidateId)}" ${c.trusted?'':'disabled'}>使用此程序</button></div>`).join('')||'<p>请安装或打开对应软件后重新探测，也可返回选择 EXE。</p>'}</div>`,true);
   $$('[data-program]').forEach(b=>b.onclick=e=>act(async()=>{await run(b.dataset.program);closeDialog()},e.target));
 };
 $('#find-program').onclick=e=>act(()=>run(),e.target);
 $('#choose-program').onclick=e=>act(async()=>{if(!window.pywebview?.api?.select_program){input.focus();throw Error('当前在网页中打开，请把 EXE 完整路径粘贴到上方；桌面软件支持文件选择器')}const picked=await window.pywebview.api.select_program();if(!picked.path)return;state.doc.settings[field]=picked.path;input.value=picked.path;markDirty();await run()},e.target);
}
function proxyScopeHtml(ids,attribute){
 const existing=new Set(state.proxies.sources.map(s=>s.id));
 return state.proxies.sources.map(s=>`<label class="chip"><input ${attribute}="${esc(s.id)}" type="checkbox" ${ids.includes(s.id)?'checked':''}>${esc(s.name)}${s.enabled?'':'（停用）'}</label>`).join('')+ids.filter(id=>!existing.has(id)).map(id=>`<label class="chip error"><input ${attribute}="${esc(id)}" type="checkbox" checked>来源已删除：${esc(id)}</label>`).join('');
}
function bindProxyScopeRefresh(scope,ids){
 const container=$('#'+scope+'-proxy-options');if(!container)return;
 const attribute=scope==='plan'?'data-source':'data-create-source';
 const render=selected=>{container.querySelector('.chips').innerHTML=proxyScopeHtml(selected,attribute)||'<small>请先添加代理来源；指定来源不能为空。</small>';if(scope==='plan')container.querySelectorAll('input').forEach(el=>el.onchange=()=>{state.plan.settings.proxy_sources=Array.from(container.querySelectorAll('input:checked')).map(n=>n.getAttribute(attribute));markDirty()})};
 render(ids);
 const button=document.createElement('button');button.type='button';button.textContent='刷新代理来源';button.id=scope+'-refresh-proxies';container.appendChild(button);
 button.onclick=e=>act(async()=>{state.proxies=await api('/proxies');const selected=Array.from(container.querySelectorAll('input:checked')).map(n=>n.getAttribute(attribute));render(selected);toast('代理来源已刷新，保留当前勾选')},e.target);
 return render;
}
async function modelDialog(refresh){
 const key=state.key,id=state.editId,result=await api('/providers/'+key+'/models','POST',{settings:state.doc.settings,refresh});
 if(id!==state.editId)return;const models=result.models;
 openDialog('Android 型号库',`<div class="catalog-dialog"><div class="flex"><input id="model-search" placeholder="搜索品牌或型号"><button id="refresh-models">刷新列表</button></div><p class="note">来源：${esc(result.source)}。${esc(catalogCacheNote(result))}。型号与版本配套选择；厂商未提供版本时由厂商决定。</p><div id="model-list" class="device-list catalog-list"></div></div>`,true);
 const render=()=>{const q=$('#model-search').value.toLowerCase();$('#model-list').innerHTML=models.filter(m=>(m.brand+' '+m.value).toLowerCase().includes(q)).map((m,i)=>`<div class="catalog-model-row android-model-row"><div><strong>${esc(m.brand)} · ${esc(m.value)}</strong><select data-model-version="${esc(m.value)}"><option value="">${m.versions.length?'支持版本内随机':'厂商决定版本'}</option>${m.versions.map(v=>`<option value="${esc(v)}">Android ${esc(v)}</option>`).join('')}</select></div><div class="catalog-actions"><button data-model="${esc(m.value)}">固定此型号</button><button data-add-model="${esc(m.value)}">加入随机范围</button><button data-brand="${esc(m.brand)}">加入品牌范围</button></div></div>`).join('')||'<p>没有匹配型号</p>';
 $$('[data-add-model]').forEach(b=>b.onclick=()=>{state.doc.settings.android_mode='models';if(!state.doc.settings.android_models.includes(b.dataset.addModel))state.doc.settings.android_models.push(b.dataset.addModel);const version=$$('[data-model-version]').find(n=>n.dataset.modelVersion===b.dataset.addModel).value;if(version&&!state.doc.settings.android_versions.includes(version))state.doc.settings.android_versions.push(version);$('[data-field="android_mode"]').value='models';$('[data-field="android_models"]').value=state.doc.settings.android_models.join(', ');$('[data-field="android_versions"]').value=state.doc.settings.android_versions.join(', ');markDirty();syncEnvironmentFields();b.textContent='已加入随机范围';toast('已加入随机型号范围，保存后生效')});
 $$('[data-model]').forEach(b=>b.onclick=()=>{state.doc.settings.android_mode='fixed';state.doc.settings.android_model=b.dataset.model;state.doc.settings.android_version=$$('[data-model-version]').find(n=>n.dataset.modelVersion===b.dataset.model).value;markDirty();closeDialog();renderProvider()});
 $$('[data-brand]').forEach(b=>b.onclick=()=>{state.doc.settings.android_mode='brands';if(!state.doc.settings.android_brands.includes(b.dataset.brand))state.doc.settings.android_brands.push(b.dataset.brand);markDirty();closeDialog();renderProvider()})};render();$('#model-search').oninput=render;$('#refresh-models').onclick=e=>act(()=>modelDialog(true),e.target);
}
let proxyTouched=new Set();
let proxyParsePending=null;
function bindProxyPaste(){proxyTouched=new Set();for(const k of ['protocol','port','username','password'])$('#p-'+k).oninput=()=>proxyTouched.add(k);$('#p-host').onchange=()=>act(parseProxyHost)}
function applyProxyPasteChanges(original,edits){
 let value=original;
 if(edits.every(c=>Number.isInteger(c.start)&&Number.isInteger(c.end))){
  for(const change of edits.sort((a,b)=>b.start-a.start)){
   value=value.slice(0,change.start)+change.replaced+value.slice(change.end);
  }
 }else{
  for(const change of edits)value=value.replace(change.original,()=>change.replaced);
 }
 return value;
}
function confirmProxyPaste(raw,parsed,conflicts){return new Promise(resolve=>{
 const original=parsed.original||{username:parsed.username||'',password:parsed.password||''};
 const changes=(parsed.changes||[]).filter(change=>change.applied);
 const overlay=document.createElement('div');overlay.className='proxy-paste-backdrop';
 overlay.innerHTML=`<section class="proxy-paste-card" role="dialog" aria-modal="true" aria-label="确认代理粘贴结果"><h2>确认代理粘贴结果</h2>
  <p class="note">已拆出代理地址和账号密码。下面是建议转换；可取消任意一处，或直接改账号和密码。</p>
  <details><summary>查看粘贴原文</summary><pre>${esc(raw)}</pre></details>
  <div class="proxy-paste-summary"><span>协议：${esc(parsed.protocol)}</span><span>地址：${esc(parsed.host)}</span><span>端口：${esc(parsed.port)}</span></div>
  ${conflicts.length?`<p class="noticebox">已手动填写的 ${esc(conflicts.join('、'))} 与粘贴内容不同；点“确认填入”才会覆盖。</p>`:''}
  <label>转换后的账号<textarea data-paste-user rows="2" spellcheck="false">${esc(parsed.username||'')}</textarea></label>
  <label>转换后的密码<textarea data-paste-password rows="2" spellcheck="false">${esc(parsed.password||'')}</textarea></label>
	  <small>可用：&#36;{country} 国家小写、&#36;{countryUpper} 国家大写、&#36;{proxyState} 州名、&#36;{proxyStateCode} 州缩写、&#36;{proxyCity} 城市、&#36;{proxyZip} 邮编、@{8} 随机会话</small>
  <div class="proxy-paste-changes">${changes.map((change,i)=>`<label><input type="checkbox" data-paste-change="${i}" checked><span>${esc(change.field==='password'?'密码':'账号')}：${esc(change.original)} → ${esc(change.replaced)}<small>${esc(change.note)}</small></span></label>`).join('')||'<p class="note">没有需要自动替换的片段；可检查拆分结果并直接修改。</p>'}</div>
  <div class="dialog-footer"><button data-paste-cancel>取消</button><button data-paste-original>保持原样填入</button><button class="primary" data-paste-confirm>确认填入</button></div></section>`;
 document.body.append(overlay);
 const user=overlay.querySelector('[data-paste-user]'),password=overlay.querySelector('[data-paste-password]');
 const rebuild=()=>{
  const selected=changes.filter((change,i)=>overlay.querySelector(`[data-paste-change="${i}"]`).checked);
  for(const [field,input] of [['username',user],['password',password]]){
   input.value=applyProxyPasteChanges(original[field],selected.filter(change=>change.field===field));
  }
 };
 overlay.querySelectorAll('[data-paste-change]').forEach(box=>box.onchange=rebuild);
 const finish=value=>{document.removeEventListener('keydown',onKey,true);overlay.remove();resolve(value)};
 const onKey=event=>{if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();finish(null)}};
 document.addEventListener('keydown',onKey,true);
 overlay.onclick=event=>{if(event.target===overlay)finish(null)};
 overlay.querySelector('[data-paste-cancel]').onclick=()=>finish(null);
 overlay.querySelector('[data-paste-original]').onclick=()=>finish(original);
 overlay.querySelector('[data-paste-confirm]').onclick=()=>finish({username:user.value,password:password.value});
 user.focus({preventScroll:true});
})}
async function parseProxyHost(){
 if(proxyParsePending)return proxyParsePending;
 const value=$('#p-host').value;
 if(!(/:\d+/.test(value)||value.includes('://')||value.includes('@')))return true;
 proxyParsePending=(async()=>{
  const parsed=await api('/proxies/parse','POST',{value,protocol:$('#p-protocol').value});
  const labels={port:'端口',protocol:'协议',username:'账号',password:'密码'};
  const conflicts=Object.keys(labels).filter(key=>proxyTouched.has(key)&&String(parsed[key])!==$('#p-'+key).value).map(key=>labels[key]);
  const confirmed=await confirmProxyPaste(value,parsed,conflicts);
  if(!confirmed)return false;
  for(const key of ['host','port','protocol','geo_format']){const input=$('#p-'+key);if(input&&parsed[key]!=null)input.value=parsed[key]}
  $('#p-username').value=confirmed.username;$('#p-password').value=confirmed.password;
  proxyTouched.clear();
  return true;
 })();
 try{return await proxyParsePending}finally{proxyParsePending=null}
}
