"""Shared failure categories and safe, actionable messages."""

FAILURE_DETAILS = {
    "COOKIE_MISSING": ("缺少登录凭据", "请先在浏览器完成人工登录，再重新检查。"),
    "LOGIN_REQUIRED": ("登录已失效", "请在学校网站重新登录后再检查。"),
    "TLS_ERROR": ("TLS 连接失败", "请检查当前网络、代理和系统时间，再重新尝试。"),
    "TIMEOUT": ("请求超时", "请检查网络连接，稍后重试只读查询。"),
    "NETWORK_ERROR": ("网络连接异常", "请检查当前网络和代理设置。"),
    "RATE_LIMITED": ("服务端限流", "请停止连续请求，稍后再试。"),
    "SERVICE_UNAVAILABLE": ("服务端暂不可用", "请稍后再试。"),
    "INVALID_RESPONSE": ("接口响应格式异常", "可在学校网页核对服务状态。"),
    "REQUEST_ERROR": ("请求失败，原因未确认", "请核对网络状态和诊断日志。"),
    "SEARCH_TIMEOUT": ("在等待期限内未确认预约成功", "可在学校页面核对排期和已有预约。"),
    "UNKNOWN": ("失败原因未确认", "请查看出错时的终端提示和诊断日志。"),
}

FAILURE_STAGES = {
    "login": "登录检查",
    "hall": "读取预约大厅",
    "venue_rules": "读取场馆规则",
    "calendar": "读取排期",
    "selection": "准备预约计划",
    "reservation_history": "读取已有预约",
    "booking": "等待预约结果",
}

READ_STAGES = {
    "/site/user/get-name": "login",
    "/site/reservation/hall": "hall",
    "/site/reservation/resource-info": "venue_rules",
    "/site/reservation/resource-info-margin": "calendar",
}


def classify_failure(code=None, error_type=None):
    code = str(code) if type(code) in (str, int) else None
    if code in ("10042", "10043"):
        return "LOGIN_REQUIRED"
    if code == "429":
        return "RATE_LIMITED"
    if code == "408" or error_type in ("ConnectTimeoutError", "ReadTimeoutError", "TimeoutError"):
        return "TIMEOUT"
    if error_type in ("SSLError", "SSLEOFError", "SSLCertVerificationError"):
        return "TLS_ERROR"
    if error_type in ("ProtocolError", "ProxyError", "NameResolutionError", "NewConnectionError",
                      "ConnectionResetError", "ConnectionRefusedError", "RemoteDisconnected"):
        return "NETWORK_ERROR"
    if code == "SHAPE" or error_type == "JSONDecodeError":
        return "INVALID_RESPONSE"
    if code in ("500", "502", "503", "504"):
        return "SERVICE_UNAVAILABLE"
    if code == "EXC" or error_type:
        return "REQUEST_ERROR"
    return None
