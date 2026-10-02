"""展后供需对接：可追溯需求/产品版本、授权匹配、线索合并、会谈纪要与阶段份额。

设计要点：
- 需求(match_demands)与产品(match_offerings)沿用平台版本化实体链，回应与机会必须绑定
  明确的版本号；修订追加新版本，历史版本永不被覆盖。
- 匹配只投影双方通过 disclosures 实体授权（published 且未过期、受众匹配）的字段。
- 多渠道线索先经 Inbox 去重/冲突，再按指纹合并；每个来源逐行保留，字段冲突待主管裁决。
- 会谈预约在单事务内同时占用场地与人员两类资源；纪要版本化，由双方分别确认，
  一方改变范围时旧版保留并形成待双方确认的新版本。
- 机会按份额推进样品、报价、框架协议、合同、退出各阶段，份额之和守恒；
  部分成交只关闭对应份额，剩余机会保持开放。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, Sequence

from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .repository import EntityRepository
from .security import AccessContext
from .timeutil import canonical_instant, parse_instant


DEMAND_TYPE = "match_demands"
OFFERING_TYPE = "match_offerings"
DISCLOSURE_TYPE = "disclosures"

DEMAND_FIELDS = ("buyer_org", "category", "quantity_min", "quantity_max", "unit",
                 "delivery_regions", "certifications", "window_start", "window_end",
                 "contact_id", "notes",)
OFFERING_FIELDS = ("supplier_org", "category", "product_name", "product_version",
                   "certifications", "delivery_regions", "moq", "capacity_per_month",
                   "unit", "contact_id", "notes",)

# 匹配时允许在双方之间投影的字段，必须与披露授权 fields 中的字段名一致。
MATCHABLE_DEMAND_FIELDS = ("category", "quantity_min", "quantity_max", "unit",
                           "delivery_regions", "certifications", "window_start", "window_end")
MATCHABLE_OFFERING_FIELDS = ("category", "unit", "delivery_regions", "certifications",
                             "moq", "capacity_per_month", "product_name", "product_version")
# 回应需求前双方至少要授权的字段。
RESPONSE_REQUIRED_FIELDS = ("category", "delivery_regions", "certifications")

STAGES = ("sample", "quote", "framework", "contract", "exited")
STAGE_ORDER = {"sample": 0, "quote": 1, "framework": 2, "contract": 3}
OPPORTUNITY_STATUSES = ("open", "partial", "contracted", "closed")

_PARTY_BUYER = "buyer"
_PARTY_SELLER = "seller"


def _clean_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


def _clean_str_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{label}必须是非空列表")
    result = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(result) != len(value):
        raise ValidationError(f"{label}只能包含非空字符串")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{label}必须是正整数")
    return value


def _validate_demand(values: Mapping[str, object]) -> dict:
    payload = {
        "buyer_org": _clean_str(values.get("buyer_org"), "采购机构"),
        "category": _clean_str(values.get("category"), "品类"),
        "quantity_min": _positive_int(values.get("quantity_min"), "数量下限"),
        "quantity_max": _positive_int(values.get("quantity_max"), "数量上限"),
        "unit": _clean_str(values.get("unit"), "单位"),
        "delivery_regions": _clean_str_list(values.get("delivery_regions"), "交付地区"),
        "certifications": _clean_str_list(values.get("certifications"), "认证要求"),
        "window_start": canonical_instant(values["window_start"]) if values.get("window_start") else "",
        "window_end": canonical_instant(values["window_end"]) if values.get("window_end") else "",
        "contact_id": _clean_str(values.get("contact_id"), "联系人"),
        "notes": str(values.get("notes", "")).strip(),
    }
    if payload["quantity_min"] > payload["quantity_max"]:
        raise ValidationError("数量区间下限不能大于上限")
    if payload["window_start"] and payload["window_end"] and parse_instant(payload["window_start"]) >= parse_instant(payload["window_end"]):
        raise ValidationError("时间窗口开始必须早于结束")
    return payload


def _validate_offering(values: Mapping[str, object]) -> dict:
    return {
        "supplier_org": _clean_str(values.get("supplier_org"), "参展机构"),
        "category": _clean_str(values.get("category"), "品类"),
        "product_name": _clean_str(values.get("product_name"), "产品名称"),
        "product_version": _clean_str(values.get("product_version"), "产品版本"),
        "certifications": _clean_str_list(values.get("certifications"), "认证"),
        "delivery_regions": _clean_str_list(values.get("delivery_regions"), "交付地区"),
        "moq": _positive_int(values.get("moq"), "起订量"),
        "capacity_per_month": _positive_int(values.get("capacity_per_month"), "月产能"),
        "unit": _clean_str(values.get("unit"), "单位"),
        "contact_id": _clean_str(values.get("contact_id"), "联系人"),
        "notes": str(values.get("notes", "")).strip(),
    }


@dataclass(frozen=True)
class MatchmakingService:
    """展后供需对接的应用服务，复用平台仓库、收件箱、预约、任务队列与发件箱。"""

    repository: EntityRepository
    inbox: object
    reservations: object
    jobs: object
    outbox: object

    # ------------------------------------------------------------------ #
    # 需求版本
    # ------------------------------------------------------------------ #
    def register_demand(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:match-demands")
        payload = _validate_demand(values)
        payload["state"] = "draft"
        return self.repository.create(DEMAND_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def revise_demand(self, context: AccessContext, demand_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        """采购商修改品类/数量区间/交付地区/认证/时间窗口：旧内容进入版本链，形成可追溯新版本。"""
        context.require("write:match-demands")
        current = self.repository.get(DEMAND_TYPE, demand_id)
        if current["state"] == "closed":
            raise ConflictError("需求已关闭，请重新登记")
        unknown = set(values) - set(DEMAND_FIELDS)
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        merged = {key: current[key] for key in DEMAND_FIELDS}
        merged.update(values)
        return self.repository.update(DEMAND_TYPE, demand_id, _validate_demand(merged),
                                     actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def transition_demand(self, context: AccessContext, demand_id: str, target: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        context.require("transition:match-demands")
        if not reason.strip():
            raise ValidationError("状态变更必须说明原因")
        current = self.repository.get(DEMAND_TYPE, demand_id)
        allowed = {"draft": {"open"}, "open": {"on_hold", "closed"}, "on_hold": {"open", "closed"}, "closed": set()}
        if target not in allowed.get(current["state"], set()):
            raise ConflictError(f"需求不允许从 {current['state']} 转到 {target}")
        return self.repository.update(DEMAND_TYPE, demand_id, {"state": target, "transition_reason": reason.strip()},
                                     actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def demand_history(self, context: AccessContext, demand_id: str) -> list[dict]:
        context.require("history:match-demands")
        rows = self.repository.history(DEMAND_TYPE, demand_id)
        if not rows:
            raise NotFoundError("需求不存在")
        return rows

    # ------------------------------------------------------------------ #
    # 产品版本（参展商只能以明确版本回应）
    # ------------------------------------------------------------------ #
    def register_offering(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:match-offerings")
        payload = _validate_offering(values)
        payload["state"] = "draft"
        return self.repository.create(OFFERING_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def revise_offering(self, context: AccessContext, offering_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        context.require("write:match-offerings")
        current = self.repository.get(OFFERING_TYPE, offering_id)
        if current["state"] == "withdrawn":
            raise ConflictError("产品版本已撤回，请登记新版本")
        unknown = set(values) - set(OFFERING_FIELDS)
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        merged = {key: current[key] for key in OFFERING_FIELDS}
        merged.update(values)
        return self.repository.update(OFFERING_TYPE, offering_id, _validate_offering(merged),
                                     actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def transition_offering(self, context: AccessContext, offering_id: str, target: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        context.require("transition:match-offerings")
        if not reason.strip():
            raise ValidationError("状态变更必须说明原因")
        current = self.repository.get(OFFERING_TYPE, offering_id)
        allowed = {"draft": {"active"}, "active": {"suspended", "withdrawn"},
                   "suspended": {"active", "withdrawn"}, "withdrawn": set()}
        if target not in allowed.get(current["state"], set()):
            raise ConflictError(f"产品不允许从 {current['state']} 转到 {target}")
        return self.repository.update(OFFERING_TYPE, offering_id, {"state": target, "transition_reason": reason.strip()},
                                     actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def offering_history(self, context: AccessContext, offering_id: str) -> list[dict]:
        context.require("history:match-offerings")
        rows = self.repository.history(OFFERING_TYPE, offering_id)
        if not rows:
            raise NotFoundError("产品不存在")
        return rows

    # ------------------------------------------------------------------ #
    # 披露授权与授权字段匹配
    # ------------------------------------------------------------------ #
    def authorize_disclosure(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """登记一份披露授权（沿用 disclosures 实体及其 draft→approved→published 生命周期）。"""
        context.require("write:disclosures")
        subject_type = _clean_str(values.get("subject_type"), "主体类型")
        if subject_type not in (DEMAND_TYPE, OFFERING_TYPE):
            raise ValidationError("披露授权主体必须是需求或产品")
        subject_id = _clean_str(values.get("subject_id"), "主体标识")
        self.repository.get(subject_type, subject_id)
        audience = _clean_str(values.get("audience"), "授权对象")
        fields = values.get("fields")
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f.strip() for f in fields):
            raise ValidationError("授权字段必须是非空字符串列表")
        valid_fields = MATCHABLE_DEMAND_FIELDS if subject_type == DEMAND_TYPE else MATCHABLE_OFFERING_FIELDS
        unknown = sorted({f.strip() for f in fields} - set(valid_fields))
        if unknown:
            raise ValidationError("字段不可授权: " + ", ".join(unknown))
        valid_until = canonical_instant(values["valid_until"]) if values.get("valid_until") else ""
        payload = {"subject_type": subject_type, "subject_id": subject_id, "audience": audience,
                   "fields": sorted({f.strip() for f in fields}), "valid_until": valid_until,
                   "state": "draft"}
        return self.repository.create(DISCLOSURE_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def publish_disclosure(self, context: AccessContext, disclosure_id: str, *, expected_version: int, request_key: str) -> dict:
        """审批并发布授权：draft→approved→published 在单事务内完成。"""
        context.require("transition:disclosures")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="disclosure-publish", request_key=request_key,
                request={"disclosure_id": disclosure_id, "expected_version": expected_version},
                operation=lambda: self._publish_disclosure(connection, context, disclosure_id, expected_version))

        return self.repository.transact(operation)

    def _publish_disclosure(self, connection, context: AccessContext, disclosure_id: str, expected_version: int) -> dict:
        current = self.repository.load(connection, DISCLOSURE_TYPE, disclosure_id)
        if current["state"] == "published":
            return current
        if current["state"] == "draft":
            if current["version"] != expected_version:
                raise ConflictError(f"版本冲突，当前为 {current['version']}")
            current = self.repository.apply_update(connection, DISCLOSURE_TYPE, disclosure_id, {"state": "approved"},
                                                   actor=context.actor_id, expected_version=current["version"],
                                                   request_key=f"publish:{disclosure_id}:approve")
        if current["state"] != "approved":
            raise ConflictError(f"{current['state']} 状态的授权不能发布")
        return self.repository.apply_update(connection, DISCLOSURE_TYPE, disclosure_id, {"state": "published"},
                                           actor=context.actor_id, expected_version=current["version"],
                                           request_key=f"publish:{disclosure_id}:publish")

    def _published_disclosures(self, connection, subject_type: str, subject_id: str) -> list[dict]:
        now = parse_instant(self.repository.clock.now())
        result = []
        for row in connection.execute("SELECT * FROM entities WHERE entity_type=? AND state='published'", (DISCLOSURE_TYPE,)):
            payload = json.loads(row["payload_json"])
            if payload.get("subject_type") != subject_type or payload.get("subject_id") != subject_id:
                continue
            if payload.get("valid_until") and parse_instant(payload["valid_until"]) < now:
                continue
            result.append(payload)
        return result

    def _authorized_fields(self, connection, subject_type: str, subject_id: str, counterparty_org: str) -> set[str]:
        """对方机构能看到的字段：任一 published 授权的受众覆盖该机构（* 表示所有机构）。"""
        fields: set[str] = set()
        for disclosure in self._published_disclosures(connection, subject_type, subject_id):
            if disclosure.get("audience") in ("*", counterparty_org):
                fields.update(disclosure.get("fields", []))
        return fields

    def suggest_matches(self, context: AccessContext, demand_id: str, *, supplier_org: str | None = None, limit: int = 50) -> list[dict]:
        """为一条需求寻找有效产品；返回内容只包含双方均授权披露的字段。"""
        context.require("match:match-demands")
        with self.repository.database.connect() as connection:
            demand = self.repository.load(connection, DEMAND_TYPE, demand_id)
            if demand["state"] not in ("open", "on_hold"):
                raise ConflictError("只有开放中的需求可以进行匹配")
            suggestions = []
            for row in connection.execute("SELECT * FROM entities WHERE entity_type=? AND state='active' ORDER BY entity_id LIMIT 500", (OFFERING_TYPE,)):
                offering = self.repository._row_to_dict(row)
                if supplier_org and offering["supplier_org"] != supplier_org:
                    continue
                d_fields = self._authorized_fields(connection, DEMAND_TYPE, demand_id, offering["supplier_org"])
                o_fields = self._authorized_fields(connection, OFFERING_TYPE, offering["entity_id"], demand["buyer_org"])
                if not d_fields or not o_fields:
                    continue
                demand_view = {key: demand[key] for key in MATCHABLE_DEMAND_FIELDS if key in d_fields}
                offering_view = {key: offering[key] for key in MATCHABLE_OFFERING_FIELDS if key in o_fields}
                score, reasons = self._score(demand_view, offering_view)
                if score <= 0:
                    continue
                suggestions.append({
                    "demand_id": demand_id, "demand_version": demand["version"],
                    "offering_id": offering["entity_id"], "offering_version": offering["version"],
                    "demand_view": demand_view, "offering_view": offering_view,
                    "disclosed_demand_fields": sorted(d_fields & set(MATCHABLE_DEMAND_FIELDS)),
                    "disclosed_offering_fields": sorted(o_fields & set(MATCHABLE_OFFERING_FIELDS)),
                    "score": score, "reasons": reasons,
                })
            suggestions.sort(key=lambda item: (-item["score"], item["offering_id"]))
            return suggestions[:limit]

    @staticmethod
    def _score(demand_view: dict, offering_view: dict) -> tuple[int, list[str]]:
        score = 0; reasons: list[str] = []
        if demand_view.get("category") and offering_view.get("category"):
            if demand_view["category"] != offering_view["category"]:
                return 0, ["品类不一致"]
            score += 2; reasons.append("品类一致")
        if "certifications" in demand_view and "certifications" in offering_view:
            missing = set(demand_view["certifications"]) - set(offering_view["certifications"])
            if missing:
                return 0, ["缺少认证: " + ",".join(sorted(missing))]
            score += 2; reasons.append("认证齐备")
        if "delivery_regions" in demand_view and "delivery_regions" in offering_view:
            if not set(demand_view["delivery_regions"]) & set(offering_view["delivery_regions"]):
                return 0, ["交付地区无交集"]
            score += 1; reasons.append("交付地区有交集")
        if "quantity_min" in demand_view and "capacity_per_month" in offering_view:
            if offering_view["capacity_per_month"] < demand_view["quantity_min"]:
                return 0, ["月产能低于需求下限"]
            score += 1; reasons.append("产能可覆盖")
        if "moq" in offering_view and "quantity_max" in demand_view:
            if offering_view["moq"] > demand_view["quantity_max"]:
                return 0, ["起订量高于需求上限"]
            score += 1
        return score, reasons

    # ------------------------------------------------------------------ #
    # 多渠道线索：去重、合并、逐源留痕、冲突待裁
    # ------------------------------------------------------------------ #
    def ingest_lead(self, context: AccessContext, *, source: str, source_key: str, sequence: int,
                    payload: dict, occurred_at: str) -> dict:
        """接收一条渠道线索。相同回执重复到达不再次推进；标识相同内容冲突时待主管核对。"""
        context.require("ingest:match-leads")
        normalized = self._normalize_lead_payload(payload)
        fingerprint = digest_json({"category": normalized["category"], "buyer_org": normalized["buyer_org"],
                                   "product_name": normalized["product_name"]})
        # Inbox 先做通道级幂等：重复回执返回 duplicate，内容不同进入 inbox_conflicts 并报错。
        receipt = self.inbox.receive(source=source, source_key=source_key, sequence=sequence,
                                    payload=payload, occurred_at=occurred_at)

        def operation(connection) -> dict:
            # 回执可能已确认但合并事务此前崩溃：来源行不存在时继续完成合并，存在才判重，
            # 避免“中断后重发回执被吞掉、线索永远合并不了”的窗口。
            source_row = connection.execute(
                "SELECT lead_id FROM match_lead_sources WHERE source=? AND source_key=? AND sequence=?",
                (source, source_key, sequence)).fetchone()
            if receipt["status"] == "duplicate" and source_row is not None:
                return {"status": "duplicate", "fingerprint": fingerprint, "lead_id": source_row["lead_id"]}
            row = connection.execute("SELECT * FROM match_leads WHERE fingerprint=?", (fingerprint,)).fetchone()
            now = self.repository.clock.now()
            if row is None:
                lead_id = new_id("lead")
                connection.execute(
                    "INSERT INTO match_leads(lead_id,fingerprint,status,demand_id,payload_json,version,created_at,updated_at,created_by,updated_by)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (lead_id, fingerprint, "merged", None, canonical_json(normalized), 1, now, now, context.actor_id, context.actor_id))
                self._insert_source(connection, lead_id, source, source_key, sequence, payload, now)
                self.repository.audit.append(connection, actor_id=context.actor_id, action="lead.merge",
                                             entity_type="match_leads", entity_id=lead_id, version=1,
                                             detail={"sources": [source], "fingerprint": fingerprint})
                return {"status": "accepted", "lead_id": lead_id, "fingerprint": fingerprint, "sources": [source]}
            lead_id = row["lead_id"]
            self._insert_source(connection, lead_id, source, source_key, sequence, payload, now)
            existing_payload = json.loads(row["payload_json"])
            conflicts = self._field_conflicts(existing_payload, normalized)
            if conflicts:
                for field_name, old_value, new_value in conflicts:
                    connection.execute(
                        "INSERT INTO match_lead_conflicts(lead_id,source,source_key,sequence,field_name,existing_value_json,incoming_value_json,status,created_at)"
                        " VALUES(?,?,?,?,?,?,?,'pending',?)",
                        (lead_id, source, source_key, sequence, field_name, canonical_json(old_value), canonical_json(new_value), now))
                connection.execute("UPDATE match_leads SET status='conflict',version=version+1,updated_at=?,updated_by=? WHERE lead_id=?",
                                   (now, context.actor_id, lead_id))
                self.repository.audit.append(connection, actor_id=context.actor_id, action="lead.conflict",
                                             entity_type="match_leads", entity_id=lead_id, version=row["version"] + 1,
                                             detail={"source": source, "fields": [c[0] for c in conflicts]})
                return {"status": "conflict", "lead_id": lead_id, "fingerprint": fingerprint,
                        "conflict_fields": [c[0] for c in conflicts]}
            connection.execute("UPDATE match_leads SET updated_at=?,updated_by=? WHERE lead_id=?", (now, context.actor_id, lead_id))
            self.repository.audit.append(connection, actor_id=context.actor_id, action="lead.merge",
                                         entity_type="match_leads", entity_id=lead_id, version=row["version"],
                                         detail={"sources": [source]})
            return {"status": "merged", "lead_id": lead_id, "fingerprint": fingerprint}

        return self.repository.transact(operation)

    @staticmethod
    def _insert_source(connection, lead_id: str, source: str, source_key: str, sequence: int, payload: dict, now: str) -> None:
        connection.execute(
            "INSERT INTO match_lead_sources(lead_id,source,source_key,sequence,payload_digest,received_at) VALUES(?,?,?,?,?,?)",
            (lead_id, source, source_key, sequence, digest_json(payload), now))

    @staticmethod
    def _normalize_lead_payload(payload: Mapping[str, object]) -> dict:
        if not isinstance(payload, Mapping):
            raise ValidationError("线索内容必须是对象")
        try:
            quantity = int(payload["quantity"]) if payload.get("quantity") is not None else None
        except (TypeError, ValueError) as exc:
            raise ValidationError("线索数量必须是整数") from exc
        if quantity is not None and quantity <= 0:
            raise ValidationError("线索数量必须为正")
        return {
            "buyer_org": _clean_str(payload.get("buyer_org"), "采购机构"),
            "category": _clean_str(payload.get("category"), "品类"),
            "product_name": _clean_str(payload.get("product_name"), "产品名称"),
            "supplier_org": str(payload.get("supplier_org", "")).strip(),
            "quantity": quantity,
            "delivery_region": str(payload.get("delivery_region", "")).strip(),
        }

    @staticmethod
    def _field_conflicts(existing: dict, incoming: dict) -> list[tuple[str, object, object]]:
        return [(name, existing.get(name), incoming.get(name))
                for name in ("supplier_org", "quantity", "delivery_region")
                if existing.get(name) and incoming.get(name) and existing.get(name) != incoming.get(name)]

    def resolve_lead_conflict(self, context: AccessContext, conflict_id: int, *, resolution: str, request_key: str) -> dict:
        """主管核对内容冲突：选择保留旧值或采用新值；合并后的所有来源仍全部可见。"""
        context.require("resolve:match-leads")
        if resolution not in ("keep_existing", "use_incoming"):
            raise ValidationError("裁决必须是 keep_existing 或 use_incoming")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="lead-conflict-resolve", request_key=request_key,
                request={"conflict_id": conflict_id, "resolution": resolution},
                operation=lambda: self._apply_conflict_resolution(connection, context, conflict_id, resolution))

        return self.repository.transact(operation)

    def _apply_conflict_resolution(self, connection, context: AccessContext, conflict_id: int, resolution: str) -> dict:
        row = connection.execute("SELECT * FROM match_lead_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
        if not row:
            raise NotFoundError("冲突记录不存在")
        if row["status"] != "pending":
            raise ConflictError("冲突已经裁决")
        now = self.repository.clock.now()
        lead = connection.execute("SELECT * FROM match_leads WHERE lead_id=?", (row["lead_id"],)).fetchone()
        payload = json.loads(lead["payload_json"])
        incoming_value = json.loads(row["incoming_value_json"])
        chosen = incoming_value if resolution == "use_incoming" else payload.get(row["field_name"])
        if resolution == "use_incoming":
            payload[row["field_name"]] = chosen
        connection.execute(
            "UPDATE match_lead_conflicts SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE conflict_id=?",
            (resolution, context.actor_id, now, conflict_id))
        pending = connection.execute(
            "SELECT COUNT(*) AS n FROM match_lead_conflicts WHERE lead_id=? AND status='pending'", (row["lead_id"],)).fetchone()["n"]
        new_status = "conflict" if pending else "merged"
        new_version = lead["version"] + 1
        connection.execute("UPDATE match_leads SET status=?,payload_json=?,version=?,updated_at=?,updated_by=? WHERE lead_id=?",
                           (new_status, canonical_json(payload), new_version, now, context.actor_id, row["lead_id"]))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="lead.resolve",
                                     entity_type="match_leads", entity_id=row["lead_id"], version=new_version,
                                     detail={"conflict_id": conflict_id, "field": row["field_name"], "resolution": resolution})
        return {"conflict_id": conflict_id, "lead_id": row["lead_id"], "status": new_status,
                "field": row["field_name"], "chosen": chosen}

    def get_lead(self, context: AccessContext, lead_id: str) -> dict:
        context.require("read:match-leads")
        with self.repository.database.connect() as connection:
            row = connection.execute("SELECT * FROM match_leads WHERE lead_id=?", (lead_id,)).fetchone()
            if not row:
                raise NotFoundError("线索不存在")
            sources = [dict(r) for r in connection.execute(
                "SELECT source,source_key,sequence,payload_digest,received_at FROM match_lead_sources WHERE lead_id=? ORDER BY received_at,source", (lead_id,))]
            conflicts = [dict(r) for r in connection.execute(
                "SELECT conflict_id,source,field_name,existing_value_json,incoming_value_json,status,resolution,resolved_by FROM match_lead_conflicts WHERE lead_id=? ORDER BY conflict_id", (lead_id,))]
            result = dict(row); result["payload"] = json.loads(row["payload_json"]); result.pop("payload_json")
            result["sources"] = sources; result["conflicts"] = conflicts
            return result

    def list_pending_conflicts(self, context: AccessContext) -> list[dict]:
        context.require("read:match-leads")
        with self.repository.database.connect() as connection:
            return [dict(r) for r in connection.execute(
                "SELECT conflict_id,lead_id,source,source_key,field_name,existing_value_json,incoming_value_json,created_at FROM match_lead_conflicts WHERE status='pending' ORDER BY conflict_id")]

    # ------------------------------------------------------------------ #
    # 参展商以明确产品版本回应
    # ------------------------------------------------------------------ #
    def respond_to_demand(self, context: AccessContext, *, demand_id: str, demand_version: int,
                          offering_id: str, offering_version: int, request_key: str) -> dict:
        context.require("respond:match-demands")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="demand-response", request_key=request_key,
                request={"demand_id": demand_id, "demand_version": demand_version,
                         "offering_id": offering_id, "offering_version": offering_version},
                operation=lambda: self._create_response(connection, context, demand_id, demand_version, offering_id, offering_version))

        return self.repository.transact(operation)

    def _create_response(self, connection, context: AccessContext, demand_id: str, demand_version: int, offering_id: str, offering_version: int) -> dict:
        demand = self._demand_version(connection, demand_id, demand_version)
        offering = self._offering_version(connection, offering_id, offering_version)
        if demand["state"] != "open":
            raise ConflictError("只有开放中的需求版本可以被回应")
        # 回应锁定明确版本内容；产品本身当前必须处于有效状态（draft/withdrawn 版本不得对外回应）。
        offering_current = self.repository.load(connection, OFFERING_TYPE, offering_id)
        if offering_current["state"] != "active":
            raise ConflictError("产品当前未处于有效状态，不能回应需求")
        d_fields = self._authorized_fields(connection, DEMAND_TYPE, demand_id, offering["supplier_org"])
        o_fields = self._authorized_fields(connection, OFFERING_TYPE, offering_id, demand["buyer_org"])
        if not set(RESPONSE_REQUIRED_FIELDS) <= d_fields:
            raise PermissionDenied("需求方尚未授权对该供应商披露匹配所需字段")
        if not set(RESPONSE_REQUIRED_FIELDS) <= o_fields:
            raise PermissionDenied("供应商尚未授权对该采购商披露匹配所需字段")
        duplicate = connection.execute(
            "SELECT response_id FROM match_responses WHERE demand_id=? AND demand_version=? AND offering_id=? AND offering_version=? AND status='submitted'",
            (demand_id, demand_version, offering_id, offering_version)).fetchone()
        if duplicate:
            return {"response_id": duplicate["response_id"], "status": "duplicate"}
        response_id = new_id("resp")
        payload = {"demand_version": demand_version, "offering_version": offering_version,
                   "supplier_org": offering["supplier_org"], "buyer_org": demand["buyer_org"]}
        connection.execute(
            "INSERT INTO match_responses(response_id,demand_id,demand_version,offering_id,offering_version,supplier_org,status,payload_json,created_at,created_by)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (response_id, demand_id, demand_version, offering_id, offering_version, offering["supplier_org"],
             "submitted", canonical_json(payload), self.repository.clock.now(), context.actor_id))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="demand.respond",
                                     entity_type="match_responses", entity_id=response_id, version=1, detail=payload)
        return {"response_id": response_id, "status": "submitted", **payload}

    def list_responses(self, context: AccessContext, demand_id: str) -> list[dict]:
        context.require("read:match-demands")
        with self.repository.database.connect() as connection:
            return [dict(r) for r in connection.execute(
                "SELECT * FROM match_responses WHERE demand_id=? ORDER BY created_at,response_id", (demand_id,))]

    def _demand_version(self, connection, demand_id: str, version: int) -> dict:
        row = connection.execute("SELECT * FROM entity_versions WHERE entity_type=? AND entity_id=? AND version=?",
                                 (DEMAND_TYPE, demand_id, version)).fetchone()
        if not row:
            raise NotFoundError("需求版本不存在")
        return self.repository._version_to_dict(row)

    def _offering_version(self, connection, offering_id: str, version: int) -> dict:
        row = connection.execute("SELECT * FROM entity_versions WHERE entity_type=? AND entity_id=? AND version=?",
                                 (OFFERING_TYPE, offering_id, version)).fetchone()
        if not row:
            raise NotFoundError("产品版本不存在")
        return self.repository._version_to_dict(row)

    # ------------------------------------------------------------------ #
    # 机会：会谈、纪要、份额推进
    # ------------------------------------------------------------------ #
    def open_opportunity(self, context: AccessContext, *, response_id: str, total_quantity: int, request_key: str) -> dict:
        context.require("write:match-opportunities")
        total_quantity = _positive_int(total_quantity, "机会总量")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="opportunity-open", request_key=request_key,
                request={"response_id": response_id, "total_quantity": total_quantity},
                operation=lambda: self._open_opportunity(connection, context, response_id, total_quantity))

        return self.repository.transact(operation)

    def _open_opportunity(self, connection, context: AccessContext, response_id: str, total_quantity: int) -> dict:
        row = connection.execute("SELECT * FROM match_responses WHERE response_id=? AND status='submitted'", (response_id,)).fetchone()
        if not row:
            raise NotFoundError("有效回应不存在")
        demand = self._demand_version(connection, row["demand_id"], row["demand_version"])
        if not (demand["quantity_min"] <= total_quantity <= demand["quantity_max"]):
            raise ValidationError(f"机会总量必须落在需求版本 {row['demand_version']} 的数量区间内")
        opportunity_id = new_id("opp")
        now = self.repository.clock.now()
        connection.execute(
            "INSERT INTO match_opportunities(opportunity_id,lead_id,demand_id,demand_version,offering_id,offering_version,meeting_id,response_id,total_quantity,allocated_quantity,status,created_at,updated_at,created_by,updated_by)"
            " VALUES(?,?,?,?,?,?,?,?,?,0,'open',?,?,?,?)",
            (opportunity_id, None, row["demand_id"], row["demand_version"], row["offering_id"], row["offering_version"],
             None, response_id, total_quantity, now, now, context.actor_id, context.actor_id))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="opportunity.open",
                                     entity_type="match_opportunities", entity_id=opportunity_id, version=1,
                                     detail={"total_quantity": total_quantity})
        return {"opportunity_id": opportunity_id, "total_quantity": total_quantity, "allocated_quantity": 0, "status": "open"}

    def book_meeting(self, context: AccessContext, *, opportunity_id: str, room_id: str, room_capacity: int,
                     attendee_ids: Sequence[str], start_at: str, end_at: str, request_key: str) -> dict:
        """预约会谈：单事务同时占用场地与每个参会人员，任一冲突则整体失败。"""
        context.require("write:match-meetings")
        attendees = list(attendee_ids)
        if not attendees or len(set(attendees)) != len(attendees):
            raise ValidationError("参会人员不能为空且不能重复")
        for person in attendees:
            require_safe(person, "参会人员")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="meeting-book", request_key=request_key,
                request={"opportunity_id": opportunity_id, "room_id": room_id,
                         "attendee_ids": sorted(attendees), "start_at": start_at, "end_at": end_at},
                operation=lambda: self._book_meeting(connection, context, opportunity_id, room_id, room_capacity, attendees, start_at, end_at))

        return self.repository.transact(operation)

    def _book_meeting(self, connection, context: AccessContext, opportunity_id: str, room_id: str, room_capacity: int, attendees: list[str], start_at: str, end_at: str) -> dict:
        opp = self._load_opportunity(connection, opportunity_id)
        if opp["status"] == "closed" and self._active_quantity(connection, opportunity_id) == 0:
            raise ConflictError("机会已关闭，不能再预约会谈")
        start = canonical_instant(start_at); end = canonical_instant(end_at)
        subject = f"meeting-for:{opportunity_id}"
        room = self.reservations.reserve_in(connection, resource_id=f"room:{room_id}", subject_id=subject,
                                            quantity=1, capacity=room_capacity, start_at=start, end_at=end, actor=context.actor_id)
        people_reservations = []
        for person in attendees:
            reservation = self.reservations.reserve_in(connection, resource_id=f"person:{person}", subject_id=subject,
                                                       quantity=1, capacity=1, start_at=start, end_at=end, actor=context.actor_id)
            people_reservations.append(reservation["reservation_id"])
        meeting_id = new_id("meeting")
        connection.execute(
            "INSERT INTO match_meetings(meeting_id,opportunity_id,room_reservation_id,people_reservation_id,start_at,end_at,parties_json,status,created_at,created_by)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (meeting_id, opportunity_id, room["reservation_id"], canonical_json(people_reservations), start, end,
             canonical_json(attendees), "scheduled", self.repository.clock.now(), context.actor_id))
        connection.execute("UPDATE match_opportunities SET meeting_id=?,updated_at=?,updated_by=? WHERE opportunity_id=?",
                           (meeting_id, self.repository.clock.now(), context.actor_id, opportunity_id))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="meeting.book",
                                     entity_type="match_meetings", entity_id=meeting_id, version=1,
                                     detail={"room": room_id, "attendees": attendees})
        return {"meeting_id": meeting_id, "opportunity_id": opportunity_id,
                "room_reservation_id": room["reservation_id"], "people_reservation_ids": people_reservations,
                "status": "scheduled"}

    def cancel_meeting(self, context: AccessContext, meeting_id: str, *, reason: str, request_key: str) -> dict:
        context.require("write:match-meetings")
        if not reason.strip():
            raise ValidationError("取消会谈必须说明原因")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="meeting-cancel", request_key=request_key,
                request={"meeting_id": meeting_id},
                operation=lambda: self._cancel_meeting(connection, context, meeting_id, reason))

        return self.repository.transact(operation)

    def _cancel_meeting(self, connection, context: AccessContext, meeting_id: str, reason: str) -> dict:
        row = connection.execute("SELECT * FROM match_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
        if not row:
            raise NotFoundError("会谈不存在")
        if row["status"] != "scheduled":
            raise ConflictError("只有已预约的会谈可以取消")
        self.reservations.release_in(connection, row["room_reservation_id"])
        for reservation_id in json.loads(row["people_reservation_id"]):
            self.reservations.release_in(connection, reservation_id)
        connection.execute("UPDATE match_meetings SET status='cancelled' WHERE meeting_id=?", (meeting_id,))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="meeting.cancel",
                                     entity_type="match_meetings", entity_id=meeting_id, version=1, detail={"reason": reason})
        return {"meeting_id": meeting_id, "status": "cancelled"}

    def draft_minutes(self, context: AccessContext, *, meeting_id: str, party: str, content: Mapping[str, object], request_key: str) -> dict:
        """创建首版纪要；一方改变范围时保留旧版并形成待双方确认的新版本。"""
        context.require("write:match-minutes")
        self._require_party(party)
        if not isinstance(content, Mapping) or not content:
            raise ValidationError("纪要内容不能为空")
        clean = {str(k).strip(): v for k, v in content.items() if str(k).strip()}
        if not clean:
            raise ValidationError("纪要内容不能为空")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="minutes-draft", request_key=request_key,
                request={"meeting_id": meeting_id, "party": party, "content": clean},
                operation=lambda: self._draft_minutes(connection, context, meeting_id, party, clean))

        return self.repository.transact(operation)

    def _draft_minutes(self, connection, context: AccessContext, meeting_id: str, party: str, content: dict) -> dict:
        meeting = connection.execute("SELECT * FROM match_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
        if not meeting:
            raise NotFoundError("会谈不存在")
        if meeting["status"] == "cancelled":
            raise ConflictError("会谈已取消，不能记录纪要")
        latest = connection.execute("SELECT * FROM match_meeting_minutes WHERE meeting_id=? ORDER BY version DESC LIMIT 1", (meeting_id,)).fetchone()
        if latest is None:
            version = 1; note = "初稿"
        elif digest_json(json.loads(latest["content_json"])) == digest_json(content):
            return {"minute_id": latest["minute_id"], "version": latest["version"], "status": "unchanged"}
        else:
            version = latest["version"] + 1
            note = f"{party}方改变范围，旧版保留，待双方重新确认"
        minute_id = new_id("minute")
        now = self.repository.clock.now()
        connection.execute(
            "INSERT INTO match_meeting_minutes(minute_id,meeting_id,version,content_json,changed_by_party,change_note,buyer_confirmed_at,seller_confirmed_at,superseded_by,created_at)"
            " VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?)",
            (minute_id, meeting_id, version, canonical_json(content), party, note, now))
        if latest is not None:
            connection.execute("UPDATE match_meeting_minutes SET superseded_by=? WHERE minute_id=?", (minute_id, latest["minute_id"]))
        connection.execute("UPDATE match_meetings SET status='held' WHERE meeting_id=? AND status='scheduled'", (meeting_id,))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="minutes.draft",
                                     entity_type="match_meeting_minutes", entity_id=minute_id, version=version,
                                     detail={"meeting_id": meeting_id, "party": party, "note": note})
        return {"minute_id": minute_id, "meeting_id": meeting_id, "version": version,
                "buyer_confirmed": False, "seller_confirmed": False, "both_confirmed": False, "change_note": note}

    def confirm_minutes(self, context: AccessContext, *, meeting_id: str, party: str, request_key: str) -> dict:
        """买卖双方分别确认当前纪要版本；改版后旧确认清零，需要双方重新确认。"""
        context.require("confirm:match-minutes")
        self._require_party(party)

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="minutes-confirm", request_key=request_key,
                request={"meeting_id": meeting_id, "party": party},
                operation=lambda: self._confirm_minutes(connection, context, meeting_id, party))

        return self.repository.transact(operation)

    def _confirm_minutes(self, connection, context: AccessContext, meeting_id: str, party: str) -> dict:
        latest = connection.execute("SELECT * FROM match_meeting_minutes WHERE meeting_id=? ORDER BY version DESC LIMIT 1", (meeting_id,)).fetchone()
        if not latest:
            raise NotFoundError("纪要尚不存在")
        column = "buyer_confirmed_at" if party == _PARTY_BUYER else "seller_confirmed_at"
        if latest[column]:
            return {"minute_id": latest["minute_id"], "version": latest["version"], "status": "already_confirmed"}
        connection.execute(f"UPDATE match_meeting_minutes SET {column}=? WHERE minute_id=?",
                           (self.repository.clock.now(), latest["minute_id"]))
        buyer_confirmed = latest["buyer_confirmed_at"] is not None or party == _PARTY_BUYER
        seller_confirmed = latest["seller_confirmed_at"] is not None or party == _PARTY_SELLER
        self.repository.audit.append(connection, actor_id=context.actor_id, action="minutes.confirm",
                                     entity_type="match_meeting_minutes", entity_id=latest["minute_id"],
                                     version=latest["version"], detail={"party": party})
        return {"minute_id": latest["minute_id"], "version": latest["version"],
                "buyer_confirmed": buyer_confirmed, "seller_confirmed": seller_confirmed,
                "both_confirmed": buyer_confirmed and seller_confirmed}

    def get_minutes(self, context: AccessContext, meeting_id: str) -> list[dict]:
        context.require("read:match-minutes")
        with self.repository.database.connect() as connection:
            result = []
            for row in connection.execute("SELECT * FROM match_meeting_minutes WHERE meeting_id=? ORDER BY version", (meeting_id,)):
                item = dict(row); item["content"] = json.loads(row["content_json"]); item.pop("content_json")
                result.append(item)
            if not result:
                raise NotFoundError("纪要不存在")
            return result

    @staticmethod
    def _require_party(party: str) -> None:
        if party not in (_PARTY_BUYER, _PARTY_SELLER):
            raise ValidationError("会谈方必须是 buyer 或 seller")

    # ------------------------------------------------------------------ #
    # 阶段份额
    # ------------------------------------------------------------------ #
    def allocate_share(self, context: AccessContext, *, opportunity_id: str, stage: str, quantity: int,
                       owner_id: str, request_key: str) -> dict:
        context.require("write:match-shares")
        if stage not in ("sample", "quote", "framework", "contract"):
            raise ValidationError("初始份额阶段必须是样品、报价、框架协议或合同")
        quantity = _positive_int(quantity, "份额数量")
        owner_id = _clean_str(owner_id, "责任人")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="share-allocate", request_key=request_key,
                request={"opportunity_id": opportunity_id, "stage": stage, "quantity": quantity, "owner_id": owner_id},
                operation=lambda: self._allocate_share(connection, context, opportunity_id, stage, quantity, owner_id))

        return self.repository.transact(operation)

    def _allocate_share(self, connection, context: AccessContext, opportunity_id: str, stage: str, quantity: int, owner_id: str) -> dict:
        opp = self._load_opportunity(connection, opportunity_id)
        if opp["status"] == "closed":
            raise ConflictError("机会已关闭，不能再分配份额")
        allocated = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS n FROM match_opportunity_shares WHERE opportunity_id=?",
            (opportunity_id,)).fetchone()["n"]
        if allocated + quantity > opp["total_quantity"]:
            raise ConflictError(f"份额总量将超过机会总量 {opp['total_quantity']}（已分配 {allocated}）")
        share_id = new_id("share")
        now = self.repository.clock.now()
        connection.execute(
            "INSERT INTO match_opportunity_shares(share_id,opportunity_id,stage,quantity,owner_id,status,reference,created_at,updated_at)"
            " VALUES(?,?,?,?,?,'active','',?,?)",
            (share_id, opportunity_id, stage, quantity, owner_id, now, now))
        self._refresh_opportunity(connection, opportunity_id, actor=context.actor_id)
        self._event(connection, share_id, opportunity_id, "allocate", "", stage, {"quantity": quantity, "owner_id": owner_id}, context.actor_id)
        self._schedule_stage_followups(connection, opportunity_id, share_id, stage, quantity)
        return {"share_id": share_id, "opportunity_id": opportunity_id, "stage": stage,
                "quantity": quantity, "owner_id": owner_id, "status": "active"}

    def advance_share(self, context: AccessContext, *, share_id: str, to_stage: str,
                      owner_id: str | None = None, reference: str = "", request_key: str) -> dict:
        """样品→报价→框架协议→合同逐级推进，每阶段由对应责任人负责，成交以正式合同为准。"""
        context.require("advance:match-shares")
        if to_stage not in STAGE_ORDER:
            raise ValidationError("未知阶段")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="share-advance", request_key=request_key,
                request={"share_id": share_id, "to_stage": to_stage, "owner_id": owner_id, "reference": reference},
                operation=lambda: self._advance_share(connection, context, share_id, to_stage, owner_id, reference))

        return self.repository.transact(operation)

    def _advance_share(self, connection, context: AccessContext, share_id: str, to_stage: str, owner_id: str | None, reference: str) -> dict:
        row = connection.execute("SELECT * FROM match_opportunity_shares WHERE share_id=?", (share_id,)).fetchone()
        if not row:
            raise NotFoundError("份额不存在")
        if row["status"] == "exited":
            raise ConflictError("已退出的份额不能继续推进")
        from_stage = row["stage"]
        if STAGE_ORDER[to_stage] != STAGE_ORDER[from_stage] + 1:
            raise ConflictError(f"份额只能逐级推进，不能从 {from_stage} 跳到 {to_stage}")
        new_owner = (owner_id or row["owner_id"]).strip()
        if to_stage == "contract" and not reference.strip():
            raise ValidationError("推进到正式合同必须填写合同参考号")
        now = self.repository.clock.now()
        new_status = "contracted" if to_stage == "contract" else "active"
        connection.execute("UPDATE match_opportunity_shares SET stage=?,owner_id=?,status=?,reference=?,updated_at=? WHERE share_id=?",
                           (to_stage, new_owner, new_status, reference.strip(), now, share_id))
        self._refresh_opportunity(connection, row["opportunity_id"], actor=context.actor_id)
        self._event(connection, share_id, row["opportunity_id"], "advance", from_stage, to_stage,
                    {"owner_id": new_owner, "reference": reference.strip()}, context.actor_id)
        if to_stage == "contract":
            self.outbox.enqueue_in(connection, topic="match.contracted", aggregate_id=row["opportunity_id"],
                                   payload={"share_id": share_id, "contract_reference": reference.strip()})
            self._schedule_stage_followups(connection, row["opportunity_id"], share_id, "contract", row["quantity"])
        return {"share_id": share_id, "stage": to_stage, "owner_id": new_owner,
                "status": new_status, "reference": reference.strip()}

    def exit_share(self, context: AccessContext, *, share_id: str, reason: str, request_key: str) -> dict:
        context.require("advance:match-shares")
        if not reason.strip():
            raise ValidationError("退出必须说明原因")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="share-exit", request_key=request_key,
                request={"share_id": share_id, "reason": reason},
                operation=lambda: self._exit_share(connection, context, share_id, reason))

        return self.repository.transact(operation)

    def _exit_share(self, connection, context: AccessContext, share_id: str, reason: str) -> dict:
        row = connection.execute("SELECT * FROM match_opportunity_shares WHERE share_id=?", (share_id,)).fetchone()
        if not row:
            raise NotFoundError("份额不存在")
        if row["status"] == "exited":
            raise ConflictError("份额已经退出")
        if row["stage"] == "contract":
            raise ConflictError("已正式成交的份额不能退出，应走合同变更")
        now = self.repository.clock.now()
        connection.execute("UPDATE match_opportunity_shares SET status='exited',updated_at=? WHERE share_id=?", (now, share_id))
        self._refresh_opportunity(connection, row["opportunity_id"], actor=context.actor_id)
        self._event(connection, share_id, row["opportunity_id"], "exit", row["stage"], "exited",
                    {"reason": reason.strip()}, context.actor_id)
        return {"share_id": share_id, "status": "exited", "reason": reason.strip()}

    def handover_share(self, context: AccessContext, *, share_id: str, to_owner: str, note: str, request_key: str) -> dict:
        """责任人交接：份额继续推进，每次交接逐次留痕，供成交反查。"""
        context.require("handover:match-shares")
        to_owner = _clean_str(to_owner, "新责任人")
        if not note.strip():
            raise ValidationError("交接必须说明原因")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="share-handover", request_key=request_key,
                request={"share_id": share_id, "to_owner": to_owner, "note": note},
                operation=lambda: self._handover_share(connection, context, share_id, to_owner, note))

        return self.repository.transact(operation)

    def _handover_share(self, connection, context: AccessContext, share_id: str, to_owner: str, note: str) -> dict:
        row = connection.execute("SELECT * FROM match_opportunity_shares WHERE share_id=?", (share_id,)).fetchone()
        if not row:
            raise NotFoundError("份额不存在")
        if row["owner_id"] == to_owner:
            raise ValidationError("新责任人与当前责任人相同")
        now = self.repository.clock.now()
        handover_id = new_id("handover")
        connection.execute(
            "INSERT INTO match_handovers(handover_id,share_id,from_owner,to_owner,note,created_at,created_by)"
            " VALUES(?,?,?,?,?,?,?)",
            (handover_id, share_id, row["owner_id"], to_owner, note.strip(), now, context.actor_id))
        connection.execute("UPDATE match_opportunity_shares SET owner_id=?,updated_at=? WHERE share_id=?", (to_owner, now, share_id))
        self._event(connection, share_id, row["opportunity_id"], "handover", "", "",
                    {"from_owner": row["owner_id"], "to_owner": to_owner}, context.actor_id)
        return {"handover_id": handover_id, "share_id": share_id, "from_owner": row["owner_id"], "to_owner": to_owner}

    def get_opportunity(self, context: AccessContext, opportunity_id: str) -> dict:
        context.require("read:match-opportunities")
        with self.repository.database.connect() as connection:
            return self._opportunity_view(connection, opportunity_id)

    def list_opportunities(self, context: AccessContext, *, status: str | None = None) -> list[dict]:
        context.require("read:match-opportunities")
        if status is not None and status not in OPPORTUNITY_STATUSES:
            raise ValidationError("未知机会状态")
        with self.repository.database.connect() as connection:
            sql = "SELECT opportunity_id FROM match_opportunities"
            params: list[object] = []
            if status:
                sql += " WHERE status=?"; params.append(status)
            sql += " ORDER BY created_at,opportunity_id"
            return [self._opportunity_view(connection, r["opportunity_id"])
                    for r in connection.execute(sql, params)]

    def _opportunity_view(self, connection, opportunity_id: str) -> dict:
        opp = self._load_opportunity(connection, opportunity_id)
        opp["shares"] = [dict(r) for r in connection.execute(
            "SELECT share_id,stage,quantity,owner_id,status,reference,updated_at FROM match_opportunity_shares WHERE opportunity_id=? ORDER BY created_at,share_id", (opportunity_id,))]
        opp["events"] = []
        for r in connection.execute(
            "SELECT event_type,from_stage,to_stage,detail_json,actor_id,created_at FROM match_share_events WHERE opportunity_id=? ORDER BY event_id", (opportunity_id,)):
            event = dict(r); event["detail"] = json.loads(event.pop("detail_json")); opp["events"].append(event)
        return opp

    def _load_opportunity(self, connection, opportunity_id: str) -> dict:
        row = connection.execute("SELECT * FROM match_opportunities WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        if not row:
            raise NotFoundError("机会不存在")
        return dict(row)

    def _active_quantity(self, connection, opportunity_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS n FROM match_opportunity_shares WHERE opportunity_id=? AND status='active'",
            (opportunity_id,)).fetchone()["n"]

    def _refresh_opportunity(self, connection, opportunity_id: str, *, actor: str) -> None:
        """按份额守恒重算机会状态：部分成交只改变对应份额，绝不关闭剩余机会。"""
        opp = self._load_opportunity(connection, opportunity_id)
        rows = connection.execute(
            "SELECT status,COALESCE(SUM(quantity),0) AS n FROM match_opportunity_shares WHERE opportunity_id=? GROUP BY status",
            (opportunity_id,)).fetchall()
        quantities = {r["status"]: int(r["n"]) for r in rows}
        active_qty = quantities.get("active", 0)
        contracted_qty = quantities.get("contracted", 0)
        exited_qty = quantities.get("exited", 0)
        total = int(opp["total_quantity"])
        allocated = active_qty + contracted_qty + exited_qty
        if allocated > total:
            raise ConflictError("份额守恒被破坏：已分配数量超过机会总量")
        if active_qty > 0:
            new_status = "partial" if contracted_qty > 0 else "open"
        elif contracted_qty == total:
            new_status = "contracted"
        elif contracted_qty > 0:
            # 有成交但已无在途份额：余量已退出则关闭，仍有未分配余量则保持部分开放。
            new_status = "closed" if allocated == total else "partial"
        else:
            new_status = "closed" if allocated == total else "open"
        connection.execute(
            "UPDATE match_opportunities SET allocated_quantity=?,status=?,updated_at=?,updated_by=? WHERE opportunity_id=?",
            (allocated, new_status, self.repository.clock.now(), actor, opportunity_id))

    def _event(self, connection, share_id: str, opportunity_id: str, event_type: str, from_stage: str, to_stage: str, detail: dict, actor: str) -> None:
        connection.execute(
            "INSERT INTO match_share_events(share_id,opportunity_id,event_type,from_stage,to_stage,detail_json,actor_id,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (share_id, opportunity_id, event_type, from_stage, to_stage, canonical_json(detail), actor, self.repository.clock.now()))

    def _schedule_stage_followups(self, connection, opportunity_id: str, share_id: str, stage: str, quantity: int) -> None:
        """样品登记样品期限、合同登记履约回访；持久任务随事务落库，中断恢复后继续生效。"""
        if stage == "sample":
            due_at = (parse_instant(self.repository.clock.now()) + timedelta(days=7)).isoformat().replace("+00:00", "Z")
            kind = "sample_deadline"
        elif stage == "contract":
            due_at = (parse_instant(self.repository.clock.now()) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
            kind = "fulfillment_followup"
        else:
            return
        due_id = new_id("due")
        job_id = self.jobs.schedule_in(connection, job_type=kind, subject_id=share_id, run_at=due_at,
                                       payload={"opportunity_id": opportunity_id, "share_id": share_id, "quantity": quantity})
        connection.execute(
            "INSERT INTO match_due_items(due_id,kind,subject_type,subject_id,due_at,status,payload_json,job_id,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (due_id, kind, "share", share_id, due_at, "waiting",
             canonical_json({"opportunity_id": opportunity_id, "quantity": quantity}), job_id, self.repository.clock.now()))
        self.outbox.enqueue_in(connection, topic=f"match.{kind}.scheduled", aggregate_id=opportunity_id,
                               payload={"share_id": share_id, "due_at": due_at})

    # ------------------------------------------------------------------ #
    # 恢复工作台与成交反查
    # ------------------------------------------------------------------ #
    def worklist(self, context: AccessContext) -> dict:
        """服务中断恢复后专班继续处理：未双方确认纪要、样品期限、履约回访，以及中断时占有的任务租约。"""
        context.require("read:match-worklist")
        with self.repository.database.connect() as connection:
            now = self.repository.clock.now()
            unconfirmed = []
            meetings = connection.execute(
                "SELECT meeting_id,opportunity_id FROM match_meetings WHERE status!='cancelled' ORDER BY meeting_id").fetchall()
            for meeting in meetings:
                latest = connection.execute(
                    "SELECT version,buyer_confirmed_at,seller_confirmed_at FROM match_meeting_minutes WHERE meeting_id=? ORDER BY version DESC LIMIT 1",
                    (meeting["meeting_id"],)).fetchone()
                if latest and (latest["buyer_confirmed_at"] is None or latest["seller_confirmed_at"] is None):
                    unconfirmed.append({"meeting_id": meeting["meeting_id"], "opportunity_id": meeting["opportunity_id"],
                                        "latest_version": latest["version"]})
            dues = [dict(r) for r in connection.execute(
                "SELECT due_id,kind,subject_id,due_at,status FROM match_due_items WHERE status!='resolved' AND due_at<=? ORDER BY due_at,due_id", (now,))]
            upcoming = [dict(r) for r in connection.execute(
                "SELECT due_id,kind,subject_id,due_at,status FROM match_due_items WHERE status='waiting' AND due_at>? ORDER BY due_at,due_id", (now,))]
            stuck_jobs = [r["job_id"] for r in connection.execute(
                "SELECT job_id FROM scheduled_jobs WHERE status='running'")]
            return {"unconfirmed_minutes": unconfirmed, "due_now": dues, "upcoming": upcoming,
                    "recoverable_jobs": stuck_jobs}

    def resolve_due(self, context: AccessContext, due_id: str, *, request_key: str) -> dict:
        context.require("resolve:match-worklist")

        def operation(connection) -> dict:
            return self.repository.idempotency.execute(
                connection, scope="due-resolve", request_key=request_key, request={"due_id": due_id},
                operation=lambda: self._resolve_due(connection, context, due_id))

        return self.repository.transact(operation)

    def _resolve_due(self, connection, context: AccessContext, due_id: str) -> dict:
        row = connection.execute("SELECT * FROM match_due_items WHERE due_id=?", (due_id,)).fetchone()
        if not row:
            raise NotFoundError("到期事项不存在")
        if row["status"] == "resolved":
            return {"due_id": due_id, "kind": row["kind"], "status": "resolved", "replayed": True}
        connection.execute("UPDATE match_due_items SET status='resolved',resolved_at=? WHERE due_id=?",
                           (self.repository.clock.now(), due_id))
        self.repository.audit.append(connection, actor_id=context.actor_id, action="due.resolve",
                                     entity_type="match_due_items", entity_id=due_id, version=1, detail={"kind": row["kind"]})
        return {"due_id": due_id, "kind": row["kind"], "status": "resolved"}

    def trace_deal(self, context: AccessContext, *, share_id: str | None = None, contract_reference: str | None = None) -> dict:
        """从任一成交反查需求版本、产品版本、会谈过程（含纪要确认）、披露授权与责任交接。"""
        context.require("trace:match-deals")
        with self.repository.database.connect() as connection:
            if share_id is None:
                if not contract_reference:
                    raise ValidationError("必须提供 share_id 或合同参考号")
                row = connection.execute(
                    "SELECT share_id FROM match_opportunity_shares WHERE reference=? AND stage='contract'",
                    (contract_reference.strip(),)).fetchone()
                if not row:
                    raise NotFoundError("找不到对应成交")
                share_id = row["share_id"]
            share = connection.execute("SELECT * FROM match_opportunity_shares WHERE share_id=?", (share_id,)).fetchone()
            if not share:
                raise NotFoundError("成交份额不存在")
            opp = self._load_opportunity(connection, share["opportunity_id"])
            demand_versions = [self.repository._version_to_dict(r) for r in connection.execute(
                "SELECT * FROM entity_versions WHERE entity_type=? AND entity_id=? ORDER BY version", (DEMAND_TYPE, opp["demand_id"]))]
            offering_versions = [self.repository._version_to_dict(r) for r in connection.execute(
                "SELECT * FROM entity_versions WHERE entity_type=? AND entity_id=? ORDER BY version", (OFFERING_TYPE, opp["offering_id"]))]
            disclosures = []
            for r in connection.execute("SELECT * FROM entities WHERE entity_type=? ORDER BY entity_id,version", (DISCLOSURE_TYPE,)):
                payload = json.loads(r["payload_json"])
                if (payload.get("subject_type"), payload.get("subject_id")) not in (
                        (DEMAND_TYPE, opp["demand_id"]), (OFFERING_TYPE, opp["offering_id"])):
                    continue
                disclosures.append({"disclosure_id": r["entity_id"], "version": r["version"],
                                    "state": r["state"], **payload})
            meetings = []
            for r in connection.execute("SELECT * FROM match_meetings WHERE opportunity_id=? ORDER BY start_at,meeting_id", (opp["opportunity_id"],)):
                minutes = [dict(m) for m in connection.execute(
                    "SELECT version,changed_by_party,change_note,buyer_confirmed_at,seller_confirmed_at,superseded_by,created_at FROM match_meeting_minutes WHERE meeting_id=? ORDER BY version", (r["meeting_id"],))]
                meetings.append({"meeting_id": r["meeting_id"], "status": r["status"], "start_at": r["start_at"],
                                 "end_at": r["end_at"], "room_reservation_id": r["room_reservation_id"],
                                 "attendees": json.loads(r["parties_json"]), "minutes": minutes})
            handovers = [dict(r) for r in connection.execute(
                "SELECT handover_id,from_owner,to_owner,note,created_at,created_by FROM match_handovers WHERE share_id=? ORDER BY created_at", (share_id,))]
            timeline = []
            for r in connection.execute(
                "SELECT event_type,from_stage,to_stage,detail_json,actor_id,created_at FROM match_share_events WHERE share_id=? ORDER BY event_id", (share_id,)):
                event = dict(r); event["detail"] = json.loads(event.pop("detail_json")); timeline.append(event)
            return {
                "deal": {"share_id": share_id, "stage": share["stage"], "quantity": share["quantity"],
                         "contract_reference": share["reference"], "owner_id": share["owner_id"], "status": share["status"]},
                "opportunity": self._opportunity_view(connection, opp["opportunity_id"]),
                "demand": {"demand_id": opp["demand_id"], "matched_version": opp["demand_version"], "versions": demand_versions},
                "offering": {"offering_id": opp["offering_id"], "matched_version": opp["offering_version"], "versions": offering_versions},
                "disclosures": disclosures,
                "meetings": meetings,
                "handovers": handovers,
                "timeline": timeline,
            }
