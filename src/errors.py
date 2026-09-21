"""志愿服务登记系统的异常体系。

所有业务异常都继承自 :class:`VolunteerServiceError`，调用方可以只捕获基类，
也可以按具体类型区分处理。
"""


class VolunteerServiceError(Exception):
    """业务异常的基类。"""


class NotFoundError(VolunteerServiceError):
    """请求的实体不存在。"""


class PermissionDeniedError(VolunteerServiceError):
    """当前操作者没有执行该操作的权限（角色或组织授权范围不符）。"""


class ValidationError(VolunteerServiceError):
    """输入参数不合法（时间倒置、缺少必填原因等）。"""


class StateError(VolunteerServiceError):
    """实体当前状态不允许该操作（如冻结记录被修改、重复复核等）。"""
