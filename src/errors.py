"""领域层异常类型。"""


class VolunteerSystemError(Exception):
    """所有业务异常的基类。"""


class ValidationError(VolunteerSystemError):
    """入参不满足业务约束（如缺少原因、时间非法）。"""


class AuthzError(VolunteerSystemError):
    """当前角色无权执行该操作（如跨组织审核、非本人查他人明细）。"""


class NotFoundError(VolunteerSystemError):
    """目标对象不存在。"""


class StateError(VolunteerSystemError):
    """对象当前状态不允许该操作（如重复复核、冻结记录被修改）。"""
