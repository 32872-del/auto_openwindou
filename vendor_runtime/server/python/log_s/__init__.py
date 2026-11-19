"""新版适配日志接口；不载入双端的节点和任务模块。"""
import logging


class Log:
    def __getattr__(self, level):
        def write(message, *args, **context):
            function = getattr(logging.getLogger('launcher.vendor'), level if level in ('debug','info','warning','error','exception') else 'info')
            function(str(message), *args, exc_info=context.get('exc_info'))
        return write


log = Log()


def safe_client_text(value, *args, **kwargs):
    return str(value)
