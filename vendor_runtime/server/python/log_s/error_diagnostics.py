def diagnostic_text(value, *args, **kwargs):
    return str(value)


def diagnostic_value(value, *args, **kwargs):
    if isinstance(value, dict):
        return {str(k):diagnostic_value(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [diagnostic_value(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):
        return value
    return str(value)
