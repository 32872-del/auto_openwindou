"""配置原子保存、版本检查与本机运行账本。"""
import json
import hashlib
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from copy import deepcopy
from datetime import date, timedelta
from urllib.parse import urlparse
from .catalog import BY_KEY, defaults, validate
from .paths import Layout, atomic_json, read_json

LOCAL_PROVIDER_FIELDS = {'manager_path', 'browser_path', 'core_root', 'core_directory', 'fingerprint_directory'}
SHARED_SYSTEM_FIELDS = {'proxy_test_url','proxy_timeout','sync_url','sync_timeout','v2_url','v2_key','v2_wait_count','site_timeout'}


def shared_default():
    return {'format':'window-studio-shared', 'version':1, 'revision':0,
            'providers':{}, 'plans':{}, 'proxies':{'revision':0,'sources':[],'defaults':[]},
            'weights':{'revision':0,'weights':{}}, 'services':{'revision':0}}


def shared_slot(path):
    p=Path(path).as_posix()
    if p.startswith('配置/厂商/') and p.endswith('.json'):return ('providers',Path(p).stem)
    if p.startswith('配置/方案/') and p.endswith('.json'):return ('plans',Path(p).stem)
    return {'配置/代理来源.json':('proxies',),'配置/随机比例.json':('weights',),
            '配置/服务.json':('services',)}.get(p)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self._cloud_run_limits = {}
        self.paths = Layout(self.root)
        self._recover_config_transaction()
        self._migrate_layout()
        self.database = self.paths.state / '窗口.sqlite'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, updated REAL NOT NULL, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS logs(seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS run_order(number INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL);
              CREATE TABLE IF NOT EXISTS email_bindings(email_key TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT UNIQUE NOT NULL, email TEXT NOT NULL, PRIMARY KEY(email_key,kind));
              INSERT OR IGNORE INTO run_order(id) SELECT id FROM runs ORDER BY rowid;''')
            # 旧版一个邮箱只能绑定一个环境；按类型保留原绑定，不改写任何运行记录。
            db.execute('BEGIN IMMEDIATE')
            if 'kind' not in {column[1] for column in db.execute('PRAGMA table_info(email_bindings)')}:
                db.execute('CREATE TABLE email_bindings_by_kind(email_key TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT UNIQUE NOT NULL, email TEXT NOT NULL, PRIMARY KEY(email_key,kind))')
                db.execute("INSERT INTO email_bindings_by_kind SELECT b.email_key, CASE WHEN json_extract(r.data,'$.kind') IN ('phone','app') THEN 'phone' ELSE 'desktop' END, b.run_id, b.email FROM email_bindings b LEFT JOIN runs r ON r.id=b.run_id")
                db.execute('DROP TABLE email_bindings')
                db.execute('ALTER TABLE email_bindings_by_kind RENAME TO email_bindings')
        self._log_housekeeping_day=''
        self._maintain_logs()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    def resource_owner(self):
        """实例、电脑及目录都一致才允许重启后自动清理旧资源。"""
        with self.lock:
            identity = self.paths.local / '实例.json'
            value = read_json(identity, {})
            if not value.get('uuid'):
                value = {'uuid': uuid.uuid4().hex}
                atomic_json(identity, value)
            source = '|'.join((value['uuid'], os.environ.get('COMPUTERNAME', ''), os.path.normcase(str(self.root))))
            return hashlib.sha256(source.encode('utf-8')).hexdigest()

    def unfinished_runs(self):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM runs WHERE json_extract(data,'$.status') IN ('queued','creating','running','closing','cleanup_failed')").fetchall()
        return [json.loads(row[0]) for row in rows]

    def read(self, path, default):
        slot=shared_slot(path)
        if slot:
            value=self.shared()
            for key in slot:
                if key not in value:return deepcopy(default)
                value=value[key]
            return deepcopy(value)
        return read_json(self.paths.local_file(path),default)

    def shared(self):
        value=read_json(self.paths.shared,shared_default())
        if value.get('format')!='window-studio-shared' or value.get('version')!=1:
            raise ValueError('共用配置版本不受支持；未覆盖原文件')
        for key in ('providers','plans','proxies','weights','services'):
            if not isinstance(value.get(key),dict):raise ValueError('共用配置缺少有效的 '+key)
        return value

    def _recover_config_transaction(self):
        journal=self.paths.state/'配置写入事务.json'
        if not journal.exists():return
        value=read_json(journal,{})
        files=value.get('before')
        if not isinstance(files,dict):raise ValueError('配置事务记录损坏，未覆盖配置')
        for relative,old in files.items():
            dest=(self.root/relative).resolve()
            if not (dest.is_relative_to(self.paths.common) or dest.is_relative_to(self.paths.local)):
                raise ValueError('配置事务记录含未授权位置，未恢复')
            if old is None:dest.unlink(missing_ok=True)
            else:atomic_json(dest,old)
        journal.unlink()

    def _commit_config(self,files):
        """共用设置与本机覆盖同步提交；异常/重启可恢复完整旧版本。"""
        before={}
        for path in files:
            path=Path(path).resolve()
            if not (path.is_relative_to(self.paths.common) or path.is_relative_to(self.paths.local)):
                raise ValueError('配置提交位置超出已登记目录')
            before[path.relative_to(self.root).as_posix()]=read_json(path,{}) if path.exists() else None
        journal=self.paths.state/'配置写入事务.json'
        atomic_json(journal,{'before':before})
        try:
            for path,value in files.items():atomic_json(path,value)
            journal.unlink()
        except Exception:
            self._recover_config_transaction()
            raise

    def _migrate_layout(self):
        """仅新项目旧布局迁移。先校验和写新配置，原件留专属目录可回退。"""
        old_config=self.root/'配置'
        if not self.paths.shared.exists():
            common=shared_default();local_providers={}
            if old_config.is_dir():
                for folder,section in [('厂商','providers'),('方案','plans')]:
                    for file in (old_config/folder).glob('*.json'):
                        doc=read_json(file,{})
                        if section=='providers':
                            setting=doc.get('settings',{})
                            local_providers[file.stem]={k:v for k,v in setting.items() if k in LOCAL_PROVIDER_FIELDS}
                            doc['settings']={k:v for k,v in setting.items() if k not in LOCAL_PROVIDER_FIELDS}
                        common[section][file.stem]=doc
                for filename,section in [('代理来源.json','proxies'),('随机比例.json','weights')]:
                    if (old_config/filename).exists():common[section]=read_json(old_config/filename,{})
                old_system=read_json(old_config/'系统.json',{})
                common['services'].update({k:v for k,v in old_system.items() if k in SHARED_SYSTEM_FIELDS})
                local={k:v for k,v in old_system.items() if k not in SHARED_SYSTEM_FIELDS|{'alias','sync_alias','operator_required'}}
                if local.get('archive_dir')=='./检测存档':local['archive_dir']='./专属配置/网页存档'
                if not (self.paths.local/'系统.json').exists():atomic_json(self.paths.local/'系统.json',local)
                if old_system.get('alias') and not (self.root/'状态/操作人.json').exists():
                    atomic_json(self.paths.local/'操作人.json',{'alias':old_system['alias']})
            if local_providers:atomic_json(self.paths.local/'本机程序.json',{'providers':local_providers})
            atomic_json(self.paths.shared,common)
        # 不把源项目任务搬过来，只收纳本软件自身已有数据。
        moves=[];directories=[]
        for old_name,dest in [('状态',self.paths.state),('日志',self.paths.logs),('检测存档',self.paths.archives),('文件缓存',self.paths.cache)]:
            source=self.root/old_name
            if not source.is_dir():continue
            directories.append(source)
            for item in list(source.iterdir()):
                target=self.paths.local/item.name if old_name=='状态' and item.name in ('操作人.json','实例.json') else dest/item.name
                if target.exists():
                    raise ValueError(f'迁移发现同名数据：{target}；保留了两份原件，请先核对')
                if not item.resolve().is_relative_to(self.root) or not target.resolve().is_relative_to(self.root):
                    raise ValueError('旧数据包含指向软件外部的链接，未移动')
                moves.append((item,target))
        moved=[]
        try:
            for item,target in moves:item.rename(target);moved.append((item,target))
        except Exception:
            for item,target in reversed(moved):target.rename(item)
            raise
        for source in directories:source.rmdir()
        if old_config.is_dir():
            destination=self.paths.state/'旧版配置原件'
            if destination.exists():destination=destination.with_name(destination.name+'-'+uuid.uuid4().hex[:8])
            old_config.rename(destination)

    def write(self, path, value, revision=None):
        with self.lock:
            old = self.read(path, {'revision': 0})
            if revision is not None and revision != old.get('revision', 0):
                raise Conflict('配置已在另一窗口更新，请重新载入后再保存')
            saved = deepcopy(value)
            saved['revision'] = old.get('revision', 0) + 1
            slot=shared_slot(path)
            if slot:
                common=self.shared();parent=common
                for key in slot[:-1]:parent=parent.setdefault(key,{})
                parent[slot[-1]]=saved;common['revision']+=1
                atomic_json(self.paths.shared,common)
            else:atomic_json(self.paths.local_file(path),saved)
            return saved

    def provider(self, key):
        if key not in BY_KEY:
            raise ValueError('未知厂商')
        value = self.read(f'配置/厂商/{key}.json', {'revision': 0, 'settings': defaults(key)})
        value['settings'] = {**defaults(key), **value.get('settings', {})}
        value['settings'].update(self.read('配置/本机程序.json',{}).get('providers',{}).get(key,{}))
        return value

    def save_provider(self, key, payload):
        self.envelope(payload)
        values=validate(key,payload['settings'])
        with self.lock:
            current=self.provider(key)
            if current['revision']!=payload['revision']:raise Conflict('厂商配置已更新，请重新载入')
            local=self.read('配置/本机程序.json',{'providers':{}})
            local.setdefault('providers',{})[key]={k:v for k,v in values.items() if k in LOCAL_PROVIDER_FIELDS}
            local['revision']=local.get('revision',0)+1
            common=self.shared();common['revision']+=1
            common['providers'][key]={'revision':payload['revision']+1,'settings':{k:v for k,v in values.items() if k not in LOCAL_PROVIDER_FIELDS}}
            self._commit_config({self.paths.local/'本机程序.json':local,self.paths.shared:common})
            return self.provider(key)

    def cloud_run_limit(self, key):
        """运行名额是厂商的当前设置，排队轮询仅在共用配置变动时重新读取。"""
        with self.lock:
            try:
                stat = self.paths.shared.stat()
                version = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except FileNotFoundError:
                version = None
            cached = self._cloud_run_limits.get(key)
            if cached is None or cached[0] != version:
                limit = self.provider(key)['settings']['max_concurrent']
                self._cloud_run_limits[key] = (version, limit)
            return self._cloud_run_limits[key][1]

    @staticmethod
    def envelope(payload):
        if not isinstance(payload.get('settings'),dict):raise ValueError('配置请求缺少 settings 对象')
        Store.revision(payload)

    @staticmethod
    def revision(payload):
        value=payload.get('revision')
        if isinstance(value,bool) or not isinstance(value,int) or value<0:raise ValueError('配置请求缺少有效版本号，请重新载入配置')

    def plan(self, kind, key):
        self.check_kind(kind, key)
        value=self.read(f'配置/方案/{kind}-{key}.json', {'revision': 0, 'settings': {
            'url': 'about:blank', 'package': 'com.android.chrome' if kind == 'phone' else '',
            'apk_path': '', 'launch_label': 'Chrome' if kind == 'phone' else '', 'proxy_sources': [], 'preserve': False}})
        value['settings'].setdefault('apk_path','')
        value['settings'].setdefault('proxy_mode','selected' if value['settings'].get('proxy_sources') else 'inherit')
        value['settings'].update(self.read('本机设置/方案附件.json',{}).get('plans',{}).get(kind+'-'+key,{}))
        return value

    @staticmethod
    def check_kind(kind, key):
        if key not in BY_KEY or kind not in {'desktop', 'phone', 'app'} or ((kind == 'desktop') != (BY_KEY[key]['family'] == 'desktop')):
            raise ValueError('环境类型与厂商不匹配；电脑浏览器与云手机不能混合')

    def save_plan(self, kind, key, payload):
        self.check_kind(kind, key)
        self.envelope(payload)
        raw = payload['settings']
        if set(raw) - {'url','package','apk_path','launch_label','proxy_sources','proxy_mode','preserve'}:
            raise ValueError('方案包含不支持的字段')
        value = {**self.plan(kind, key)['settings'], **raw}
        for name in ('url','package','apk_path','launch_label'):
            if not isinstance(value.get(name,''),str):raise ValueError(f'{name} 必须是文本')
            value[name]=value.get(name,'').strip()
        if not isinstance(value['proxy_sources'], list) or any(not isinstance(x, str) for x in value['proxy_sources']):
            raise ValueError('代理来源必须是 ID 列表')
        known = {s['id'] for s in self.proxies()['sources']}
        value.setdefault('proxy_mode','selected' if value['proxy_sources'] else 'inherit')
        if value['proxy_mode'] not in ('inherit','selected','direct'):raise ValueError('代理选择方式无效')
        if value['proxy_mode']=='selected' and not value['proxy_sources']:raise ValueError('指定代理时至少选择一个来源')
        if value['proxy_mode']=='selected' and set(value['proxy_sources']) - known:
            raise ValueError('指定代理来源不存在，请重新选择')
        if not isinstance(value['preserve'], bool):
            raise ValueError('保留环境应为开关')
        if kind != 'app' and value['url'] != 'about:blank':
            parsed=urlparse(value['url'])
            if parsed.scheme not in {'http','https'} or not parsed.hostname or any(c.isspace() for c in value['url']):
                raise ValueError('启动网页应为完整的 http(s) 地址或 about:blank')
        if not isinstance(self,Store):return {'settings':value}  # 导入时复用纯校验
        with self.lock:
            if self.plan(kind,key)['revision']!=payload['revision']:raise Conflict('启动方案已更新，请重新载入')
            attachments=self.read('本机设置/方案附件.json',{'plans':{}})
            attachments.setdefault('plans',{})[kind+'-'+key]={'apk_path':value.pop('apk_path','')}
            attachments['revision']=attachments.get('revision',0)+1
            common=self.shared();common['revision']+=1
            common['plans'][kind+'-'+key]={'revision':payload['revision']+1,'settings':value}
            self._commit_config({self.paths.local/'方案附件.json':attachments,self.paths.shared:common})
            return self.plan(kind,key)

    def proxies(self):
        return self.read('配置/代理来源.json', {'revision':0, 'sources':[], 'defaults':[]})

    def save_proxies(self, payload):
        from .proxy import validate_sources
        self.revision(payload)
        saved=validate_sources(payload)
        with self.lock:
            previous=self.proxies()
            if previous['revision']!=payload['revision']:raise Conflict('代理来源已更新，请重新载入后再保存')
            removed={s['id'] for s in previous['sources']}-{s['id'] for s in saved['sources']}
            references=[]
            for name,plan in self.shared()['plans'].items():
                settings=plan.get('settings',{})
                if settings.get('proxy_mode','selected' if settings.get('proxy_sources') else 'inherit')=='selected' and removed.intersection(settings.get('proxy_sources',[])):
                    references.append('环境方案“'+name+'”')
            for row in self.unfinished_runs():
                if row['status'] in ('queued','creating') and removed.intersection(row.get('_plan',{}).get('proxy_sources',[])):
                    references.append('排队/创建窗口“'+row['name']+'”')
            if references:raise ValueError('不能删除仍被使用的代理来源：'+'、'.join(references[:10])+'。请先修改方案或取消对应队列')
            return self.write('配置/代理来源.json',saved,payload['revision'])

    def system(self):
        from .settings import DEFAULTS, REMOVED_FIELDS
        common={**deepcopy(DEFAULTS),**self.read('配置/系统.json',{})}
        for key in REMOVED_FIELDS:common.pop(key,None)
        services=self.read('配置/服务.json',{})
        common.update({k:v for k,v in services.items() if k in SHARED_SYSTEM_FIELDS})
        # 旧配置的 sync_auto=False 只是原默认值；只要设了中转站地址，
        # 本地 outbox 就应在断线恢复后继续发送。
        common['sync_auto']=bool(common['sync_url'])
        common['common_revision']=self.shared()['revision']
        local=self.read('状态/操作人.json',{})
        # 操作人属于这台软件，导入通用配置不会改变身份。
        common['alias']=local.get('alias','')
        common['sync_alias']=common['alias']
        common['operator_required']=not bool(common['alias'])
        return common

    def save_system(self,value,revision):
        from .settings import REMOVED_FIELDS
        local=deepcopy(value);alias=local.pop('alias','').strip()
        for key in REMOVED_FIELDS:local.pop(key,None)
        with self.lock:
            if revision!=self.system()['revision'] or local.pop('common_revision',0)!=self.shared()['revision']:
                raise Conflict('设置已更新，请重新载入后再保存')
            services={k:local.pop(k) for k in SHARED_SYSTEM_FIELDS if k in local}
            local.pop('sync_alias',None);local.pop('operator_required',None)
            common=self.shared();common['revision']+=1
            common['services']={**services,'revision':common['services'].get('revision',0)+1}
            local['revision']=revision+1
            operator={'alias':alias,'revision':self.read('状态/操作人.json',{}).get('revision',0)+1}
            self._commit_config({self.paths.shared:common,self.paths.local/'系统.json':local,self.paths.local/'操作人.json':operator})
        return self.system()

    def history(self,identity):
        self.get_run(identity)
        with self.connect() as db:
            rows=db.execute('SELECT updated,data FROM events WHERE run_id=? ORDER BY seq LIMIT 1000',(identity,)).fetchall()
        result=[];previous=None
        for at,raw in rows:
            item=json.loads(raw)
            cleanup=item.get('status') in ('closing','cleanup_failed') or bool(item.get('cleanup_operation'))
            error=item.get('cleanup_error','') if cleanup else item.get('error','')
            operation='cleanup' if cleanup else 'start'
            key=(item.get('stage'),item.get('status'),error,operation,item.get('cleanup_attempt_id') if cleanup else None)
            if key==previous:continue
            previous=key
            result.append({'time':time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(at)),'stage':key[0],'status':key[1],'error':key[2],
                           'operation':operation,'attempt_id':key[4]})
        return result

    def put_run(self, record, *, expected_updated=None, rebind=False):
        expected={record['id']:expected_updated} if expected_updated is not None else None
        return self.put_runs([record],expected_updated=expected,rebind=rebind)[0]

    def put_runs(self, records, *, expected_updated=None, rebind=False):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            at = time.time()
            saved = [{**record, 'updated':at} for record in records]
            for record in saved:
                if expected_updated is not None:
                    previous=db.execute('SELECT updated,data FROM runs WHERE id=?',(record['id'],)).fetchone()
                    if previous is None or previous[0]!=expected_updated.get(record['id']):
                        raise Conflict('窗口记录已变化，请重新打开编辑后保存')
                    old=json.loads(previous[1])
                    if record.get('kind') in ('phone','app') and record.get('planned_device')!=old.get('planned_device'):
                        target=record.get('planned_device')
                        if target in self.reserved_cloud_devices(record['provider'],exclude=record['id']):
                            raise Conflict('该云手机已被其他记录绑定，请重新选择')
                if rebind:
                    if expected_updated is None:raise ValueError('修改邮箱绑定必须提供窗口版本')
                    from .email_accounts import row_email, email_key, account_kind
                    email=row_email(record)
                    if email:
                        # 兼容旧账本：部分历史记录没有 email_bindings，也必须防重复。
                        for (payload,) in db.execute('SELECT data FROM runs WHERE id!=?',(record['id'],)):
                            other=json.loads(payload)
                            if account_kind(other['kind'])==account_kind(record['kind']) and row_email(other) and email_key(row_email(other))==email_key(email):
                                raise Conflict('邮箱已经绑定该类型的固定环境，请使用原记录')
                    db.execute('DELETE FROM email_bindings WHERE run_id=?',(record['id'],))
                if record.get('account_email'):
                    from .email_accounts import email_key, account_kind
                    key = email_key(record['account_email'])
                    kind = account_kind(record['kind'])
                    bound = db.execute('SELECT run_id FROM email_bindings WHERE email_key=? AND kind=?', (key,kind)).fetchone()
                    previous = db.execute('SELECT email_key,kind FROM email_bindings WHERE run_id=?', (record['id'],)).fetchone()
                    if (bound and bound[0] != record['id']) or (previous and previous != (key,kind)):
                        raise Conflict('邮箱已经绑定该类型的固定环境，请重新预览后打开原环境')
                    db.execute('INSERT OR IGNORE INTO email_bindings(email_key,kind,run_id,email) VALUES(?,?,?,?)', (key,kind,record['id'],record['account_email']))
                db.execute('INSERT OR IGNORE INTO run_order(id) VALUES(?)',(record['id'],))
                record['number']=db.execute('SELECT number FROM run_order WHERE id=?',(record['id'],)).fetchone()[0]
                record.setdefault('created_at',time.strftime('%Y-%m-%d %H:%M:%S'))
                data = json.dumps(record, ensure_ascii=False)
                db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?)', (record['id'],at,data))
                db.execute('INSERT INTO events(run_id,updated,data) VALUES(?,?,?)', (record['id'],at,data))
            # 增量队列有界；客户端落后于范围时重新取当前页。
            db.execute('DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-5000 FROM events)')
            return saved

    def find_run(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_run(self, run_id):
        row=self.find_run(run_id)
        if row is None:
            raise ValueError('未找到窗口记录')
        return row

    def email_runs_index(self):
        from .email_accounts import email_key, row_email
        with self.connect() as db:
            records = db.execute("SELECT data FROM runs WHERE json_extract(data,'$.kind') IN ('desktop','phone','app')").fetchall()
        index={}
        for (data,) in records:
            row=json.loads(data);email=row_email(row)
            if email:index.setdefault(email_key(email),[]).append(row)
        return index

    def reserved_cloud_devices(self, provider, *, exclude=''):
        """邮箱设备（含回收站）及未收尾任务不能分配给另一条资料。"""
        from .email_accounts import row_email
        with self.connect() as db:
            rows = db.execute("SELECT data FROM runs WHERE json_extract(data,'$.kind') IN ('phone','app') "
                              "AND json_extract(data,'$.provider')=?", (provider,)).fetchall()
        result = set()
        for (data,) in rows:
            row = json.loads(data)
            if row['id']==exclude:continue
            if row_email(row) or row['status'] in ('queued','creating','running','closing','cleanup_failed') or (
                    row.get('_cloud_owned') and not row.get('_cleanup_complete')):
                result.update(str(row[key]) for key in ('profile_id','planned_device') if row.get(key))
        return result

    def find_email_run(self, email, *, index=None, kind=None):
        """兼容旧账本；多个历史匹配必须由用户处理，不能任选一个环境。"""
        from .email_accounts import email_key, account_kind
        if index is None:index=self.email_runs_index()
        matches=index.get(email_key(email),[])
        if kind is not None:
            kind=account_kind(kind)
            matches=[row for row in matches if account_kind(row['kind'])==kind]
        if len(matches) > 1:
            numbers = '、'.join(str(row.get('number', row['id'][:8])) for row in matches[:5])
            raise ValueError(f'邮箱 {email} 对应多个历史记录（{numbers}），请先核实并处理多余记录')
        return matches[0] if matches else None

    def runs(self, limit=100, offset=0, query='', category='all', kind='all', descending=False):
        if category not in ('all','kept','trash') or kind not in ('all','desktop','phone','app'):
            raise ValueError('窗口筛选条件无效')
        clauses=["COALESCE(json_extract(data,'$.trashed'),0)="+('1' if category=='trash' else '0')]
        params=[]
        if category=='kept':
            clauses.append("json_extract(data,'$.keep')=1")
            clauses.append("json_extract(data,'$.kind')='desktop'")
            clauses.append("json_extract(data,'$.status')='stopped'")
        if kind=='phone':clauses.append("json_extract(data,'$.kind') IN ('phone','app')")
        elif kind!='all':clauses.append("json_extract(data,'$.kind')=?");params.append(kind)
        if query:
            fields=['name','account_email','input_data.email','launch','label','source','ip','os','error','address','note']
            clauses.append('('+' OR '.join("instr(lower(COALESCE(json_extract(data,'$."+f+"'),'')),lower(?))>0" for f in fields)+')')
            params.extend([query]*len(fields))
        where=' AND '.join(clauses)
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM runs WHERE '+where,params).fetchone()[0]
            cursor = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            rows=[]
            for data,number in db.execute('SELECT data,number FROM runs JOIN run_order USING(id) WHERE '+where+' ORDER BY number '+('DESC' if descending else 'ASC')+' LIMIT ? OFFSET ?',(*params,limit,offset)):
                item=json.loads(data);item['number']=number;rows.append(item)
            stats=self._run_stats(db)
        return {'rows':rows,'total':total,'cursor':cursor,'stats':stats}

    def kept_u_browsers(self):
        """Return this application's stopped U profiles that can be resumed."""
        with self.connect() as db:
            rows=db.execute("SELECT data FROM runs WHERE "
                "COALESCE(json_extract(data,'$.trashed'),0)=0 AND "
                "json_extract(data,'$.kind')='desktop' AND "
                "json_extract(data,'$.provider')='undetectable' AND "
                "json_extract(data,'$.keep')=1 AND "
                "json_extract(data,'$.status')='stopped' AND "
                "LENGTH(COALESCE(json_extract(data,'$.profile_id'),''))>0 "
                "ORDER BY updated DESC").fetchall()
        return [{'id':row['id'],'name':row.get('name',''),'launch':row.get('launch',''),
                 'profile_id':row['profile_id']} for (data,) in rows for row in [json.loads(data)]]

    def archive_run(self,identity,*,restore=False):
        with self.lock:
            row=self.get_run(identity)
            from .resource_state import unresolved_creation, UNCERTAIN_MESSAGE
            if not restore and unresolved_creation(row):raise ValueError(UNCERTAIN_MESSAGE)
            if row['status'] in ('running','creating','queued','closing','cleanup_failed'):
                raise ValueError('请先关闭窗口并完成清理，再移入回收站')
            row['trashed']=not restore
            if restore:row.pop('trashed_at',None)
            elif not row.get('trashed_at'):row['trashed_at']=time.time()
            return self.put_run(row)

    def expired_trash(self,cutoff):
        with self.connect() as db:
            return [value[0] for value in db.execute(
                "SELECT id FROM runs WHERE json_extract(data,'$.trashed')=1 "
                "AND COALESCE(json_extract(data,'$.account_email'),'')='' "
                "AND CAST(COALESCE(json_extract(data,'$.trashed_at'),0) AS REAL)>0 "
                "AND CAST(json_extract(data,'$.trashed_at') AS REAL)<=?",(cutoff,))]

    def delete_record(self,identity,*,local_only=False):
        with self.lock,self.connect() as db:
            row=self.find_run(identity)
            if row is None:return
            from .resource_state import unresolved_creation, UNCERTAIN_MESSAGE
            detach_octo=local_only and row['kind']=='desktop' and row['provider']=='octo'
            if row['status'] in ('queued','creating') or (row['status'] in ('running','closing') and not detach_octo):
                raise ValueError('任务仍在执行，未移除记录')
            if not local_only and unresolved_creation(row):raise ValueError(UNCERTAIN_MESSAGE)
            if not local_only and row['status']=='cleanup_failed':raise ValueError('清理未完成，未删除记录')
            if not local_only and not row.get('trashed'):raise ValueError('仅回收站里的记录可彻底删除')
            if not local_only and row.get('profile_id') and not row.get('_deleted') and row['kind']=='desktop':
                raise ValueError('该记录仍对应保留的厂商环境，请先恢复并处理该环境；未删除厂商环境或本地记录')
            db.execute('DELETE FROM runs WHERE id=?',(identity,))
            db.execute('DELETE FROM email_bindings WHERE run_id=?',(identity,))
            db.execute('INSERT INTO events(run_id,updated,data) VALUES(?,?,?)',(identity,time.time(),json.dumps({'id':identity,'deleted':True,'local_only':local_only})))

    def events(self, after):
        with self.connect() as db:
            oldest = db.execute('SELECT COALESCE(MIN(seq),0) FROM events').fetchone()[0]
            rows = db.execute('SELECT seq,data FROM events WHERE seq>? ORDER BY seq LIMIT 300', (after,)).fetchall()
            stats=self._run_stats(db)
        return {'reset':bool(after and after < oldest-1), 'cursor':rows[-1][0] if rows else after,
                'stats':stats,'events':[{'seq':seq,'row':json.loads(data)} for seq,data in rows]}

    @staticmethod
    def _run_stats(db):
        stats={key:0 for key in ('total','running','pending','failed')}
        for status,count in db.execute("SELECT json_extract(data,'$.status'),COUNT(*) FROM runs WHERE COALESCE(json_extract(data,'$.trashed'),0)=0 GROUP BY 1"):
            stats['total']+=count
            if status=='running':stats['running']+=count
            if status in ('queued','creating'):stats['pending']+=count
            if status in ('failed','cleanup_failed'):stats['failed']+=count
        return stats

    def log(self, message, **context):
        item = {'message':str(message), **context, 'time':time.strftime('%Y-%m-%d %H:%M:%S')}
        text = json.dumps(item,ensure_ascii=False)
        with self.lock:
            self._maintain_logs()
            with self.connect() as db:db.execute('INSERT INTO logs(at,data) VALUES(?,?)',(time.time(),text))
            with (self.paths.logs / ('运行-'+item['time'][:10]+'.jsonl')).open('a',encoding='utf-8') as stream:
                stream.write(text+'\n')

    def _maintain_logs(self):
        today=date.today()
        if self._log_housekeeping_day==today.isoformat():return
        cutoff=today-timedelta(days=14)  # 含今天的最近十五个自然日
        legacy=self.paths.logs/'运行.jsonl'
        if legacy.is_file():
            groups={}
            for line in legacy.read_text(encoding='utf-8').splitlines():
                if not line.strip():continue
                try:day=date.fromisoformat(str(json.loads(line).get('time',''))[:10])
                except (ValueError,TypeError,AttributeError):day=today
                if day>=cutoff:groups.setdefault(day.isoformat(),[]).append(line)
            for day,lines in groups.items():
                target=self.paths.logs/('运行-'+day+'.jsonl')
                temporary=target.with_suffix('.jsonl.tmp')
                with temporary.open('w',encoding='utf-8',newline='\n') as stream:
                    stream.write('\n'.join(lines)+'\n')
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,target)
            legacy.unlink()
        with self.connect() as db:
            db.execute('DELETE FROM logs WHERE at < ?',(time.mktime(cutoff.timetuple()),))
        for path in self.paths.logs.glob('运行-*.jsonl'):
            try:day=date.fromisoformat(path.name[3:13])
            except ValueError:continue
            if path.name=='运行-'+day.isoformat()+'.jsonl' and day<cutoff:path.unlink()
        self._log_housekeeping_day=today.isoformat()

    def logs(self):
        with self.lock:
            self._maintain_logs()
            with self.connect() as db:
                return [json.loads(row[0]) for row in db.execute('SELECT data FROM logs ORDER BY seq DESC LIMIT 500')]
