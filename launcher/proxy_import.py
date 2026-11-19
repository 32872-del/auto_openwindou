"""代理表格先校验，再整体保存。"""
import csv
import io
import uuid
from .proxy import validate_sources

HEADERS={'来源名称':'name','协议':'protocol','主机':'host','端口':'port','账号':'username','密码':'password','类型':'source_type','提取接口':'api_endpoint','权重':'weight','换IP链接':'rotate_url','换IP等待秒数':'rotate_wait'}

def preview(text,document):
    if not isinstance(text,str) or len(text)>1_000_000:raise ValueError('代理表格内容为空或过大')
    lines=list(csv.reader(io.StringIO(text.lstrip('\ufeff')),delimiter='\t'))
    if not lines:raise ValueError('请粘贴表格内容')
    headers=[HEADERS.get(k.strip(),k.strip()) for k in lines[0]]
    if not {'name','protocol'}.issubset(headers):raise ValueError('请使用模板表头，至少包含“来源名称”和“协议”')
    if len(set(headers))!=len(headers) or set(headers)-set(HEADERS.values()):raise ValueError('表头重复或包含不支持的列')
    if len(lines)>501:raise ValueError('一次最多导入 500 个代理来源')
    current={s['name']:s for s in document['sources']};rows=[];merged=list(document['sources']);seen=set()
    for number,cells in enumerate(lines[1:],2):
        if not any(cells):continue
        if len(cells)!=len(headers):raise ValueError(f'第 {number} 行列数与表头不一致')
        raw=dict(zip(headers,cells));name=raw['name'].strip()
        if name in seen:raise ValueError(f'第 {number} 行来源名称重复：{name}')
        seen.add(name);old=current.get(name)
        raw.update(id=old['id'] if old else uuid.uuid4().hex,name=name,enabled=old['enabled'] if old else True)
        raw['source_type']=raw.get('source_type') or (old or {}).get('source_type','static')
        try:
            for key in ('port','weight','rotate_wait'):
                if key in raw:raw[key]=int(raw[key] or {'port':1080,'weight':1,'rotate_wait':5}[key])
            raw=validate_sources({'sources':[{**(old or {}),**raw}]})['sources'][0]
        except (ValueError,TypeError) as exc:raise ValueError(f'第 {number} 行：{exc}') from exc
        merged=[s for s in merged if s['id']!=raw['id']]+[raw]
        rows.append({'line':number,'name':name,'action':'更新同名来源' if old else '新增'})
    if not rows:raise ValueError('表格没有可导入的内容')
    value=validate_sources({'sources':merged,'defaults':document['defaults']})
    return {'rows':rows,'document':{**value,'revision':document['revision']}}
