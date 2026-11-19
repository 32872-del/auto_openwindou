"""按本次设备范围分配唯一的云手机，并解释候选数量。"""
from collections import Counter

from . import providers
from .selection import allocate, allocate_unique


def use_all_idle(config):
    """只修改本次新设备的配置快照，保留厂商方案和原邮箱绑定。"""
    config.update(device_selection='all_idle', device_ids=[], device_brands=[], device_models=[],
                  device_versions=[], device_weights={}, device_brand_weights={}, device_mode='sequential')


def allocate_devices(store, provider_key, config, count):
    from server.python.cloud_phone_s.framework.device_catalog import detail, matches
    from server.python.cloud_phone_s.framework.device_pool import DeviceLease

    reserved = store.reserved_cloud_devices(provider_key) | set(DeviceLease.owners(provider_key))
    selected_only = config['device_selection'] == 'selected'
    selected_ids = config['device_ids']
    mode = config.get('device_mode', 'weighted')
    brand_weights = {name.casefold(): weight for name, weight in config.get('device_brand_weights', {}).items()
                     if mode == 'weighted'}
    needs_details = bool(config.get('device_models') or config.get('device_brands') or brand_weights)
    scan = not selected_only or needs_details or bool(config.get('device_versions'))
    excluded = Counter()
    brands = {}
    if scan:
        provider = providers.build_cloud(provider_key, config, store.root)
        try:
            devices = provider.list_devices()
            scope = f'厂商返回 {len(devices)} 台'
            for device in devices:
                if selected_only and device.id not in selected_ids:
                    excluded['未勾选'] += 1
                elif device.id in reserved:
                    excluded['本地已绑定或占用'] += 1
                elif not selected_only and device.status != 'idle':
                    excluded['厂商非空闲'] += 1
                else:
                    if needs_details and provider_key == 'duoplus':
                        device = detail(provider, device.id, device=device)
                    if matches(device, config, provider_key=provider_key):
                        brands[device.id] = device.public()['brand']
                    else:
                        excluded['品牌、型号或系统不匹配'] += 1
        finally:
            provider.close()
    else:
        # 指定设备可在启动时等待空闲，此处只统计方案中允许的 ID。
        scope = f'方案勾选 {len(selected_ids)} 台'
        for device_id in selected_ids:
            if device_id in reserved:
                excluded['本地已绑定或占用'] += 1
            else:
                brands[device_id] = ''

    weights = {}
    for device_id, brand in brands.items():
        weight = config.get('device_weights', {}).get(device_id, 1)
        if weight > 0 and brand_weights.get(brand.casefold(), 1) > 0:
            weights[device_id] = weight
        else:
            excluded['设备或品牌比例为零'] += 1
    if len(weights) < count:
        reasons = '，'.join(f'{reason} {number} 台' for reason, number in excluded.items())
        message = f'可分配云手机只有 {len(weights)} 台，本次需要 {count} 台。{scope}'
        if reasons:
            message += '，' + reasons
        if selected_only or excluded['品牌、型号或系统不匹配'] or excluded['设备或品牌比例为零']:
            message += '。请检查厂商设备状态、已有邮箱绑定及环境方案；粘贴创建的新云手机默认从全部空闲设备分配'
        raise ValueError(message)

    if not brand_weights:
        return allocate_unique(weights, count, mode=mode)
    pools = {}
    for device_id, weight in weights.items():
        pools.setdefault(brands[device_id].casefold(), {})[device_id] = weight
    result = []
    for _ in range(count):
        brand = allocate({name: brand_weights.get(name, 1) for name in pools}, 1)[0]
        device_id = allocate_unique(pools[brand], 1)[0]
        result.append(device_id)
        del pools[brand][device_id]
        if not pools[brand]:
            del pools[brand]
    return result
