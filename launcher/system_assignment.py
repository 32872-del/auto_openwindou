"""粘贴邮箱的本次系统分配；预览结果原样进入任务，不改写环境方案。"""
from collections import Counter
from copy import deepcopy
import random

from .email_accounts import describe_accounts, email_key
from .selection import allocate, validate_weights


PROVIDERS = {'octo', 'undetectable'}
FAMILIES = {'win': ('Win/Win10', 'Win/Win11'),
            'mac': ('Mac/Intel', 'Mac/ARM'), 'android': ('Android',)}
LABELS = {'Win/Win10': 'Win10', 'Win/Win11': 'Win11',
          'Mac/Intel': 'Mac · Intel', 'Mac/ARM': 'Mac · Apple Silicon', 'Android': 'Android'}


def normalize(value):
    if not isinstance(value, dict) or set(value) - {'family', 'mode', 'win10', 'win11'}:
        raise ValueError('本次系统分配格式不正确')
    family, mode = value.get('family'), value.get('mode', 'random')
    if not isinstance(family, str) or family not in FAMILIES or mode not in ('random', 'weighted'):
        raise ValueError('请选择 win / mac / android，以及随机或比例分配')
    weights = {'Win/Win10': value.get('win10', 1), 'Win/Win11': value.get('win11', 1)}
    validate_weights(weights, require_positive=family == 'win' and mode == 'weighted')
    return dict(family=family, mode=mode, win10=weights['Win/Win10'], win11=weights['Win/Win11'])


def scope(kind, provider):
    if kind != 'desktop' or not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError('本次系统分配仅用于粘贴邮箱新建 O / U 浏览器环境')


def system_pool(selection, config):
    family = selection['family']
    if family == 'win':
        return {'Win/Win10': selection['win10'], 'Win/Win11': selection['win11']} if selection['mode'] == 'weighted' else dict.fromkeys(FAMILIES['win'], 1)
    if family == 'android':
        return {'Android': 1}
    from .providers import system_weights
    mac = {s: w for s, w in system_weights(config).items() if s in FAMILIES['mac'] and w > 0}
    return mac or {'Mac/Intel': 1}


def preview(store, items, *, kind, provider, selection, rng=None):
    scope(kind, provider)
    selection = normalize(selection)
    accounts = describe_accounts(store, items, kind=kind)
    new_items = [item for item, account in zip(items, accounts) if account['action'] == 'create']
    config = store.provider(provider)['settings']
    weights = system_pool(selection, config)
    rng = rng or random.SystemRandom()
    if not new_items:
        systems = []
    elif selection['family'] == 'win' and selection['mode'] == 'random':
        systems = rng.choices(list(weights), k=len(new_items))
    else:
        systems = allocate(weights, len(new_items), rng=rng)
    return {'accounts': accounts, 'selection': selection,
            'assignments': [{'email': item['email'], 'system': system} for item, system in zip(new_items, systems)],
            'counts': dict(Counter(systems)), 'labels': LABELS}


def validate_assignments(data, new_items):
    """提交时重新检查邮箱集合和配额，防止预览后绑定变化导致分配错位。"""
    if 'system_selection' not in data and 'system_assignments' not in data:
        return None
    scope(data['kind'], data['provider'])
    if not data.get('email_mode') or not isinstance(data.get('items'), list):
        raise ValueError('请先粘贴邮箱再设置本次系统分配')
    selection = normalize(data.get('system_selection'))
    assignments = data.get('system_assignments')
    if not isinstance(assignments, list) or len(assignments) > 100:
        raise ValueError('请先完成系统分配预览')
    result = {}
    for row in assignments:
        if not isinstance(row, dict) or set(row) != {'email', 'system'} or not isinstance(row['email'], str):
            raise ValueError('系统分配记录格式不正确')
        email, system = email_key(row['email']), row['system']
        if email in result or system not in FAMILIES[selection['family']]:
            raise ValueError('系统分配存在重复邮箱或不匹配的系统')
        result[email] = system
    if set(result) != {email_key(item['email']) for item in new_items}:
        raise ValueError('邮箱绑定状态或资料已变化，请重新打开粘贴预览后分配系统')
    if selection['family'] == 'win' and selection['mode'] == 'weighted' and result:
        weights = system_pool(selection, {})
        total, count = sum(weights.values()), len(result)
        counts = Counter(result.values())
        bases = {key: count * weight // total for key, weight in weights.items()}
        remainders = {key: count * weight % total for key, weight in weights.items()}
        awarded = []
        for key in weights:
            extra = counts[key] - bases[key]
            if extra not in (0, 1) or (extra and not remainders[key]):
                raise ValueError('系统数量与比例不一致，请重新分配')
            if extra: awarded.append(key)
        if any(remainders[a] < remainders[b] for a in awarded for b in weights if b not in awarded):
            raise ValueError('系统数量与比例不一致，请重新分配')
    return result


def row_config(config, provider, system):
    result = deepcopy(config)
    result.update(system_mode='fixed', fixed_system=system, systems=[system], system_weights={system: 1})
    # U 的自动生成接口在没有匹配系统时会自行换系统；明确分配时绑定真实配置 ID。
    if provider == 'undetectable':
        result['fp_source'] = 'pool'
    return result
