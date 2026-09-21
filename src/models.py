"""领域枚举定义。

所有枚举都是 ``str`` 子类，直接以字符串形式落库，便于审计查询与人工核对。
"""

from __future__ import annotations

import enum


class Role(str, enum.Enum):
    """用户角色。"""

    VOLUNTEER = "VOLUNTEER"  # 志愿者：签到签退、查看本人明细、提出异议、申请补录/修改
    REVIEWER = "REVIEWER"    # 审核员：在授权组织范围内复核补录/修改、处理异议
    ADMIN = "ADMIN"          # 管理员：维护组织与账号、授权审核员、冻结/解冻争议记录


class ActivityStatus(str, enum.Enum):
    """活动状态。只有非取消状态的活动才计入时长。"""

    PLANNED = "PLANNED"      # 已发布未开始
    ONGOING = "ONGOING"      # 进行中
    COMPLETED = "COMPLETED"  # 已结束
    CANCELLED = "CANCELLED"  # 已取消：其下所有签到记录不计入时长


class RecordStatus(str, enum.Enum):
    """签到记录的复核状态。"""

    PENDING = "PENDING"      # 待复核（志愿者补录产生），不计入时长
    EFFECTIVE = "EFFECTIVE"  # 有效，计入时长
    REJECTED = "REJECTED"    # 复核驳回，不计入时长


class RecordSource(str, enum.Enum):
    """记录来源。"""

    ONSITE = "ONSITE"        # 现场签到签退
    BACKFILL = "BACKFILL"    # 事后补录（必须填写原因并复核）
    IMPORT = "IMPORT"        # 批量导入（由授权审核员/管理员执行，视同已复核）


class AmendmentStatus(str, enum.Enum):
    """修改申请的状态。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ObjectionStatus(str, enum.Enum):
    """异议的处理状态。"""

    OPEN = "OPEN"                        # 待处理
    RESOLVED_UPHELD = "RESOLVED_UPHELD"  # 异议成立
    RESOLVED_REJECTED = "RESOLVED_REJECTED"  # 异议驳回


# 时长汇总中“不计入”的原因码，随计算依据一起返回，保证口径可解释。
EXCLUDE_RECORD_REJECTED = "record_rejected"        # 记录被复核驳回
EXCLUDE_PENDING_REVIEW = "pending_review"          # 补录待复核
EXCLUDE_FROZEN = "frozen"                          # 记录被管理员冻结
EXCLUDE_ACTIVITY_CANCELLED = "activity_cancelled"  # 活动已取消
EXCLUDE_INCOMPLETE = "incomplete"                  # 只有签到没有签退
