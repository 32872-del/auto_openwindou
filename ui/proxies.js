const proxyTypes={static:'固定地址 / 网关模板',rotate:'换 IP 链接 / 返回代理',api:'接口提取（可再换 IP）',local:'本机代理端口',list:'地址列表'};
async function proxyPage(){
 state.proxies=await api('/proxies');$('#page').innerHTML=head('代理来源','可以配置多个来源；指定一个固定使用，多个按权重随机。','<div class="flex"><button id="proxy-template">下载模板</button><button id="proxy-import">批量导入</button><button class="primary" id="add-source">＋ 添加来源</button></div>')+
 `<section class="card"><div class="proxy-global-bar"><strong>全局随机范围</strong><span id="global-selection-note"></span><button id="select-enabled">勾选全部启用</button><button id="clear-global">清空勾选</button><button id="save-global" class="primary">保存全局范围</button></div>${state.proxies.sources.map(s=>`<div class="proxy-source-row"><label title="用于未指定代理的方案"><input data-global="${esc(s.id)}" type="checkbox" ${state.proxies.defaults.includes(s.id)?'checked':''}></label><div class="proxy-source-info"><h3>${esc(s.name)}</h3><p>${esc(proxyTypes[s.source_type||'static'])} · ${esc(s.protocol)}${s.host?' · '+esc(s.host)+':'+s.port:''}</p></div><span class="proxy-meta">权重 ${s.weight??1}</span><span class="proxy-meta">${s.enabled?'已启用':'已停用'}</span><div class="proxy-source-actions"><button data-test-source="${esc(s.id)}">检测</button><button data-edit-source="${esc(s.id)}">编辑</button><button data-clone-source="${esc(s.id)}">复制</button><button data-delete-source="${esc(s.id)}">删除</button></div></div>`).join('')||'<div class="empty"><strong>还没有代理来源</strong>可以添加来源，或导入你已有的通用配置。</div>'}</section><p class="note">勾选项仅决定未指定代理时的全局随机范围；检测、编辑、复制和删除在对应来源右侧。未勾选时电脑浏览器直连，云手机保留设备已有代理。</p>`;
 $$('[data-clone-source]').forEach(b=>b.onclick=()=>act(async()=>{const source=state.proxies.sources.find(s=>s.id===b.dataset.cloneSource);if(!source)throw Error('此来源已不存在，请刷新列表');await api('/proxies','PUT',{...state.proxies,sources:[...state.proxies.sources,{...source,id:crypto.randomUUID(),name:source.name+' 副本'}]});await refreshProxyPage();toast('已复制，可单独修改账号和比例')},b));
 $$('[data-delete-source]').forEach(b=>b.onclick=()=>deleteProxy(b.dataset.deleteSource));
 $('#add-source').onclick=()=>editProxy();$$('[data-edit-source]').forEach(b=>b.onclick=()=>editProxy(b.dataset.editSource));
 $$('[data-test-source]').forEach(b=>b.onclick=()=>proxyTestDialog(b.dataset.testSource));
	 $('#save-global').onclick=e=>act(async()=>{state.proxies=await api('/proxies','PUT',{...state.proxies,defaults:$$('[data-global]:checked').map(n=>n.dataset.global)});selectedNote();toast('全局来源已保存')},e.target);
 const selectedNote=()=>{
  const count=$$('[data-global]:checked').length,dirty=JSON.stringify($$('[data-global]:checked').map(n=>n.dataset.global).sort())!==JSON.stringify([...state.proxies.defaults].sort());
  $('#global-selection-note').textContent='已勾选 '+count+' 项'+(dirty?' · 尚未保存':' · 已保存');
 };
 $$('[data-global]').forEach(el=>el.onchange=selectedNote);
 $('#select-enabled').onclick=()=>{$$('[data-global]').forEach(el=>{const source=state.proxies.sources.find(s=>s.id===el.dataset.global);el.checked=!!source?.enabled});selectedNote()};
 $('#clear-global').onclick=()=>{$$('[data-global]').forEach(el=>el.checked=false);selectedNote()};
 selectedNote();
 $('#proxy-template').onclick=()=>download('代理来源导入模板.tsv','\ufeff来源名称\t协议\t主机\t端口\t账号\t密码\t权重\t类型\t换IP链接\t换IP等待秒数\r\n');$('#proxy-import').onclick=proxyImportDialog;
 const refresh=document.createElement('button');refresh.id='refresh-proxy-sources';refresh.textContent='刷新来源';$('#add-source').before(refresh);
 refresh.onclick=e=>act(async()=>{const selected=$$('[data-global]:checked').map(n=>n.dataset.global);const dirty=JSON.stringify([...selected].sort())!==JSON.stringify([...state.proxies.defaults].sort());await proxyPage();if(dirty)$$('[data-global]').forEach(n=>n.checked=selected.includes(n.dataset.global));toast('来源已刷新'+(dirty?'，保留未保存的全局勾选':''))},e.target);
}
function proxyTestDialog(id){
 const source=state.proxies.sources.find(s=>s.id===id);if(!source){toast('此来源已不存在，请刷新列表');return}
 const canRotate=source.source_type==='rotate'||(source.source_type==='api'&&!!source.rotate_url);
 const needsRotate=source.source_type==='rotate'&&!source.host;
 openDialog('检测代理 · '+source.name,`<div class="stack"><p class="note">只检测本来源，不启动环境。带地区模板时填写测试地区。</p>
 ${locationInputs('test-')}
 <label>IP 类型<select id="test-family"><option value="any">不限</option><option value="ipv4">只使用 IPv4</option><option value="ipv6">只使用 IPv6</option><option value="dual">IPv4 + IPv6</option></select></label>
 ${canRotate?'<p class="note">“换 IP 并检测”会请求供应商链接；共用网关的其他窗口也可能受影响。普通检测不请求换 IP 链接。</p>':''}
 <div class="flex">${needsRotate?'':`<button class="primary" id="run-proxy-test">${source.source_type==='api'?'提取并检测':'检测当前出口'}</button>`}${canRotate?`<button id="rotate-proxy-test">${needsRotate?'请求新代理并检测':source.source_type==='api'?'提取、换 IP 并检测':'换 IP 并检测'}</button>`:''}</div><div id="proxy-test-result" role="status" aria-live="polite"></div></div>`);
 const output=$('#proxy-test-result');
 act(()=>bindLocation('test-'));
 const run=async rotate=>{const buttons=$$('#dialog-body button');buttons.forEach(b=>b.disabled=true);output.textContent=rotate?(source.source_type==='api'?'正在提取代理、请求换 IP，再检测出口…':'正在请求换 IP，等待生效后检测出口…'):(source.source_type==='api'?'正在提取代理并检测出口…':'正在分别检测 IPv4 和 IPv6…');try{
  const location=readLocation('test-');
  const data=await api('/proxies/'+id+'/test','POST',{location,ip_family:$('#test-family').value,rotate});if(!output.isConnected)return;
  output.innerHTML=`<strong>${esc(data.protocol)} · ${data.latency} ms</strong><p>IPv4：${esc(data.ipv4||'未连通')}<br>IPv6：${esc(data.ipv6||'未连通')}</p>${data.rotation?`<p class="note">${esc(data.rotation.message)}</p><details><summary>换 IP 接口响应</summary><pre>${esc(data.rotation.response||'空响应')}</pre></details>`:''}<p class="note">${esc(data.geo_note||'')}</p>${data.probe_errors&&Object.keys(data.probe_errors).length?`<details><summary>检测详情</summary><pre>${esc(JSON.stringify(data.probe_errors,null,2))}</pre></details>`:''}`;
 }catch(err){if(output.isConnected)output.textContent=err.message;throw err}finally{buttons.forEach(b=>b.disabled=false)}};
 if($('#run-proxy-test'))$('#run-proxy-test').onclick=()=>act(()=>run(false));if($('#rotate-proxy-test'))$('#rotate-proxy-test').onclick=()=>act(()=>run(true));
}
function editProxy(id){
 const value=id?state.proxies.sources.find(s=>s.id===id):{id:crypto.randomUUID(),name:'',protocol:'socks5',host:'',port:1080,username:'',password:'',enabled:true,source_type:'static',api_endpoint:'',addresses:[],weight:1};
 if(!value){toast('此来源已不存在，请刷新列表');return}
 openDialog(id?'编辑代理来源':'添加代理来源',`<div class="stack proxy-editor">
 <div class="row"><label>来源名称<input id="p-name" value="${esc(value.name)}"></label><label>来源类型<select id="p-source_type">${Object.entries(proxyTypes).map(([k,v])=>`<option value="${k}" ${k===(value.source_type||'static')?'selected':''}>${v}</option>`).join('')}</select></label></div>
 <div class="row"><label>协议<select id="p-protocol">${['socks5','http','https'].map(v=>`<option ${v===value.protocol?'selected':''}>${v}</option>`).join('')}</select></label><label><span class="field-name">随机权重${help('多个来源按权重随机。0 不参与随机；只指定一个来源时固定使用。')}</span><input id="p-weight" type="number" min="0" max="1000" value="${value.weight??1}"></label></div>
 <div id="static-fields" class="proxy-address-fields"><label>主机（可粘贴完整代理；链接返回代理时可留空）<input id="p-host" value="${esc(value.host)}"></label><label>端口<input id="p-port" type="number" min="1" max="65535" value="${value.port}"></label></div>
 <label id="api-fields">提取代理接口<textarea id="p-api_endpoint" rows="2">${esc(value.api_endpoint||'')}</textarea><small>先返回代理地址或包含 ip、port 的 JSON；需要再发请求换 IP 时，填写下方独立链接。</small></label>
 <div id="rotate-fields" class="stack"><label>换 IP 链接<textarea id="p-rotate_url" rows="3" placeholder="https://供应商/换IP?参数">${esc(value.rotate_url||'')}</textarea></label><label>请求后等待（秒）<input id="p-rotate_wait" type="number" min="0" max="60" value="${value.rotate_wait??5}"></label><p class="note" id="rotate-flow-note"></p></div>
 <label id="list-fields">地址列表<textarea id="p-addresses" rows="5" placeholder="每行一个代理">${esc((value.addresses||[]).join('\n'))}</textarea></label>
 <label>账号<textarea id="p-username" rows="2" spellcheck="false">${esc(value.username)}</textarea></label><label>密码<textarea id="p-password" rows="2" spellcheck="false" autocomplete="off">${esc(value.password)}</textarea></label>
 <p class="note">地区可以写在账号或密码里。模板支持 &#36;{country}（小写国家）、&#36;{countryUpper}（大写国家）、&#36;{proxyState}、&#36;{proxyStateCode}、&#36;{proxyCity}、&#36;{proxyZip}、&#36;{session}、@{8}。粘贴完整代理后可在弹窗中逐项确认或手改。</p>
 <label>代理地区参数格式<select id="p-geo_format">${Object.entries({auto:'自动识别供应商',raw:'州省代码 / 城市原文',bolt:'Bolt：州省代码 / 城市去空格',nine:'9P：州省全称 / 城市名称',iproyal:'IPRoyal：密码中的地区和会话'}).map(([k,v])=>`<option value="${k}" ${(value.geo_format||'auto')===k?'selected':''}>${v}</option>`).join('')}</select><small>按每条数据使用国家、州省和城市；账号、密码或接口中需有对应模板。</small></label>
 <label class="inline"><input id="p-enabled" type="checkbox" ${value.enabled?'checked':''}>启用此来源</label><div id="proxy-save-error" class="error inline-error" role="alert" hidden></div></div>
 <div class="dialog-footer"><button id="cancel-source">取消</button><button class="primary" id="save-source">保存来源</button></div>`,true);
 const show=()=>{const kind=$('#p-source_type').value;$('#static-fields').hidden=!['static','local','rotate'].includes(kind);$('#api-fields').hidden=kind!=='api';$('#list-fields').hidden=kind!=='list';$('#rotate-fields').hidden=!['rotate','api'].includes(kind);$('#rotate-flow-note').textContent=kind==='api'?'创建或重试时：先提取代理，再请求此链接换 IP，等待后检测最终出口。链接可留空；若返回新代理地址则使用新地址。': '创建或重试时先请求此链接；返回新代理地址就使用新地址，只返回成功状态则使用上方固定网关。主机留空时链接必须返回完整代理。共用网关可能影响已有窗口。'};show();$('#p-source_type').onchange=show;
 bindProxyPaste();$('#cancel-source').onclick=closeDialog;
 $('#save-source').onclick=e=>act(async()=>{const output=$('#proxy-save-error');output.hidden=true;try{
	  if(!await parseProxyHost())return;const item={...value};for(const k of ['name','host','username','password','protocol','source_type','api_endpoint','geo_format','rotate_url'])item[k]=$('#p-'+k).value;
  item.port=Number($('#p-port').value);item.weight=Number($('#p-weight').value);item.rotate_wait=Number($('#p-rotate_wait').value);item.addresses=$('#p-addresses').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);item.enabled=$('#p-enabled').checked;
  const sources=id?state.proxies.sources.map(s=>s.id===id?item:s):[...state.proxies.sources,item];
  await api('/proxies','PUT',{...state.proxies,sources});closeDialog();await refreshProxyPage();toast('代理来源已保存');
 }catch(err){if(output.isConnected){output.hidden=false;output.textContent=err.message;output.scrollIntoView({block:'nearest'})}throw err}},e.target);
}
function proxyImportDialog(){
 let document=null;openDialog('批量导入代理来源',`<div class="stack"><p>从模板表格中复制表头和内容，粘贴到下面。同名来源会更新，其余新增。类型留空为固定地址；链接换 IP 填 rotate，并填写换IP链接及等待秒数。</p><input id="proxy-import-file" type="file" accept=".tsv,.txt"><textarea id="proxy-import-text" rows="10" placeholder="来源名称　协议　主机　端口　账号　密码　权重"></textarea><div class="flex"><button id="proxy-preview">识别并预览</button><button id="proxy-apply" class="primary" disabled>确认导入</button></div><div id="proxy-preview-list" class="table-scroll"></div></div>`,true);
 $('#proxy-import-file').onchange=e=>act(async()=>{if(e.target.files[0])$('#proxy-import-text').value=await e.target.files[0].text();document=null;$('#proxy-apply').disabled=true});
 $('#proxy-import-text').oninput=()=>{document=null;$('#proxy-apply').disabled=true};
 $('#proxy-preview').onclick=e=>act(async()=>{document=null;$('#proxy-apply').disabled=true;const result=await api('/proxies/import-preview','POST',{text:$('#proxy-import-text').value});document=result.document;$('#proxy-preview-list').innerHTML=`<table><thead><tr><th>行</th><th>来源</th><th>处理</th></tr></thead><tbody>${result.rows.map(r=>`<tr><td>${r.line}</td><td>${esc(r.name)}</td><td>${esc(r.action)}</td></tr>`).join('')}</tbody></table>`;$('#proxy-apply').disabled=false},e.target);
 $('#proxy-apply').onclick=e=>act(async()=>{if(!document)throw Error('请先预览');await api('/proxies','PUT',document);closeDialog();await refreshProxyPage();toast('代理来源已导入')},e.target);
}

async function refreshProxyPage(){
 const selected=$$('[data-global]:checked').map(n=>n.dataset.global);
 const dirty=JSON.stringify([...selected].sort())!==JSON.stringify([...state.proxies.defaults].sort());
 await proxyPage();if(dirty)$$('[data-global]').forEach(n=>n.checked=selected.includes(n.dataset.global));
}
function deleteProxy(id){
 const source=state.proxies.sources.find(s=>s.id===id);if(!source){toast('此来源已不存在，请刷新列表');return}
 openDialog('删除代理来源',`<div class="stack"><p>删除「${esc(source.name)}」。全局选择会同步移除；被环境方案或排队任务指定的来源，需要先解除引用。</p><div id="proxy-delete-error" class="error inline-error" role="alert" hidden></div><div class="flex"><button id="cancel-proxy-delete">取消</button><button id="confirm-proxy-delete" class="danger">删除来源</button></div></div>`);
 $('#cancel-proxy-delete').onclick=closeDialog;
 $('#confirm-proxy-delete').onclick=e=>act(async()=>{const output=$('#proxy-delete-error');output.hidden=true;try{await api('/proxies','PUT',{...state.proxies,sources:state.proxies.sources.filter(s=>s.id!==id),defaults:state.proxies.defaults.filter(x=>x!==id)});closeDialog();await refreshProxyPage();toast('代理来源已删除')}catch(err){if(output.isConnected){output.hidden=false;output.textContent=err.message}throw err}},e.target);
}
