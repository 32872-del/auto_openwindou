"""厂商各自声明配置字段；界面与校验使用同一份定义。"""
from copy import deepcopy
from urllib.parse import urlparse


def field(key, label, default='', *, kind='text', group='连接', hint='', options=None, minimum=0, maximum=65535):
    return dict(key=key, label=label, default=default, type=kind, group=group, hint=hint,
                options=options or [], minimum=minimum, maximum=maximum)


def connection(port, *, token=False, remote=''):
    fields = [field('local_api_url', '本机 API 地址', f'http://127.0.0.1:{port}' if port else '', kind='url'),
              field('manager_path', '管理器程序路径', hint='选择该厂商主程序；隔离启动前会重新核对文件和运行进程。')]
    if token:
        fields += [field('api_token', 'API 密钥', kind='secret')]
    if remote:
        fields += [field('remote_api_url', '厂商服务地址', remote, kind='url')]
    fields += [field('request_timeout', '接口超时（秒）', 15, kind='number', minimum=1, maximum=120)]
    return fields


SYSTEMS = [('Win/Win10', 'Windows 10'), ('Win/Win11', 'Windows 11'), ('Mac/Intel', 'macOS · Intel'),
           ('Mac/ARM', 'macOS · Apple Silicon'), ('Android', 'Android 模拟环境')]
PAIRS = [('vendor', '由厂商生成'), ('balanced', '合理组合随机'), ('4/8', '4 核 / 8 GB'), ('8/16', '8 核 / 16 GB'), ('8/32', '8 核 / 32 GB'), ('12/32', '12 核 / 32 GB'), ('16/64', '16 核 / 64 GB')]


def system_fields(arm=False):
    options=SYSTEMS if arm else [s for s in SYSTEMS if s[0] != 'Mac/ARM']
    return [field('system_mode','系统分配','weighted',kind='select',group='环境',options=[('fixed','固定系统'),('weighted','按比例打乱'),('sequential','按顺序轮转')]),
            field('fixed_system','固定使用','Win/Win11',kind='select',group='环境',options=options),
            field('systems', '系统范围', ['Win/Win11'], kind='multi', group='环境', options=options,
                  hint='按比例或顺序模式使用所选范围；旧配置没有比例时等权。'),
            field('system_weights','系统比例',{},kind='weights',group='环境',options=options,
                  hint='仅勾选系统参与。0 不参与；整批按最大余数分配数量后打乱。')]


def android_fields():
    return [field('android_mode','Android 型号选择','all',kind='select',group='Android',options=[('all','全部型号随机'),('brands','按品牌比例'),('models','指定多个型号随机'),('fixed','指定型号')]),
            field('android_brands','品牌范围',[],kind='list',group='Android',hint='使用厂商设备库中的品牌名称，可在下方机型库选择。'),
            field('android_brand_weights','品牌比例',{},kind='weights',group='Android'),
            field('android_models','型号范围',[],kind='list',group='Android',hint='从下方型号库加入多个型号；只在所选范围随机。'),
            field('android_versions','版本范围',[],kind='list',group='Android',hint='留空不限；按各型号实际支持版本匹配，不会把不支持的版本传给厂商。'),
            field('android_model','指定型号','',kind='text',group='Android',hint='从厂商设备库选择；不是自填任意型号。'),
            field('android_version','Android 版本','',group='Android',hint='留空在该型号支持的版本内随机；指定时需与机型库一致。')]


def hardware(*, resolution=True, noise=True, default='vendor', versions=True):
    fields = [field('hardware', 'CPU 与内存', default, kind='select', options=PAIRS, group='环境',
                    hint='按整组选择；显卡交给厂商生成，不单独拼接型号。')]
    if versions:
        fields += [field('chrome_versions', '内核版本池', [], kind='list', group='环境', hint='逗号或换行分隔；留空遵循本厂商默认规则。')]
    if resolution:
        fields += [field('resolutions', '分辨率池', [], kind='list', group='环境', hint='例如 1920x1080，留空由厂商生成。')]
    if noise:
        fields += [field('noise', '开启指纹噪声', True, kind='bool', group='环境')]
    return fields


def provider(key, name, fields, note, *, cloud=False, capabilities=None):
    return dict(key=key, name=name, family='cloud' if cloud else 'desktop', fields=fields,
                note=note, capabilities=capabilities or [], verification='待新版真机验证')


PROVIDERS = [
    provider('octo', 'O 浏览器', connection(58888, token=True, remote='https://app.octobrowser.net/api/v2/automation') + system_fields(True) + [
        field('group', '环境标签', '', group='环境')] + android_fields(),
        'O 的系统、机型与硬件由厂商配套生成；API 密钥同时用于创建和删除。'),
    provider('undetectable', 'Undetectable', connection(25325) + system_fields(True) + [
        field('fp_source', '指纹来源', 'pool', kind='select', group='环境', options=[('pool','账号可用指纹库'),('generated','厂商生成')]),
        field('config_ids','指定配置库范围',[],kind='list',group='环境',hint='通过下方配置库选择；一个固定使用，多个在本次系统匹配的配置中随机。留空使用整个可用库。')
    ] + hardware(versions=False, noise=False), '优先使用匹配系统的指纹库配置；不把内核版本作为独立选项。'),
    provider('morelogin', 'MoreLogin', connection(40000) + system_fields() + hardware(resolution=False, default='balanced') + [
        field('group', '分组 ID', '', group='环境', hint='留空不指定分组；ID 按字符串保存。')],
        '内核池留空时，从接口返回的最新三个可用版本随机选择；CPU 和内存按整组生成。'),
    provider('adspower', 'AdsPower', connection(50325, token=True) + system_fields() + hardware() + [
        field('group', '分组 ID', '0', group='环境')], '需要账号开放 Local API 权限；接口失败保留厂商原文。'),
    provider('bitbrowser', 'BitBrowser', connection(54345) + system_fields() + hardware() + [
        field('group', '分组 ID', '', group='环境')], '浏览器内核、系统与噪声参数使用比特接口的对应字段。'),
    provider('ruyi', '如意浏览器', [
        field('browser_path', 'Firefox 内核路径'),
        field('fingerprint_directory', '指纹模板目录', './本机设置/如意指纹'),
        field('core_root', '内核目录', './文件缓存/ruyi-core'),
        field('fingerprint_mode', '指纹模板选择', 'random', kind='select', group='环境', options=[('random','目录内随机'),('fixed','指定模板')]),
        field('fingerprint_template', '指定模板文件', '', group='环境', hint='指定模板时必填；系统、分辨率、显卡从同一份模板读取。'),
        field('auto_download_core', '缺少内核时允许下载', False, kind='bool', group='高级'),
        field('download_base_url', '内核下载地址', '', kind='url', group='高级')],
        '本地 Firefox 驱动；没有本机 API 地址。模板决定整套指纹。'),
    provider('helix', 'Helix Browser', connection(57777) + [
        field('core_family', '内核类型', 'chromium', kind='select', group='环境', options=[('chromium','Chromium')]),
        field('core_version', '指定内核版本', '', group='环境'),
        field('humanize', '厂商拟人参数', True, kind='bool', group='环境'),
        field('auto_download_core', '允许下载内核', False, kind='bool', group='高级')],
        '先识别本机服务版本；使用该版本提供的环境模板。'),
    provider('kameleo', 'Kameleo', connection(5050) + system_fields() + [
        field('browser_product', '浏览器产品', 'chrome', kind='select', group='环境', options=[('chrome','Chrome'),('edge','Edge')]),
        field('chrome_versions', '内核版本池', [], kind='list', group='环境'),
        field('fingerprint_ids', '指定指纹 ID 池', [], kind='list', group='环境', hint='留空从厂商真实指纹库按系统选择。')],
        '使用 Chroma 的 Chrome / Edge 指纹库，硬件参数由选中的完整指纹提供。'),
    provider('hidemium', 'Hidemium', connection(0) + system_fields() + hardware() + [
        field('is_local', '使用本地环境', True, kind='bool', group='连接'),
        field('language', '语言', '', group='环境'), field('group', '文件夹名称', '', group='环境')],
        '需要先在 Hidemium 激活本机设备。真实创建验证按此前要求跳过。'),
    provider('vmlogin', 'VMLogin', connection(0, token=True, remote='https://api.vmlogin.com/v1') + system_fields() + hardware(),
        '云端 API 负责创建和删除，本机 API 负责启动和关闭；两者分别配置。'),
    provider('hub', 'Hub 浏览器', connection(6873) + [
        field('authorization', 'Authorization', kind='secret'), field('app_id', 'App ID'),
        field('app_secret', 'App Secret', kind='secret'), field('group_code', '组授权码', kind='secret'),
        field('core_directory', '内核目录', '', group='高级')
    ] + system_fields() + hardware(resolution=False, noise=False), '支持授权令牌或 App ID / Secret / 组授权码登录。'),
    provider('chrome', '本地 Chrome', [field('browser_path', 'Chrome 程序路径', hint='留空自动探测本机 Chrome。')],
        '使用本机真实系统和硬件；独立用户目录，不虚构指纹参数。'),
]


def cloud_fields(geelark=False):
    fields = [field('api_url', 'API 地址', 'https://openapi.geelark.cn/open/v1' if geelark else 'https://openapi.duoplus.net', kind='url'),
              field('api_key', 'API Token / Key' if geelark else 'API Key', kind='secret')]
    if geelark:
        fields += [field('auth_mode', '认证方式', 'token', kind='select', options=[('token','API Token'),('key','API Key + App ID')]),
                   field('app_id', 'App ID', hint='API Key 认证时必填；Token 模式留空。')]
    fields += [field('device_selection', '设备范围', 'selected', kind='select', group='设备', options=[('selected','仅勾选设备'),('all_idle','全部空闲设备')]),
               field('device_brands','允许的手机品牌',[],kind='list',group='设备',hint='从型号目录选择；留空不限。只挑选已拥有的匹配设备，不会自动重置。'),
               field('device_models','允许的手机型号',[],kind='list',group='设备',hint='从已有设备详情或型号库选择，可选多个。'),
               field('device_versions','允许的系统版本',[],kind='list',group='设备',hint='从已有设备读取完整系统名称，留空不限。'),
               field('device_brand_weights','品牌比例',{},kind='weights',group='设备',hint='先按品牌比例分配，再按品牌内设备比例分配。0 不参与。'),
               field('device_ids', '指定设备 ID', [], kind='list', group='设备', hint='扫描后勾选；正在运行或被占用的设备不会分配。'),
               field('device_mode','设备分配','weighted',kind='select',group='设备',options=[('fixed','指定一台'),('weighted','按设备比例打乱'),('sequential','顺序轮转')]),
               field('device_weights','设备比例',{},kind='weights',group='设备',hint='使用设备 ID，未填写的选中设备按 1；0 不参与随机。'),
               field('max_concurrent', '最多同时运行', 1, kind='number', group='设备', minimum=1, maximum=32),
               field('idle_wait', '无空闲设备时等待（秒）', 60, kind='number', group='设备', minimum=0, maximum=3600),
               field('rename_pattern', '本次设备名称', '{project}_{record}_{timestamp}', group='设备', hint='支持 project、record、timestamp、provider、device 占位符。'),
               field('close_after_run', '关闭窗口后停止本次设备', True, kind='bool', group='兼容',hint='新版云手机固定关机；旧配置的关闭开关不再影响关机。')]
    fields.append(field('network_mode', '联网模式', 'keep', kind='select', group='设备',
                        options=[('keep', '沿用设备设置'), ('wifi', 'Wi-Fi'), ('mobile', '移动网络')]))
    if not geelark:
        fields += [field('ip_environment', '按代理 IP 调整定位与语言', False, kind='bool', group='设备',
                         hint='指定了目标地区且分配任务代理时自动启用。勾选后，其他任务也会按代理 IP 调整。')]
    return fields


PROVIDERS += [provider('duoplus', 'DuoPlus', cloud_fields(), '使用已有空闲设备，通过厂商接口启动和关机；不弹出控制画面，不购买或重置设备。', cloud=True),
              provider('geelark', 'GeeLark', cloud_fields(True), '使用已有空闲设备；支持 Token 或签名认证。不显示本厂商未支持的“按 IP 重置环境”选项。', cloud=True)]
BY_KEY = {p['key']: p for p in PROVIDERS}
BY_KEY['hub']['fields'] += android_fields()
for item in BY_KEY['morelogin']['fields']:
    if item['key']=='hardware':item['options']=[choice for choice in PAIRS if choice[0]!='vendor']

for key in ('undetectable','morelogin'):
    for item in BY_KEY[key]['fields']:
        if item['key']=='hardware':item['options']=([('vendor','由厂商生成')] if key=='undetectable' else [])+[('balanced','合理组合随机'),('4/8','4 核 / 8 GB'),('8/8','8 核 / 8 GB')]


def defaults(key):
    return {f['key']: deepcopy(f['default']) for f in BY_KEY[key]['fields']}


def validate(key, raw):
    if key not in BY_KEY:
        raise ValueError('未注册的厂商：' + key)
    values = defaults(key)
    known = set(values)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{BY_KEY[key]['name']} 不支持这些字段：{', '.join(sorted(unknown))}")
    for f in BY_KEY[key]['fields']:
        name, kind = f['key'], f['type']
        value = raw.get(name, values[name])
        if kind == 'bool':
            if not isinstance(value, bool):
                raise ValueError(f"{f['label']} 必须是开关值")
        elif kind == 'number':
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not f['minimum'] <= value <= f['maximum']:
                raise ValueError(f"{f['label']} 应在 {f['minimum']}～{f['maximum']} 之间")
            value = int(value)
        elif kind == 'weights':
            from .selection import validate_weights
            if value == {}:value={}
            else:value=validate_weights(value,allowed=[o[0] for o in f['options']] or None,require_positive=False)
        elif kind in ('list', 'multi'):
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise ValueError(f"{f['label']} 必须是文本列表")
            value = list(dict.fromkeys(x.strip() for x in value if x.strip()))
            if kind == 'multi' and (not value or set(value) - {o[0] for o in f['options']}):
                raise ValueError(f"{f['label']} 至少选择一个支持的选项")
        else:
            if not isinstance(value, str):
                raise ValueError(f"{f['label']} 必须是文本")
            value = value if kind == 'secret' else value.strip()
            if kind == 'select' and value not in {o[0] for o in f['options']}:
                raise ValueError(f"{f['label']} 的选项不受支持")
            if kind == 'url' and value:
                parsed = urlparse(value)
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError(f"{f['label']} 需要 http:// 或 https:// 地址，认证信息填在密钥字段")
                value = value.rstrip('/')
        values[name] = value
    if key == 'ruyi' and values['fingerprint_mode'] == 'fixed' and not values['fingerprint_template']:
        raise ValueError('如意已选择指定模板，请填写模板文件')
    if key == 'geelark' and values['auth_mode'] == 'key' and not values['app_id']:
        raise ValueError('GeeLark 的 API Key 认证需要 App ID')
    if 'chrome_versions' in values and any(not x.isdigit() or int(x) < 1 for x in values['chrome_versions']):
        raise ValueError('内核版本池使用正整数主版本号，例如 146, 147')
    if 'resolutions' in values:
        import re
        if any(not re.fullmatch(r'[1-9]\d{2,4}x[1-9]\d{2,4}', x) for x in values['resolutions']):
            raise ValueError('分辨率使用宽x高，例如 1920x1080')
    if 'systems' in values and values['system_mode']!='fixed':
        if not any(values['system_weights'].get(k,1) for k in values['systems']):raise ValueError('所选系统的比例不能全部为 0')
    if values.get('android_mode')=='fixed' and not values['android_model']:raise ValueError('指定型号模式需要选择 Android 型号')
    if values.get('android_mode')=='brands' and not values['android_brands']:raise ValueError('按品牌选择需要至少一个品牌')
    if values.get('android_mode')=='models' and not values['android_models']:raise ValueError('多个型号随机需要选择至少一个型号')
    if BY_KEY[key]['family']=='cloud':
        values['close_after_run']=True
        if values['device_mode']=='fixed' and (values['device_selection']!='selected' or len(values['device_ids'])!=1):
            raise ValueError('固定设备模式需要仅选择一台设备')
    return values
