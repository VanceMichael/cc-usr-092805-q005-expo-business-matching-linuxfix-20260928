from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.disclosures import DisclosureService
from civicflow.errors import ConflictError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


DEMAND = {
    "category": "portable-power-station",
    "quantity_min": 500,
    "quantity_max": 1000,
    "delivery_regions": ["CN-51", "CN-50"],
    "certifications": ["CE"],
    "window_from": "2026-11-01T00:00:00+08:00",
    "window_to": "2026-11-30T23:59:59+08:00",
}
PRODUCT = {
    "category": "portable-power-station",
    "quantity_min": 100,
    "quantity_max": 2000,
    "delivery_regions": ["CN-51", "CN-44"],
    "certifications": ["CE", "RoHS"],
    "window_from": "2026-10-20T00:00:00+08:00",
    "window_to": "2026-12-20T23:59:59+08:00",
}


class ExpoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "expo.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-10-02T09:00:00+08:00")
        self.ctx = AccessContext.system("tester")
        self.expo = self.app.expo

    def tearDown(self):
        self.temp.cleanup()

    # -- 工具 ---------------------------------------------------------------

    def _grant(self, *, subject_type, subject_id, audience, fields, valid_until="2027-01-01T00:00:00+08:00", key="g"):
        service = DisclosureService(self.app.repository)
        row = service.create(self.ctx, {"subject_type": subject_type, "subject_id": subject_id, "audience": audience, "fields": fields, "valid_until": valid_until}, request_key=f"{key}:create")
        row = service.transition(self.ctx, row["entity_id"], "approved", expected_version=row["version"], reason="授权", request_key=f"{key}:approve")
        row = service.transition(self.ctx, row["entity_id"], "published", expected_version=row["version"], reason="授权", request_key=f"{key}:publish")
        return row["entity_id"]

    def _demand_with_leads(self, identity="lead:x", key="d"):
        for i, (source, ref) in enumerate([("concierge", "scan-1"), ("booth", "talk-2"), ("delegation", "reg-3")]):
            self.expo.register_lead(self.ctx, demand_identity=identity, source=source, source_ref=ref, payload={"raw": i}, request_key=f"{key}-lead-{i}")
        return self.expo.create_demand(self.ctx, demand_identity=identity, buyer_org="org:buyer", values=DEMAND, sources=[{"source": "concierge", "source_ref": "scan-1"}, {"source": "booth", "source_ref": "talk-2"}, {"source": "delegation", "source_ref": "reg-3"}], request_key=f"{key}-create")

    def _published_product(self, key="p", values=None):
        product = self.expo.create_product(self.ctx, supplier_org="org:supplier", values=values or PRODUCT, request_key=f"{key}-create")
        self.expo.publish_product_version(self.ctx, product["product_id"], 1, request_key=f"{key}-publish")
        return product

    def _opportunity(self, demand, product, qty=800, key="opp"):
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier", fields=["category", "quantity", "delivery_regions", "certifications", "window"], key=f"{key}-bg")
        self._grant(subject_type="product", subject_id=product["product_id"], audience="buyer:org:buyer", fields=["category", "quantity", "delivery_regions", "certifications", "window"], key=f"{key}-sg")
        return self.expo.create_opportunity(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1, total_qty=qty, request_key=f"{key}-create")

    # -- 线索合并 ------------------------------------------------------------

    def test_lead_dedupe_conflict_and_sources_visible(self):
        payload = {"qty": 100}
        first = self.expo.register_lead(self.ctx, demand_identity="lead:1", source="concierge", source_ref="s1", payload=payload, request_key="l1")
        replay = self.expo.register_lead(self.ctx, demand_identity="lead:1", source="concierge", source_ref="s1", payload=dict(payload), request_key="l2")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(replay["status"], "duplicate")
        with self.assertRaises(ConflictError):
            self.expo.register_lead(self.ctx, demand_identity="lead:1", source="concierge", source_ref="s1", payload={"qty": 200}, request_key="l3")
        conflicts = self.expo.list_receipt_conflicts(self.ctx)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["kind"], "lead")
        demand = self.expo.create_demand(
            self.ctx, demand_identity="lead:1", buyer_org="org:buyer", values=DEMAND,
            sources=[{"source": "concierge", "source_ref": "s1"}], request_key="d1")
        detail = self.expo.get_demand(self.ctx, demand["demand_id"])
        self.assertEqual([s["source"] for s in detail["sources"]], ["concierge"])
        # 后到的新渠道并入后仍然全部可见
        other = self.expo.register_lead(self.ctx, demand_identity="lead:1", source="booth", source_ref="b2", payload={"x": 1}, request_key="l4")
        self.assertEqual(other["status"], "accepted")
        attached = self.expo.attach_source(self.ctx, demand_id=demand["demand_id"], source="booth", source_ref="b2")
        self.assertEqual(attached["status"], "attached")
        detail = self.expo.get_demand(self.ctx, demand["demand_id"])
        self.assertEqual({s["source"] for s in detail["attached_sources"]}, {"concierge", "booth"})

    def test_supervisor_resolves_conflict(self):
        self.expo.register_lead(self.ctx, demand_identity="lead:2", source="a", source_ref="r", payload={"v": 1}, request_key="c1")
        with self.assertRaises(ConflictError):
            self.expo.register_lead(self.ctx, demand_identity="lead:2", source="a", source_ref="r", payload={"v": 2}, request_key="c2")
        conflict = self.expo.list_receipt_conflicts(self.ctx)[0]
        resolved = self.expo.resolve_conflict(self.ctx, conflict["conflict_id"], resolution="以会务登记为准", request_key="r1")
        self.assertEqual(resolved["status"], "resolved")
        with self.assertRaises(ConflictError):
            self.expo.resolve_conflict(self.ctx, conflict["conflict_id"], resolution="重复核对", request_key="r2")

    # -- 版本化需求与产品 ----------------------------------------------------

    def test_demand_versions_are_append_only(self):
        demand = self._demand_with_leads()
        self.assertEqual(demand["current_version"], 1)
        revised = self.expo.revise_demand(self.ctx, demand["demand_id"], {**DEMAND, "quantity_max": 1200}, request_key="rev1")
        self.assertEqual(revised["version_no"], 2)
        history = self.expo.demand_history(self.ctx, demand["demand_id"])
        self.assertEqual([v["version_no"] for v in history], [1, 2])
        self.assertEqual(history[0]["shape"]["quantity"][1], 1000)  # 旧内容保留
        self.assertEqual(history[1]["shape"]["quantity"][1], 1200)

    def test_invalid_demand_shape_rejected(self):
        with self.assertRaises(ValidationError):
            self.expo.create_demand(self.ctx, demand_identity="lead:bad", buyer_org="b", values={**DEMAND, "quantity_min": 1200}, request_key="bad")
        with self.assertRaises(ValidationError):
            self.expo.create_demand(self.ctx, demand_identity="lead:bad2", buyer_org="b", values={**DEMAND, "window_to": "2026-10-01T00:00:00+08:00"}, request_key="bad2")

    def test_only_published_product_versions_can_respond(self):
        product = self.expo.create_product(self.ctx, supplier_org="org:supplier", values=PRODUCT, request_key="pp1")
        demand = self._demand_with_leads(key="dd")
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier", fields=["category"], key="g1")
        self._grant(subject_type="product", subject_id=product["product_id"], audience="buyer:org:buyer", fields=["category"], key="g2")
        with self.assertRaises(ConflictError):
            self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)
        self.expo.publish_product_version(self.ctx, product["product_id"], 1, request_key="pub1")
        # 修订产生草稿 v2，v1 仍可用于回应
        self.expo.revise_product(self.ctx, product["product_id"], {**PRODUCT, "quantity_max": 3000}, request_key="prev2")
        scored = self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)
        self.assertEqual(scored["compatible_fields"], ["category"])
        # 草稿 v2 不影响采购商检索到仍有效的已发布 v1
        found = self.expo.search_products_for_buyer(self.ctx, buyer_org="org:buyer")
        self.assertEqual([(r["product_id"], r["version_no"]) for r in found], [(product["product_id"], 1)])
        with self.assertRaises(ConflictError):
            self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=2)

    # -- 授权字段匹配 --------------------------------------------------------

    def test_match_requires_mutual_disclosure_and_respects_fields(self):
        demand = self._demand_with_leads(key="ma")
        product = self._published_product(key="mp")
        with self.assertRaises(PermissionDenied):
            self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier", fields=["category", "certifications"], key="mb")
        self._grant(subject_type="product", subject_id=product["product_id"], audience="buyer:org:buyer", fields=["category", "quantity"], key="ms")
        scored = self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)
        self.assertEqual(scored["usable_fields"], ["category"])  # 只有双方共同授权的字段
        self.assertEqual(scored["compatible_fields"], ["category"])

    def test_expired_or_wrong_audience_grant_not_effective(self):
        demand = self._demand_with_leads(key="ea")
        product = self._published_product(key="ep")
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:other", fields=["category"], key="e1")
        self._grant(subject_type="product", subject_id=product["product_id"], audience="buyer:org:buyer", fields=["category"], key="e2")
        with self.assertRaises(PermissionDenied):
            self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier", fields=["category"], valid_until="2026-09-01T00:00:00+08:00", key="e3")
        with self.assertRaises(PermissionDenied):
            self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=product["product_id"], product_version_no=1)

    def test_match_detects_incompatible_fields(self):
        demand = self._demand_with_leads(key="ia")
        bad_product = self.expo.create_product(self.ctx, supplier_org="org:supplier", values={**PRODUCT, "certifications": ["RoHS"]}, request_key="ip1")
        self.expo.publish_product_version(self.ctx, bad_product["product_id"], 1, request_key="ip2")
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier", fields=["category", "certifications", "delivery_regions", "window"], key="i1")
        self._grant(subject_type="product", subject_id=bad_product["product_id"], audience="buyer:org:buyer", fields=["category", "certifications", "delivery_regions", "window"], key="i2")
        scored = self.expo.propose_match(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=bad_product["product_id"], product_version_no=1)
        self.assertIn("certifications", scored["incompatible_fields"])
        with self.assertRaises(ConflictError):
            self.expo.create_opportunity(self.ctx, demand_id=demand["demand_id"], demand_version_no=1, product_id=bad_product["product_id"], product_version_no=1, total_qty=100, request_key="iopp")

    def test_search_only_returns_authorized_projections(self):
        demand = self._demand_with_leads(key="sa")
        product = self._published_product(key="sp")
        # 未授权：供应商什么都搜不到
        self.assertEqual(self.expo.search_demands_for_supplier(self.ctx, supplier_org="org:supplier"), [])
        self._grant(subject_type="demand", subject_id=demand["demand_id"], audience="supplier:org:supplier", fields=["category", "delivery_regions"], key="s1")
        rows = self.expo.search_demands_for_supplier(self.ctx, supplier_org="org:supplier", filters={"category": "portable-power-station"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["disclosed_fields"]), {"category", "delivery_regions"})  # 未见数量/认证/窗口
        self.assertEqual(self.expo.search_demands_for_supplier(self.ctx, supplier_org="org:supplier", filters={"category": "other"}), [])

    # -- 会谈与纪要 ----------------------------------------------------------

    def _meeting(self, opp, key="m"):
        return self.expo.schedule_meeting(self.ctx, opp["opportunity_id"], start_at="2026-10-10T14:00:00+08:00", end_at="2026-10-10T15:00:00+08:00", venue_resource_id="room:1", venue_capacity=4, personnel=["person:a", "person:b"], request_key=key)

    def test_meeting_holds_venue_and_personnel(self):
        demand = self._demand_with_leads(key="mta")
        product = self._published_product(key="mtp")
        opp = self._opportunity(demand, product, key="mto")
        meeting = self._meeting(opp)
        self.assertEqual(meeting["status"], "requested")
        # 场地时间冲突
        with self.assertRaises(ConflictError):
            self._meeting(opp, key="m2")
        # 同一人员同一时段也无法占用（换大场地）
        with self.assertRaises(ConflictError):
            self.expo.schedule_meeting(self.ctx, opp["opportunity_id"], start_at="2026-10-10T14:30:00+08:00", end_at="2026-10-10T15:30:00+08:00", venue_resource_id="room:2", venue_capacity=4, personnel=["person:a"], request_key="m3")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "buyer", request_key="cb")
        self.assertEqual(self.expo.trace(self.ctx, opp["opportunity_id"])["meetings"][0]["status"], "requested")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "supplier", request_key="cs")
        self.assertEqual(self.expo.trace(self.ctx, opp["opportunity_id"])["meetings"][0]["status"], "confirmed")
        # 取消后释放场地与人员，可重新预约
        self.expo.cancel_meeting(self.ctx, meeting["meeting_id"], reason="改期", request_key="cx")
        again = self._meeting(opp, key="m4")
        self.assertTrue(again["meeting_id"])

    def test_minutes_double_confirmation_and_amendment_keeps_old(self):
        demand = self._demand_with_leads(key="mna")
        product = self._published_product(key="mnp")
        opp = self._opportunity(demand, product, key="mno")
        meeting = self._meeting(opp, key="mn")
        # 未双方确认不能出纪要
        with self.assertRaises(ConflictError):
            self.expo.draft_minutes(self.ctx, meeting["meeting_id"], content={"a": 1}, scope_summary="范围", proposed_by="buyer", request_key="x1")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "buyer", request_key="cb2")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "supplier", request_key="cs2")
        v1 = self.expo.draft_minutes(self.ctx, meeting["meeting_id"], content={"qty": "800"}, scope_summary="800台", proposed_by="buyer", request_key="mv1")
        self.assertEqual(v1["status"], "draft")
        self.expo.confirm_minutes(self.ctx, meeting["meeting_id"], "buyer", request_key="mc1b", notifier=self.app.outbox)
        still_draft = self.expo.minutes_history(self.ctx, meeting["meeting_id"])[0]
        self.assertEqual(still_draft["status"], "draft")
        self.expo.confirm_minutes(self.ctx, meeting["meeting_id"], "supplier", request_key="mc1s", notifier=self.app.outbox)
        # 重复确认不改变状态
        self.expo.confirm_minutes(self.ctx, meeting["meeting_id"], "buyer", request_key="mc1b2", notifier=self.app.outbox)
        # 一方改变范围：旧版保留为 superseded，新版待确认
        v2 = self.expo.amend_minutes(self.ctx, meeting["meeting_id"], content={"qty": "300+500"}, scope_summary="首批300", proposed_by="supplier", request_key="mv2")
        self.assertEqual(v2["status"], "draft")
        self.assertEqual(v2["minutes_no"], 2)
        history = self.expo.minutes_history(self.ctx, meeting["meeting_id"])
        self.assertEqual([m["status"] for m in history], ["superseded", "draft"])
        self.assertEqual(history[0]["content"], {"qty": "800"})  # 旧内容仍可查
        self.assertTrue(history[0]["buyer_confirmed"] and history[0]["supplier_confirmed"])
        # 待确认期间不能连续再改
        with self.assertRaises(ConflictError):
            self.expo.amend_minutes(self.ctx, meeting["meeting_id"], content={"qty": "100"}, scope_summary="再改", proposed_by="buyer", request_key="mv3")

    # -- 管线、回执与部分成交 -----------------------------------------------

    def test_pipeline_receipts_partial_fulfilment_and_exit(self):
        demand = self._demand_with_leads(key="pa")
        product = self._published_product(key="pp2")
        opp = self._opportunity(demand, product, qty=800, key="po")
        opp_id = opp["opportunity_id"]
        # 样品阶段必须给期限
        with self.assertRaises(ValidationError):
            self.expo.advance_stage(self.ctx, opp_id, stage="sample", quantity=2, owner_id="person:s", receipt_key="r0", request_key="a0")
        sample = self.expo.advance_stage(self.ctx, opp_id, stage="sample", quantity=2, owner_id="person:s", receipt_key="r-sample", request_key="a1", sample_expires_at="2026-10-15T18:00:00+08:00", scheduler=self.app.jobs)
        self.assertTrue(sample["job_id"])
        # 同一业务回执重复到达：不再次推进
        replay = self.expo.advance_stage(self.ctx, opp_id, stage="sample", quantity=2, owner_id="person:s", receipt_key="r-sample", request_key="a2", sample_expires_at="2026-10-15T18:00:00+08:00", scheduler=self.app.jobs)
        self.assertEqual(replay["status"], "duplicate")
        # 标识相同内容冲突：进主管队列，不推进
        with self.assertRaises(ConflictError):
            self.expo.advance_stage(self.ctx, opp_id, stage="sample", quantity=3, owner_id="person:s", receipt_key="r-sample", request_key="a3", sample_expires_at="2026-10-15T18:00:00+08:00")
        self.assertEqual(self.expo.list_receipt_conflicts(self.ctx, kind="receipt")[0]["intake_key"], "r-sample")
        # 阶段不能回退
        self.expo.advance_stage(self.ctx, opp_id, stage="quote", quantity=300, owner_id="person:q", receipt_key="r-q", request_key="a4")
        with self.assertRaises(ConflictError):
            self.expo.advance_stage(self.ctx, opp_id, stage="sample", quantity=1, owner_id="person:s2", receipt_key="r-s2", request_key="a5", sample_expires_at="2026-10-15T18:00:00+08:00")
        self.expo.advance_stage(self.ctx, opp_id, stage="framework", quantity=300, owner_id="person:l", receipt_key="r-f", request_key="a6")
        # 超过剩余数量被拒绝
        with self.assertRaises(ConflictError):
            self.expo.advance_stage(self.ctx, opp_id, stage="contract", quantity=801, owner_id="person:c", receipt_key="r-big", request_key="a7")
        # 部分成交：剩余机会不关闭
        partial = self.expo.advance_stage(self.ctx, opp_id, stage="contract", quantity=300, owner_id="person:c", receipt_key="r-c1", request_key="a8", followup_at="2026-12-01T10:00:00+08:00", scheduler=self.app.jobs)
        self.assertEqual(partial["opportunity"]["status"], "open")
        self.assertEqual(partial["opportunity"]["remaining_qty"], 500)
        self.assertEqual(partial["opportunity"]["fulfilled_qty"], 300)
        # 剩余份额可由另一个责任人继续成交
        rest = self.expo.advance_stage(self.ctx, opp_id, stage="contract", quantity=500, owner_id="person:c2", receipt_key="r-c2", request_key="a9")
        self.assertEqual(rest["opportunity"]["status"], "fulfilled")
        self.assertEqual(rest["opportunity"]["remaining_qty"], 0)
        # 已成交不能再推进
        with self.assertRaises(ConflictError):
            self.expo.advance_stage(self.ctx, opp_id, stage="contract", quantity=1, owner_id="person:c3", receipt_key="r-c3", request_key="a10")

    def test_exit_closes_remaining_opportunity(self):
        demand = self._demand_with_leads(key="xa")
        product = self._published_product(key="xp")
        opp = self._opportunity(demand, product, qty=100, key="xo")
        result = self.expo.advance_stage(self.ctx, opp["opportunity_id"], stage="exited", quantity=0, owner_id="person:lead", receipt_key="r-exit", request_key="e1", note="采购商放弃")
        self.assertEqual(result["opportunity"]["status"], "exited")
        self.assertEqual(result["opportunity"]["remaining_qty"], 100)

    # -- 中断恢复与溯源 ------------------------------------------------------

    def test_worklist_survives_restart(self):
        demand = self._demand_with_leads(key="wa")
        product = self._published_product(key="wp")
        opp = self._opportunity(demand, product, qty=800, key="wo")
        meeting = self._meeting(opp, key="wm")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "buyer", request_key="wb")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "supplier", request_key="ws")
        self.expo.draft_minutes(self.ctx, meeting["meeting_id"], content={"qty": 800}, scope_summary="800", proposed_by="buyer", request_key="wmin")
        self.expo.advance_stage(self.ctx, opp["opportunity_id"], stage="sample", quantity=2, owner_id="person:s", receipt_key="wrs", request_key="wa1", sample_expires_at="2026-10-15T18:00:00+08:00", scheduler=self.app.jobs)
        self.expo.advance_stage(self.ctx, opp["opportunity_id"], stage="contract", quantity=300, owner_id="person:c", receipt_key="wrc", request_key="wa2", followup_at="2026-12-01T10:00:00+08:00", scheduler=self.app.jobs)
        # 服务中断：重新打开应用，时钟拨到样品期限之后
        later = CivicFlow.open(self.db_path, fixed_now="2026-10-16T09:00:00+08:00")
        worklist = later.expo.worklist(AccessContext.system("tester"))
        pending = worklist["pending_minutes"]
        self.assertEqual([m["minutes_no"] for m in pending], [1])
        samples = worklist["sample_deadlines"]
        self.assertEqual(len(samples), 1)
        self.assertTrue(samples[0]["needs_action"])  # 样品期限已过且任务未完成
        self.assertEqual(len(worklist["fulfillment_followups"]), 1)  # 部分成交也要回访
        # 持久任务可被领取并完成，完成后不再提示需要处理
        claimed = later.jobs.claim_due()
        self.assertEqual([j["job_type"] for j in claimed], ["expo.sample_due"])
        later.jobs.finish(claimed[0]["job_id"])
        worklist2 = later.expo.worklist(AccessContext.system("tester"))
        self.assertFalse(worklist2["sample_deadlines"][0]["needs_action"])

    def test_trace_from_deal_covers_versions_grants_meetings_and_handovers(self):
        demand = self._demand_with_leads(key="ta")
        # 需求修订到 v2，机会冻结在 v1
        self.expo.revise_demand(self.ctx, demand["demand_id"], {**DEMAND, "quantity_max": 1500}, request_key="trev")
        product = self._published_product(key="tp")
        self.expo.revise_product(self.ctx, product["product_id"], {**PRODUCT, "quantity_max": 3000}, request_key="tprev")
        self.expo.publish_product_version(self.ctx, product["product_id"], 2, request_key="tpublish2")
        opp = self._opportunity(demand, product, qty=800, key="to")
        meeting = self._meeting(opp, key="tm")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "buyer", request_key="tcb")
        self.expo.confirm_meeting(self.ctx, meeting["meeting_id"], "supplier", request_key="tcs")
        self.expo.draft_minutes(self.ctx, meeting["meeting_id"], content={"qty": 800}, scope_summary="800", proposed_by="buyer", request_key="tmin")
        self.expo.confirm_minutes(self.ctx, meeting["meeting_id"], "buyer", request_key="tmcb", notifier=self.app.outbox)
        self.expo.confirm_minutes(self.ctx, meeting["meeting_id"], "supplier", request_key="tmcs", notifier=self.app.outbox)
        self.expo.advance_stage(self.ctx, opp["opportunity_id"], stage="sample", quantity=2, owner_id="person:s", receipt_key="trs", request_key="ta1", sample_expires_at="2026-10-15T18:00:00+08:00")
        self.expo.advance_stage(self.ctx, opp["opportunity_id"], stage="contract", quantity=800, owner_id="person:c", receipt_key="trc", request_key="ta2")
        trace = self.expo.trace(self.ctx, opp["opportunity_id"])
        self.assertEqual(trace["demand_version"]["version_no"], 1)  # 冻结的成交依据
        self.assertEqual(len(trace["demand_versions"]), 2)
        self.assertEqual(trace["product_version"]["version_no"], 1)
        self.assertEqual(len(trace["product_versions"]), 2)
        self.assertEqual({s["source"] for s in trace["sources"]}, {"concierge", "booth", "delegation"})
        self.assertEqual(set(trace["match_grants"]), {"buyer", "supplier"})
        self.assertTrue(trace["match_grants"]["buyer"]["fields"])
        self.assertEqual(trace["meetings"][0]["minutes"][0]["status"], "confirmed")
        owners = {(step["stage"], step["owner_id"]) for step in trace["stage_owners"]}
        self.assertEqual(owners, {("sample", "person:s"), ("contract", "person:c")})


if __name__ == "__main__":
    unittest.main()
