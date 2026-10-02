from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.disclosures import DisclosureService
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.matchmaking import (
    DEMAND_TYPE,
    OFFERING_TYPE,
    MatchmakingService,
)
from civicflow.security import AccessContext


BUYER = "org:buyer-a"
SUPPLIER = "org:supplier-b"

DEMAND_VALUES = {
    "buyer_org": BUYER, "category": "便携储能", "quantity_min": 100, "quantity_max": 500,
    "unit": "台", "delivery_regions": ["华东", "华南"], "certifications": ["CE", "UN38.3"],
    "window_start": "2026-10-10T00:00:00+08:00", "window_end": "2026-11-30T00:00:00+08:00",
    "contact_id": "person:buyer-li", "notes": "路演意向",
}
OFFERING_VALUES = {
    "supplier_org": SUPPLIER, "category": "便携储能", "product_name": "户外电源 X1",
    "product_version": "X1-2026.09", "certifications": ["CE", "UN38.3", "FCC"],
    "delivery_regions": ["华东", "西南"], "moq": 50, "capacity_per_month": 800, "unit": "台",
    "contact_id": "person:supplier-wang", "notes": "样品同款",
}
DEMAND_FIELDS = ["category", "quantity_min", "quantity_max", "unit", "delivery_regions", "certifications"]
OFFERING_FIELDS = ["category", "unit", "delivery_regions", "certifications", "moq",
                   "capacity_per_month", "product_name", "product_version"]


class MatchmakingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "match.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-09-28T12:00:00+08:00")
        self.ctx = AccessContext.system("tester")
        self.match = self.app.matchmaking

    def tearDown(self):
        self.temp.cleanup()

    # ---------- 测试夹具 ---------- #
    def open_demand(self, request_key="d1", *, values=None):
        demand = self.match.register_demand(self.ctx, values or DEMAND_VALUES, request_key=request_key)
        self.match.transition_demand(self.ctx, demand["entity_id"], "open", expected_version=1,
                                     reason="专班开放", request_key=request_key + ":open")
        return self.match.repository.get(DEMAND_TYPE, demand["entity_id"])

    def active_offering(self, request_key="o1", *, values=None):
        offering = self.match.register_offering(self.ctx, values or OFFERING_VALUES, request_key=request_key)
        self.match.transition_offering(self.ctx, offering["entity_id"], "active", expected_version=1,
                                       reason="在售", request_key=request_key + ":active")
        return self.match.repository.get(OFFERING_TYPE, offering["entity_id"])

    def publish_auth(self, subject_type, subject_id, audience, fields, key, *, valid_until="2026-12-31T23:59:59+08:00"):
        auth = self.match.authorize_disclosure(self.ctx, {"subject_type": subject_type, "subject_id": subject_id,
                                                          "audience": audience, "fields": fields,
                                                          "valid_until": valid_until}, request_key=key)
        self.match.publish_disclosure(self.ctx, auth["entity_id"], expected_version=auth["version"], request_key=key + ":pub")
        return auth

    def authorized_pair(self, *, demand=None, offering=None):
        demand = demand or self.open_demand()
        offering = offering or self.active_offering()
        self.publish_auth(DEMAND_TYPE, demand["entity_id"], SUPPLIER, DEMAND_FIELDS, "auth-d")
        self.publish_auth(OFFERING_TYPE, offering["entity_id"], BUYER, OFFERING_FIELDS, "auth-o")
        return demand, offering

    def response_and_opportunity(self, *, demand_version=None, offering_version=1, total=400):
        demand, offering = self.authorized_pair()
        dv = demand_version or demand["version"]
        response = self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=dv,
                                                offering_id=offering["entity_id"], offering_version=offering_version,
                                                request_key="resp-1")
        opp = self.match.open_opportunity(self.ctx, response_id=response["response_id"],
                                          total_quantity=total, request_key="opp-1")
        return demand, offering, response, opp

    # ---------- 1. 需求版本可追溯 ---------- #
    def test_demand_versions_are_traceable(self):
        demand = self.match.register_demand(self.ctx, DEMAND_VALUES, request_key="demand-v1")
        self.match.transition_demand(self.ctx, demand["entity_id"], "open", expected_version=1,
                                     reason="开放", request_key="demand-open")
        revised = self.match.revise_demand(self.ctx, demand["entity_id"], {"quantity_min": 200, "delivery_regions": ["华北"]},
                                          expected_version=2, request_key="demand-v2")
        self.assertEqual(revised["version"], 3)
        history = self.match.demand_history(self.ctx, demand["entity_id"])
        self.assertEqual([row["version"] for row in history], [1, 2, 3])
        # 旧版本内容原样保留，可追溯到登记时的数量与地区。
        self.assertEqual(history[0]["quantity_min"], 100)
        self.assertIn("华东", history[0]["delivery_regions"])
        self.assertEqual(history[-1]["quantity_min"], 200)
        self.match.transition_demand(self.ctx, demand["entity_id"], "closed", expected_version=3,
                                     reason="全部成交关闭", request_key="demand-close")
        with self.assertRaises(ConflictError):
            self.match.revise_demand(self.ctx, demand["entity_id"], {"quantity_min": 1},
                                    expected_version=4, request_key="demand-v3")

    def test_demand_validation(self):
        with self.assertRaises(ValidationError):
            self.match.register_demand(self.ctx, {**DEMAND_VALUES, "quantity_min": 600}, request_key="bad-1")
        with self.assertRaises(ValidationError):
            self.match.register_demand(self.ctx, {**DEMAND_VALUES, "window_end": "2026-10-01T00:00:00+08:00"}, request_key="bad-2")

    # ---------- 2. 只能以明确产品版本回应 ---------- #
    def test_response_pins_explicit_versions(self):
        demand, offering = self.authorized_pair()
        revised = self.match.revise_offering(self.ctx, offering["entity_id"], {"capacity_per_month": 1200},
                                             expected_version=offering["version"], request_key="offering-v2")
        self.assertEqual(revised["version"], offering["version"] + 1)
        r1 = self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=demand["version"],
                                         offering_id=offering["entity_id"], offering_version=1, request_key="r-v1")
        r2 = self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=demand["version"],
                                         offering_id=offering["entity_id"], offering_version=2, request_key="r-v2")
        self.assertNotEqual(r1["response_id"], r2["response_id"])
        # 重复回应同版本组合不产生新回应。
        replay = self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=demand["version"],
                                              offering_id=offering["entity_id"], offering_version=1, request_key="r-v1")
        self.assertEqual(replay["response_id"], r1["response_id"])
        with self.assertRaises(NotFoundError):
            self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=99,
                                         offering_id=offering["entity_id"], offering_version=1, request_key="r-bad")

    def test_only_open_demand_can_be_responded(self):
        demand = self.match.register_demand(self.ctx, DEMAND_VALUES, request_key="d-draft")
        offering = self.active_offering()
        self.publish_auth(DEMAND_TYPE, demand["entity_id"], SUPPLIER, DEMAND_FIELDS, "ad")
        self.publish_auth(OFFERING_TYPE, offering["entity_id"], BUYER, OFFERING_FIELDS, "ao")
        with self.assertRaises(ConflictError):
            self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=1,
                                         offering_id=offering["entity_id"], offering_version=1, request_key="rr")

    # ---------- 3. 匹配只用双方授权字段 ---------- #
    def test_matching_uses_only_authorized_fields(self):
        demand, offering = self.open_demand(), self.active_offering()
        self.assertEqual(self.match.suggest_matches(self.ctx, demand["entity_id"]), [])
        self.publish_auth(DEMAND_TYPE, demand["entity_id"], SUPPLIER, DEMAND_FIELDS, "ad")
        # 只有需求方授权时仍不能匹配。
        self.assertEqual(self.match.suggest_matches(self.ctx, demand["entity_id"]), [])
        self.publish_auth(OFFERING_TYPE, offering["entity_id"], BUYER, OFFERING_FIELDS, "ao")
        suggestions = self.match.suggest_matches(self.ctx, demand["entity_id"])
        self.assertEqual(len(suggestions), 1)
        suggestion = suggestions[0]
        self.assertEqual(suggestion["demand_version"], demand["version"])
        self.assertEqual(suggestion["offering_version"], offering["version"])
        # 联系人、备注等未授权字段绝不出现。
        self.assertNotIn("contact_id", suggestion["demand_view"])
        self.assertNotIn("notes", suggestion["offering_view"])
        self.assertNotIn("contact_id", suggestion["offering_view"])
        self.assertEqual(set(suggestion["demand_view"]), set(DEMAND_FIELDS))

    def test_matching_respects_audience_and_expiry(self):
        demand = self.open_demand()
        other = self.active_offering("o2", values={**OFFERING_VALUES, "supplier_org": "org:supplier-c"})
        # 需求方只授权给 supplier-b，supplier-c 看不到字段。
        self.publish_auth(DEMAND_TYPE, demand["entity_id"], SUPPLIER, DEMAND_FIELDS, "ad")
        self.publish_auth(OFFERING_TYPE, other["entity_id"], BUYER, OFFERING_FIELDS, "ao")
        self.assertEqual(self.match.suggest_matches(self.ctx, demand["entity_id"]), [])

        target = self.active_offering("o3")
        self.publish_auth(OFFERING_TYPE, target["entity_id"], BUYER, OFFERING_FIELDS, "ao2")
        self.assertEqual(len(self.match.suggest_matches(self.ctx, demand["entity_id"])), 1)
        # 过期授权不再参与匹配。
        self.publish_auth(DEMAND_TYPE, demand["entity_id"], SUPPLIER, DEMAND_FIELDS, "ad-expired",
                          valid_until="2026-09-01T00:00:00+08:00")

    def test_response_requires_live_authorization(self):
        demand, offering = self.authorized_pair()
        auths = self.match.repository.list("disclosures")
        demand_auth = next(a for a in auths if a["subject_id"] == demand["entity_id"])
        DisclosureService(self.match.repository).transition(
            self.ctx, demand_auth["entity_id"], "withdrawn", expected_version=demand_auth["version"],
            reason="撤回授权", request_key="withdraw-auth")
        with self.assertRaises(PermissionDenied):
            self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=demand["version"],
                                         offering_id=offering["entity_id"], offering_version=1, request_key="blocked")

    # ---------- 4. 会谈同时占用场地与人员 ---------- #
    def test_meeting_books_room_and_people_atomically(self):
        _, _, _, opp = self.response_and_opportunity()
        booked = self.match.book_meeting(self.ctx, opportunity_id=opp["opportunity_id"], room_id="R1", room_capacity=2,
                                         attendee_ids=["person:a", "person:b"],
                                         start_at="2026-10-08T10:00:00+08:00", end_at="2026-10-08T11:00:00+08:00",
                                         request_key="meet-1")
        # 同一人时间冲突：整体失败，场地占用不应泄漏（房间容量 2 还能再约一场其他人）。
        with self.assertRaises(ConflictError):
            self.match.book_meeting(self.ctx, opportunity_id=opp["opportunity_id"], room_id="R1", room_capacity=2,
                                    attendee_ids=["person:a", "person:c"],
                                    start_at="2026-10-08T10:30:00+08:00", end_at="2026-10-08T11:30:00+08:00",
                                    request_key="meet-2")
        second = self.match.book_meeting(self.ctx, opportunity_id=opp["opportunity_id"], room_id="R1", room_capacity=2,
                                         attendee_ids=["person:c", "person:d"],
                                         start_at="2026-10-08T10:00:00+08:00", end_at="2026-10-08T11:00:00+08:00",
                                         request_key="meet-3")
        self.assertEqual(self.app.reservations.usage("room:R1", at="2026-10-08T10:30:00+08:00"), 2)
        # 取消后场地与人员全部释放。
        self.match.cancel_meeting(self.ctx, booked["meeting_id"], reason="改期", request_key="cancel-1")
        self.assertEqual(self.app.reservations.usage("person:a", at="2026-10-08T10:30:00+08:00"), 0)
        self.assertEqual(self.app.reservations.usage("room:R1", at="2026-10-08T10:30:00+08:00"), 1)
        self.assertTrue(second["meeting_id"])

    # ---------- 5. 纪要双方确认与改版 ---------- #
    def test_minutes_dual_confirmation_and_revision(self):
        _, _, _, opp = self.response_and_opportunity()
        meeting = self.match.book_meeting(self.ctx, opportunity_id=opp["opportunity_id"], room_id="R2", room_capacity=4,
                                          attendee_ids=["person:a", "person:b"],
                                          start_at="2026-10-08T14:00:00+08:00", end_at="2026-10-08T15:00:00+08:00",
                                          request_key="m")
        v1 = self.match.draft_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer",
                                      content={"scope": "样品 100 台"}, request_key="min-1")
        self.assertEqual(v1["version"], 1)
        # 相同内容重复提交不产生新版本。
        same = self.match.draft_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer",
                                        content={"scope": "样品 100 台"}, request_key="min-1b")
        self.assertEqual(same["status"], "unchanged")
        cb = self.match.confirm_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer", request_key="cb-1")
        self.assertFalse(cb["both_confirmed"])
        # 卖方改变范围：旧版保留，形成待确认新版本，确认状态清零。
        v2 = self.match.draft_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="seller",
                                      content={"scope": "样品 100 台，追加 50 台备选"}, request_key="min-2")
        self.assertEqual(v2["version"], 2)
        self.assertFalse(v2["buyer_confirmed"])
        versions = self.match.get_minutes(self.ctx, meeting["meeting_id"])
        self.assertEqual(len(versions), 2)
        self.assertIsNotNone(versions[0]["buyer_confirmed_at"])  # 旧版确认痕迹保留
        self.assertEqual(versions[0]["superseded_by"], versions[1]["minute_id"])
        self.match.confirm_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer", request_key="cb-2")
        cs = self.match.confirm_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="seller", request_key="cs-2")
        self.assertTrue(cs["both_confirmed"])
        worklist = self.match.worklist(self.ctx)
        self.assertNotIn(meeting["meeting_id"], [item["meeting_id"] for item in worklist["unconfirmed_minutes"]])

    # ---------- 6. 多渠道线索合并与冲突 ---------- #
    def test_leads_merge_with_visible_sources_and_conflicts(self):
        common = {"buyer_org": BUYER, "category": "便携储能", "product_name": "户外电源 X1"}
        first = self.match.ingest_lead(self.ctx, source="affairs", source_key="intent-1", sequence=1,
                                       payload={**common, "supplier_org": SUPPLIER, "quantity": 300, "delivery_region": "华东"},
                                       occurred_at=self.app.clock.now())
        second = self.match.ingest_lead(self.ctx, source="exhibitor", source_key="E-77", sequence=1,
                                        payload={**common, "supplier_org": SUPPLIER, "quantity": 300, "delivery_region": "华东"},
                                        occurred_at=self.app.clock.now())
        self.assertEqual(first["lead_id"], second["lead_id"])
        self.assertEqual(second["status"], "merged")
        # 同一业务回执重复到达不再推进。
        duplicate = self.match.ingest_lead(self.ctx, source="affairs", source_key="intent-1", sequence=1,
                                           payload={**common, "supplier_org": SUPPLIER, "quantity": 300, "delivery_region": "华东"},
                                           occurred_at=self.app.clock.now())
        self.assertEqual(duplicate["status"], "duplicate")
        # 标识相同（来源序号相同）但内容冲突：进入收件箱冲突，不得静默覆盖。
        with self.assertRaises(ConflictError):
            self.match.ingest_lead(self.ctx, source="affairs", source_key="intent-1", sequence=1,
                                   payload={**common, "supplier_org": SUPPLIER, "quantity": 999, "delivery_region": "华东"},
                                   occurred_at=self.app.clock.now())
        # 另一渠道数量不一致：合并但挂起冲突交由主管核对。
        third = self.match.ingest_lead(self.ctx, source="trade-delegation", source_key="d-9", sequence=1,
                                       payload={**common, "supplier_org": SUPPLIER, "quantity": 420, "delivery_region": "华东"},
                                       occurred_at=self.app.clock.now())
        self.assertEqual(third["status"], "conflict")
        lead = self.match.get_lead(self.ctx, first["lead_id"])
        self.assertEqual([s["source"] for s in lead["sources"]], ["affairs", "exhibitor", "trade-delegation"])
        pending = self.match.list_pending_conflicts(self.ctx)
        self.assertEqual([c["field_name"] for c in pending], ["quantity"])
        resolved = self.match.resolve_lead_conflict(self.ctx, pending[0]["conflict_id"],
                                                    resolution="use_incoming", request_key="resolve-1")
        self.assertEqual(resolved["chosen"], 420)
        lead_after = self.match.get_lead(self.ctx, first["lead_id"])
        self.assertEqual(lead_after["status"], "merged")
        self.assertEqual(lead_after["payload"]["quantity"], 420)
        self.assertEqual(len(lead_after["sources"]), 3)
        with self.assertRaises(ConflictError):
            self.match.resolve_lead_conflict(self.ctx, pending[0]["conflict_id"],
                                             resolution="keep_existing", request_key="resolve-2")

    # ---------- 7. 阶段份额、部分成交 ---------- #
    def test_shares_conserve_and_partial_deal_stays_open(self):
        _, _, _, opp = self.response_and_opportunity(total=100)
        s1 = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                       quantity=60, owner_id="person:sample", request_key="sh-1")
        with self.assertRaises(ConflictError):
            self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="quote",
                                      quantity=50, owner_id="person:q", request_key="sh-over")
        s2 = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="quote",
                                       quantity=40, owner_id="person:quote", request_key="sh-2")
        # 不能跨阶段跳。
        with self.assertRaises(ConflictError):
            self.match.advance_share(self.ctx, share_id=s1["share_id"], to_stage="framework", request_key="jump")
        # 合同必须有合同参考号。
        self.match.advance_share(self.ctx, share_id=s1["share_id"], to_stage="quote", request_key="a1")
        self.match.advance_share(self.ctx, share_id=s1["share_id"], to_stage="framework", request_key="a2")
        with self.assertRaises(ValidationError):
            self.match.advance_share(self.ctx, share_id=s1["share_id"], to_stage="contract", request_key="a3-bad")
        deal = self.match.advance_share(self.ctx, share_id=s1["share_id"], to_stage="contract",
                                        reference="HT-001", request_key="a3")
        self.assertEqual(deal["status"], "contracted")
        view = self.match.get_opportunity(self.ctx, opp["opportunity_id"])
        # 部分成交：机会保持 partial，报价份额继续开放。
        self.assertEqual(view["status"], "partial")
        self.assertEqual(view["allocated_quantity"], 100)
        quote_share = next(s for s in view["shares"] if s["share_id"] == s2["share_id"])
        self.assertEqual(quote_share["status"], "active")
        # 已成交份额不能退出。
        with self.assertRaises(ConflictError):
            self.match.exit_share(self.ctx, share_id=s1["share_id"], reason="反悔", request_key="exit-contract")

    def test_remaining_exit_does_not_close_won_part(self):
        _, _, _, opp = self.response_and_opportunity(total=100)
        won = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                        quantity=60, owner_id="p1", request_key="w1")
        rest = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                         quantity=40, owner_id="p2", request_key="w2")
        for stage, key in (("quote", "wq"), ("framework", "wf")):
            self.match.advance_share(self.ctx, share_id=won["share_id"], to_stage=stage, request_key=key)
        self.match.advance_share(self.ctx, share_id=won["share_id"], to_stage="contract",
                                 reference="HT-002", request_key="wc")
        self.match.exit_share(self.ctx, share_id=rest["share_id"], reason="客户预算取消", request_key="exit-rest")
        view = self.match.get_opportunity(self.ctx, opp["opportunity_id"])
        self.assertEqual(view["status"], "closed")
        contracted = [s for s in view["shares"] if s["status"] == "contracted"]
        self.assertEqual(len(contracted), 1)
        self.assertEqual(contracted[0]["quantity"], 60)

    def test_share_handover_is_recorded(self):
        _, _, _, opp = self.response_and_opportunity(total=100)
        share = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                          quantity=100, owner_id="p-old", request_key="hs")
        self.match.handover_share(self.ctx, share_id=share["share_id"], to_owner="p-new",
                                  note="样品负责人轮换", request_key="hv")
        with self.assertRaises(ValidationError):
            self.match.handover_share(self.ctx, share_id=share["share_id"], to_owner="p-new",
                                      note="重复交接", request_key="hv2")
        trace = self.match.trace_deal(self.ctx, share_id=share["share_id"])
        self.assertEqual([(h["from_owner"], h["to_owner"]) for h in trace["handovers"]], [("p-old", "p-new")])

    # ---------- 8. 幂等与冲突回执 ---------- #
    def test_idempotent_request_keys(self):
        demand = self.open_demand(request_key="idem-d")
        offering = self.active_offering(request_key="idem-o")
        self.publish_auth(DEMAND_TYPE, demand["entity_id"], SUPPLIER, DEMAND_FIELDS, "idem-ad")
        self.publish_auth(OFFERING_TYPE, offering["entity_id"], BUYER, OFFERING_FIELDS, "idem-ao")
        values = dict(demand_id=demand["entity_id"], demand_version=demand["version"],
                      offering_id=offering["entity_id"], offering_version=1)
        first = self.match.respond_to_demand(self.ctx, **values, request_key="same-key")
        second = self.match.respond_to_demand(self.ctx, **values, request_key="same-key")
        self.assertEqual(first["response_id"], second["response_id"])
        # 同键不同内容必须报错而不是第二次推进。
        with self.assertRaises(ConflictError):
            self.match.respond_to_demand(self.ctx, demand_id=demand["entity_id"], demand_version=1,
                                         offering_id=offering["entity_id"], offering_version=1, request_key="same-key")

    # ---------- 9. 中断恢复 ---------- #
    def test_recovery_worklist_and_due_items(self):
        _, _, _, opp = self.response_and_opportunity(total=100)
        meeting = self.match.book_meeting(self.ctx, opportunity_id=opp["opportunity_id"], room_id="R3", room_capacity=4,
                                          attendee_ids=["person:a"], start_at="2026-10-08T09:00:00+08:00",
                                          end_at="2026-10-08T10:00:00+08:00", request_key="rec-m")
        self.match.draft_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer",
                                 content={"scope": "样品"}, request_key="rec-min")
        share = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                          quantity=100, owner_id="p-s", request_key="rec-sh")
        worklist = self.match.worklist(self.ctx)
        self.assertIn(meeting["meeting_id"], [item["meeting_id"] for item in worklist["unconfirmed_minutes"]])
        self.assertEqual([item["kind"] for item in worklist["upcoming"]], ["sample_deadline"])

        # 模拟服务中断：时间走到样品期限后任务到期，worker 认领后进程退出（租约悬挂）。
        crashed = CivicFlow.open(self.db_path, fixed_now="2026-10-10T08:00:00+08:00")
        claimed = crashed.jobs.claim_due()
        self.assertEqual(len(claimed), 1)
        recovered = CivicFlow.open(self.db_path, fixed_now="2026-10-10T09:00:00+08:00")
        worklist = recovered.matchmaking.worklist(AccessContext.system("tester"))
        self.assertEqual(worklist["recoverable_jobs"], [claimed[0]["job_id"]])
        kinds = {item["kind"]: item for item in worklist["due_now"]}
        self.assertIn("sample_deadline", kinds)
        # 恢复程序回收过期租约，任务可被重新认领继续处理。
        recovered_ids = recovered.jobs.recover_stale()
        self.assertEqual(recovered_ids, [claimed[0]["job_id"]])
        reclaimed = recovered.jobs.claim_due()
        self.assertEqual([item["job_id"] for item in reclaimed], [claimed[0]["job_id"]])
        recovered.jobs.finish(claimed[0]["job_id"])
        resolved = recovered.matchmaking.resolve_due(AccessContext.system("tester"),
                                                     kinds["sample_deadline"]["due_id"], request_key="due-done")
        self.assertEqual(resolved["status"], "resolved")
        replay = recovered.matchmaking.resolve_due(AccessContext.system("tester"),
                                                   kinds["sample_deadline"]["due_id"], request_key="due-done")
        self.assertEqual(replay["status"], "resolved")
        self.assertEqual(
            recovered.database.connect().execute(
                "SELECT COUNT(*) FROM match_due_items WHERE due_id=?", (kinds["sample_deadline"]["due_id"],)).fetchone()[0],
            1)

    def test_fulfillment_followup_after_contract(self):
        _, _, _, opp = self.response_and_opportunity(total=100)
        share = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                          quantity=100, owner_id="p", request_key="f1")
        for stage, key in (("quote", "f2"), ("framework", "f3")):
            self.match.advance_share(self.ctx, share_id=share["share_id"], to_stage=stage, request_key=key)
        self.match.advance_share(self.ctx, share_id=share["share_id"], to_stage="contract",
                                 reference="HT-009", request_key="f4")
        upcoming = self.match.worklist(self.ctx)["upcoming"]
        self.assertEqual({item["kind"] for item in upcoming}, {"sample_deadline", "fulfillment_followup"})

    # ---------- 10. 成交反查 ---------- #
    def test_trace_deal_full_chain(self):
        demand, offering, _, opp = self.response_and_opportunity(total=100)
        share = self.match.allocate_share(self.ctx, opportunity_id=opp["opportunity_id"], stage="sample",
                                          quantity=100, owner_id="p-old", request_key="t1")
        meeting = self.match.book_meeting(self.ctx, opportunity_id=opp["opportunity_id"], room_id="R4", room_capacity=4,
                                          attendee_ids=["person:a"], start_at="2026-10-08T09:00:00+08:00",
                                          end_at="2026-10-08T10:00:00+08:00", request_key="t2")
        self.match.draft_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer",
                                 content={"scope": "100 台"}, request_key="t3")
        self.match.confirm_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="buyer", request_key="t4")
        self.match.confirm_minutes(self.ctx, meeting_id=meeting["meeting_id"], party="seller", request_key="t5")
        self.match.handover_share(self.ctx, share_id=share["share_id"], to_owner="p-new",
                                  note="转责任人", request_key="t6")
        for stage, ref, key in (("quote", "", "t7"), ("framework", "", "t8"), ("contract", "HT-TRACE", "t9")):
            self.match.advance_share(self.ctx, share_id=share["share_id"], to_stage=stage,
                                     reference=ref, request_key=key)
        trace = self.match.trace_deal(self.ctx, contract_reference="HT-TRACE")
        self.assertEqual(trace["deal"]["contract_reference"], "HT-TRACE")
        self.assertEqual(trace["demand"]["demand_id"], demand["entity_id"])
        self.assertGreaterEqual(len(trace["demand"]["versions"]), 2)
        self.assertEqual(trace["offering"]["matched_version"], 1)
        self.assertEqual({d["state"] for d in trace["disclosures"]}, {"published"})
        self.assertEqual(len(trace["meetings"]), 1)
        self.assertTrue(trace["meetings"][0]["minutes"][0]["buyer_confirmed_at"])
        self.assertEqual([h["to_owner"] for h in trace["handovers"]], ["p-new"])
        self.assertEqual([e["to_stage"] for e in trace["timeline"] if e["event_type"] == "advance"],
                         ["quote", "framework", "contract"])
        with self.assertRaises(NotFoundError):
            self.match.trace_deal(self.ctx, contract_reference="MISSING")

    # ---------- 11. 权限 ---------- #
    def test_permissions_required(self):
        limited = AccessContext(actor_id="reader", permissions=frozenset({"read:match-demands"}))
        with self.assertRaises(PermissionDenied):
            self.match.register_demand(limited, DEMAND_VALUES, request_key="denied")
        with self.assertRaises(PermissionDenied):
            self.match.worklist(limited)


if __name__ == "__main__":
    unittest.main()
