"""本机程序探测与启动前重绑定；信任信息只写本机文件。"""
from pathlib import Path
from types import SimpleNamespace
import socket
import threading
import time
from urllib.parse import urlsplit
import psutil
from . import providers

_service=None
_locks_guard=threading.Lock()
_launch_locks={}


class ProgramRequiredError(ValueError):
    """UI 可恢复错误：只选择当前厂商的 EXE，不提交部分创建任务。"""
    code='PROGRAM_REQUIRED'

    def __init__(self,key,message):
        self.provider=key
        self.label=providers.BY_KEY[key]['name']
        super().__init__(message+'；请选择 '+self.label+' 的主程序 EXE 位置')

    def public(self):
        return {'code':self.code,'provider':self.provider,'label':self.label,'message':str(self)}

def service():
    global _service
    if _service is None:
        from server.python.browser_s.browser_module_s.browser_program import BrowserProgramService
        _service=BrowserProgramService()
    return _service

def setting(key,config,store=None):
    binding=store.read('本机设置/程序绑定.json',{}).get(key,{}) if store else {}
    path=config.get('manager_path') or config.get('browser_path','')
    if binding.get('program_path')!=path:binding={}
    return SimpleNamespace(**{**binding,'program_path':path,'browser_path':config.get('browser_path',''),
                              'core_root':config.get('core_root','./文件缓存/ruyi-core')})

def probe(key,config,store=None,*,force=True,candidate_id=''):
    if key not in providers.BY_KEY or providers.BY_KEY[key]['family']!='desktop':raise ValueError('云手机使用 API，不需要本机浏览器程序')
    probe_key='hubstudio' if key=='hub' else key
    value=setting(key,config,store)
    status=service().reprobe(probe_key,value,force=force)
    candidate=service().confirm(probe_key,value,candidate_id) if candidate_id else service().automatic_candidate(status,value)
    if candidate and not candidate_id:
        # status 已校验文件时间戳和摘要，启动前无需第二次全盘读同一 EXE。
        candidate=service()._inspect(service().specs[probe_key],Path(candidate.path),candidate.source)
        if not candidate.trusted:candidate=None
    result=status.model_dump(mode='json',by_alias=True)
    result['selected']=candidate.model_dump(mode='json',by_alias=True) if candidate else None
    if candidate:result['message']='已识别 '+providers.BY_KEY[key]['name']+'：'+candidate.path
    return result

def ensure(key,config,store=None):
    try:result=probe(key,config,store,force=False)
    except Exception as exc:raise ProgramRequiredError(key,'程序探测失败：'+str(exc)) from exc
    candidate=result['selected']
    if not candidate:raise ProgramRequiredError(key,result['message'])
    save_binding(key,config,candidate,store)
    return Path(candidate['path']),candidate['sha256']


def save_binding(key,config,candidate,store):
    field='browser_path' if key in ('chrome','ruyi') else 'manager_path'
    config[field]=candidate['path']
    if store:
        binding={'program_path':candidate['path'],'program_candidate_id':candidate['candidateId'],
                 'program_sha256':candidate['sha256'],'program_source':candidate['source']}
        with store.lock:
            bindings=store.read('本机设置/程序绑定.json',{})
            if bindings.get(key)!=binding:
                bindings[key]=binding;store.write('本机设置/程序绑定.json',bindings)
            local=store.read('配置/本机程序.json',{'providers':{}})
            current=local.setdefault('providers',{}).setdefault(key,{})
            if current.get(field)!=candidate['path']:
                current[field]=candidate['path'];local['revision']=local.get('revision',0)+1
                store.write('配置/本机程序.json',local)


def bind_path(key,path,store):
    if key not in providers.BY_KEY or providers.BY_KEY[key]['family']!='desktop':
        raise ValueError('请选择电脑浏览器厂商')
    if not isinstance(path,str) or not path.strip():raise ValueError('请选择该浏览器主程序的 EXE 文件')
    path=Path(path.strip().strip('"')).expanduser()
    if not path.is_absolute() or path.suffix.lower()!='.exe' or not path.is_file():
        raise ValueError('请选择本机存在的完整 EXE 文件路径')
    config=store.provider(key)['settings']
    config['browser_path' if key in ('chrome','ruyi') else 'manager_path']=str(path)
    result=probe(key,config,store,force=True)
    candidate=result.get('selected')
    if not candidate or Path(candidate['path']).resolve()!=path.resolve():
        raise ValueError('所选 EXE 未通过 '+providers.BY_KEY[key]['name']+' 的程序核验，请选择该厂商主程序；不会改绑其他程序')
    save_binding(key,config,candidate,store)
    return {'ok':True,'provider':key,'path':candidate['path'],'message':'已绑定 '+providers.BY_KEY[key]['name']+'，后续启动会自动使用'}


def _running(path):
    target=str(path.resolve()).casefold()
    for process in psutil.process_iter(['exe']):
        try:
            if process.info.get('exe') and str(Path(process.info['exe']).resolve()).casefold()==target:return True
        except (OSError,psutil.Error):continue
    return False


def _port_ready(host,port):
    try:
        with socket.create_connection((host,port),timeout=.4):return True
    except OSError:return False


def ensure_ready(key,config,store,*,cancelled=lambda:False,on_stage=lambda stage:None,timeout=60):
    """先探测/绑定并打开厂商客户端，再读取指纹库；同厂商并发只启动一次。"""
    if key in ('chrome','ruyi'):return None
    with _locks_guard:lock=_launch_locks.setdefault(key,threading.Lock())
    while not lock.acquire(timeout=.2):
        if cancelled():raise InterruptedError('已取消等待浏览器客户端')
    try:
        if cancelled():raise InterruptedError('已取消启动浏览器客户端')
        # 程序位置和本机接口属于当前电脑，不能沿用旧失败记录中的失效地址。
        current=store.provider(key)['settings']
        for field in ('manager_path','local_api_url'):
            if current.get(field):config[field]=current[field]
        on_stage('探测并绑定浏览器客户端')
        path,digest=ensure(key,config,store)
        endpoint=urlsplit(config.get('local_api_url',''))
        if endpoint.hostname not in ('localhost','127.0.0.1','::1'):
            return path,digest
        port=endpoint.port or (443 if endpoint.scheme=='https' else 80)
        if _port_ready(endpoint.hostname,port):return path,digest
        if not _running(path):
            on_stage('正在自动打开 '+providers.BY_KEY[key]['name'])
            try:
                service().start('hubstudio' if key=='hub' else key,setting(key,config,store),automatic=True)
            except Exception as exc:
                # 已由另一批任务启动时继续等 API，不再启动第二个客户端。
                if getattr(exc,'code','')!='BROWSER_PROGRAM_START_COOLDOWN':raise
        on_stage('等待浏览器客户端就绪，请在客户端完成登录')
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if cancelled():raise InterruptedError('已取消等待浏览器客户端')
            if _port_ready(endpoint.hostname,port):return path,digest
            time.sleep(.25)
        raise ValueError(providers.BY_KEY[key]['name']+' 已尝试打开，但本机接口仍未就绪。请完成客户端登录，并核对“环境方案 → 连接”的 API 地址；不会创建窗口或消耗代理')
    finally:lock.release()
