"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .errors import ConflictError
from .matchmaking import MatchmakingService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    try:
        reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    except ConflictError:
        reservation = {"status": "already_reserved"}
    try:
        debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
        credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
        entries = [debit, credit]
    except ConflictError:
        entries = []
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})

    match = MatchmakingService(app.repository, app.inbox, app.reservations, app.jobs, app.outbox)
    showcase = demo_matchmaking(context, match, app.clock.now())
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": entries,
            "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message,
            "matchmaking": showcase, "verification": app.verify()}


def demo_matchmaking(context: AccessContext, match: MatchmakingService, now: str) -> dict:
    """展后专班一条完整链路：线索合并→授权匹配→版本回应→会谈纪要→阶段份额→部分成交。"""
    # 采购商登记需求并形成可追溯版本：v1 登记，v2 调数量区间。
    demand = match.register_demand(context, {
        "buyer_org": "org:buyer-a", "category": "便携储能", "quantity_min": 100, "quantity_max": 500,
        "unit": "台", "delivery_regions": ["华东", "华南"], "certifications": ["CE", "UN38.3"],
        "window_start": "2026-10-10T00:00:00+08:00", "window_end": "2026-11-30T00:00:00+08:00",
        "contact_id": "person:buyer-li", "notes": "路演后回访"}, request_key="demo-demand-v1")
    if demand["version"] == 1 and match.repository.get("match_demands", demand["entity_id"])["state"] == "draft":
        demand = match.transition_demand(context, demand["entity_id"], "open", expected_version=1,
                                         reason="专班确认开放", request_key="demo-demand-open")
        demand = match.revise_demand(context, demand["entity_id"], {"quantity_min": 200},
                                    expected_version=2, request_key="demo-demand-v2")

    # 参展商登记明确产品版本并激活。
    offering = match.register_offering(context, {
        "supplier_org": "org:supplier-b", "category": "便携储能", "product_name": "户外电源 X1",
        "product_version": "X1-2026.09", "certifications": ["CE", "UN38.3", "FCC"],
        "delivery_regions": ["华东", "西南"], "moq": 50, "capacity_per_month": 800, "unit": "台",
        "contact_id": "person:supplier-wang", "notes": "路演样品同款"}, request_key="demo-offering-v1")
    if match.repository.get("match_offerings", offering["entity_id"])["state"] == "draft":
        match.transition_offering(context, offering["entity_id"], "active", expected_version=offering["version"],
                                  reason="参展商确认在售", request_key="demo-offering-active")

    # 双方各自登记并发布披露授权，匹配只使用授权字段。
    d_auth = match.authorize_disclosure(context, {
        "subject_type": "match_demands", "subject_id": demand["entity_id"], "audience": "org:supplier-b",
        "fields": ["category", "quantity_min", "quantity_max", "unit", "delivery_regions", "certifications"],
        "valid_until": "2026-12-31T23:59:59+08:00"}, request_key="demo-demand-auth")
    if match.repository.get("disclosures", d_auth["entity_id"])["state"] == "draft":
        match.publish_disclosure(context, d_auth["entity_id"], expected_version=d_auth["version"], request_key="demo-demand-auth-pub")
    o_auth = match.authorize_disclosure(context, {
        "subject_type": "match_offerings", "subject_id": offering["entity_id"], "audience": "org:buyer-a",
        "fields": ["category", "unit", "delivery_regions", "certifications", "moq", "capacity_per_month",
                   "product_name", "product_version"],
        "valid_until": "2026-12-31T23:59:59+08:00"}, request_key="demo-offering-auth")
    if match.repository.get("disclosures", o_auth["entity_id"])["state"] == "draft":
        match.publish_disclosure(context, o_auth["entity_id"], expected_version=o_auth["version"], request_key="demo-offering-auth-pub")

    demand = match.repository.get("match_demands", demand["entity_id"])
    offering = match.repository.get("match_offerings", offering["entity_id"])
    suggestions = match.suggest_matches(context, demand["entity_id"])

    # 三个渠道登记同一意向：会务、展商、地方交易团；第三条与首条内容冲突，主管裁决。
    common = {"buyer_org": "org:buyer-a", "category": "便携储能", "product_name": "户外电源 X1"}
    lead_1 = match.ingest_lead(context, source="affairs", source_key="intent-001", sequence=1,
                               payload={**common, "supplier_org": "org:supplier-b", "quantity": 300, "delivery_region": "华东"},
                               occurred_at=now)
    lead_2 = match.ingest_lead(context, source="exhibitor", source_key="E-77", sequence=1,
                               payload={**common, "supplier_org": "org:supplier-b", "quantity": 300, "delivery_region": "华东"},
                               occurred_at=now)
    lead_3 = match.ingest_lead(context, source="trade-delegation", source_key="delegation-12", sequence=1,
                               payload={**common, "supplier_org": "org:supplier-b", "quantity": 420, "delivery_region": "华东"},
                               occurred_at=now)
    if lead_3["status"] == "conflict":
        pending = match.list_pending_conflicts(context)
        chosen = next(item for item in pending if item["lead_id"] == lead_3["lead_id"])
        match.resolve_lead_conflict(context, chosen["conflict_id"], resolution="use_incoming",
                                    request_key=f"demo-resolve-{chosen['conflict_id']}")
    lead_view = match.get_lead(context, lead_1["lead_id"])

    # 参展商只能用明确产品版本回应开放中的需求版本。
    response = match.respond_to_demand(context, demand_id=demand["entity_id"], demand_version=demand["version"],
                                       offering_id=offering["entity_id"], offering_version=1,
                                       request_key="demo-response")
    # 机会总量落在需求版本数量区间内。
    opp = match.open_opportunity(context, response_id=response["response_id"], total_quantity=400,
                                 request_key="demo-opportunity")
    booked = match.book_meeting(context, opportunity_id=opp["opportunity_id"], room_id="room-301", room_capacity=8,
                                attendee_ids=["person:buyer-li", "person:supplier-wang"],
                                start_at="2026-10-08T14:00:00+08:00", end_at="2026-10-08T15:00:00+08:00",
                                request_key="demo-meeting")
    minutes = match.draft_minutes(context, meeting_id=booked["meeting_id"], party="buyer",
                                  content={"scope": "首批 200 台样品验证", "sample_due": "2026-10-15"},
                                  request_key="demo-minutes-v1")
    match.confirm_minutes(context, meeting_id=booked["meeting_id"], party="buyer", request_key="demo-minutes-cb")
    revised = match.draft_minutes(context, meeting_id=booked["meeting_id"], party="seller",
                                  content={"scope": "首批 200 台样品验证，追加 50 台备选", "sample_due": "2026-10-15"},
                                  request_key="demo-minutes-v2")
    match.confirm_minutes(context, meeting_id=booked["meeting_id"], party="buyer", request_key="demo-minutes-v2-cb")
    match.confirm_minutes(context, meeting_id=booked["meeting_id"], party="seller", request_key="demo-minutes-v2-cs")

    # 样品 200 台 + 直接报价 200 台；样品份额一路推进到部分成交，报价份额继续保留。
    sample_share = match.allocate_share(context, opportunity_id=opp["opportunity_id"], stage="sample",
                                        quantity=200, owner_id="person:staff-sample",
                                        request_key="demo-share-sample")
    quote_share = match.allocate_share(context, opportunity_id=opp["opportunity_id"], stage="quote",
                                       quantity=200, owner_id="person:staff-quote",
                                       request_key="demo-share-quote")
    match.handover_share(context, share_id=sample_share["share_id"], to_owner="person:staff-contract",
                         note="样品通过，转合同责任人", request_key="demo-handover-1")
    match.advance_share(context, share_id=sample_share["share_id"], to_stage="quote",
                        owner_id="person:staff-contract", request_key="demo-adv-1")
    match.advance_share(context, share_id=sample_share["share_id"], to_stage="framework",
                        request_key="demo-adv-2")
    match.advance_share(context, share_id=sample_share["share_id"], to_stage="contract",
                        reference="HT-2026-1008", request_key="demo-adv-3")
    opportunity = match.get_opportunity(context, opp["opportunity_id"])
    trace = match.trace_deal(context, share_id=sample_share["share_id"])
    worklist = match.worklist(context)
    return {"demand_id": demand["entity_id"], "demand_version": demand["version"],
            "offering_id": offering["entity_id"], "suggestions": suggestions,
            "lead": {"lead_id": lead_view["lead_id"], "status": lead_view["status"],
                     "sources": [s["source"] for s in lead_view["sources"]]},
            "response_id": response["response_id"], "meeting_id": booked["meeting_id"],
            "minutes_versions": revised["version"], "opportunity": opportunity,
            "worklist": worklist, "deal_contract": trace["deal"]["contract_reference"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    commands.add_parser("match-worklist")
    commands.add_parser("list-opportunities")
    trace = commands.add_parser("trace-deal")
    trace.add_argument("--share", required=True)
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    elif args.command == "match-worklist": emit(app.matchmaking.worklist(AccessContext.system("cli")))
    elif args.command == "list-opportunities": emit(app.matchmaking.list_opportunities(AccessContext.system("cli")))
    elif args.command == "trace-deal": emit(app.matchmaking.trace_deal(AccessContext.system("cli"), share_id=args.share))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
