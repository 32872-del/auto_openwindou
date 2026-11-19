// Only an explicit backend classification triggers this notice. No retry, device or
// fallback mutation is performed here. Keep edit drawers intact while polling.
const proxyCityNotices=new Set();
function notifyProxyCityUnavailable(rows){
 if(state.demo)return;
 const pending=[];
 for(const row of rows){
  if(!row||row.proxy_city_unavailable!==true)continue;
  const key=JSON.stringify([row.id,row.launch??'']);
  if(proxyCityNotices.has(key))continue;
  proxyCityNotices.add(key);pending.push(row.name||row.id);
 }
 if(pending.length)window.alert(pending.join('、')+'：目标城市暂无可用代理，建议切换为允许同州回退。\n本次不会自动退避或重试。请关闭并等待清理完成后编辑设置。');
}

const runEditInputFields=[['email','邮箱'],['firstName','名'],['lastName','姓'],['phone','电话'],['country','国家（us / ca）'],['state','州 / 省'],['city','城市'],['zip','邮编'],['name','资料名称'],['url','打开网址']];
function runEditOptions(options,value){
 const rows=options.slice();
 // Preserve legacy/unknown values until the user explicitly changes them.
 if(!rows.some(([key])=>String(key)===String(value)))rows.push([value,'当前值：'+value]);
 return rows.map(([key,label])=>`<option value="${esc(key)}" ${String(key)===String(value)?'selected':''}>${esc(label)}</option>`).join('');
}
function runEditPatch(form,original,allowed,phone){
 const changes={};
 for(const el of form.querySelectorAll('[data-run-edit]')){
  const key=el.dataset.runEdit;
  if(!allowed.has(key)||el.disabled||(!phone&&['planned_device','network_mode','allow_city_fallback'].includes(key)))continue;
  const value=key==='allow_city_fallback'?el.value==='state':el.type==='checkbox'?el.checked:el.value;
  if(value!==original[key])changes[key]=value;
 }
 if(allowed.has('input_data')){
  const data={};
  for(const el of form.querySelectorAll('[data-run-input]')){
   if(!el.disabled&&el.value!==(original.input_data[el.dataset.runInput]??''))data[el.dataset.runInput]=el.value;
  }
  if(Object.keys(data).length)changes.input_data=data;
 }
 if(allowed.has('proxy_sources')){
  const sources=Array.from(form.querySelectorAll('[data-run-source]:checked')).map(el=>el.dataset.runSource);
  const same=sources.length===original.proxy_sources.length&&sources.every(id=>original.proxy_sources.includes(id));
  // Merely hiding source controls must not clear an old selection.
  if(!same&&!form.querySelector('#edit-proxy-sources').disabled)changes.proxy_sources=sources;
 }
 return changes;
}
async function editRun(id){
 if(state.demo){toast('模拟记录不支持编辑，请退出演示后操作');return}
 const generation=state.generation;
 const [options,proxyDocument]=await Promise.all([api('/runs/'+encodeURIComponent(id)+'/edit-options'),api('/proxies').catch(()=>null)]);
 if(proxyDocument)state.proxies=proxyDocument;
 if(generation!==state.generation||state.page!=='work')return;
 const row=options.row,phone=row.kind==='phone'||row.kind==='app',allowed=new Set(options.editable_fields||[]);
 const values=structuredClone(options.values||{});
 values.input_data=values.input_data||{};values.proxy_sources=values.proxy_sources||[];
 values.planned_device=row.profile_id||values.planned_device||'';
 // Require a real snapshot version; never derive one from the live table.
 const editable=options.editable===true&&Number.isFinite(options.expected_updated);
 const text=(key,label)=>`<label>${label}<input data-run-edit="${key}" value="${esc(values[key]??'')}" ${allowed.has(key)?'':'disabled'}></label>`;
 const select=(key,label,choices,value=values[key])=>`<label>${label}<select data-run-edit="${key}" ${allowed.has(key)?'':'disabled'}>${runEditOptions(choices,value)}</select></label>`;
 const devices=(options.devices||[]).filter(device=>device.id===values.planned_device||device.status==='idle').map(device=>[device.id,[device.name||device.id,device.id,device.brand,device.model,device.system,device.status==='idle'?'空闲':device.status].filter(Boolean).join(' · ')]);
 if(!devices.some(([key])=>key===values.planned_device))devices.unshift([values.planned_device,'保持当前设备：'+(values.planned_device||'尚未分配')]);
 const knownSources=state.proxies?.sources||[];
 const sources=new Map(knownSources.map(source=>[source.id,source.name+(source.enabled===false?'（停用）':'')]));
 for(const source of values.proxy_sources)if(!sources.has(source))sources.set(source,'原来源：'+source);
 const testAllowed=!phone&&row.provider!=='ruyi';
 openDialog('编辑窗口 · '+(row.name||id),`<form id="run-edit-form" class="stack">
  <p class="note">${esc(options.message||'只修改本地资料及下次启动设置，不自动关闭、重置或删除设备。')}</p>
  <p id="run-edit-error" class="error" role="alert">${editable?'':esc(options.reason||'无法确认编辑版本，请关闭后重新打开编辑。')}</p>
  <dl class="details-grid"><dt>环境类型</dt><dd>${esc(phone?'云手机':'电脑浏览器')}</dd><dt>厂商（不可更改）</dt><dd>${esc(row.label||row.provider)}</dd></dl>
  <fieldset id="run-edit-fields" class="stack" ${editable?'':'disabled'}>
   ${text('name','窗口名称')}
   <div class="fields">${runEditInputFields.map(([key,label])=>`<label>${label}<input data-run-input="${key}" value="${esc(values.input_data[key]??'')}" ${allowed.has('input_data')?'':'disabled'}></label>`).join('')}</div>
   ${text('address','地址')}${text('note','备注')}
   ${phone?`<div class="noticebox">仅可更换为同厂商的空闲设备（保存时重新核验占用）。只修改下次打开时的本地绑定，不迁移应用数据，也不清理原设备数据。</div>
    ${select('planned_device','云手机设备',devices)}
    ${options.devices_error?`<p class="error">${esc(options.devices_error)}；当前设备仍保留。关闭后重新打开编辑可重查。</p>`:''}
    ${select('network_mode','联网方式',[['keep','沿用设备当前设置'],['wifi','Wi-Fi'],['mobile','移动网络']])}
    ${select('allow_city_fallback','代理城市匹配',[['city','精确到城市（无可用代理则停止）'],['state','允许同州 / 省退避']],values.allow_city_fallback===true?'state':'city')}`:''}
   <details><summary>高级 · 代理与检测</summary><div class="stack">
    ${select('proxy_mode','代理使用方式',[['inherit','沿用环境方案'],['selected','指定代理来源'],['direct',phone?'保留设备已有代理':'直连']])}
    <fieldset id="edit-proxy-sources" ${allowed.has('proxy_sources')?'':'disabled'}><legend>指定代理来源（隐藏时保留已选项）</legend><div class="chips">${Array.from(sources,([source,label])=>`<label class="chip"><input type="checkbox" data-run-source="${esc(source)}" ${values.proxy_sources.includes(source)?'checked':''}>${esc(label)}</label>`).join('')||'<small>未加载代理来源；可先在“代理来源”配置后重新编辑。</small>'}</div></fieldset>
    ${select('ip_family','代理 IP 类型',[['any','不限 IPv4 / IPv6'],['ipv4','仅 IPv4'],['ipv6','仅 IPv6'],['dual','双栈']])}
    ${testAllowed?['website_test','tls_test'].map(key=>`<label class="inline"><input type="checkbox" data-run-edit="${key}" ${values[key]?'checked':''} ${allowed.has(key)?'':'disabled'}>${key==='website_test'?'打开网页检测并存档':'打开 TLS 检测并存档'}</label>`).join(''):''}
   </div></details>
  </fieldset>
  <div class="flex"><button type="submit" id="save-run-edit" class="primary" ${editable?'':'disabled'}>保存修改</button><button type="button" id="cancel-run-edit">取消</button><button type="button" id="reload-run-edit" hidden>重新读取（放弃本次输入）</button></div>
 </form>`,true);
 const form=$('#run-edit-form'),fields=$('#run-edit-fields'),save=$('#save-run-edit'),error=$('#run-edit-error'),reload=$('#reload-run-edit');
 const deviceSelect=form.querySelector('[data-run-edit="planned_device"]');
 if(deviceSelect&&options.devices_error)deviceSelect.disabled=true;
 const sourceBox=$('#edit-proxy-sources'),mode=form.querySelector('[data-run-edit="proxy_mode"]');
 const showSources=()=>sourceBox.hidden=mode.value!=='selected';showSources();mode.onchange=showSources;
 $('#cancel-run-edit').onclick=closeDialog;
 reload.onclick=()=>{if(window.confirm('重新读取会放弃当前未保存的输入，是否继续？'))act(()=>editRun(id),reload)};
 let saving=false,conflicted=false;
 form.onsubmit=async event=>{
  event.preventDefault();if(saving||!editable||conflicted)return;
  const changes=runEditPatch(form,values,allowed,phone);
  if(!Object.keys(changes).length){error.textContent='没有需要保存的修改';return}
  saving=true;save.disabled=true;fields.disabled=true;error.textContent='';
  try{
   const updated=await api('/runs/'+encodeURIComponent(id),'PUT',{expected_updated:options.expected_updated,...changes});
   // Only use the server's public row. Never synthesize success from the form.
   if(state.page==='work'){
    state.rows=state.rows.map(current=>current.id===updated.id?updated:current);
    renderRows();
   }
   if(form.isConnected)closeDialog();
   toast('已保存；修改在下次打开时使用，不会自动启动设备');
  }catch(failure){
   if(form.isConnected){
    conflicted=failure.status===409;
    error.textContent=conflicted?'记录已被其他操作更新，未覆盖。当前输入已保留；请核对后重新读取再保存。':failure.message;
    reload.hidden=false;
   }
  }finally{
   saving=false;if(form.isConnected){fields.disabled=false;save.disabled=conflicted}
  }
 };
}
