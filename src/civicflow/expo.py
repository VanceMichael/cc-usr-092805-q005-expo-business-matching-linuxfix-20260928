"""展后供需对接业务域。

在协同事务平台既有能力（版本化实体仓库、审计链、幂等键、资源预约、
发件箱通知、可恢复任务队列）之上，实现路演供需意向的展后交接：

- 采购商需求与参展商产品均为不可变版本，匹配只引用明确版本号；
- 匹配只使用双方通过 disclosures 授权披露且在有效期内的字段；
- 多渠道线索登记到同一需求身份，合并后保留全部来源；
- 会谈同时占用场地与人员，纪要双方各自确认，改范围生成待确认新版本；
- 样品/报价/框架协议/正式合同按份额推进，回执幂等，冲突留主管核对；
- 部分成交不关闭剩余机会，退出须显式记录原因；
- 样品期限与履约回访进入持久任务队列，中断恢复后可继续处理；
- 任一成交可反查需求版本、会谈纪要、披露授权与责任交接。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .idempotency import IdempotencyStore
from .jsonutil import canonical_json, digest_json
from .outbox import Outbox
from .jobs import JobQueue
from .security import AccessContext
from .timeutil import Clock, canonical_instant, parse_instant

# 可匹配的规范字段名（需求版本与产品版本共用）
FIELD_CATEGORY = "category"
FIELD_QUANTITY = "quantity"
FIELD_REGIONS = "delivery_regions"
FIELD_CERTS = "certifications"
FIELD_WINDOW = "window"
MATCH_FIELDS = (FIELD_CATEGORY, FIELD_QUANTITY, FIELD_REGIONS, FIELD_CERTS, FIELD_WINDOW)

# 管线阶段：允许前进（可跨阶段），任一阶段可显式退出
STAGES = ("sample", "quote", "framework", "contract")
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}
EXIT_STAGE = "exited"

SAMPLE_JOB = "expo.sample_due"
FOLLOWUP_JOB = "expo.followup_due"


def _loads(raw: str) -> object:
    return json.loads(raw)


def _string_list(value: object, label: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{label}必须是列表")
    items = [item.strip() for item in value if isinstance(item, str)]
    if len(items) != len(value) or any(not item for item in items):
        raise ValidationError(f"{label}必须是非空字符串列表")
    if not items and not allow_empty:
        raise ValidationError(f"{label}不能为空")
    return items


def _validate_shape(values: Mapping[str, object]) -> dict:
    missing = {"category", "quantity_min", "quantity_max", "delivery_regions", "certifications", "window_from", "window_to"} - set(values)
    if missing:
        raise ValidationError("缺少字段: " + ", ".join(sorted(missing)))
    category = values["category"]
    if not isinstance(category, str) or not category.strip():
        raise ValidationError("category 不能为空字符串")
    try:
        qty_min = int(values["quantity_min"]); qty_max = int(values["quantity_max"])
    except (TypeError, ValueError) as exc:
        raise ValidationError("数量区间必须是整数") from exc
    if isinstance(values["quantity_min"], bool) or isinstance(values["quantity_max"], bool) or qty_min <= 0 or qty_max <= 0 or qty_min > qty_max:
        raise ValidationError("数量区间必须为正数且下限不大于上限")
    regions = _string_list(values["delivery_regions"], "交付地区", allow_empty=False)
    certs = _string_list(values["certifications"], "认证", allow_empty=True)
    window_from = canonical_instant(values["window_from"])  # type: ignore[arg-type]
    window_to = canonical_instant(values["window_to"])  # type: ignore[arg-type]
    if parse_instant(window_from) >= parse_instant(window_to):
        raise ValidationError("时间窗口开始必须早于结束")
    return {
        "category": category.strip(),
        "quantity_min": qty_min,
        "quantity_max": qty_max,
        "delivery_regions": regions,
        "certifications": certs,
        "window_from": window_from,
        "window_to": window_to,
    }


def _shape_digest(shape: Mapping[str, object]) -> str:
    return digest_json({key: shape[key] for key in ("category", "quantity_min", "quantity_max", "delivery_regions", "certifications", "window_from", "window_to")})


def _project_shape(*, category: str, qmin: int, qmax: int, regions: Iterable[str], certs: Iterable[str], wfrom: str, wto: str) -> dict:
    return {
        FIELD_CATEGORY: category,
        FIELD_QUANTITY: [qmin, qmax],
        FIELD_REGIONS: sorted(regions),
        FIELD_CERTS: sorted(certs),
        FIELD_WINDOW: [wfrom, wto],
    }


def _intervals_overlap(a: list, b: list) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _windows_overlap(a: list, b: list) -> bool:
    return parse_instant(a[0]) < parse_instant(b[1]) and parse_instant(b[0]) < parse_instant(a[1])


@dataclass(frozen=True)
class ExpoService:
    """展后供需对接服务。"""

    database: Database
    clock: Clock
    audit: AuditLog
    idempotency: IdempotencyStore

    # ---- 需求版本 ----------------------------------------------------------

    def register_lead(self, context: AccessContext, *, demand_identity: str, source: str, source_ref: str, payload: Mapping[str, object], request_key: str) -> dict:
        """登记一条渠道线索。同一(身份,渠道,渠道标识)重复到达按内容判重/冲突。"""
        context.require("write:expo-leads")
        require_safe(demand_identity, "需求身份"); require_safe(source, "线索来源"); require_safe(source_ref, "来源标识")
        if not isinstance(payload, Mapping) or not payload:
            raise ValidationError("线索内容不能为空")
        digest = digest_json(payload)

        def operation(connection) -> dict:
            row = connection.execute("SELECT payload_digest FROM expo_lead_registry WHERE demand_identity=? AND source=? AND source_ref=?", (demand_identity, source, source_ref)).fetchone()
            if row:
                if row["payload_digest"] != digest:
                    self._record_conflict(connection, kind="lead", subject_key=demand_identity, intake_key=f"{source}/{source_ref}", existing=row["payload_digest"], incoming=digest)
                    return {"status": "conflict", "message": "线索标识相同但内容冲突，已转主管核对"}
                return {"status": "duplicate", "demand_identity": demand_identity, "source": source, "source_ref": source_ref}
            connection.execute("INSERT INTO expo_lead_registry(demand_identity,source,source_ref,payload_digest,first_seen_at) VALUES(?,?,?,?,?)", (demand_identity, source, source_ref, digest, self.clock.now()))
            self.audit.append(connection, actor_id=context.actor_id, action="expo.lead_registered", entity_type="expo_demands", entity_id=demand_identity, version=0, detail={"source": source, "source_ref": source_ref})
            return {"status": "accepted", "demand_identity": demand_identity, "source": source, "source_ref": source_ref}

        result = self._execute_idempotent(scope="expo:lead", request_key=request_key, request={"identity": demand_identity, "source": source, "ref": source_ref, "digest": digest}, operation=operation)
        if result.get("status") == "conflict":
            raise ConflictError(result["message"])
        return result

    def _execute_idempotent(self, *, scope: str, request_key: str, request: dict, operation) -> dict:
        """在事务内执行幂等操作；冲突记录须随事务提交，故冲突以返回值带出。"""
        with self.database.transaction() as connection:
            return self.idempotency.execute(connection, scope=scope, request_key=request_key, request=request, operation=lambda: operation(connection))

    def create_demand(self, context: AccessContext, *, demand_identity: str, buyer_org: str, values: Mapping[str, object], sources: Iterable[Mapping[str, str]] = (), request_key: str) -> dict:
        """采购商建立需求首版本，并把已登记的多渠道线索挂到需求下。"""
        context.require("write:expo-demand")
        require_safe(demand_identity, "需求身份"); require_safe(buyer_org, "采购机构")
        shape = _validate_shape(values)
        sources = list(sources)
        with self.database.transaction() as connection:
            def operation() -> dict:
                if connection.execute("SELECT 1 FROM expo_demands WHERE demand_identity=?", (demand_identity,)).fetchone():
                    raise ConflictError("需求身份已经建立过需求")
                demand_id = new_id("demand"); now = self.clock.now()
                connection.execute("INSERT INTO expo_demands(demand_id,demand_identity,buyer_org,current_version,status,created_at,created_by) VALUES(?,?,?,1,'active',?,?)", (demand_id, demand_identity, buyer_org, now, context.actor_id))
                self._insert_demand_version(connection, demand_id=demand_id, version_no=1, shape=shape, actor=context.actor_id, request_key=request_key)
                for source in sources:
                    self._attach_source(connection, demand_id=demand_id, identity=demand_identity, source=source["source"], source_ref=source["source_ref"])
                self.audit.append(connection, actor_id=context.actor_id, action="expo.demand_created", entity_type="expo_demands", entity_id=demand_id, version=1, detail={"identity": demand_identity, "buyer_org": buyer_org})
                return self._demand_detail(connection, demand_id)
            return self.idempotency.execute(connection, scope="expo:demand-create", request_key=request_key, request={"identity": demand_identity, "shape": shape, "sources": sources}, operation=operation)

    def attach_source(self, context: AccessContext, *, demand_id: str, source: str, source_ref: str) -> dict:
        """把后到的渠道线索并入已有需求，来源始终保留可见。"""
        context.require("write:expo-leads")
        with self.database.transaction() as connection:
            demand = self._demand_row(connection, demand_id)
            result = self._attach_source(connection, demand_id=demand_id, identity=demand["demand_identity"], source=source, source_ref=source_ref)
            self.audit.append(connection, actor_id=context.actor_id, action="expo.source_attached", entity_type="expo_demands", entity_id=demand_id, version=int(demand["current_version"]), detail={"source": source, "source_ref": source_ref})
            return result

    def revise_demand(self, context: AccessContext, demand_id: str, values: Mapping[str, object], *, request_key: str) -> dict:
        """采购商修订需求：旧版本保留，产生新的待匹配版本。"""
        context.require("write:expo-demand")
        shape = _validate_shape(values)
        with self.database.transaction() as connection:
            def operation() -> dict:
                demand = self._demand_row(connection, demand_id)
                if demand["status"] != "active":
                    raise ConflictError("需求已关闭，不能修订")
                new_version = int(demand["current_version"]) + 1
                self._insert_demand_version(connection, demand_id=demand_id, version_no=new_version, shape=shape, actor=context.actor_id, request_key=request_key)
                connection.execute("UPDATE expo_demands SET current_version=? WHERE demand_id=?", (new_version, demand_id))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.demand_revised", entity_type="expo_demands", entity_id=demand_id, version=new_version, detail={"digest": _shape_digest(shape)})
                return self._demand_version_row(connection, demand_id, new_version)
            return self.idempotency.execute(connection, scope=f"expo:demand-revise:{demand_id}", request_key=request_key, request=shape, operation=operation)

    def close_demand(self, context: AccessContext, demand_id: str, *, reason: str, request_key: str) -> dict:
        context.require("write:expo-demand")
        if not reason.strip():
            raise ValidationError("关闭需求必须说明原因")
        with self.database.transaction() as connection:
            def operation() -> dict:
                demand = self._demand_row(connection, demand_id)
                if demand["status"] != "active":
                    raise ConflictError("需求不是活动状态")
                connection.execute("UPDATE expo_demands SET status='closed' WHERE demand_id=?", (demand_id,))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.demand_closed", entity_type="expo_demands", entity_id=demand_id, version=int(demand["current_version"]), detail={"reason": reason.strip()})
                return {"demand_id": demand_id, "status": "closed"}
            return self.idempotency.execute(connection, scope=f"expo:demand-close:{demand_id}", request_key=request_key, request={"reason": reason.strip()}, operation=operation)

    def get_demand(self, context: AccessContext, demand_id: str) -> dict:
        context.require("read:expo")
        with self.database.connect() as connection:
            return self._demand_detail(connection, demand_id)

    def demand_history(self, context: AccessContext, demand_id: str) -> list[dict]:
        context.require("read:expo")
        with self.database.connect() as connection:
            self._demand_row(connection, demand_id)
            return [self._demand_version_row(connection, demand_id, row["version_no"]) for row in connection.execute("SELECT version_no FROM expo_demand_versions WHERE demand_id=? ORDER BY version_no", (demand_id,))]

    # ---- 产品版本 ----------------------------------------------------------

    def create_product(self, context: AccessContext, *, supplier_org: str, values: Mapping[str, object], request_key: str) -> dict:
        """参展商登记产品首版本（草稿，需发布后才能用于回应需求）。"""
        context.require("write:expo-product")
        require_safe(supplier_org, "参展机构")
        shape = _validate_shape(values)
        with self.database.transaction() as connection:
            def operation() -> dict:
                product_id = new_id("product"); now = self.clock.now()
                connection.execute("INSERT INTO expo_products(product_id,supplier_org,current_version,status,created_at,created_by) VALUES(?,? ,1,'draft',?,?)", (product_id, supplier_org, now, context.actor_id))
                self._insert_product_version(connection, product_id=product_id, version_no=1, shape=shape, status="draft", actor=context.actor_id, request_key=request_key)
                self.audit.append(connection, actor_id=context.actor_id, action="expo.product_created", entity_type="expo_products", entity_id=product_id, version=1, detail={"supplier_org": supplier_org})
                return self._product_detail(connection, product_id)
            return self.idempotency.execute(connection, scope="expo:product-create", request_key=request_key, request={"supplier_org": supplier_org, "shape": shape}, operation=operation)

    def revise_product(self, context: AccessContext, product_id: str, values: Mapping[str, object], *, request_key: str) -> dict:
        """参展商修订产品：已发布的旧版本继续有效，新版本为草稿待发布。"""
        context.require("write:expo-product")
        shape = _validate_shape(values)
        with self.database.transaction() as connection:
            def operation() -> dict:
                product = self._product_row(connection, product_id)
                new_version = int(product["current_version"]) + 1
                self._insert_product_version(connection, product_id=product_id, version_no=new_version, shape=shape, status="draft", actor=context.actor_id, request_key=request_key)
                connection.execute("UPDATE expo_products SET current_version=? WHERE product_id=?", (new_version, product_id))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.product_revised", entity_type="expo_products", entity_id=product_id, version=new_version, detail={"digest": _shape_digest(shape)})
                return self._product_version_row(connection, product_id, new_version)
            return self.idempotency.execute(connection, scope=f"expo:product-revise:{product_id}", request_key=request_key, request=shape, operation=operation)

    def publish_product_version(self, context: AccessContext, product_id: str, version_no: int, *, request_key: str) -> dict:
        context.require("write:expo-product")
        with self.database.transaction() as connection:
            def operation() -> dict:
                self._product_row(connection, product_id)
                changed = connection.execute("UPDATE expo_product_versions SET status='published' WHERE product_id=? AND version_no=? AND status='draft'", (product_id, version_no)).rowcount
                if changed != 1:
                    row = connection.execute("SELECT status FROM expo_product_versions WHERE product_id=? AND version_no=?", (product_id, version_no)).fetchone()
                    if not row:
                        raise NotFoundError("产品版本不存在")
                    raise ConflictError(f"产品版本状态为 {row['status']}，不能发布")
                self.audit.append(connection, actor_id=context.actor_id, action="expo.product_published", entity_type="expo_products", entity_id=product_id, version=version_no, detail={})
                return self._product_version_row(connection, product_id, version_no)
            return self.idempotency.execute(connection, scope=f"expo:product-publish:{product_id}:{version_no}", request_key=request_key, request={}, operation=operation)

    def get_product(self, context: AccessContext, product_id: str) -> dict:
        context.require("read:expo")
        with self.database.connect() as connection:
            return self._product_detail(connection, product_id)

    # ---- 授权字段匹配 ------------------------------------------------------

    def propose_match(self, context: AccessContext, *, demand_id: str, demand_version_no: int, product_id: str, product_version_no: int) -> dict:
        """计算两个明确版本的匹配结果；只用双方授权披露且有效的字段。"""
        context.require("write:expo-match")
        with self.database.connect() as connection:
            demand = self._demand_row(connection, demand_id)
            product = self._product_row(connection, product_id)
            dver = self._demand_version_row(connection, demand_id, demand_version_no)
            pver = self._product_version_row(connection, product_id, product_version_no)
            if pver["status"] != "published":
                raise ConflictError("只能用已发布的产品版本回应需求")
            buyer_grant = self._effective_grant(connection, disclosure_subject=("demand", demand_id), audience_kind="supplier", audience_id=product["supplier_org"], counterparty_fields_hint=None)
            supplier_grant = self._effective_grant(connection, disclosure_subject=("product", product_id), audience_kind="buyer", audience_id=demand["buyer_org"], counterparty_fields_hint=None)
            return self._score_match(dver=dver, pver=pver, buyer_fields=buyer_grant["fields"], supplier_fields=supplier_grant["fields"], buyer_grant=buyer_grant, supplier_grant=supplier_grant)

    def search_demands_for_supplier(self, context: AccessContext, *, supplier_org: str, filters: Mapping[str, object] | None = None) -> list[dict]:
        """参展商按品类/数量/地区/认证/窗口检索需求，只见该供应商被授权的字段。"""
        context.require("read:expo")
        filters = filters or {}
        with self.database.connect() as connection:
            result = []
            for demand in connection.execute("SELECT * FROM expo_demands WHERE status='active'"):
                dver = self._demand_version_row(connection, demand["demand_id"], int(demand["current_version"]))
                try:
                    grant = self._effective_grant(connection, disclosure_subject=("demand", demand["demand_id"]), audience_kind="supplier", audience_id=supplier_org, counterparty_fields_hint=None)
                except PermissionDenied:
                    continue
                projected = {key: dver["shape"][key] for key in grant["fields"] if key in dver["shape"]}
                if self._passes_filters(projected, filters):
                    result.append({"demand_id": demand["demand_id"], "buyer_org": demand["buyer_org"], "version_no": dver["version_no"], "disclosed_fields": projected, "disclosure_id": grant["disclosure_id"]})
            return result

    def search_products_for_buyer(self, context: AccessContext, *, buyer_org: str, filters: Mapping[str, object] | None = None) -> list[dict]:
        """采购商检索参展商可回应的已发布产品版本，只见被授权的字段。"""
        context.require("read:expo")
        filters = filters or {}
        with self.database.connect() as connection:
            result = []
            rows = connection.execute(
                "SELECT pv.*, p.supplier_org FROM expo_product_versions pv JOIN expo_products p ON p.product_id=pv.product_id WHERE pv.status='published' AND NOT EXISTS (SELECT 1 FROM expo_product_versions newer WHERE newer.product_id=pv.product_id AND newer.version_no>pv.version_no AND newer.status='published')"
            ).fetchall()
            for row in rows:
                try:
                    grant = self._effective_grant(connection, disclosure_subject=("product", row["product_id"]), audience_kind="buyer", audience_id=buyer_org, counterparty_fields_hint=None)
                except PermissionDenied:
                    continue
                pver = self._product_version_row(connection, row["product_id"], row["version_no"])
                projected = {key: pver["shape"][key] for key in grant["fields"] if key in pver["shape"]}
                if self._passes_filters(projected, filters):
                    result.append({"product_id": row["product_id"], "supplier_org": row["supplier_org"], "version_no": pver["version_no"], "disclosed_fields": projected, "disclosure_id": grant["disclosure_id"]})
            return result

    # ---- 机会与管线份额 ----------------------------------------------------

    def create_opportunity(self, context: AccessContext, *, demand_id: str, demand_version_no: int, product_id: str, product_version_no: int, total_qty: int, request_key: str) -> dict:
        """把匹配落成机会；冻结当时双方授权快照，后续反查以此为据。"""
        context.require("write:expo-match")
        if isinstance(total_qty, bool) or not isinstance(total_qty, int) or total_qty <= 0:
            raise ValidationError("机会总量必须是正整数")
        with self.database.transaction() as connection:
            def operation() -> dict:
                demand = self._demand_row(connection, demand_id)
                product = self._product_row(connection, product_id)
                dver = self._demand_version_row(connection, demand_id, demand_version_no)
                pver = self._product_version_row(connection, product_id, product_version_no)
                if pver["status"] != "published":
                    raise ConflictError("只能用已发布的产品版本建立机会")
                buyer_grant = self._effective_grant(connection, disclosure_subject=("demand", demand_id), audience_kind="supplier", audience_id=product["supplier_org"], counterparty_fields_hint=None)
                supplier_grant = self._effective_grant(connection, disclosure_subject=("product", product_id), audience_kind="buyer", audience_id=demand["buyer_org"], counterparty_fields_hint=None)
                scored = self._score_match(dver=dver, pver=pver, buyer_fields=buyer_grant["fields"], supplier_fields=supplier_grant["fields"], buyer_grant=buyer_grant, supplier_grant=supplier_grant)
                if scored["incompatible_fields"]:
                    raise ConflictError("授权字段存在不相容项: " + ", ".join(scored["incompatible_fields"]))
                opportunity_id = new_id("opp"); now = self.clock.now()
                grants = {
                    "buyer": {"disclosure_id": buyer_grant["disclosure_id"], "version": buyer_grant["version"], "fields": buyer_grant["fields"], "digest": buyer_grant["digest"]},
                    "supplier": {"disclosure_id": supplier_grant["disclosure_id"], "version": supplier_grant["version"], "fields": supplier_grant["fields"], "digest": supplier_grant["digest"]},
                }
                connection.execute(
                    "INSERT INTO expo_opportunities(opportunity_id,demand_id,demand_version_no,product_id,product_version_no,total_qty,fulfilled_qty,remaining_qty,current_stage,stage_version,status,match_grants_json,created_at,created_by) VALUES(?,?,?,?,?,?,0,?,NULL,0,'open',?,?,?)",
                    (opportunity_id, demand_id, demand_version_no, product_id, product_version_no, total_qty, total_qty, canonical_json(grants), now, context.actor_id),
                )
                self.audit.append(connection, actor_id=context.actor_id, action="expo.opportunity_created", entity_type="expo_opportunities", entity_id=opportunity_id, version=0, detail={"demand_version": demand_version_no, "product_version": product_version_no, "grants": grants})
                return self._opportunity_row(connection, opportunity_id)
            return self.idempotency.execute(connection, scope="expo:opportunity-create", request_key=request_key, request={"demand": demand_id, "dv": demand_version_no, "product": product_id, "pv": product_version_no, "qty": total_qty}, operation=operation)

    def advance_stage(self, context: AccessContext, opportunity_id: str, *, stage: str, quantity: int, owner_id: str, receipt_key: str, request_key: str, note: str = "", sample_expires_at: str | None = None, followup_at: str | None = None, scheduler: JobQueue | None = None, notifier: Outbox | None = None) -> dict:
        """责任人持业务回执推进一个份额。同一回执重复到达不再次推进。

        部分成交（数量小于剩余）只扣减剩余机会，不关闭机会；
        数量等于剩余时机会成交关闭；exited 为显式退出并关闭剩余机会。
        """
        context.require("write:expo-pipeline")
        require_safe(owner_id, "责任人"); require_safe(receipt_key, "业务回执标识")
        exiting = stage == EXIT_STAGE
        if not exiting:
            if stage not in STAGES:
                raise ValidationError(f"未知阶段: {stage}")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError("推进数量必须是正整数")
        due_at = None
        if stage == "sample":
            if not sample_expires_at:
                raise ValidationError("进入样品阶段必须给出样品期限")
            due_at = canonical_instant(sample_expires_at)
        if stage == "contract" and followup_at:
            due_at = canonical_instant(followup_at)
        with self.database.transaction() as connection:
            def operation() -> dict:
                opp = self._opportunity_row(connection, opportunity_id)
                if opp["status"] != "open":
                    raise ConflictError(f"机会已{opp['status']}，不能继续推进")
                dup = connection.execute("SELECT * FROM expo_pipeline_records WHERE receipt_key=?", (receipt_key,)).fetchone()
                if dup:
                    incoming = digest_json({"stage": stage, "quantity": quantity, "owner_id": owner_id, "note": note})
                    if dup["payload_digest"] != incoming:
                        self._record_conflict(connection, kind="receipt", subject_key=opportunity_id, intake_key=receipt_key, existing=dup["payload_digest"], incoming=incoming)
                        return {"status": "conflict", "message": "回执标识相同但内容冲突，已转主管核对"}
                    return {"status": "duplicate", "record_id": dup["record_id"]}
                if not exiting:
                    current_idx = -1 if opp["current_stage"] is None else STAGE_INDEX[opp["current_stage"]]
                    if STAGE_INDEX[stage] < current_idx:
                        raise ConflictError("阶段不能回退")
                    if quantity > int(opp["remaining_qty"]):
                        raise ConflictError("推进数量超过剩余机会数量")
                record_id = new_id("rec"); now = self.clock.now(); stage_no = int(opp["stage_version"]) + 1
                payload = {"stage": stage, "quantity": 0 if exiting else quantity, "owner_id": owner_id, "note": note.strip()}
                connection.execute(
                    "INSERT INTO expo_pipeline_records(record_id,opportunity_id,stage_no,stage,quantity,owner_id,note,receipt_key,payload_digest,due_at,job_id,recorded_at,recorded_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, opportunity_id, stage_no, stage, 0 if exiting else quantity, owner_id, note.strip(), receipt_key, digest_json(payload), due_at, None, now, context.actor_id),
                )
                new_status = "open"
                fulfilled = int(opp["fulfilled_qty"]); remaining = int(opp["remaining_qty"])
                if exiting:
                    new_status = "exited"
                else:
                    if stage == "contract":
                        fulfilled += quantity; remaining -= quantity
                        if remaining == 0:
                            new_status = "fulfilled"
                connection.execute(
                    "UPDATE expo_opportunities SET current_stage=?,stage_version=?,fulfilled_qty=?,remaining_qty=?,status=? WHERE opportunity_id=?",
                    (stage, stage_no, fulfilled, remaining, new_status, opportunity_id),
                )
                self.audit.append(connection, actor_id=context.actor_id, action="expo.stage_advanced", entity_type="expo_opportunities", entity_id=opportunity_id, version=stage_no, detail={"record_id": record_id, **payload, "remaining_qty": remaining, "status": new_status})
                job_id = None
                if due_at and scheduler is not None:
                    job_type = SAMPLE_JOB if stage == "sample" else FOLLOWUP_JOB
                    job_id = new_id("job")
                    JobQueue.insert(connection, job_id=job_id, job_type=job_type, subject_id=opportunity_id, run_at=due_at, payload={"opportunity_id": opportunity_id, "record_id": record_id, "owner_id": owner_id})
                    connection.execute("UPDATE expo_pipeline_records SET job_id=? WHERE record_id=?", (job_id, record_id))
                if notifier is not None:
                    Outbox.insert(connection, message_id=new_id("msg"), topic="expo.stage_advanced", aggregate_id=opportunity_id, payload={"opportunity_id": opportunity_id, "stage": stage, "record_id": record_id, "owner_id": owner_id}, available_at=now)
                return {"status": "accepted", "record": {"record_id": record_id, "stage": stage, "quantity": 0 if exiting else quantity, "owner_id": owner_id, "due_at": due_at, "job_id": job_id}, "opportunity": self._opportunity_row(connection, opportunity_id), "job_id": job_id}
            result = self.idempotency.execute(connection, scope=f"expo:advance:{opportunity_id}", request_key=request_key, request={"receipt": receipt_key, "stage": stage, "quantity": quantity, "owner": owner_id, "note": note, "due_at": due_at}, operation=operation)
        if result.get("status") == "conflict":
            raise ConflictError(result["message"])
        return result

    def list_receipt_conflicts(self, context: AccessContext, *, kind: str | None = None) -> list[dict]:
        context.require("read:expo")
        sql = "SELECT * FROM expo_intake_conflicts"; params: list[object] = []
        if kind:
            sql += " WHERE kind=?"; params.append(kind)
        sql += " ORDER BY conflict_id"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def resolve_conflict(self, context: AccessContext, conflict_id: int, *, resolution: str, request_key: str) -> dict:
        """主管核对线索或回执冲突并记录处理结论。"""
        context.require("write:expo-supervisor")
        if not resolution.strip():
            raise ValidationError("核对结论不能为空")
        with self.database.transaction() as connection:
            def operation() -> dict:
                row = connection.execute("SELECT * FROM expo_intake_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
                if not row:
                    raise NotFoundError("冲突记录不存在")
                if row["status"] != "open":
                    raise ConflictError("冲突已经核对")
                now = self.clock.now()
                connection.execute("UPDATE expo_intake_conflicts SET status='resolved',resolved_by=?,resolved_at=?,resolution=? WHERE conflict_id=?", (context.actor_id, now, resolution.strip(), conflict_id))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.conflict_resolved", entity_type="expo_intake_conflicts", entity_id=str(conflict_id), version=1, detail={"kind": row["kind"], "resolution": resolution.strip()})
                return {"conflict_id": conflict_id, "status": "resolved", "resolved_by": context.actor_id, "resolved_at": now}
            return self.idempotency.execute(connection, scope=f"expo:conflict-resolve:{conflict_id}", request_key=request_key, request={"resolution": resolution.strip()}, operation=operation)

    # ---- 会谈预约与双签纪要 ------------------------------------------------

    def schedule_meeting(self, context: AccessContext, opportunity_id: str, *, start_at: str, end_at: str, venue_resource_id: str, venue_capacity: int, personnel: Iterable[str], request_key: str) -> dict:
        """预约会谈：在同一事务内同时占用场地与每名人员，任一冲突即失败。"""
        context.require("write:expo-meeting")
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        if parse_instant(start_at) >= parse_instant(end_at):
            raise ValidationError("会谈结束时间必须晚于开始时间")
        persons = list(dict.fromkeys(personnel))
        if not persons:
            raise ValidationError("会谈至少占用一名人员")
        require_safe(venue_resource_id, "场地资源")
        with self.database.transaction() as connection:
            def operation() -> dict:
                self._opportunity_row(connection, opportunity_id)
                meeting_id = new_id("mtg"); now = self.clock.now()
                venue_reservation = new_id("reservation")
                self._hold_resource(connection, reservation_id=venue_reservation, resource_id=venue_resource_id, quantity=1, capacity=venue_capacity, start_at=start_at, end_at=end_at, subject_id=meeting_id, actor=context.actor_id)
                person_reservations = []
                for person in persons:
                    rid = new_id("reservation")
                    self._hold_resource(connection, reservation_id=rid, resource_id=person, quantity=1, capacity=1, start_at=start_at, end_at=end_at, subject_id=meeting_id, actor=context.actor_id)
                    person_reservations.append({"person": person, "reservation_id": rid})
                connection.execute(
                    "INSERT INTO expo_meetings(meeting_id,opportunity_id,start_at,end_at,venue_resource_id,personnel_json,venue_reservation_id,personnel_reservations_json,status,created_at,created_by) VALUES(?,?,?,?,?,?,?,?, 'requested',?,?)",
                    (meeting_id, opportunity_id, start_at, end_at, venue_resource_id, canonical_json(persons), venue_reservation, canonical_json(person_reservations), now, context.actor_id),
                )
                self.audit.append(connection, actor_id=context.actor_id, action="expo.meeting_scheduled", entity_type="expo_meetings", entity_id=meeting_id, version=1, detail={"venue": venue_resource_id, "personnel": persons})
                return self._meeting_row(connection, meeting_id)
            return self.idempotency.execute(connection, scope="expo:meeting-schedule", request_key=request_key, request={"opportunity": opportunity_id, "start": start_at, "end": end_at, "venue": venue_resource_id, "personnel": persons}, operation=operation)

    def confirm_meeting(self, context: AccessContext, meeting_id: str, party: str, *, request_key: str) -> dict:
        """采购商/参展商各自确认预约，双方确认后会谈成立。"""
        context.require("write:expo-meeting")
        party = self._party(party)
        with self.database.transaction() as connection:
            def operation() -> dict:
                row = connection.execute("SELECT * FROM expo_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
                if not row:
                    raise NotFoundError("会谈不存在")
                confirmed = {"buyer": bool(row["buyer_confirmed"]), "supplier": bool(row["supplier_confirmed"])}
                if row["status"] == "cancelled":
                    raise ConflictError("会谈已取消")
                if confirmed[party]:
                    return self._meeting_row(connection, meeting_id)
                column = "buyer_confirmed" if party == "buyer" else "supplier_confirmed"
                connection.execute(f"UPDATE expo_meetings SET {column}=1 WHERE meeting_id=?", (meeting_id,))
                confirmed[party] = True
                if all(confirmed.values()):
                    connection.execute("UPDATE expo_meetings SET status='confirmed' WHERE meeting_id=?", (meeting_id,))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.meeting_confirmed", entity_type="expo_meetings", entity_id=meeting_id, version=1, detail={"party": party})
                return self._meeting_row(connection, meeting_id)
            return self.idempotency.execute(connection, scope=f"expo:meeting-confirm:{meeting_id}:{party}", request_key=request_key, request={}, operation=operation)

    def cancel_meeting(self, context: AccessContext, meeting_id: str, *, reason: str, request_key: str) -> dict:
        context.require("write:expo-meeting")
        if not reason.strip():
            raise ValidationError("取消会谈必须说明原因")
        with self.database.transaction() as connection:
            def operation() -> dict:
                row = connection.execute("SELECT * FROM expo_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
                if not row:
                    raise NotFoundError("会谈不存在")
                if row["status"] == "cancelled":
                    return self._meeting_row(connection, meeting_id)
                for rid in [row["venue_reservation_id"], *[item["reservation_id"] for item in _loads(row["personnel_reservations_json"])]]:
                    connection.execute("UPDATE resource_reservations SET status='released',version=version+1 WHERE reservation_id=? AND status IN ('held','confirmed')", (rid,))
                connection.execute("UPDATE expo_meetings SET status='cancelled' WHERE meeting_id=?", (meeting_id,))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.meeting_cancelled", entity_type="expo_meetings", entity_id=meeting_id, version=1, detail={"reason": reason.strip()})
                return self._meeting_row(connection, meeting_id)
            return self.idempotency.execute(connection, scope=f"expo:meeting-cancel:{meeting_id}", request_key=request_key, request={"reason": reason.strip()}, operation=operation)

    def draft_minutes(self, context: AccessContext, meeting_id: str, *, content: Mapping[str, object], scope_summary: str, proposed_by: str, request_key: str) -> dict:
        """起草首轮纪要；内容为结构化对象，范围摘由人工给出。"""
        context.require("write:expo-meeting")
        if not isinstance(content, Mapping) or not content:
            raise ValidationError("纪要内容不能为空")
        if not scope_summary.strip():
            raise ValidationError("范围摘要不能为空")
        party = self._party(proposed_by)
        with self.database.transaction() as connection:
            def operation() -> dict:
                meeting = connection.execute("SELECT * FROM expo_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
                if not meeting:
                    raise NotFoundError("会谈不存在")
                if meeting["status"] != "confirmed":
                    raise ConflictError("会谈未双方确认，不能生成纪要")
                if connection.execute("SELECT 1 FROM expo_minutes WHERE meeting_id=?", (meeting_id,)).fetchone():
                    raise ConflictError("首轮纪要已存在，改变范围请使用 amend_minutes")
                minutes_id = new_id("min"); now = self.clock.now()
                connection.execute(
                    "INSERT INTO expo_minutes(minutes_id,meeting_id,minutes_no,content_json,scope_summary,status,buyer_confirmed,supplier_confirmed,created_at,proposed_by,confirmed_at) VALUES(?,?,?,? ,?,'draft',0,0,?,? ,NULL)",
                    (minutes_id, meeting_id, 1, canonical_json(dict(content)), scope_summary.strip(), now, party),
                )
                self.audit.append(connection, actor_id=context.actor_id, action="expo.minutes_drafted", entity_type="expo_minutes", entity_id=minutes_id, version=1, detail={"meeting_id": meeting_id, "scope": scope_summary.strip()})
                return self._minutes_row(connection, minutes_id)
            return self.idempotency.execute(connection, scope=f"expo:minutes-draft:{meeting_id}", request_key=request_key, request={"content": dict(content), "scope": scope_summary.strip()}, operation=operation)

    def amend_minutes(self, context: AccessContext, meeting_id: str, *, content: Mapping[str, object], scope_summary: str, proposed_by: str, request_key: str) -> dict:
        """一方改变范围：旧纪要原样保留，新版本为待双方确认的草稿。"""
        context.require("write:expo-meeting")
        if not isinstance(content, Mapping) or not content:
            raise ValidationError("纪要内容不能为空")
        if not scope_summary.strip():
            raise ValidationError("范围摘要不能为空")
        party = self._party(proposed_by)
        with self.database.transaction() as connection:
            def operation() -> dict:
                last = connection.execute("SELECT * FROM expo_minutes WHERE meeting_id=? ORDER BY minutes_no DESC LIMIT 1", (meeting_id,)).fetchone()
                if not last:
                    raise NotFoundError("请先起草首轮纪要")
                if last["status"] == "draft":
                    raise ConflictError("上一版本纪要尚未确认，不能再改范围")
                minutes_id = new_id("min"); now = self.clock.now(); number = int(last["minutes_no"]) + 1
                connection.execute("UPDATE expo_minutes SET status='superseded' WHERE meeting_id=? AND minutes_no=?", (meeting_id, last["minutes_no"]))
                connection.execute(
                    "INSERT INTO expo_minutes(minutes_id,meeting_id,minutes_no,content_json,scope_summary,status,buyer_confirmed,supplier_confirmed,created_at,proposed_by,confirmed_at) VALUES(?,?,?,? ,?,'draft',0,0,?,? ,NULL)",
                    (minutes_id, meeting_id, number, canonical_json(dict(content)), scope_summary.strip(), now, party),
                )
                self.audit.append(connection, actor_id=context.actor_id, action="expo.minutes_amended", entity_type="expo_minutes", entity_id=minutes_id, version=number, detail={"meeting_id": meeting_id, "supersedes": last["minutes_no"], "scope": scope_summary.strip()})
                return self._minutes_row(connection, minutes_id)
            return self.idempotency.execute(connection, scope=f"expo:minutes-amend:{meeting_id}", request_key=request_key, request={"content": dict(content), "scope": scope_summary.strip()}, operation=operation)

    def confirm_minutes(self, context: AccessContext, meeting_id: str, party: str, *, request_key: str, notifier: Outbox | None = None) -> dict:
        """双方分别确认纪要；两方都确认后版本生效。"""
        context.require("write:expo-meeting")
        party = self._party(party)
        with self.database.transaction() as connection:
            def operation() -> dict:
                row = connection.execute("SELECT * FROM expo_minutes WHERE meeting_id=? ORDER BY minutes_no DESC LIMIT 1", (meeting_id,)).fetchone()
                if not row:
                    raise NotFoundError("纪要不存在")
                if row["status"] == "confirmed":
                    return self._minutes_row(connection, row["minutes_id"])  # 重复确认幂等
                if row["status"] != "draft":
                    raise ConflictError("该版本纪要不是待确认状态")
                column = "buyer_confirmed" if party == "buyer" else "supplier_confirmed"
                if bool(row[column]):
                    return self._minutes_row(connection, row["minutes_id"])
                connection.execute(f"UPDATE expo_minutes SET {column}=1 WHERE minutes_id=?", (row["minutes_id"],))
                updated = connection.execute("SELECT * FROM expo_minutes WHERE minutes_id=?", (row["minutes_id"],)).fetchone()
                fully = bool(updated["buyer_confirmed"]) and bool(updated["supplier_confirmed"])
                now = self.clock.now()
                if fully:
                    connection.execute("UPDATE expo_minutes SET status='confirmed',confirmed_at=? WHERE minutes_id=?", (now, row["minutes_id"]))
                self.audit.append(connection, actor_id=context.actor_id, action="expo.minutes_confirmed", entity_type="expo_minutes", entity_id=row["minutes_id"], version=int(row["minutes_no"]), detail={"party": party, "fully_confirmed": fully})
                if notifier is not None:
                    Outbox.insert(connection, message_id=new_id("msg"), topic="expo.minutes_confirmed" if fully else "expo.minutes_party_confirmed", aggregate_id=meeting_id, payload={"meeting_id": meeting_id, "minutes_no": row["minutes_no"], "party": party, "fully_confirmed": fully}, available_at=now)
                return self._minutes_row(connection, row["minutes_id"])
            return self.idempotency.execute(connection, scope=f"expo:minutes-confirm:{meeting_id}:{party}", request_key=request_key, request={"minutes_meeting": meeting_id}, operation=operation)

    def minutes_history(self, context: AccessContext, meeting_id: str) -> list[dict]:
        context.require("read:expo")
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM expo_minutes WHERE meeting_id=? ORDER BY minutes_no", (meeting_id,)).fetchall()
            if not rows:
                raise NotFoundError("纪要不存在")
            return [self._minutes_row(connection, row["minutes_id"]) for row in rows]

    # ---- 恢复与溯源 --------------------------------------------------------

    def worklist(self, context: AccessContext) -> dict:
        """中断恢复后的接续清单：未确认纪要、样品期限、履约回访。"""
        context.require("read:expo")
        now = self.clock.now()
        with self.database.connect() as connection:
            pending_minutes = [
                self._minutes_row(connection, row["minutes_id"])
                for row in connection.execute("SELECT minutes_id FROM expo_minutes WHERE status='draft' ORDER BY created_at,minutes_id")
            ]
            samples: list[dict] = []
            followups: list[dict] = []
            records = connection.execute(
                "SELECT r.*, o.status AS opp_status, o.current_stage, o.fulfilled_qty FROM expo_pipeline_records r JOIN expo_opportunities o ON o.opportunity_id=r.opportunity_id WHERE r.due_at IS NOT NULL ORDER BY r.due_at"
            ).fetchall()
            for record in records:
                job_status = None
                if record["job_id"]:
                    job_row = connection.execute("SELECT status,attempt,last_error FROM scheduled_jobs WHERE job_id=?", (record["job_id"],)).fetchone()
                    job_status = dict(job_row) if job_row else None
                done = job_status is not None and job_status["status"] == "succeeded"
                due_now = parse_instant(record["due_at"]) <= parse_instant(now)
                item = {"opportunity_id": record["opportunity_id"], "record_id": record["record_id"], "owner_id": record["owner_id"], "due_at": record["due_at"], "due_now": due_now, "needs_action": due_now and not done, "job": job_status}
                if record["stage"] == "sample" and record["opp_status"] != "exited":
                    samples.append(item)
                elif record["stage"] == "contract" and record["opp_status"] != "exited" and int(record["fulfilled_qty"]) > 0:
                    followups.append(item)
            return {"as_of": now, "pending_minutes": pending_minutes, "sample_deadlines": samples, "fulfillment_followups": followups}

    def trace(self, context: AccessContext, opportunity_id: str) -> dict:
        """从任一成交（机会）反查需求版本、产品版本、授权、会谈、纪要与责任交接。"""
        context.require("read:expo")
        with self.database.connect() as connection:
            opp = self._opportunity_row(connection, opportunity_id)
            demand = self._demand_detail(connection, opp["demand_id"])
            product = self._product_detail(connection, opp["product_id"])
            dver = self._demand_version_row(connection, opp["demand_id"], opp["demand_version_no"])
            pver = self._product_version_row(connection, opp["product_id"], opp["product_version_no"])
            records = [dict(row) for row in connection.execute("SELECT record_id,stage_no,stage,quantity,owner_id,note,receipt_key,due_at,job_id,recorded_at,recorded_by FROM expo_pipeline_records WHERE opportunity_id=? ORDER BY stage_no", (opportunity_id,))]
            meetings = []
            for mrow in connection.execute("SELECT meeting_id FROM expo_meetings WHERE opportunity_id=? ORDER BY start_at", (opportunity_id,)):
                meeting = self._meeting_row(connection, mrow["meeting_id"])
                meeting["minutes"] = [self._minutes_row(connection, row["minutes_id"]) for row in connection.execute("SELECT minutes_id FROM expo_minutes WHERE meeting_id=? ORDER BY minutes_no", (mrow["meeting_id"],))]
                meetings.append(meeting)
            sources = [dict(row) for row in connection.execute("SELECT source,source_ref,payload_digest,attached_at FROM expo_lead_sources WHERE demand_id=? ORDER BY attached_at,source", (opp["demand_id"],))]
            return {
                "opportunity": opp,
                "demand_identity": demand["demand_identity"],
                "sources": sources,
                "demand_version": dver,
                "product_version": pver,
                "demand_versions": demand["versions"],
                "product_versions": product["versions"],
                "match_grants": opp["match_grants"],
                "pipeline": records,
                "stage_owners": [{"stage": row["stage"], "owner_id": row["owner_id"], "record_id": row["record_id"], "recorded_at": row["recorded_at"]} for row in records],
                "meetings": meetings,
            }

    # ---- 内部辅助 ----------------------------------------------------------

    @staticmethod
    def _party(party: str) -> str:
        if party not in ("buyer", "supplier"):
            raise ValidationError("会谈方必须是 buyer 或 supplier")
        return party

    def _demand_row(self, connection, demand_id: str):
        row = connection.execute("SELECT * FROM expo_demands WHERE demand_id=?", (demand_id,)).fetchone()
        if not row:
            raise NotFoundError("需求不存在")
        return row

    def _product_row(self, connection, product_id: str):
        row = connection.execute("SELECT * FROM expo_products WHERE product_id=?", (product_id,)).fetchone()
        if not row:
            raise NotFoundError("产品不存在")
        return row

    def _opportunity_row(self, connection, opportunity_id: str) -> dict:
        row = connection.execute("SELECT * FROM expo_opportunities WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        if not row:
            raise NotFoundError("供需机会不存在")
        result = dict(row); result["match_grants"] = _loads(row["match_grants_json"]); result.pop("match_grants_json")
        return result

    def _insert_demand_version(self, connection, *, demand_id: str, version_no: int, shape: Mapping[str, object], actor: str, request_key: str) -> None:
        connection.execute(
            "INSERT INTO expo_demand_versions(demand_id,version_no,category,quantity_min,quantity_max,delivery_regions_json,certifications_json,window_from,window_to,digest,created_at,created_by,request_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (demand_id, version_no, shape["category"], shape["quantity_min"], shape["quantity_max"], canonical_json(shape["delivery_regions"]), canonical_json(shape["certifications"]), shape["window_from"], shape["window_to"], _shape_digest(shape), self.clock.now(), actor, request_key),
        )

    def _insert_product_version(self, connection, *, product_id: str, version_no: int, shape: Mapping[str, object], status: str, actor: str, request_key: str) -> None:
        connection.execute(
            "INSERT INTO expo_product_versions(product_id,version_no,category,qty_min,qty_max,delivery_regions_json,certifications_json,window_from,window_to,digest,status,created_at,created_by,request_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, version_no, shape["category"], shape["quantity_min"], shape["quantity_max"], canonical_json(shape["delivery_regions"]), canonical_json(shape["certifications"]), shape["window_from"], shape["window_to"], _shape_digest(shape), status, self.clock.now(), actor, request_key),
        )

    def _demand_version_row(self, connection, demand_id: str, version_no: int) -> dict:
        row = connection.execute("SELECT * FROM expo_demand_versions WHERE demand_id=? AND version_no=?", (demand_id, version_no)).fetchone()
        if not row:
            raise NotFoundError("需求版本不存在")
        return {"demand_id": demand_id, "version_no": version_no, "digest": row["digest"], "created_at": row["created_at"], "created_by": row["created_by"], "shape": {
            FIELD_CATEGORY: row["category"],
            FIELD_QUANTITY: [row["quantity_min"], row["quantity_max"]],
            FIELD_REGIONS: _loads(row["delivery_regions_json"]),
            FIELD_CERTS: _loads(row["certifications_json"]),
            FIELD_WINDOW: [row["window_from"], row["window_to"]],
        }}

    def _product_version_row(self, connection, product_id: str, version_no: int) -> dict:
        row = connection.execute("SELECT * FROM expo_product_versions WHERE product_id=? AND version_no=?", (product_id, version_no)).fetchone()
        if not row:
            raise NotFoundError("产品版本不存在")
        return {"product_id": product_id, "version_no": version_no, "status": row["status"], "digest": row["digest"], "created_at": row["created_at"], "created_by": row["created_by"], "shape": {
            FIELD_CATEGORY: row["category"],
            FIELD_QUANTITY: [row["qty_min"], row["qty_max"]],
            FIELD_REGIONS: _loads(row["delivery_regions_json"]),
            FIELD_CERTS: _loads(row["certifications_json"]),
            FIELD_WINDOW: [row["window_from"], row["window_to"]],
        }}

    def _demand_detail(self, connection, demand_id: str) -> dict:
        row = self._demand_row(connection, demand_id)
        versions = [self._demand_version_row(connection, demand_id, r["version_no"]) for r in connection.execute("SELECT version_no FROM expo_demand_versions WHERE demand_id=? ORDER BY version_no", (demand_id,))]
        sources = [dict(r) for r in connection.execute("SELECT source,source_ref,payload_digest,first_seen_at FROM expo_lead_registry WHERE demand_identity=? ORDER BY first_seen_at,source", (row["demand_identity"],))]
        attached = [dict(r) for r in connection.execute("SELECT source,source_ref,payload_digest,attached_at FROM expo_lead_sources WHERE demand_id=? ORDER BY attached_at,source", (demand_id,))]
        return {"demand_id": demand_id, "demand_identity": row["demand_identity"], "buyer_org": row["buyer_org"], "current_version": row["current_version"], "status": row["status"], "current": versions[-1] if versions else None, "versions": versions, "sources": sources, "attached_sources": attached}

    def _product_detail(self, connection, product_id: str) -> dict:
        row = self._product_row(connection, product_id)
        versions = [self._product_version_row(connection, product_id, r["version_no"]) for r in connection.execute("SELECT version_no FROM expo_product_versions WHERE product_id=? ORDER BY version_no", (product_id,))]
        return {"product_id": product_id, "supplier_org": row["supplier_org"], "current_version": row["current_version"], "status": row["status"], "current": versions[-1] if versions else None, "versions": versions}

    def _attach_source(self, connection, *, demand_id: str, identity: str, source: str, source_ref: str) -> dict:
        require_safe(source, "线索来源"); require_safe(source_ref, "来源标识")
        reg = connection.execute("SELECT payload_digest FROM expo_lead_registry WHERE demand_identity=? AND source=? AND source_ref=?", (identity, source, source_ref)).fetchone()
        if not reg:
            raise NotFoundError(f"渠道线索 {source}/{source_ref} 未登记到该需求身份")
        if connection.execute("SELECT 1 FROM expo_lead_sources WHERE demand_id=? AND source=? AND source_ref=?", (demand_id, source, source_ref)).fetchone():
            return {"status": "duplicate", "demand_id": demand_id, "source": source, "source_ref": source_ref}
        connection.execute("INSERT INTO expo_lead_sources(demand_id,source,source_ref,payload_digest,attached_at) VALUES(?,?,?,?,?)", (demand_id, source, source_ref, reg["payload_digest"], self.clock.now()))
        return {"status": "attached", "demand_id": demand_id, "source": source, "source_ref": source_ref}

    def _hold_resource(self, connection, *, reservation_id: str, resource_id: str, quantity: int, capacity: int, start_at: str, end_at: str, subject_id: str, actor: str) -> None:
        if capacity <= 0 or quantity <= 0 or quantity > capacity:
            raise ValidationError("预约数量或容量不合法")
        row = connection.execute("SELECT COALESCE(SUM(quantity),0) AS used FROM resource_reservations WHERE resource_id=? AND status IN ('held','confirmed') AND start_at<? AND end_at>?", (resource_id, end_at, start_at)).fetchone()
        if int(row["used"]) + quantity > capacity:
            raise ConflictError(f"资源 {resource_id} 在该时段不可用")
        connection.execute("INSERT INTO resource_reservations(reservation_id,resource_id,subject_id,quantity,start_at,end_at,status,version,created_by) VALUES(?,?,?,?,?,?, 'confirmed',1,?)", (reservation_id, resource_id, subject_id, quantity, start_at, end_at, actor))

    def _effective_grant(self, connection, *, disclosure_subject: tuple[str, str], audience_kind: str, audience_id: str, counterparty_fields_hint) -> dict:
        """读取针对某主体最新已发布、在有效期、受众匹配的披露授权。"""
        subject_type, subject_id = disclosure_subject
        rows = connection.execute(
            "SELECT e.entity_id,e.version,e.payload_json FROM entities e WHERE e.entity_type='disclosures' AND e.state='published' AND json_extract(e.payload_json,'$.subject_type')=? AND json_extract(e.payload_json,'$.subject_id')=? ORDER BY e.updated_at DESC,e.entity_id",
            (subject_type, subject_id),
        ).fetchall()
        now = parse_instant(self.clock.now())
        for row in rows:
            payload = _loads(row["payload_json"])
            valid_until = payload.get("valid_until")
            if valid_until and parse_instant(valid_until) < now:
                continue
            audience = str(payload.get("audience", ""))
            if audience != f"{audience_kind}:*" and audience != f"{audience_kind}:{audience_id}":
                continue
            fields = payload.get("fields")
            if not isinstance(fields, list) or any(field not in MATCH_FIELDS for field in fields):
                raise ValidationError("披露授权字段不合法")
            return {"disclosure_id": row["entity_id"], "version": row["version"], "fields": fields, "digest": digest_json({"fields": fields, "audience": audience, "valid_until": valid_until})}
        raise PermissionDenied(f"缺少针对 {audience_kind}:{audience_id} 的有效披露授权")

    def _score_match(self, *, dver: dict, pver: dict, buyer_fields: list[str], supplier_fields: list[str], buyer_grant: dict, supplier_grant: dict) -> dict:
        dshape = dver["shape"]; pshape = pver["shape"]
        usable = sorted(set(buyer_fields) & set(supplier_fields))
        compatible: list[str] = []; incompatible: list[str] = []
        checks = {
            FIELD_CATEGORY: lambda: dshape[FIELD_CATEGORY] == pshape[FIELD_CATEGORY],
            FIELD_QUANTITY: lambda: _intervals_overlap(dshape[FIELD_QUANTITY], pshape[FIELD_QUANTITY]),
            FIELD_REGIONS: lambda: bool(set(dshape[FIELD_REGIONS]) & set(pshape[FIELD_REGIONS])),
            FIELD_CERTS: lambda: set(dshape[FIELD_CERTS]).issubset(set(pshape[FIELD_CERTS])),
            FIELD_WINDOW: lambda: _windows_overlap(dshape[FIELD_WINDOW], pshape[FIELD_WINDOW]),
        }
        for field in usable:
            (compatible if checks[field]() else incompatible).append(field)
        return {
            "demand_version": dver["version_no"],
            "product_version": pver["version_no"],
            "usable_fields": usable,
            "compatible_fields": compatible,
            "incompatible_fields": incompatible,
            "buyer_authorized_fields": buyer_fields,
            "supplier_authorized_fields": supplier_fields,
            "grants": {"buyer": {"disclosure_id": buyer_grant["disclosure_id"], "version": buyer_grant["version"]}, "supplier": {"disclosure_id": supplier_grant["disclosure_id"], "version": supplier_grant["version"]}},
        }

    @staticmethod
    def _passes_filters(projected: Mapping[str, object], filters: Mapping[str, object]) -> bool:
        if not projected:
            return False
        if "category" in filters and projected.get(FIELD_CATEGORY) != filters["category"]:
            return False
        if "delivery_region" in filters and filters["delivery_region"] not in projected.get(FIELD_REGIONS, []):
            return False
        if "certification" in filters and filters["certification"] not in projected.get(FIELD_CERTS, []):
            return False
        qty = filters.get("quantity")
        if qty is not None:
            interval = projected.get(FIELD_QUANTITY)
            if not interval or not (interval[0] <= int(qty) <= interval[1]):
                return False
        window = filters.get("window_at")
        if window is not None:
            interval = projected.get(FIELD_WINDOW)
            instant = parse_instant(window)  # type: ignore[arg-type]
            if not interval or not (parse_instant(interval[0]) <= instant < parse_instant(interval[1])):
                return False
        return True

    def _meeting_row(self, connection, meeting_id: str) -> dict:
        row = connection.execute("SELECT * FROM expo_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
        if not row:
            raise NotFoundError("会谈不存在")
        return {"meeting_id": meeting_id, "opportunity_id": row["opportunity_id"], "start_at": row["start_at"], "end_at": row["end_at"], "venue_resource_id": row["venue_resource_id"], "personnel": _loads(row["personnel_json"]), "venue_reservation_id": row["venue_reservation_id"], "personnel_reservations": _loads(row["personnel_reservations_json"]), "status": row["status"], "buyer_confirmed": bool(row["buyer_confirmed"]), "supplier_confirmed": bool(row["supplier_confirmed"]), "created_at": row["created_at"]}

    def _minutes_row(self, connection, minutes_id: str) -> dict:
        row = connection.execute("SELECT * FROM expo_minutes WHERE minutes_id=?", (minutes_id,)).fetchone()
        if not row:
            raise NotFoundError("纪要不存在")
        return {"minutes_id": minutes_id, "meeting_id": row["meeting_id"], "minutes_no": row["minutes_no"], "content": _loads(row["content_json"]), "scope_summary": row["scope_summary"], "status": row["status"], "buyer_confirmed": bool(row["buyer_confirmed"]), "supplier_confirmed": bool(row["supplier_confirmed"]), "proposed_by": row["proposed_by"], "created_at": row["created_at"], "confirmed_at": row["confirmed_at"]}

    def _record_conflict(self, connection, *, kind: str, subject_key: str, intake_key: str, existing: str, incoming: str) -> None:
        connection.execute("INSERT INTO expo_intake_conflicts(kind,subject_key,intake_key,existing_digest,incoming_digest,received_at) VALUES(?,?,?,?,?,?)", (kind, subject_key, intake_key, existing, incoming, self.clock.now()))
