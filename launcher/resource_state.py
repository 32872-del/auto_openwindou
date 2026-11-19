"""区分未发送创建请求、已创建和结果未知；空环境 ID 不是不存在的证据。"""


def confirmed_absent(row):
    if row.get('profile_id') or row.get('pid') or row.get('_start_requested'):
        return False
    state = row.get('creation_state')
    if state is not None:
        return state in ('not_requested', 'rejected')
    # 旧版只在到达相应阶段时写入 _failed_stage；严格接受创建之前的失败。
    stage = row.get('_failed_stage', '')
    return (row.get('status') in ('failed', 'cancelled', 'cleanup_failed') and
            (stage in ('检查浏览器客户端', '探测并绑定浏览器客户端', '取得代理', '检测原代理') or
             stage.startswith(('检测城市代理 ', '检测州代理 ', '检测国家代理 '))))


def unresolved_creation(row):
    return (row.get('kind') == 'desktop' and not row.get('profile_id') and
            not row.get('_deleted') and not confirmed_absent(row))


UNCERTAIN_MESSAGE = '创建请求结果无法确认，远端可能已有环境；请核对厂商列表，或选择“仅移除本地记录”（远端环境仍保留）'
