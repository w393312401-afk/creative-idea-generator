#!/usr/bin/env python3
"""Validate a video beat package without pretending to inspect video semantics.

Standard library only. Exit 0: no structural errors (warnings may remain),
1: invalid package, 2: unreadable input/tool or export failure.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys


MODES = {"reverse", "ideas", "prompts", "full"}
EPS = 0.001


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def valid_id(value):
    return nonempty(value) or (isinstance(value, int) and not isinstance(value, bool) and value > 0)


def local_path(value, base):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


class Validator:
    def __init__(self, package, base):
        self.p = package
        self.base = Path(base)
        self.errors = []
        self.warnings = []
        self.frames = {}
        self.candidates = {}
        self.evidence_duration = None
        self.evidence_reviewed = True
        self.render_status = "not_run"
        self.actor_contract_declared = False
        self.actor_rows = {}

    def issue(self, code, path, message, warning=False):
        (self.warnings if warning else self.errors).append(
            {"code": code, "path": path, "message": message})

    def need_text(self, obj, key, path):
        if not nonempty(obj.get(key)):
            self.issue("required_text", f"{path}.{key}", "需要非空文本。")

    def array(self, obj, key, path="package"):
        value = obj.get(key)
        if not isinstance(value, list):
            self.issue("required_array", f"{path}.{key}", "需要数组；无此产物时使用 []。")
            return []
        return value

    def indexed(self, rows, path, continuous=False):
        result = {}
        prefixes = set()
        for index, row in enumerate(rows, 1):
            where = f"{path}[{index - 1}]"
            if not isinstance(row, dict):
                self.issue("object_required", where, "条目必须是对象。")
                continue
            value = row.get("id")
            if not valid_id(value):
                self.issue("id_required", where, "需要非空 id。")
                continue
            if value in result:
                self.issue("duplicate_id", where, f"重复 id: {value}")
            result[value] = row
            if continuous:
                match = re.fullmatch(r"(.*?)(\d+)", str(value))
                if not match or int(match.group(2)) != index:
                    self.issue("numbering", where, "编号需按数组顺序从 1 连续递增，例如 I001、I002。")
                elif match:
                    prefixes.add(match.group(1))
        if continuous and len(prefixes) > 1:
            self.issue("numbering_prefix", path, "同类条目的编号前缀必须一致。")
        return result

    def interval(self, row, path, duration):
        start, end = row.get("start_sec"), row.get("end_sec")
        if not number(start) or not number(end) or start < 0 or end <= start:
            self.issue("invalid_interval", path, "start_sec/end_sec 必须是有限秒数，0 <= start < end；不设最短拍长。")
            return None
        if duration is not None and end > duration + EPS:
            self.issue("time_out_of_range", path, "结束时间超过源片时长。")
        return start, end

    def read_evidence(self):
        value = self.p.get("evidence_file")
        if value is None:
            return
        if not nonempty(value):
            self.issue("evidence_path", "evidence_file", "应为证据索引路径或 null。")
            return
        target = local_path(value, self.base)
        try:
            evidence = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.issue("evidence_unreadable", "evidence_file", str(exc))
            return
        if not isinstance(evidence, dict):
            self.issue("evidence_object", "evidence_file", "证据索引须为对象。")
            return
        if evidence.get("version") != 1:
            self.issue("evidence_version", "evidence_file.version", "仅支持 version: 1。")
        source = evidence.get("source", {})
        evidence_source_path = source.get("path") if isinstance(source, dict) else None
        package_source = self.p.get("source")
        package_source_path = package_source.get("path") if isinstance(package_source, dict) else None
        if nonempty(evidence_source_path) and nonempty(package_source_path):
            if local_path(evidence_source_path, target.parent).resolve() != local_path(package_source_path, self.base).resolve():
                self.issue("evidence_source_mismatch", "evidence_file.source.path", "证据索引绑定的源片路径与 package.source.path 不一致。")
        elif evidence_source_path is None:
            self.issue("evidence_source_path_missing", "evidence_file.source.path", "旧证据索引缺少源片路径，无法核对是否绑定同一视频。", True)
        elif not nonempty(evidence_source_path):
            self.issue("evidence_source_path_invalid", "evidence_file.source.path", "源片路径必须为非空文本。")
        origin = source.get("time_origin_sec") if isinstance(source, dict) else None
        if evidence.get("review_status") in {"pending", "unreviewed"}:
            self.evidence_reviewed = False
            self.issue("unreviewed_evidence_index", "evidence_file.review_status", "证据索引仍标为未审核。", True)
        self.evidence_duration = source.get("duration_sec") if isinstance(source, dict) else None
        if not number(self.evidence_duration) or self.evidence_duration <= 0:
            self.issue("evidence_duration", "evidence_file.source", "需要有效源片时长。")
            self.evidence_duration = None
        rows = self.array(evidence, "frames", "evidence_file")
        self.frames = self.indexed(rows, "evidence_file.frames")
        for ident, frame in self.frames.items():
            where = f"evidence_file.frames.{ident}"
            name = frame.get("file")
            if not nonempty(name) or not local_path(name, target.parent).is_file():
                self.issue("missing_frame_file", where, "证据帧文件不存在。")
            for field in ("actual_time_sec", "requested_time_sec"):
                t = frame.get(field)
                if not number(t) or t < 0 or (self.evidence_duration is not None and t > self.evidence_duration + EPS):
                    self.issue("frame_time", f"{where}.{field}", "帧时间无效或超出源片范围。")
            pts = frame.get("pts_time_sec")
            if pts is not None:
                if not number(pts) or not number(origin):
                    self.issue("pts_origin", where, "有 pts_time_sec 时需要有效 source.time_origin_sec。")
                elif number(frame.get("actual_time_sec")) and abs(frame["actual_time_sec"] - (pts - origin)) > 0.00001:
                    self.issue("pts_actual_mismatch", where, "actual_time_sec 与 pts_time_sec - source.time_origin_sec 不一致。")
                if "pts_time_raw" in frame:
                    try:
                        raw = float(frame["pts_time_raw"])
                        if not math.isfinite(raw) or not number(pts) or abs(raw - pts) > 0.00001:
                            raise ValueError("mismatch")
                    except (ValueError, TypeError):
                        self.issue("pts_raw_mismatch", where, "pts_time_raw 与 pts_time_sec 不一致。")
        rows = self.array(evidence, "candidates", "evidence_file")
        self.candidates = self.indexed(rows, "evidence_file.candidates")
        for ident, candidate in self.candidates.items():
            where = f"evidence_file.candidates.{ident}"
            t = candidate.get("time_sec")
            if not number(t) or t < 0 or (self.evidence_duration is not None and t > self.evidence_duration + EPS):
                self.issue("candidate_time", where, "候选时间无效。")
            if not number(candidate.get("score")):
                self.issue("candidate_score", where, "候选需要有限数值 score。")
            status = candidate.get("review_status", "unreviewed")
            if status not in {"accepted", "rejected", "motion", "unreviewed", "pending"}:
                self.issue("candidate_status", where, "review_status 必须为 accepted/rejected/motion/unreviewed/pending。")
            if status in {"rejected", "motion"} and not nonempty(candidate.get("reason")):
                self.issue("candidate_reason", where, "驳回候选或判定为运镜需填写 reason。")
            if status in {"unreviewed", "pending"}:
                self.evidence_reviewed = False
                self.issue("unreviewed_candidate", where, "变化候选尚未人工审阅，结构检查不能代替画面复核。", True)

    def observed(self, rows, duration, spaces):
        beats = self.indexed(rows, "observed_beats", continuous=True)
        intervals = []
        linked_candidates = set()
        last_start = -1
        for ident, beat in beats.items():
            where = f"observed_beats.{ident}"
            for key in ("space_id", "action", "state_before", "state_after"):
                self.need_text(beat, key, where)
            space_id = beat.get("space_id")
            if spaces and space_id not in spaces:
                self.issue("space_reference", where, f"未知空间: {space_id}")
            interval = self.interval(beat, where, duration)
            if interval:
                if interval[0] < last_start:
                    self.issue("beat_order", where, "观察节拍须按原片开始时间排序。")
                last_start = interval[0]
                intervals.append(interval)
            frame_ids = self.array(beat, "evidence_frame_ids", where)
            if not frame_ids:
                self.issue("missing_evidence", where, "观察拍至少引用一张实际证据帧。")
            tolerance = beat.get("boundary_tolerance_sec", 0)
            if not number(tolerance) or not 0 <= tolerance <= 0.5:
                self.issue("boundary_tolerance", where, "边界容差只能在 0–0.5 秒之间。")
                tolerance = 0
            if tolerance and not nonempty(beat.get("boundary_reason")):
                self.issue("boundary_reason", where, "使用边界容差需说明 boundary_reason。")
            for frame_id in frame_ids:
                frame = self.frames.get(frame_id) if isinstance(frame_id, str) else None
                if frame is None:
                    self.issue("frame_reference", where, f"未知证据帧: {frame_id}")
                elif interval and number(frame.get("actual_time_sec")):
                    t = frame["actual_time_sec"]
                    if t < interval[0] - tolerance - EPS or t > interval[1] + tolerance + EPS:
                        self.issue("frame_outside_beat", where, f"帧 {frame_id} 实际时间 {t} 不在该拍范围内。")
            self.array(beat, "uncertainties", where)
            review = beat.get("evidence_review_status", "unreviewed")
            if review not in {"reviewed", "unreviewed"}:
                self.issue("beat_review_status", where, "evidence_review_status 必须为 reviewed/unreviewed。")
            if review != "reviewed":
                self.evidence_reviewed = False
                self.issue("unreviewed_beat", where, "该拍尚未声明已人工核对画面。", True)
            if not isinstance(beat.get("candidate_ids", []), list):
                self.issue("candidate_ids_array", where, "candidate_ids 必须是数组。")
            for candidate_id in beat.get("candidate_ids", []) if isinstance(beat.get("candidate_ids", []), list) else []:
                candidate = self.candidates.get(candidate_id) if isinstance(candidate_id, str) else None
                if candidate is None:
                    self.issue("candidate_reference", where, f"未知变化候选: {candidate_id}")
                elif candidate.get("review_status") != "accepted":
                    self.issue("candidate_not_accepted", where, "映射到节拍的候选应标为 accepted。")
                else:
                    linked_candidates.add(candidate_id)
        for ident, candidate in self.candidates.items():
            if candidate.get("review_status") == "accepted" and ident not in linked_candidates:
                self.issue("accepted_candidate_unmapped", f"candidates.{ident}", "已接纳候选尚未通过 beat.candidate_ids 映射到任何节拍。")
        explanations = self.p.get("coverage_explanations", [])
        if not isinstance(explanations, list):
            self.issue("coverage_array", "coverage_explanations", "需要区间说明数组。")
            explanations = []
        for index, row in enumerate(explanations):
            where = f"coverage_explanations[{index}]"
            if not isinstance(row, dict):
                self.issue("coverage_object", where, "说明必须是对象。")
                continue
            self.need_text(row, "reason", where)
            interval = self.interval(row, where, duration)
            if interval:
                intervals.append(interval)
        if rows and duration is not None:
            cursor = 0
            for start, end in sorted(intervals):
                if start > cursor + EPS:
                    self.issue("coverage_gap", "observed_beats", f"未覆盖原片 {cursor:.3f}–{start:.3f} 秒，补节拍或填写覆盖说明。")
                cursor = max(cursor, end)
            if cursor < duration - EPS:
                self.issue("coverage_gap", "observed_beats", f"未覆盖原片 {cursor:.3f}–{duration:.3f} 秒。")
        return beats

    def actor_contract(self, contract):
        """Check declared identities and heights; appearance remains a human visual check."""
        if "actors" not in contract:
            return
        self.actor_contract_declared = True
        rows = self.array(contract, "actors", "spatial_contract")
        for index, actor in enumerate(rows):
            where = f"spatial_contract.actors[{index}]"
            if not isinstance(actor, dict):
                self.issue("object_required", where, "人物合同条目必须是对象。")
                continue
            ident = actor.get("id")
            if not nonempty(ident):
                self.issue("actor_id_required", f"{where}.id", "人物 id 必须为非空字符串。")
            elif ident in self.actor_rows:
                self.issue("duplicate_actor_id", f"{where}.id", f"重复人物 id: {ident}")
            else:
                self.actor_rows[ident] = actor
            for field in ("reference_source", "appearance", "height_provenance", "state_baseline"):
                self.need_text(actor, field, where)
            if "height_m" not in actor:
                self.issue("actor_height_missing", f"{where}.height_m", "需要 height_m；米数未知时明确填 null 并在 height_provenance 说明 H 的依据。")
            else:
                height = actor["height_m"]
                if height is not None and (not number(height) or height <= 0):
                    self.issue("actor_height", f"{where}.height_m", "人物身高必须为正米数或 null；不能为通过检查而编造米数。")
        reference_id = contract.get("reference_actor_id")
        reference_actor = None
        if "reference_actor_id" in contract:
            if not nonempty(reference_id) or reference_id not in self.actor_rows:
                self.issue("reference_actor_id", "spatial_contract.reference_actor_id", "reference_actor_id 必须引用 actors 中的非空字符串 id。")
            else:
                reference_actor = self.actor_rows[reference_id]
        elif len(self.actor_rows) == 1:
            reference_actor = next(iter(self.actor_rows.values()))
        reference_height = contract.get("reference_height_m")
        if reference_actor is not None:
            actor_height = reference_actor.get("height_m")
            if number(reference_height) and number(actor_height) and abs(reference_height - actor_height) > EPS:
                self.issue("actor_height_mismatch", "spatial_contract.reference_height_m", "全局参考身高与对应人物的 height_m 不一致；多人场景只对照 reference_actor_id 指向的人物。")

    def actor_references(self, row, path, required=False):
        if not self.actor_contract_declared:
            return
        if "actor_ids" not in row:
            if required:
                self.issue("actor_ids_missing", f"{path}.actor_ids", "已有人物合同的生成段应显式列出 actor_ids；无人段使用 []。", warning=True)
            return
        values = row["actor_ids"]
        if not isinstance(values, list):
            self.issue("actor_ids_array", f"{path}.actor_ids", "actor_ids 必须是数组；无人时使用 []。")
            return
        seen = set()
        for index, ident in enumerate(values):
            where = f"{path}.actor_ids[{index}]"
            if not nonempty(ident):
                self.issue("actor_reference", where, "人物引用必须是 actors 中的非空字符串 id。")
                continue
            if ident in seen:
                self.issue("duplicate_actor_reference", where, f"重复人物引用: {ident}")
            seen.add(ident)
            if ident not in self.actor_rows:
                self.issue("actor_reference", where, f"未知人物: {ident}")
        if path.startswith("images.") and values and row.get("people_allowed") is not True:
            self.issue("image_actor_ids_people", f"{path}.actor_ids", "状态图声明人物引用但未设置 people_allowed: true；状态图默认无人，需明确例外或清空 actor_ids。", warning=True)

    def spatial(self, production_requested):
        contract = self.p.get("spatial_contract")
        if not isinstance(contract, dict):
            self.issue("spatial_object", "spatial_contract", "需要空间合同对象。")
            return {}
        status = contract.get("status")
        if status not in {"unplanned", "planned", "reference_conflict"}:
            self.issue("spatial_status", "spatial_contract", "status 必须为 unplanned/planned/reference_conflict。")
        if production_requested and status == "unplanned":
            self.issue("space_not_planned", "spatial_contract", "生成提示词须有独立 planned 空间方案；保留原片冲突到观察记录。")
        height = contract.get("reference_height_m")
        if height is not None and (not number(height) or height <= 0):
            self.issue("actor_height", "spatial_contract.reference_height_m", "人物参考身高必须为正米数或 null。")
        self.need_text(contract, "reference_height_provenance", "spatial_contract")
        self.actor_contract(contract)
        spaces = self.indexed(self.array(contract, "spaces", "spatial_contract"), "spatial_contract.spaces")
        if production_requested and not spaces:
            self.issue("missing_spaces", "spatial_contract", "生成需至少一个空间定义。")
        for ident, space in spaces.items():
            where = f"spatial_contract.spaces.{ident}"
            self.need_text(space, "shape", where)
            self.need_text(space, "provenance", where)
            dims = space.get("dimensions_m")
            if not isinstance(dims, dict):
                self.issue("dimensions_object", where, "需要 dimensions_m；未知值明确填 null。")
                continue
            for field in ("width", "length", "height"):
                if field not in dims:
                    self.issue("dimension_missing", f"{where}.{field}", "必需尺寸键，未知填 null。")
            for field, value in dims.items():
                if value is not None and (not number(value) or value <= 0):
                    self.issue("dimension_value", f"{where}.{field}", "尺寸必须为正米数或 null。")
            if production_requested and any(dims.get(k) is None for k in ("width", "length", "height")):
                self.issue("unknown_dimension", where, "空间仍有未知尺寸，不能宣称比例已验证。", True)
            rectangle = space.get("contained_rect_m")
            diameter = dims.get("diameter")
            if rectangle is not None:
                if not isinstance(rectangle, dict) or not all(number(rectangle.get(k)) and rectangle[k] > 0 for k in ("width", "length")):
                    self.issue("contained_rectangle", where, "contained_rect_m 需有效 width/length。")
                elif number(diameter) and math.hypot(rectangle["width"], rectangle["length"]) > diameter + EPS:
                    self.issue("rectangle_exceeds_circle", where, "声明内含的矩形对角线大于圆形直径；尺寸与形状不相容。", not production_requested)
        checks = self.indexed(self.array(contract, "checks", "spatial_contract"), "spatial_contract.checks")
        if production_requested and not checks:
            self.issue("missing_spatial_checks", "spatial_contract", "生成需记录空间检查，即使结果为 unverified。")
        corrections = contract.get("corrections", [])
        corrected = set()
        if not isinstance(corrections, list):
            self.issue("corrections_array", "spatial_contract.corrections", "corrections 必须是数组。")
            corrections = []
        for index, correction in enumerate(corrections):
            where = f"spatial_contract.corrections[{index}]"
            if not isinstance(correction, dict):
                self.issue("correction_object", where, "修正记录必须是对象。")
                continue
            source_id = correction.get("source_check_id")
            if not isinstance(source_id, (str, int)) or source_id not in checks:
                self.issue("correction_reference", where, "source_check_id 须引用原片检查。")
            elif checks[source_id].get("scope") != "source":
                self.issue("correction_scope", where, "corrections 应引用 scope: source 的检查。")
            elif nonempty(correction.get("resolution")):
                corrected.add(source_id)
            self.need_text(correction, "resolution", where)
        if production_requested and status == "reference_conflict" and not corrected:
            self.issue("uncorrected_reference_conflict", "spatial_contract", "原片冲突需要 corrections 记录对应设计修正。")
        for ident, check in checks.items():
            where = f"spatial_contract.checks.{ident}"
            self.need_text(check, "reason", where)
            scope = check.get("scope", "design")
            if scope not in {"source", "design"}:
                self.issue("check_scope", where, "scope 必须为 source/design。")
            check_status = check.get("status")
            if check_status not in {"pass", "fail", "unverified"}:
                self.issue("spatial_check_status", where, "检查状态必须为 pass/fail/unverified。")
            elif check_status == "fail":
                is_source = scope == "source"
                self.issue("spatial_check_failed", where, "空间检查已失败；生成必须修复，原片观察则保留冲突。", is_source or not production_requested)
                if production_requested and is_source and ident not in corrected:
                    self.issue("source_conflict_uncorrected", where, "原片冲突尚无对应 corrections 设计修正记录。")
            elif check_status == "unverified":
                self.issue("spatial_unverified", where, "尚未验证的空间检查。", True)
        self.spatial_checks = checks
        return spaces

    def production(self, rows, image_rows, beats):
        segments = self.indexed(rows, "production_segments", continuous=True)
        images = self.indexed(image_rows, "images", continuous=True)
        if rows and len(image_rows) != len(rows) + 1:
            self.issue("anchor_count", "images", "线性链 N 段视频必须对应 N+1 张状态图。")
        for ident, image in images.items():
            self.need_text(image, "prompt", f"images.{ident}")
            self.check_prompt(image.get("prompt"), f"images.{ident}.prompt")
            self.check_image_people(image, f"images.{ident}")
            self.actor_references(image, f"images.{ident}")
        image_ids = list(images)
        covered_beats = set()
        for index, (ident, segment) in enumerate(segments.items()):
            where = f"production_segments.{ident}"
            self.actor_references(segment, where, required=bool(self.actor_rows))
            if segment.get("kind", "construction") not in {"construction", "bridge", "reveal"}:
                self.issue("segment_kind", where, "kind 应为 construction/bridge/reveal。")
            for key in ("operation", "prompt", "start_image_id", "end_image_id"):
                if key.endswith("image_id"):
                    if not valid_id(segment.get(key)):
                        self.issue("image_id_required", f"{where}.{key}", "需要图片 id。")
                else:
                    self.need_text(segment, key, where)
            self.check_prompt(segment.get("prompt"), f"{where}.prompt")
            duration = segment.get("duration_sec")
            if not number(duration) or duration <= 0:
                self.issue("segment_duration", where, "duration_sec 必须是正秒数，区别于原片时长。")
            sources = self.array(segment, "source_beat_ids", where)
            for source_id in sources:
                if not isinstance(source_id, str) or source_id not in beats:
                    self.issue("source_beat_reference", where, f"未知来源节拍: {source_id}")
                else:
                    covered_beats.add(source_id)
            origin = segment.get("origin")
            if origin not in {"observed_adaptation", "creative_design", "generation_expansion"}:
                self.issue("segment_origin", where, "origin 需为 observed_adaptation/creative_design/generation_expansion。")
            if origin == "observed_adaptation" and not sources:
                self.issue("missing_source_beat", where, "原片改编段必须引用至少一个观察节拍。")
            if origin == "generation_expansion" and not nonempty(segment.get("expansion_reason")):
                self.issue("expansion_reason", where, "生成展开需说明 expansion_reason。")
            for key, expected_index in (("start_image_id", index), ("end_image_id", index + 1)):
                value = segment.get(key)
                if not valid_id(value) or value not in images:
                    self.issue("image_reference", where, f"未知 {key}: {value}")
                elif expected_index < len(image_ids) and value != image_ids[expected_index]:
                    self.issue("anchor_chain", where, "首尾图片必须按状态图顺序相邻引用，后一段承接前段尾图。")
        explanations = self.p.get("production_coverage_explanations", [])
        if not isinstance(explanations, list):
            self.issue("production_coverage_array", "production_coverage_explanations", "需要原拍复用或省略说明数组。")
            explanations = []
        explained = set()
        for index, explanation in enumerate(explanations):
            where = f"production_coverage_explanations[{index}]"
            if not isinstance(explanation, dict):
                self.issue("production_coverage_object", where, "原拍去向说明须为对象。")
                continue
            source_id = explanation.get("source_beat_id")
            if not isinstance(source_id, str) or source_id not in beats:
                self.issue("production_coverage_reference", where, "source_beat_id 需引用已有观察拍。")
            elif nonempty(explanation.get("reason")) and explanation.get("disposition") in {"reuse", "omit"}:
                explained.add(source_id)
            self.need_text(explanation, "reason", where)
            if explanation.get("disposition") not in {"reuse", "omit"}:
                self.issue("production_coverage_disposition", where, "disposition 必须是 reuse/omit。")
        if rows:
            for ident in beats.keys() - covered_beats - explained:
                self.issue("observed_beat_lost", f"observed_beats.{ident}", "原拍没有生成映射；需保留或说明后期复用/创意省略的理由，不能默默丢拍。")
        return segments, images

    def check_prompt(self, value, path):
        if not isinstance(value, str):
            return
        if re.search(r"(?im)^\s*(?:同上|略|待补充|TODO|TBD|\.\.\.|…|same as above|insert prompt here)\s*[。.：:]?\s*$", value) or re.search(r"\{\{[^}]+\}\}|\[(?:TODO|TBD|INSERT)[^\]]*\]", value, re.I):
            self.issue("placeholder_prompt", path, "提示词含待填占位内容；交付需完整正文。")

    def check_image_people(self, image, path):
        """Static state images are people-free by default; a person used only as a scale reference slips in easily."""
        prompt = image.get("prompt")
        if not isinstance(prompt, str) or image.get("people_allowed") is True:
            return
        nouns = r"(?:man|men|woman|women|person|people|worker|workers|builder|human|figure|silhouette)"
        negated = rf"\b(?:no|without|zero|free of|free from|empty of|devoid of|never|not any|absence of)\s+(?:\w+\s+){{0,2}}{nouns}\b"
        remaining = re.sub(negated, " ", prompt, flags=re.I)
        found = sorted({m.group(0).lower() for m in re.finditer(rf"\b{nouns}\b", remaining, re.I)})
        if found:
            self.issue("image_prompt_people", f"{path}.prompt",
                       f"状态图提示词出现人物名词 {', '.join(found)}；默认应无人。尺度参照改用无人尺度物或放入不进入成片的设计参考；"
                       "若用户明确要求人物入图，设置 people_allowed: true 并在审核记录说明。", warning=True)

    def render(self):
        review = self.p.get("render_review")
        if not isinstance(review, dict):
            self.issue("render_object", "render_review", "需要对象，未渲染使用 {status: not_run}。")
            return
        status = review.get("status", "not_run")
        self.render_status = status
        if status not in {"not_run", "pass", "fail"}:
            self.issue("render_status", "render_review", "状态必须为 not_run/pass/fail。")
            return
        if status == "fail":
            self.issue("render_failed", "render_review", "人工渲染复核已失败，不能作为验收通过交付。")
        if status == "pass":
            frames = review.get("reviewed_frames", [])
            existing_frames = set()
            if not isinstance(frames, list) or not frames:
                self.issue("render_evidence_required", "render_review", "通过复核需列出实际 reviewed_frames 文件。")
            else:
                for frame in frames:
                    if not nonempty(frame) or not local_path(frame, self.base).is_file():
                        self.issue("render_frame_missing", "render_review.reviewed_frames", "复核帧文件不存在。")
                    else:
                        existing_frames.add(local_path(frame, self.base).resolve())
            checks = getattr(self, "spatial_checks", {})
            design_checks = [c for c in checks.values() if c.get("scope", "design") == "design"]
            if not design_checks or any(c.get("status") != "pass" for c in design_checks):
                self.issue("render_checks_incomplete", "render_review", "渲染通过需有全部 pass 的空间检查记录；失败或未验证检查不能装作通过。")
            checks = self.indexed(self.array(review, "checks", "render_review"), "render_review.checks")
            if not checks:
                self.issue("render_checks_required", "render_review.checks", "必须有针对实际渲染画面的独立检查记录，不能拿文本设计检查代替。")
            contract = self.p.get("spatial_contract", {})
            actors = contract.get("actors") if isinstance(contract, dict) else None
            used_actors = any(isinstance(row, dict) and isinstance(row.get("actor_ids"), list)
                              and bool(row["actor_ids"])
                              for key in ("production_segments", "images")
                              for row in (self.p.get(key) if isinstance(self.p.get(key), list) else []))
            if isinstance(actors, list) and actors and used_actors:
                categories = {check.get("category", ident) for ident, check in checks.items()
                              if isinstance(check.get("category", ident), str)}
                for category in sorted({"identity_continuity", "actor_scale", "space_continuity"} - categories):
                    self.issue("render_actor_review_missing", "render_review.checks",
                               f"有人物的画面通过声明缺少 {category} 独立验收；无人锚点或单项比例检查不能代替。")
            for ident, check in checks.items():
                where = f"render_review.checks.{ident}"
                self.need_text(check, "reason", where)
                if check.get("status") != "pass":
                    self.issue("render_pixel_check_failed", where, "声明渲染通过时，实际画面检查须全部 pass。")
                evidence = self.array(check, "evidence", where)
                if not evidence:
                    self.issue("render_check_evidence", where, "每条实际画面检查需引用已复核帧。")
                for frame in evidence:
                    if not nonempty(frame) or local_path(frame, self.base).resolve() not in existing_frames:
                        self.issue("render_check_evidence", where, "检查证据须引用 reviewed_frames 中存在的帧。")
            self.need_text(review, "review_note", "render_review")

    def run(self):
        if not isinstance(self.p, dict):
            self.issue("package_object", "package", "输入必须是 JSON 对象。")
            return self.report()
        for key in ("version", "title", "mode", "source", "evidence_file", "observed_beats", "variants", "production_segments", "images", "spatial_contract", "render_review"):
            if key not in self.p:
                self.issue("missing_field", key, "缺少顶层字段。")
        if self.p.get("version") != 1:
            self.issue("version", "version", "仅支持 version: 1。")
        self.need_text(self.p, "title", "package")
        mode = self.p.get("mode")
        if mode not in MODES:
            self.issue("mode", "mode", "mode 必须为 reverse/ideas/prompts/full。")
        source = self.p.get("source")
        duration = None
        if source is not None:
            if not isinstance(source, dict):
                self.issue("source_object", "source", "source 必须为对象或原创任务的 null。")
            else:
                name = source.get("path")
                if not nonempty(name) or not local_path(name, self.base).is_file():
                    self.issue("source_missing", "source.path", "源片文件不存在。")
                duration = source.get("duration_sec")
                if not number(duration) or duration <= 0:
                    self.issue("source_duration", "source.duration_sec", "需要正秒数时长。")
                    duration = None
        observed_rows = self.array(self.p, "observed_beats")
        variants = self.array(self.p, "variants")
        segments = self.array(self.p, "production_segments")
        images = self.array(self.p, "images")
        needs_production = mode in {"prompts", "full"} or bool(segments or images)
        if mode == "reverse" and (source is None or not observed_rows):
            self.issue("reverse_requires_source", "mode", "反推模式需要源片和观察节拍。")
        if source is None and observed_rows:
            self.issue("observation_without_source", "observed_beats", "原创设计不能冒充原片观察。")
        if mode == "full" and source is not None and not observed_rows:
            self.issue("full_requires_observation", "observed_beats", "有源片的全流程需包含观察阶梯。")
        if mode == "ideas" and not variants:
            self.issue("ideas_required", "variants", "创意模式需交付创意条目。")
        if needs_production and (not segments or not images):
            self.issue("production_required", "production_segments", "提示词模式需完整视频段和状态图。")
        self.indexed(variants, "variants")
        self.read_evidence()
        if duration is not None and self.evidence_duration is not None and abs(duration - self.evidence_duration) > 0.05:
            self.issue("source_duration_mismatch", "evidence_file.source.duration_sec", "证据与包的源片时长相差超过 0.05 秒。")
        spaces = self.spatial(needs_production)
        beats = self.observed(observed_rows, duration, spaces)
        self.production(segments, images, beats)
        self.render()
        return self.report()

    def report(self):
        return {
            "version": 1,
            "structure_status": "fail" if self.errors else "pass",
            "overall_status": "invalid" if self.errors else ("needs_review" if self.warnings else "structure_valid"),
            "evidence_review_status": "declared_reviewed" if self.evidence_reviewed and self.frames else "not_verified",
            "render_review_status": self.render_status,
            "verification_limit": "仅检查结构、引用、时间和声明的数值约束；不识别画面，不证明真实比例、工序或人工审核结论。not_run 不会自动变为 pass。",
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_package(package, base):
    return Validator(package, base).run()


def export_package(package, target):
    """Export complete supplied prompts; never synthesize missing prompt bodies."""
    if not package.get("production_segments") or not package.get("images"):
        raise ValueError("导出需 images 和 production_segments；只反推或只创意无需导出。")
    target = Path(target)
    files = {}
    lines = ["图片提示词", ""]
    for index, item in enumerate(package["images"], 1):
        lines += [f"图片 {index}:", item["prompt"].strip(), ""]
        files[f"逐段复制/图片_{index:03d}.txt"] = item["prompt"].strip() + "\n"
    image_text = "\n".join(lines)
    video_lines = ["视频提示词", ""]
    beats = {beat["id"]: beat for beat in package["observed_beats"]}
    images = {image["id"]: image for image in package["images"]}
    timeline = ["段编号\t素材开始秒\t素材结束秒\t起始图\t结束图\t来源节拍\t原片时间范围秒\t性质"]
    cursor = 0.0
    for index, item in enumerate(package["production_segments"], 1):
        body = item["prompt"].strip()
        heading = f"视频 {index}" + (" [BRIDGE]" if item.get("kind") == "bridge" else "") + ":"
        video_lines += [heading, body, ""]
        files[f"逐段复制/视频_{index:03d}.txt"] = body + "\n"
        files[f"逐段复制/段_{index:03d}_全套.txt"] = (
            f"起始图片 {item['start_image_id']}:\n{images[item['start_image_id']]['prompt'].strip()}\n\n"
            f"结束图片 {item['end_image_id']}:\n{images[item['end_image_id']]['prompt'].strip()}\n\n"
            f"{heading}\n{body}\n")
        end = cursor + item["duration_sec"]
        source_times = ";".join(f"{sid}:{beats[sid]['start_sec']:.3f}-{beats[sid]['end_sec']:.3f}" for sid in item["source_beat_ids"])
        timeline.append(f"{item['id']}\t{cursor:.3f}\t{end:.3f}\t{item['start_image_id']}\t{item['end_image_id']}\t{','.join(item['source_beat_ids'])}\t{source_times}\t{item['origin']}")
        cursor = end
    video_text = "\n".join(video_lines)
    files["完整提示词.txt"] = image_text + "\n" + video_text
    files["图片提示词.txt"] = image_text
    files["视频提示词.txt"] = video_text
    files["时间索引.tsv"] = "\n".join(timeline) + "\n"
    conflicts = [str(target / name) for name in files if (target / name).exists()]
    if conflicts:
        raise ValueError("导出目标已有文件，请选新目录以保留旧交付：" + ", ".join(conflicts[:3]))
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="package.json 路径；相对媒体路径以此文件目录为基准")
    parser.add_argument("--report", type=Path, help="另存 JSON 审核报告")
    parser.add_argument("--export-dir", type=Path, help="仅在无结构错误时导出完整提示词、逐段文件和素材时间索引")
    args = parser.parse_args()
    try:
        package = json.loads(args.package.read_text(encoding="utf-8"))
        report = validate_package(package, args.package.resolve().parent)
        if args.export_dir and not report["errors"]:
            export_package(package, args.export_dir)
            report["export_dir"] = str(args.export_dir.resolve())
        body = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(body + "\n", encoding="utf-8")
        print(body)
        return 1 if report["errors"] else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"structure_status": "not_run", "tool_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
