'use strict';
let locationDirectoryPromise;
function locationInputs(prefix=''){
 const id=name=>prefix+name;
 return '<div class="row location-fields" data-location-prefix="'+esc(prefix)+'">'+
	  '<label>国家<select id="'+id('country')+'" disabled><option value="">不指定</option><option value="us">美国</option><option value="ca">加拿大</option><option value="other">其他国家</option></select></label>'+ 
  '<label>州 / 省<input type="search" id="'+id('region-search')+'" placeholder="搜索州或省"><select id="'+id('region')+'" aria-label="选择州或省"></select><input id="'+id('region-other')+'" hidden placeholder="输入州 / 省"></label>'+
  '<label>城市<input type="search" id="'+id('city-search')+'" placeholder="搜索城市"><select id="'+id('city')+'" aria-label="选择城市"></select><input id="'+id('city-other')+'" hidden placeholder="输入城市"></label>'+
  '<label>邮编<input id="'+id('zip')+'" type="text" inputmode="text" autocomplete="postal-code" placeholder="保留前导零"></label>'+
  '<label class="location-other" id="'+id('country-other-label')+'" hidden>其他国家代码<input id="'+id('country-other')+'" placeholder="例如 gb"></label>'+
  '<small class="location-status" id="'+id('location-status')+'">正在读取美加地区目录…</small></div>';
}
async function loadLocationDirectory(){
 if(!locationDirectoryPromise)locationDirectoryPromise=api('/locations').catch(error=>{locationDirectoryPromise=null;throw error});
 return locationDirectoryPromise;
}
async function bindLocation(prefix='',defaults={}){
 const get=name=>$('#'+prefix+name),country=get('country'),region=get('region'),city=get('city');
 const status=get('location-status');
 if(!country)return;
 get('zip').value=String(defaults.zip||'');
 let directory;
 try{directory=await loadLocationDirectory()}catch(error){if(status)status.textContent='地区目录读取失败：'+error.message;throw error}
 if(!country.isConnected)return;
 const countries=new Map((directory.countries||[]).map(item=>[item.id,item]));
 const savedCountry=String(defaults.country||'').toLowerCase();
 country.value=countries.has(savedCountry)?savedCountry:savedCountry?'other':'';
 if(country.value==='other')get('country-other').value=savedCountry;
 const setOptions=(select,entries,current,empty)=>{
  select.innerHTML='<option value="">'+empty+'</option>'+entries.map(item=>'<option value="'+esc(item.id)+'">'+esc(item.label)+'</option>').join('');
  if(current&&!entries.some(item=>item.id===current))select.insertAdjacentHTML('beforeend','<option value="'+esc(current)+'">原选择：'+esc(current)+'</option>');
  select.value=current||'';
 };
 const updateCities=(selected='')=>{
  const zone=(countries.get(country.value)?.states||[]).find(item=>item.id===region.value);
  const query=get('city-search').value.trim().toLowerCase();
  const names=(zone?.cities||[]).filter(name=>!query||name.toLowerCase().includes(query)).slice(0,3000);
  setOptions(city,names.map(name=>({id:name,label:name})),selected||city.value,'先选择城市');
  city.disabled=!zone;
  status.textContent=zone?'来自离线地区目录；城市仅用于选择，不保证代理供应商有该出口':'选择国家与州 / 省后显示城市';
 };
 const updateRegions=(selected='',selectedCity='')=>{
  const zones=countries.get(country.value)?.states||[];
  const query=get('region-search').value.trim().toLowerCase();
  setOptions(region,zones.filter(item=>!query||(item.id+' '+item.label).toLowerCase().includes(query)),selected||region.value,'选择州 / 省');
  region.disabled=!zones.length;
  updateCities(selectedCity);
 };
 const toggleOther=()=>{
  const other=country.value==='other';
  get('country-other-label').hidden=!other;
  for(const name of ['region','city']){
   get(name).hidden=other;get(name+'-search').hidden=other;get(name+'-other').hidden=!other;
  }
  if(other)status.textContent='其他国家请填写明确的国家代码和地区；不做美加自动识别';
 };
 country.onchange=()=>{get('region-search').value='';get('city-search').value='';region.value='';city.value='';toggleOther();updateRegions()};
 region.onchange=()=>{get('city-search').value='';city.value='';updateCities()};
 get('region-search').oninput=()=>updateRegions();
 get('city-search').oninput=()=>updateCities();
 get('region-other').value=String(defaults.state||'');get('city-other').value=String(defaults.city||'');
	 toggleOther();updateRegions(String(defaults.state||''),String(defaults.city||''));country.disabled=false;
}
function readLocation(prefix=''){
 const get=name=>$('#'+prefix+name),other=get('country').value==='other';
 return {country:other?get('country-other').value.trim().toLowerCase():get('country').value,
  state:other?get('region-other').value.trim():get('region').value,
  city:other?get('city-other').value.trim():get('city').value,zip:get('zip').value.trim()};
}
