"""邮箱固定环境的匹配规则与只读状态判断。"""
from .table_import import is_email


def email_key(value):
    # 本软件按大小写不敏感匹配；不合并点号、+后缀或不同邮箱域名。
    return value.strip().casefold()


def account_kind(kind):
    if kind not in ('desktop', 'phone', 'app'):
        raise ValueError('请选择电脑浏览器或云手机')
    return 'phone' if kind == 'app' else kind


def row_email(row):
    if row.get('kind') not in ('desktop', 'phone', 'app'):
        return ''
    value = row.get('account_email') or row.get('input_data', {}).get('email') or row.get('name', '')
    return value.strip() if isinstance(value, str) and is_email(value) else ''


def account_action(row, owner):
    if row is None:
        return {'action': 'create', 'label': '首次创建固定环境'}
    from .resource_state import unresolved_creation, UNCERTAIN_MESSAGE
    if row.get('status') not in ('queued','creating','running') and unresolved_creation(row):
        raise ValueError(UNCERTAIN_MESSAGE)
    if row.get('trashed'):
        raise ValueError('邮箱环境在回收站，请先恢复该记录')
    if row.get('_owner') != owner:
        raise ValueError('邮箱环境归属无法确认，请先核实原电脑或软件实例')
    status = row['status']
    if status in ('running', 'queued', 'creating'):
        return {'action': 'existing', 'label': '已打开，不重复创建' if status == 'running' else '已在启动队列'}
    if status in ('closing', 'cleanup_failed') or (status == 'failed' and row.get('profile_id') and not row.get('_cleanup_complete')):
        raise ValueError('邮箱环境正在关闭或清理未完成，请处理后再打开')
    if row.get('_deleted') or status == 'closed':
        raise ValueError('原邮箱环境已删除，不能恢复；请核实并处理原记录后再创建')
    if row.get('kind') in ('phone', 'app'):
        if status not in ('stopped', 'failed', 'cancelled'):
            raise ValueError('原邮箱云手机当前不可重新启动，请先处理原记录')
        if not (row.get('profile_id') or row.get('planned_device')):
            raise ValueError('原邮箱记录缺少云手机设备编号，请先核实原记录')
        return {'action': 'resume', 'label': '打开原绑定云手机'}
    if row.get('profile_id'):
        if status not in ('stopped', 'failed', 'cancelled') or not row.get('keep'):
            raise ValueError('原邮箱环境当前不可重新启动，请先处理原记录')
        if '_proxy' not in row:
            raise ValueError('原环境缺少代理快照，无法保证沿用原配置')
        return {'action': 'resume', 'label': '打开原固定环境'}
    if status in ('failed', 'cancelled'):
        return {'action': 'retry', 'label': '首次创建未完成，重试原记录'}
    raise ValueError('原邮箱记录缺少环境编号，请先核实原记录')


def describe_accounts(store, items, *, kind=None):
    if kind is not None:kind = account_kind(kind)
    result = []
    owner = store.resource_owner()
    index = store.email_runs_index()
    for item in items:
        email = item.get('email', '')
        info = {'email': email}
        try:
            if not is_email(email):
                raise ValueError('请填写有效邮箱')
            matches = index.get(email_key(email), [])
            if kind is None and len({account_kind(r['kind']) for r in matches}) > 1:
                info.update(action='select', label='已有电脑浏览器和云手机，请选择类型')
                result.append(info)
                continue
            row = store.find_email_run(email, index=index, kind=kind)
            info.update(account_action(row, owner))
            if row:
                info.update(run_id=row['id'], kind=row['kind'], provider=row['provider'], label_provider=row.get('label', row['provider']),
                            planned_system=row.get('planned_system',''),
                            location=row.get('location',{}), address=row.get('address',''), input_data=row.get('input_data',{}))
        except ValueError as exc:
            info.update(action='blocked', label=str(exc), error=str(exc))
        result.append(info)
    counts={}
    for item in items:
        key=email_key(item.get('email',''))
        counts[key]=counts.get(key,0)+1
    for info in result:
        if info['email'] and counts[email_key(info['email'])]>1:
            info.update(action='blocked',label='本批邮箱重复，请保留一条',error='本批邮箱重复，请保留一条')
    return result
