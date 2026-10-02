"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .disclosures import DisclosureService
from .errors import ConflictError
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def _publish_grant(app: CivicFlow, context: AccessContext, *, subject_type: str, subject_id: str, audience: str, fields: list[str], request_key: str) -> str:
    """建立并发布一条字段披露授权（draft→approved→published）。"""
    service = DisclosureService(app.repository)
    try:
        row = service.create(context, {"subject_type": subject_type, "subject_id": subject_id, "audience": audience, "fields": fields, "valid_until": "2027-12-31T23:59:59+08:00"}, request_key=f"{request_key}:create")
    except ConflictError:
        # 演示重放：按主体找回此前已建立并发布的授权
        existing = [item for item in service.find_by_subject_id(subject_id) if item["audience"] == audience]
        if not existing:
            raise
        return max(existing, key=lambda item: item["version"])["entity_id"]
    # 重放时幂等缓存返回的是创建时快照，以库内最新状态决定还要走几步
    current = service.get(context, row["entity_id"])
    if current["state"] == "draft":
        current = service.transition(context, current["entity_id"], "approved", expected_version=current["version"], reason="展后专班授权", request_key=f"{request_key}:approved")
    if current["state"] == "approved":
        current = service.transition(context, current["entity_id"], "published", expected_version=current["version"], reason="展后专班授权", request_key=f"{request_key}:published")
    return current["entity_id"]


def expo_demo(app: CivicFlow) -> dict:
    """路演供需意向展后交接的离线演示。"""
    ctx = AccessContext.system("expo-demo")
    expo = app.expo
    identity = "lead:roadshow-2026:intent-017"

    # 1) 会务、展商、地方交易团三个渠道分别登记同一条需求
    lead_specs = [
        ("concierge", "scan-017", {"品类": "便携储能", "数量": "500-1000", "地区": "西南"}),
        ("exhibitor-app", "booth-A12-talk-9", {"intent": "power-station", "qty": "约800", "cert": "CE"}),
        ("trade-delegation", "delegation-SC-07", {"need": "储能电源", "交付": "成都", "窗口": "11月"}),
    ]
    leads = []
    for index, (source, ref, payload) in enumerate(lead_specs):
        leads.append(expo.register_lead(ctx, demand_identity=identity, source=source, source_ref=ref, payload=payload, request_key=f"lead-{index}"))
    # 同一业务回执重复到达：返回 duplicate，不推进任何状态
    leads.append(expo.register_lead(ctx, demand_identity=identity, source="concierge", source_ref="scan-017", payload=lead_specs[0][2], request_key="lead-replay"))

    # 2) 采购商建立可追溯的需求版本（合并三来源）
    demand_values = {
        "category": "portable-power-station",
        "quantity_min": 500, "quantity_max": 1000,
        "delivery_regions": ["CN-51", "CN-50"],
        "certifications": ["CE"],
        "window_from": "2026-11-01T00:00:00+08:00", "window_to": "2026-11-30T23:59:59+08:00",
    }
    demand = expo.create_demand(ctx, demand_identity=identity, buyer_org="org:buyer-chain", values=demand_values, sources=[{"source": s, "source_ref": r} for s, r, _ in lead_specs], request_key="demand-v1")

    # 3) 参展商登记并发布明确的产品版本
    product = expo.create_product(ctx, supplier_org="org:supplier-a12", values={
        "category": "portable-power-station",
        "quantity_min": 100, "quantity_max": 2000,
        "delivery_regions": ["CN-51", "CN-44"],
        "certifications": ["CE", "RoHS"],
        "window_from": "2026-10-20T00:00:00+08:00", "window_to": "2026-12-20T23:59:59+08:00",
    }, request_key="product-v1")
    expo.publish_product_version(ctx, product["product_id"], 1, request_key="product-v1-publish")

    # 4) 双方只授权披露部分字段；匹配只能看到这些字段
    buyer_grant = _publish_grant(app, ctx, subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier-a12", fields=["category", "quantity", "delivery_regions", "certifications", "window"], request_key="grant-buyer")
    supplier_grant = _publish_grant(app, ctx, subject_type="product", subject_id=product["product_id"], audience="buyer:org:buyer-chain", fields=["category", "quantity", "delivery_regions", "certifications", "window"], request_key="grant-supplier")
    scored = expo.propose_match(ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)

    # 5) 建立机会并冻结授权快照
    opportunity = expo.create_opportunity(ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1, total_qty=800, request_key="opp-1")
    opp_id = opportunity["opportunity_id"]

    # 6) 会谈预约：同时占用场地与两名人员；纪要双方各自确认
    meeting = expo.schedule_meeting(ctx, opp_id, start_at="2026-10-10T14:00:00+08:00", end_at="2026-10-10T15:00:00+08:00", venue_resource_id="room:expo-3", venue_capacity=8, personnel=["person:li", "person:wang"], request_key="mtg-1")
    expo.confirm_meeting(ctx, meeting["meeting_id"], "buyer", request_key="mtg-confirm-buyer")
    expo.confirm_meeting(ctx, meeting["meeting_id"], "supplier", request_key="mtg-confirm-supplier")
    expo.draft_minutes(ctx, meeting["meeting_id"], content={"意向": "800台分两批", "样品": "2台样机"}, scope_summary="首批800台，样机先行", proposed_by="buyer", request_key="minutes-v1")
    # 一方改变范围：旧纪要保留，新版本待确认
    expo.confirm_minutes(ctx, meeting["meeting_id"], "buyer", request_key="min-v1-buyer", notifier=app.outbox)
    expo.confirm_minutes(ctx, meeting["meeting_id"], "supplier", request_key="min-v1-supplier", notifier=app.outbox)
    expo.amend_minutes(ctx, meeting["meeting_id"], content={"意向": "首批300台，其余500台视样机", "样品": "2台样机"}, scope_summary="首批改为300台", proposed_by="buyer", request_key="minutes-v2")
    expo.confirm_minutes(ctx, meeting["meeting_id"], "buyer", request_key="min-v2-buyer", notifier=app.outbox)

    # 7) 分阶段推进，样品阶段挂持久期限任务；部分成交不关闭剩余机会
    sample = expo.advance_stage(ctx, opp_id, stage="sample", quantity=2, owner_id="person:sample-desk", receipt_key="rcpt-sample-1", request_key="adv-sample", sample_expires_at="2026-10-15T18:00:00+08:00", scheduler=app.jobs, notifier=app.outbox)
    duplicate_sample = expo.advance_stage(ctx, opp_id, stage="sample", quantity=2, owner_id="person:sample-desk", receipt_key="rcpt-sample-1", request_key="adv-sample-replay", sample_expires_at="2026-10-15T18:00:00+08:00", scheduler=app.jobs, notifier=app.outbox)
    expo.advance_stage(ctx, opp_id, stage="quote", quantity=300, owner_id="person:quote-desk", receipt_key="rcpt-quote-1", request_key="adv-quote")
    expo.advance_stage(ctx, opp_id, stage="framework", quantity=300, owner_id="person:legal", receipt_key="rcpt-fw-1", request_key="adv-fw")
    partial_contract = expo.advance_stage(ctx, opp_id, stage="contract", quantity=300, owner_id="person:contract-desk", receipt_key="rcpt-contract-1", request_key="adv-contract-1", followup_at="2026-12-01T10:00:00+08:00", scheduler=app.jobs, notifier=app.outbox)

    # 8) 回执标识相同但内容冲突：不推进，转主管核对
    conflicted = None
    try:
        expo.advance_stage(ctx, opp_id, stage="contract", quantity=999, owner_id="person:other", receipt_key="rcpt-contract-1", request_key="adv-contract-conflict")
    except Exception as exc:  # noqa: BLE001 - 演示需要展示冲突路径
        conflicted = str(exc)

    return {
        "leads": leads,
        "demand_sources": demand["sources"],
        "demand_versions": len(demand["versions"]),
        "product_versions": len(product["versions"]),
        "grants": {"buyer": buyer_grant, "supplier": supplier_grant},
        "match": scored,
        "opportunity": partial_contract["opportunity"],
        "meeting_status": expo.trace(ctx, opp_id)["meetings"][0]["status"],
        "minutes_pending": expo.minutes_history(ctx, meeting["meeting_id"]),
        "sample_job": sample["job_id"],
        "duplicate_receipt": duplicate_sample,
        "receipt_conflict": conflicted,
        "open_conflicts": expo.list_receipt_conflicts(ctx),
        "worklist": expo.worklist(ctx),
        "trace": expo.trace(ctx, opp_id),
        "verification": app.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("expo-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    commands.add_parser("expo-worklist")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo":
        emit(demo(app))
    elif args.command == "expo-demo":
        emit(expo_demo(app))
    elif args.command == "expo-worklist":
        emit(app.expo.worklist(AccessContext.system("cli")))
    elif args.command == "verify":
        emit(app.verify())
    elif args.command == "list-cases":
        emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
