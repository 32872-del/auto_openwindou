"""供本地厂商启动器识别当前线程隔离桌面；不注入网页。"""
def active_task_input_protocol():
    from server.python.driver_s.win32_dp_module_s.win32_native import current_desktop_name
    return ('win32_dp_background','background') if current_desktop_name().startswith('AutoDrission_') else None
