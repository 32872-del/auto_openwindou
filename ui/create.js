function emailCreatePreview(items,accounts,kind){
 const newCount=accounts.filter(a=>a.action==='create').length;
 const blockedCount=accounts.filter(a=>a.action==='blocked').length;
 const existingCount=accounts.length-newCount-blockedCount;
 const displayItems=items.map((r,i)=>accounts[i]?.run_id?{...r,...accounts[i].input_data,...accounts[i].location,address:accounts[i].address}:r);
 return `<div class="result-box">已带入 ${items.length} 个邮箱：创建 ${newCount} 个，使用已有环境 ${existingCount} 个${blockedCount?`，需处理 ${blockedCount} 个`:""}（${kind==='phone'?'云手机':'电脑浏览器'}）。<button id="back-paste" class="link">返回修改表格</button></div><div class="import-grid"><table><thead><tr><th>数据</th><th>邮箱</th><th>名</th><th>姓</th><th>电话</th><th>处理方式</th><th>国家</th><th>州 / 省</th><th>城市</th><th>邮编</th><th>地址</th></tr></thead><tbody>${displayItems.map((r,i)=>`<tr title="原始第 ${r.source_row} 行"><td>第 ${i+1} 条</td><td>${esc(r.email||r.name||'自动编号')}</td><td>${esc(r.firstName||'')}</td><td>${esc(r.lastName||'')}</td><td>${esc(r.phone||'')}</td><td class="${accounts[i]?.error?'error':''}">${esc(accounts[i]?.label||'')}<small>${esc(accounts[i]?.label_provider||'')}</small></td><td>${esc(({us:'美国',ca:'加拿大'})[r.country]||r.country||'未填写')}</td><td>${esc(r.state)}</td><td>${esc(r.city)}</td><td>${esc(r.zip)}</td><td>${esc(r.address)}</td></tr>`).join('')}</tbody></table></div>`;
}

async function openCreate(items=null){
 const saved=state.preferences.create||{};
 const initialKind=['desktop','phone'].includes(saved.kind)?saved.kind:(state.kind==='desktop'?'desktop':'phone');
 let accounts=items?(await api('/email-accounts/preview','POST',{items,kind:initialKind})).rows:[];
 let previewPending=false,previewFailed=false,previewVersion=0;
 let systemPlan=null,systemPending=false,systemFailed=false,systemVersion=0;
 const blocked=()=>accounts.some(a=>a.action==='blocked');
 const newCount=()=>accounts.filter(a=>a.action==='create').length;
 const [proxies,system]=await Promise.all([api('/proxies'),api('/system')]);state.proxies=proxies;state.system=system;
 if(system.operator_required){operatorDialog();return}
 openDialog('创建窗口',`<div class="stack">${items?`<div id="email-create-preview">${emailCreatePreview(items,accounts,initialKind)}</div>`:''}
 <div class="row"><label>环境类型<select id="create-kind"><option value="desktop">电脑浏览器</option><option value="phone">云手机</option></select></label><label>${items?'新建环境使用厂商':'使用厂商'}<select id="create-provider"></select></label></div>
 <div id="phone-network-options" hidden>
  <label>本次分配设备范围<select id="create-device-scope">
   <option value="inherit">沿用厂商方案的设备范围</option>
   <option value="all_idle">全部空闲设备（不限品牌、型号或系统版本）</option>
  </select></label>
  <small>每条新资料分配一台设备。已绑定邮箱或被任务占用的设备会保留给原任务。</small>
  <p id="cloud-run-limit" class="note" role="status"></p>
  <label>${items?'新绑定云手机的联网方式':'云手机联网方式'}<select id="create-network-mode">
   <option value="inherit">使用厂商方案</option><option value="keep">沿用设备当前设置</option>
   <option value="wifi">Wi-Fi</option><option value="mobile">移动网络</option>
  </select></label>
  ${items?'<small>已绑定的邮箱沿用原联网设置。</small>':''}
 </div>
 <div id="create-systems" class="result-box system-settings" hidden>
  <label>新建环境使用系统<select id="create-system"><option value="win">win · Windows</option><option value="mac">mac · macOS</option><option value="android">android · Android</option></select></label>
  <div id="windows-allocation"><label>Win10 / Win11 分配方式<select id="windows-mode"><option value="random">随机分配</option><option value="weighted">比例分配</option></select></label>
   <div id="windows-weights" class="row" hidden><label>Win10 比例<input id="win10-weight" type="number" min="0" max="1000" step="1" value="1"></label><label>Win11 比例<input id="win11-weight" type="number" min="0" max="1000" step="1" value="1"></label></div>
   <p id="windows-mode-note" class="note"></p>
  </div>
  <p id="system-detail-note" class="note"></p>
  <p id="system-summary" role="status" aria-live="polite"></p><button id="refresh-systems" type="button">重新分配</button>
  <p class="note">只分配本批新建邮箱。已有环境沿用原系统；本次选择不修改环境方案。U 使用账号中匹配系统的配置，无匹配配置时提示失败。</p>
 </div>
 <div id="kept-u-box" class="result-box" hidden><strong>启动已保留 U 环境</strong><p class="note">只列本软件已保留、已关闭且未进入回收站的环境；沿用原代理和配置。</p><div class="row"><label>选择环境<select id="kept-u-id"></select></label><button id="refresh-kept-u" type="button">刷新列表</button><button id="start-kept-u" type="button">启动选中环境</button></div><small id="kept-u-status"></small></div>
 <div class="row"><label ${items?'hidden':''}>名称前缀<input id="create-name" value="窗口"></label><label ${items?'hidden':''}>数量<input id="create-count" type="number" min="1" max="100" value="${items?items.length:1}" ${items?'disabled':''}></label></div>
 <label id="target-label">启动网址<input id="create-target" placeholder="留空使用环境方案中的默认值"></label>
 ${items?'':locationInputs()}
 <label>本次代理<select id="create-proxy-mode">${proxyModeOptions('inherit',initialKind)}</select></label><div id="create-proxy-options"><span class="field-name">本次代理来源${help('一个固定使用；多个按来源权重随机。')}</span><div class="chips"></div></div>
 <p id="cloud-proxy-note" class="note" hidden>未分配任务代理时，云手机沿用设备已有代理。有目标地区时，会在开机前核验已有代理的位置。</p>
 <p id="desktop-direct-note" class="note" hidden>浏览器直连使用本机出口。要按资料中的国家和州省选择出口，请指定代理来源。</p>
 <label>代理 IP 类型<select id="create-ip-family"><option value="any">不限 IPv4 / IPv6</option><option value="ipv4">只使用 IPv4</option><option value="ipv6">只使用 IPv6</option><option value="dual">IPv4 + IPv6 双栈</option></select></label>
 <label id="cloud-city-policy" hidden>代理城市匹配<select id="create-city-policy">
  <option value="inherit">新邮箱仅限粘贴城市，已绑定邮箱沿用原设置</option>
  <option value="city">仅限粘贴城市，找不到则停止</option>
  <option value="state">允许城市不可用时使用同州 / 省的出口</option>
 </select></label>
 <p id="proxy-attempt-note" class="note"></p>
 <label class="inline" id="tls-option"><input type="checkbox" id="tls-test">打开 tls.peet.ws，保存到 Excel 后自动关闭检测页</label>
 <label class="inline" id="site-option"><input type="checkbox" id="website-test" ${system.website_check_default?'checked':''}>打开 BrowserScan、CreepJS，完成后保存网页</label>
 <label class="inline" id="cleanup-option"><input type="checkbox" id="create-cleanup">关闭浏览器后自动删除环境</label>
 <p class="note">${items?'同一邮箱可分别绑定电脑浏览器和云手机。按所选类型复用原环境；该类型尚未绑定时创建，原有另一类型环境继续保留。厂商选择仅用于新建环境。云手机一条邮箱绑定一台已有设备，数量不足时整批提示；关闭时关机并保留设备数据。':'不勾选时关闭后保留浏览器环境，可在“已保留”中继续管理；勾选后确认浏览器关闭就删除厂商环境。云手机关闭时关机，不删除设备。'}</p>
 <p class="note">${items?'邮箱作为窗口名。已打开的环境不重复启动，已关闭的环境使用原配置重新打开。':'本次选择不会改写已保存方案。'}点击下方按钮才开始创建。</p><button class="primary" id="submit-create" ${blocked()?'disabled':''}>${items?'确认打开 '+items.length+' 个邮箱环境':'创建窗口'}</button></div>`,!!items);
 $('#create-kind').value=initialKind;
 if(!items)await bindLocation('',saved);
 // 旧版只保存最后一次代理选择，它属于当时选中的环境类型。
 let proxyKind=initialKind;
 const choicesByKind={};
 for(const kind of ['desktop','phone']){
  choicesByKind[kind]={mode:saved[kind+'_proxy_mode']??(kind===initialKind?saved.proxy_mode:null)??'inherit',
   sources:saved[kind+'_proxy_sources']??(kind===initialKind?saved.proxy_sources:null)??[]};
 }
 const readProxyChoice=()=>({mode:$('#create-proxy-mode').value,sources:$$('[data-create-source]:checked').map(n=>n.dataset.createSource)});
 const renderProxySources=bindProxyScopeRefresh('create',choicesByKind[proxyKind].sources);
 $('#create-proxy-mode').value=choicesByKind[proxyKind].mode;
 const proxyMode=()=>{
  const selected=$('#create-proxy-mode').value==='selected';
  $('#create-proxy-options').hidden=!selected;
  $('#cloud-proxy-note').hidden=$('#create-kind').value!=='phone'||selected;
  $('#desktop-direct-note').hidden=$('#create-kind').value!=='desktop'||$('#create-proxy-mode').value!=='direct';
 };
 $('#create-proxy-mode').onchange=proxyMode;proxyMode();
		 const fill=()=>{const kind=$('#create-kind').value;const list=state.catalog.filter(p=>p.family===(kind==='desktop'?'desktop':'cloud'));$('#create-provider').innerHTML=list.map(p=>`<option value="${p.key}">${esc(p.name)}</option>`).join('')+(kind==='desktop'?'<option value="random">按已保存比例随机</option>':'');if(list.some(p=>p.key===state.key))$('#create-provider').value=state.key;$('#target-label').hidden=kind!=='desktop';$('#site-option').hidden=kind!=='desktop';$('#website-test').disabled=kind!=='desktop';$('#cleanup-option').hidden=kind!=='desktop';$('#create-cleanup').disabled=!!items||kind!=='desktop'};
 fill();
 const syncCreateControls=()=>{
  const desktop=$('#create-kind').value==='desktop';
  const selectionDisabled=!!items&&(previewPending||previewFailed||newCount()===0);
  $('#create-provider').disabled=selectionDisabled;
  $('#phone-network-options').hidden=desktop||(!!items&&newCount()===0);
  $('#create-device-scope').disabled=desktop||selectionDisabled;
  $('#create-network-mode').disabled=desktop||selectionDisabled;
  const pastedCloud=!!items&&!desktop;
  $('#cloud-city-policy').hidden=!pastedCloud;
  $('#create-city-policy').disabled=!pastedCloud||previewPending||previewFailed;
  $('#proxy-attempt-note').textContent=pastedCloud
   ?'每条最多检测 5 次。城市匹配设置会保留到下次打开或重试。'
   :'每条最多检测 5 次。城市不可用时，仅在所选来源支持该州或省的情况下重试。出口地区通过检测后才会启动。';
  $('#tls-option').hidden=!desktop;$('#tls-test').disabled=!desktop;
 };
 syncCreateControls();
 if([...$('#create-provider').options].some(option=>option.value===saved.provider))$('#create-provider').value=saved.provider;
 let cloudLimitVersion=0;
 const refreshCloudLimit=async()=>{
  const version=++cloudLimitVersion;
  const provider=$('#create-provider').value,note=$('#cloud-run-limit');
  if($('#create-kind').value!=='phone'||!provider){note.textContent='';return}
  const name=state.catalog.find(p=>p.key===provider)?.name||provider;
  const setting='可在「环境方案 → 云手机 → '+name+' → 设备 → 最多同时运行」调整。';
  note.textContent=setting;
  try{
   const doc=await api('/providers/'+provider);
   if($('#cloud-run-limit')!==note||version!==cloudLimitVersion)return;
   const limit=doc.settings?.max_concurrent;
   if(Number.isInteger(limit)&&limit>0)note.textContent='本程序最多同时运行 '+limit+' 台 '+name+' 云手机，已分配的其他设备将等待运行名额。'+setting;
  }catch{/* 配置暂不可读时保留设置入口说明。 */}
 };
 refreshCloudLimit();
 const systemVisible=()=>!!items&&$('#create-kind').value==='desktop'&&['octo','undetectable'].includes($('#create-provider').value);
 const systemActive=()=>systemVisible()&&newCount()>0;
 const updateSubmit=()=>$('#submit-create').disabled=!!items&&(previewPending||previewFailed||blocked()||(systemActive()&&(systemPending||systemFailed||!systemPlan)));
 const readSystemSelection=()=>({family:$('#create-system').value,mode:$('#windows-mode').value,...($('#create-system').value==='win'&&$('#windows-mode').value==='weighted'?{win10:Number($('#win10-weight').value),win11:Number($('#win11-weight').value)}:{win10:1,win11:1})});
 for(const [id,key] of [['create-system','system_family'],['windows-mode','windows_mode'],['win10-weight','win10_weight'],['win11-weight','win11_weight']]){
  if(saved[key]!=null)$('#'+id).value=saved[key];
 }
 if(!$('#create-system').value)$('#create-system').value='win';
 if(!$('#windows-mode').value)$('#windows-mode').value='random';
 const showSystems=()=>{
  $('#create-systems').hidden=!systemVisible();
  const win=$('#create-system').value==='win',weighted=$('#windows-mode').value==='weighted';
  $('#windows-allocation').hidden=!win;$('#windows-weights').hidden=!weighted;
  $('#windows-mode-note').textContent=weighted?'按比例计算整批数量，余数按最大余数补齐，再打乱邮箱顺序。0 表示不分配；例如 10 个新邮箱、3:7，将分配 3 个 Win10 和 7 个 Win11。':'每个新邮箱独立随机选择 Win10 或 Win11，各有 50% 概率；数量不保证各半。';
  $('#system-detail-note').textContent=$('#create-system').value==='mac'?'Mac 沿用环境方案中的 Mac 架构范围和比例；未配置 Mac 范围时使用 Intel。':$('#create-system').value==='android'?'本批新建邮箱全部使用 Android 浏览器环境；O 的机型范围沿用环境方案。':'';
  $$('#create-systems input, #create-systems select').forEach(el=>el.disabled=previewPending||previewFailed||newCount()===0);
  $('#refresh-systems').disabled=previewPending||previewFailed||newCount()===0||systemPending;
 };
 const renderSystems=()=>{
  const labels=systemPlan?.labels||{'Win/Win10':'Win10','Win/Win11':'Win11','Mac/Intel':'Mac · Intel','Mac/ARM':'Mac · Apple Silicon','Android':'Android'};
  const assignments=new Map((systemPlan?.assignments||[]).map(r=>[r.email.toLowerCase(),r.system]));
  $$('#email-create-preview tbody tr').forEach((tr,i)=>{
   tr.querySelector('[data-assigned-system]')?.remove();
   const cell=document.createElement('td');cell.dataset.assignedSystem='';
   const account=accounts[i],system=account?.planned_system||assignments.get(items[i].email.toLowerCase());
   cell.textContent=(account?.run_id?'原系统：':'')+(labels[system]||system||(systemActive()?'待分配':'沿用环境方案'));tr.append(cell);
  });
  const header=$('#email-create-preview thead tr');
  if(header&&!header.querySelector('[data-assigned-system]')){const th=document.createElement('th');th.dataset.assignedSystem='';th.textContent='系统分配';header.append(th)}
 };
 const refreshSystems=async()=>{
  const version=++systemVersion,box=$('#create-systems');systemPlan=null;systemPending=false;systemFailed=false;
  showSystems();renderSystems();
  if(!systemActive()||previewPending||previewFailed){$('#system-summary').textContent=newCount()===0?'本批全部沿用已有环境和原系统。':'';updateSubmit();return}
  systemPending=true;$('#system-summary').classList.remove('error');$('#system-summary').textContent='正在分配系统…';showSystems();updateSubmit();
  try{
   if($('#create-system').value==='win'&&$('#windows-mode').value==='weighted'&&['win10-weight','win11-weight'].some(id=>!$('#'+id).value||!$('#'+id).checkValidity()))throw Error('系统比例请填写 0～1000 的整数');
   const result=await api('/email-systems/preview','POST',{items,kind:$('#create-kind').value,provider:$('#create-provider').value,system_selection:readSystemSelection()});
   if(!box.isConnected||version!==systemVersion)return;
   systemPlan=result;accounts=result.accounts;
   $('#email-create-preview').innerHTML=emailCreatePreview(items,accounts,$('#create-kind').value);$('#back-paste').onclick=()=>openPaste(true);
   $('#system-summary').textContent='本批新建 '+result.assignments.length+' 个：'+Object.entries(result.counts).map(([key,count])=>(result.labels[key]||key)+' '+count+' 个').join('，')+'；已有 '+(items.length-result.assignments.length)+' 个沿用原系统。';
   renderSystems();syncCreateControls();
  }catch(error){
   if(!box.isConnected||version!==systemVersion)return;
   systemFailed=true;$('#system-summary').textContent=error.message;$('#system-summary').classList.add('error');
  }finally{
   if(box.isConnected&&version===systemVersion){systemPending=false;showSystems();updateSubmit()}
  }
 };
 let loadedU=false;
 const refreshKeptU=async()=>{
  const status=$('#kept-u-status'),select=$('#kept-u-id'),start=$('#start-kept-u');
  status.textContent='正在读取已保留环境…';start.disabled=true;
  try{
   const result=await api('/retained-u-browsers');
   if(!select.isConnected)return;
   const rows=result.rows||[];
   select.innerHTML=rows.map(r=>`<option value="${esc(r.id)}">${esc(r.name||'未命名')} · ${esc(r.launch||r.profile_id)} · ${esc(r.profile_id)}</option>`).join('');
   status.textContent=rows.length?`可启动 ${rows.length} 个已保留 U 环境`:'没有可启动的已保留 U 环境';
   start.disabled=!rows.length;loadedU=true;
  }catch(error){if(select.isConnected){select.innerHTML='';status.textContent=error.message;start.disabled=true}}
 };
 const showKeptU=()=>{const visible=!items&&$('#create-kind').value==='desktop'&&$('#create-provider').value==='undetectable';$('#kept-u-box').hidden=!visible;if(visible&&!loadedU)refreshKeptU()};
 showKeptU();
 $('#refresh-kept-u').onclick=()=>refreshKeptU();
 $('#start-kept-u').onclick=e=>act(async()=>{const identity=$('#kept-u-id').value;if(!identity)throw Error('请选择已保留 U 环境');await api('/runs/'+encodeURIComponent(identity)+'/resume','POST');closeDialog();state.offset=0;await page('work');toast('已保留 U 环境加入启动队列')},e.target);
 for(const [id,key] of [['create-name','name'],['create-count','count'],['create-target','target'],['create-ip-family','ip_family']]){
  if(saved[key]!=null&&(!items||!['name','count'].includes(key)))$('#'+id).value=saved[key];
 }
 if(typeof saved.website_test==='boolean')$('#website-test').checked=saved.website_test;
 if(typeof saved.tls_test==='boolean')$('#tls-test').checked=saved.tls_test;
 if(typeof saved.cleanup==='boolean')$('#create-cleanup').checked=saved.cleanup;
 if(['inherit','keep','wifi','mobile'].includes(saved.network_mode))$('#create-network-mode').value=saved.network_mode;
 if(items)$('#create-cleanup').checked=false;
 proxyMode();
 const refreshAccounts=async()=>{
  if(!items)return;
  const version=++previewVersion,select=$('#create-kind'),box=$('#email-create-preview'),button=$('#submit-create');
  const kind=select.value;
  previewPending=true;previewFailed=false;button.disabled=true;syncCreateControls();refreshSystems();
  box.setAttribute('aria-busy','true');
  try{
   const result=await api('/email-accounts/preview','POST',{items,kind});
   if(!select.isConnected||version!==previewVersion)return;
   accounts=result.rows;previewPending=false;
   box.innerHTML=emailCreatePreview(items,accounts,kind);
   $('#back-paste').onclick=()=>openPaste(true);
   updateSubmit();refreshSystems();
  }catch(error){
   if(!select.isConnected||version!==previewVersion)return;
   previewPending=false;previewFailed=true;button.disabled=true;
   box.innerHTML=`<div class="result-box error">${esc(error.message)}<button id="retry-email-preview" type="button">重新预览</button></div>`;
   $('#retry-email-preview').onclick=refreshAccounts;
  }finally{
   if(select.isConnected&&version===previewVersion){box.removeAttribute('aria-busy');syncCreateControls()}
  }
 };
 $('#create-kind').onchange=()=>{
  const previous=readProxyChoice();
  choicesByKind[proxyKind]=previous;
  remember('create',{[proxyKind+'_proxy_mode']:previous.mode,[proxyKind+'_proxy_sources']:previous.sources});
  proxyKind=$('#create-kind').value;
  fill();
  $('#create-proxy-mode').innerHTML=proxyModeOptions(choicesByKind[proxyKind].mode,proxyKind);
  renderProxySources(choicesByKind[proxyKind].sources);
  proxyMode();
  syncCreateControls();refreshCloudLimit();showKeptU();saveCreateDefaults();refreshAccounts();
 };
 $('#create-provider').onchange=()=>{refreshCloudLimit();showKeptU();saveCreateDefaults();refreshSystems()};
 if(items)$('#back-paste').onclick=()=>openPaste(true);
 const saveCreateDefaults=()=>{
  const choice=readProxyChoice();
  const values={kind:$('#create-kind').value,provider:$('#create-provider').value,target:$('#create-target').value,
   proxy_mode:choice.mode,proxy_sources:choice.sources,[proxyKind+'_proxy_mode']:choice.mode,[proxyKind+'_proxy_sources']:choice.sources,
		  ip_family:$('#create-ip-family').value,website_test:$('#website-test').checked,tls_test:$('#tls-test').checked,...(items?{}:{cleanup:$('#create-cleanup').checked})};
  if(!$('#create-network-mode').disabled)values.network_mode=$('#create-network-mode').value;
  if(!items)Object.assign(values,{name:$('#create-name').value,count:Math.max(1,Math.min(100,Number($('#create-count').value)||1)),...readLocation()});
  if(items){
   Object.assign(values,{system_family:$('#create-system').value,windows_mode:$('#windows-mode').value});
   for(const [id,key] of [['win10-weight','win10_weight'],['win11-weight','win11_weight']])if($('#'+id).value&&$('#'+id).checkValidity())values[key]=Number($('#'+id).value);
  }
  remember('create',values);
 };
 $('#dialog-body').oninput=saveCreateDefaults;$('#dialog-body').onchange=saveCreateDefaults;
 for(const id of ['create-system','windows-mode','win10-weight','win11-weight'])$('#'+id).oninput=()=>{saveCreateDefaults();refreshSystems()};
 $('#refresh-systems').onclick=refreshSystems;
 refreshSystems();
 $('#submit-create').onclick=e=>act(async()=>{
  if(items&&(previewPending||previewFailed||blocked()||(systemActive()&&(systemPending||systemFailed||!systemPlan))))throw Error('请等待预览和系统分配完成并处理提示');
  const network=$('#create-network-mode');
  const deviceScope=$('#create-device-scope');
  const cityPolicy=$('#create-city-policy');
  await api('/runs','POST',{
   kind:$('#create-kind').value,provider:$('#create-provider').value,count:Number($('#create-count').value),
   name:$('#create-name').value,target:$('#create-target').value,location:items?{}:readLocation(),
   proxy_mode:$('#create-proxy-mode').value,proxy_sources:$$('[data-create-source]:checked').map(n=>n.dataset.createSource),
   proxy_test:true,ip_family:$('#create-ip-family').value,
   website_test:!$('#website-test').disabled&&$('#website-test').checked,tls_test:!$('#tls-test').disabled&&$('#tls-test').checked,
   cleanup:!$('#create-cleanup').disabled&&$('#create-cleanup').checked,
   ...(!network.disabled&&network.value!=='inherit'?{network_mode:network.value}:{}),
   ...(!cityPolicy.disabled&&cityPolicy.value!=='inherit'?{allow_city_fallback:cityPolicy.value==='state'}:{}),
   ...(!deviceScope.disabled&&deviceScope.value==='all_idle'?{cloud_device_scope:deviceScope.value}:{}),
   ...(items?{items,email_mode:true}:{}),
   ...(systemActive()?{system_selection:systemPlan.selection,system_assignments:systemPlan.assignments}:{})
  });
  closeDialog();state.offset=0;await page('work');toast(items?'已按邮箱打开或加入启动队列':'已加入创建队列');
 },e.target);
}

async function weightDialog(){const value=await api('/weights');openDialog('电脑浏览器随机比例',`<div class="stack"><p>先按比例分配整批数量，再随机打乱。余数优先补给最接近下一整台的选项；相同余数随机分配。0 不参与。</p>${state.catalog.filter(p=>p.family==='desktop').map(p=>`<label class="weight-row"><span>${esc(p.name)}</span><input type="number" min="0" max="1000" data-weight="${p.key}" value="${value.weights[p.key]||0}"></label>`).join('')}<button id="save-weights" class="primary">保存随机比例</button></div>`);$('#save-weights').onclick=e=>act(async()=>{const weights={};$$('[data-weight]').forEach(el=>weights[el.dataset.weight]=Number(el.value));await api('/weights','PUT',{revision:value.revision,weights});closeDialog();toast('随机比例已保存')},e.target)}
