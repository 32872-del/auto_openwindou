"""先形成批次配额，再打乱；所有分配使用同一规则。"""
from collections import Counter
import random


def validate_weights(weights, *, allowed=None, require_positive=True):
    if not isinstance(weights, dict) or not weights:
        raise ValueError('请设置至少一个选项的比例')
    if allowed is not None and set(weights) - set(allowed):
        raise ValueError('比例包含不在选择范围内的选项')
    for key, value in weights.items():
        if not isinstance(key, str) or not key or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
            raise ValueError('比例需要 0～1000 的整数；0 表示不参与随机')
    if require_positive and not any(weights.values()):
        raise ValueError('至少一个选项的比例必须大于 0')
    return dict(weights)


def allocate(weights, count, *, mode='weighted', rng=None):
    """最大余数法；同余数随机打破平局，然后打乱整批。"""
    weights = validate_weights(weights)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('分配数量必须是正整数')
    rng = rng or random.SystemRandom()
    keys = [key for key, weight in weights.items() if weight > 0]
    if mode == 'fixed':
        if len(keys) != 1:
            raise ValueError('固定模式只允许一个选项')
        return keys * count
    if mode == 'sequential':
        return [keys[i % len(keys)] for i in range(count)]
    if mode != 'weighted':
        raise ValueError('分配模式不受支持')
    total = sum(weights.values())
    quotas = {key: count * weights[key] // total for key in keys}
    ties = keys[:]
    rng.shuffle(ties)
    ties.sort(key=lambda key: count * weights[key] % total, reverse=True)
    for key in ties[:count - sum(quotas.values())]:
        quotas[key] += 1
    result = [key for key in keys for _ in range(quotas[key])]
    rng.shuffle(result)
    return result


def summary(values):
    return dict(Counter(values))


def allocate_unique(weights, count, *, mode='weighted', rng=None):
    """有限设备池按权重抽样；一批中每台设备最多出现一次。"""
    weights = validate_weights(weights)
    available = {key: value for key, value in weights.items() if value > 0}
    if len(available) < count:
        raise ValueError(f'可分配云手机只有 {len(available)} 台，本次需要 {count} 台；请增加设备或减少资料行数')
    if mode == 'fixed' and len(available) != 1:
        raise ValueError('固定模式只允许一个选项')
    if mode not in ('fixed', 'sequential', 'weighted'):
        raise ValueError('分配模式不受支持')
    rng = rng or random.SystemRandom()
    result = []
    for _ in range(count):
        chosen = (rng.choices(list(available), weights=list(available.values()), k=1)[0]
                  if mode == 'weighted' else next(iter(available)))
        result.append(chosen)
        del available[chosen]
    return result
