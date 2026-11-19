function bindCloudCatalog(){
 if(!['duoplus','geelark'].includes(state.key))return;
 const host=$('#devices');if(!host)return;
 const reset=document.createElement('button');reset.type='button';reset.id='cloud-reset-entry';reset.textContent='单独更换设备型号';
 host.before(reset);
 reset.onclick=e=>act(()=>{
  if(state.key==='geelark')return cloudResetDialog(null);
  const models=state.doc.settings.device_models,versions=state.doc.settings.device_versions;
  if(models.length!==1||versions.length!==1)throw Error('DuoPlus 换机前，请先在型号和系统范围各选一项；普通创建不会换机');
  return cloudResetDialog({model:models[0],version:versions[0]});
 },e.target);
}
async function cloudModelDialog(){
 const key=state.key,editId=state.editId,settings=structuredClone(state.doc.settings);
 const versions=await api('/providers/'+key+'/cloud/systems','POST',{settings});
 if(editId!==state.editId)return;
 openDialog('云手机型号目录',`<div class="catalog-dialog"><div class="row"><label>系统版本<select id="cloud-model-version">${versions.items.map(v=>`<option value="${esc(v.id)}">${esc(v.label)}</option>`).join('')}</select></label><label>筛选品牌 / 型号<input id="cloud-model-search"></label></div><div class="flex"><button id="load-cloud-models">刷新列表</button><button id="use-cloud-version">加入系统范围</button></div><p class="note" id="cloud-model-note">按所选系统读取官方型号；加入范围只筛选已有设备，不会自动重置。</p><div id="cloud-model-list" class="device-list catalog-list"></div></div>`,true);
 let rows=[];const host=$('#cloud-model-list');
 const add=(field,value)=>{if(editId!==state.editId)return;const values=state.doc.settings[field];if(!values.includes(value))values.push(value);markDirty();const input=$(`[data-field="${field}"]`);if(input)input.value=values.join(', ');syncEnvironmentFields();render();toast('已加入筛选范围，保存后使用')};
 const render=()=>{const query=$('#cloud-model-search').value.toLowerCase();host.innerHTML=rows.filter(r=>(r.brand+' '+r.label+' '+r.id).toLowerCase().includes(query)).map(r=>`<div class="catalog-model-row"><div><strong>${esc(r.brand)} · ${esc(r.label)}</strong><small>${esc(r.id)}</small></div><div class="flex"><button data-cloud-brand="${esc(r.brand)}" ${state.doc.settings.device_brands.includes(r.brand)?'disabled':''}>${state.doc.settings.device_brands.includes(r.brand)?'已加入品牌':'加入品牌'}</button><button data-cloud-model="${esc(r.id)}" ${state.doc.settings.device_models.includes(r.id)?'disabled':''}>${state.doc.settings.device_models.includes(r.id)?'已加入型号':'加入型号'}</button>${key==='duoplus'?`<button data-cloud-reset="${esc(r.id)}">换为此型号</button>`:''}</div></div>`).join('')||'<p>没有匹配型号</p>';
  $$('[data-cloud-brand]').forEach(b=>b.onclick=()=>add('device_brands',b.dataset.cloudBrand));$$('[data-cloud-model]').forEach(b=>b.onclick=()=>add('device_models',b.dataset.cloudModel));$$('[data-cloud-reset]').forEach(b=>b.onclick=e=>act(()=>cloudResetDialog({model:b.dataset.cloudReset,version:$('#cloud-model-version').value}),e.target));
 };
 $('#cloud-model-search').oninput=render;$('#use-cloud-version').onclick=()=>add('device_versions',$('#cloud-model-version').value);
 let loadSequence=0;
 const load=async(refresh=false)=>{const seq=++loadSequence,button=$('#load-cloud-models'),note=$('#cloud-model-note');button.disabled=true;rows=[];host.innerHTML='<p class="catalog-empty">正在读取型号…</p>';try{const result=await api('/providers/'+key+'/cloud/models','POST',{settings:structuredClone(state.doc.settings),version:$('#cloud-model-version').value,refresh});if(!host.isConnected||seq!==loadSequence)return;rows=result.items;note.textContent=result.source+' · '+result.total+' 项。'+catalogCacheNote(result)+'。'+(result.note||'加入范围只筛选已有设备，不会自动重置。');render()}catch(e){if(host.isConnected&&seq===loadSequence){host.innerHTML='<p class="catalog-empty error">'+esc(e.message)+'</p>';note.textContent='型号读取失败，可以重新拉取'}throw e}finally{if(seq===loadSequence)button.disabled=false}};
 $('#load-cloud-models').onclick=()=>act(()=>load(true));$('#cloud-model-version').onchange=()=>act(load);
 if(versions.items.length)await act(load);else host.innerHTML='<p class="catalog-empty">厂商没有返回可用系统，请检查账号权限后重试。</p>';
}
async function cloudResetDialog(model){
 const key=state.key,editId=state.editId,settings=structuredClone(state.doc.settings);
 const result=await api('/providers/'+key+'/probe','POST',{settings});if(editId!==state.editId)return;
 const devices=(result.devices||[]).filter(d=>d.status===(key==='duoplus'?'running':'idle'));
 openDialog('单独换机',`<div class="stack"><p class="warningbox">换机会更改设备身份，可能清理应用或系统数据。这里只处理下方选定的设备；普通创建窗口不会执行此操作。</p><label>设备<select id="reset-device"><option value="">请选择设备</option>${devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)} · ${esc(d.id)}</option>`).join('')}</select></label>${key==='duoplus'?`<p>目标型号：${esc(model.model)}</p><label>资料处理<select id="reset-data-type"><option value="">请选择</option><option value="3">保留应用数据</option><option value="2">清理应用数据</option><option value="1">重装系统</option></select></label><small>DuoPlus 要求设备已开机。本软件正在使用的设备不能换机。</small>`:`<label class="inline"><input id="reset-random" type="checkbox" checked>由厂商随机新型号（取消则保留型号）</label><small>GeeLark 的现有设备换机接口不支持指定型号；需要先关闭设备和远控。地区、语言、电话和网络类型按接口保留。</small>`}<label class="inline"><input id="reset-ack" type="checkbox">我已核对目标设备及数据处理方式</label><button id="reset-submit" class="danger">执行换机</button><div id="reset-result"></div></div>`);
 const output=$('#reset-result');$('#reset-submit').onclick=e=>act(async()=>{const id=$('#reset-device').value;if(!id)throw Error('请选择目标设备');if(!$('#reset-ack').checked)throw Error('请先核对并勾选数据处理确认');if(key==='duoplus'&&!$('#reset-data-type').value)throw Error('请选择资料处理方式');const reply=await api('/providers/'+key+'/cloud/reset','POST',{settings,id,acknowledge:true,...(key==='duoplus'?{...model,data_type:Number($('#reset-data-type').value)}:{randomize:$('#reset-random').checked})});if(output.isConnected){output.textContent=reply.message;$('#reset-ack').checked=false}},e.target);
}

function renderCloudDevices(devices){
 const host=$('#devices');state.cloudDevices=devices;
 host.innerHTML='<div class="stack"><input id="device-search" placeholder="筛选设备名称、ID、品牌、型号"><div id="cloud-device-rows" class="device-list"></div></div>';
 const render=()=>{const query=$('#device-search').value.toLowerCase();$('#cloud-device-rows').innerHTML=devices.filter(d=>(d.name+' '+d.id+' '+d.brand+' '+d.model).toLowerCase().includes(query)).map(d=>`<div class="catalog-model-row"><label class="inline"><input data-device="${esc(d.id)}" type="checkbox" ${state.doc.settings.device_ids.includes(d.id)?'checked':''}><span>${esc(d.name)}<small>${esc(d.id)} · ${esc(d.system||'系统未提供')} · ${esc(d.brand||'')} ${esc(d.model||'型号待查询')}</small></span></label><div class="flex"><small>${esc(({idle:'空闲',running:'运行中',starting:'启动中'})[d.status]||d.status)}</small><button data-device-detail="${esc(d.id)}">读取型号</button><button data-use-device-version="${esc(d.system||'')}" ${d.system?'':'disabled'}>加入系统范围</button></div></div>`).join('')||'<p>没有匹配设备</p>';
 $$('[data-device]').forEach(el=>el.onchange=()=>{const values=new Set(state.doc.settings.device_ids);if(el.checked)values.add(el.dataset.device);else values.delete(el.dataset.device);state.doc.settings.device_ids=[...values];refreshChoiceSummary('device_ids');markDirty();syncEnvironmentFields()});
 $$('[data-use-device-version]').forEach(b=>b.onclick=()=>{const values=state.doc.settings.device_versions;if(!values.includes(b.dataset.useDeviceVersion))values.push(b.dataset.useDeviceVersion);refreshChoiceSummary('device_versions');markDirty();toast('已加入系统范围，保存后使用')});
 $$('[data-device-detail]').forEach(b=>b.onclick=e=>act(async()=>{const key=state.key,id=state.editId,value=await api('/providers/'+key+'/cloud/detail','POST',{settings:state.doc.settings,id:b.dataset.deviceDetail});if(id!==state.editId||!host.isConnected)return;Object.assign(devices.find(d=>d.id===value.id),value);render()},e.target));
 };$('#device-search').oninput=render;render();
}
