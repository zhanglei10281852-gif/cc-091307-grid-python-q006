"""领域枚举与数据传输对象。

这些对象是只读快照：所有写操作都经由 :class:`~src.service.VolunteerService`
落库，快照字段与 ``audit_log`` 的 JSON 载荷使用同一套命名。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    VOLUNTEER = "volunteer"
    REVIEWER = "reviewer"   # 审核员：只能处理被授权组织
    ADMIN = "admin"         # 管理员：可冻结争议记录，但不能删除原始数据


class ActivityStatus(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"   # 取消的活动不计任何时长


class EntryKind(str, Enum):
    CHECKIN = "checkin"       # 现场签到/签退
    BACKFILL = "backfill"     # 补录（必须说明原因）
    IMPORT = "import"         # 批量导入（必须说明原因，幂等）


class EntryStatus(str, Enum):
    PENDING = "pending"       # 待复核：不计入汇总
    APPROVED = "approved"     # 复核通过
    REJECTED = "rejected"     # 复核驳回：不计入汇总


class DisputeStatus(str, Enum):
    OPEN = "open"
    UPHELD = "upheld"         # 异议成立：记录被驳回/修正
    REJECTED = "rejected"     # 异议不成立


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: Role
    org_id: str | None = None                 # 审核员所属组织
    authorized_org_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    org_id: str


@dataclass(frozen=True)
class Activity:
    id: str
    title: str
    org_id: str
    group_id: str
    start_ts: float
    end_ts: float
    status: ActivityStatus


@dataclass(frozen=True)
class ServiceEntry:
    """一条经审核的服务时段。

    ``import_key`` 非空时，同 (活动, 志愿者, import_key) 的重复导入不会累加。
    """
    id: str
    activity_id: str
    volunteer_id: str
    kind: EntryKind
    check_in: float
    check_out: float | None           # 签到后、签退前为 None
    raw_hours: float                  # 按实际打卡/补录时段算出的小时数
    effective_hours: float            # 与活动有效区间求交后的小时数（计算依据）
    status: EntryStatus
    reason: str | None                # 补录/导入/修改原因
    import_key: str | None
    frozen: bool
    created_by: str
    created_ts: float
    version: int                      # 修改版本号，每次修正 +1


@dataclass(frozen=True)
class Review:
    """复核链上的一个环节（提交/通过/驳回/修正），按 seq 排列。"""
    id: str
    entry_id: str
    action: str
    actor_id: str
    ts: float
    reason: str | None
    check_in: float | None
    check_out: float | None


@dataclass(frozen=True)
class Dispute:
    id: str
    volunteer_id: str
    entry_id: str | None
    activity_id: str | None
    reason: str
    status: DisputeStatus
    created_ts: float
    resolved_by: str | None
    resolved_ts: float | None
    resolution_note: str | None


@dataclass(frozen=True)
class HoursBreakdown:
    """时长汇总结果，``components`` 给出逐条计算依据。"""
    hours: float
    components: tuple[dict, ...]
    excluded: tuple[dict, ...]       # 被排除的记录及原因（取消/待审/冻结/驳回）


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: float
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict
