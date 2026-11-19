"""云手机厂商注册入口；厂商实现相互不导入。"""
from ..framework.contracts import CloudPhoneError

def create_provider(key, config, config_root):
    if key=='duoplus':
        from .duoplus.adapter import DuoPlusProvider
        return DuoPlusProvider(config,config_root)
    if key=='geelark':
        from .geelark.adapter import GeeLarkProvider
        return GeeLarkProvider(config,config_root)
    raise CloudPhoneError('没有注册云手机厂商：'+str(key))
