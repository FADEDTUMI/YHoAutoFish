# -*- coding: utf-8 -*-
"""
抛竿检测模块 -- 从 StateMachine 提取的抛竿就绪 / 结算阻塞检测逻辑。
"""

import time
from pathlib import Path


class CastDetector:
    """负责抛竿前界面检测：F键、初始控件组合、结算阻塞等。"""

    def __init__(self, sm):
        self._sm = sm
        # ---------- 自有属性 ----------
        self.roi_f_btn = (0.75, 0.75, 0.25, 0.25)
        self.roi_initial_controls = (0.70, 0.50, 0.30, 0.50)
        self._ready_heavy_last_check = 0

    # ------------------------------------------------------------------
    # 1. 初始控件集群检测
    # ------------------------------------------------------------------
    def detect_initial_control_cluster(self, rect):
        if self._sm.sc is None or not rect:
            return {"count": 0, "matches": [], "confidence": 0.0, "valid": False}

        controls_roi = getattr(self, "roi_initial_controls", (0.70, 0.50, 0.30, 0.50))
        controls_img = self._sm.sc.capture_relative(rect, *controls_roi)
        if controls_img is None:
            return {"count": 0, "matches": [], "confidence": 0.0, "valid": False}

        button_sets = (
            ("Q", self._sm.tpl.initial_q_button_templates()),
            ("E", self._sm.tpl.initial_e_button_templates()),
            ("R", self._sm.tpl.initial_r_button_templates()),
        )
        matches = []
        best_conf = 0.0
        for key_name, templates in button_sets:
            loc, conf, matched_path, strategy_name = self._sm.vis.find_best_template_multi_strategy(
                controls_img,
                templates,
                self._sm.tpl.initial_control_match_strategies(),
                threshold=0.58,
                scale_range=self._sm.tpl.scale_range(rect, 0.50, 1.80),
                scale_steps=5,
            )
            best_conf = max(best_conf, float(conf or 0.0))
            if loc:
                matches.append({
                    "key": key_name,
                    "location": loc,
                    "confidence": conf,
                    "template": matched_path,
                    "strategy": strategy_name,
                })

        if matches:
            avg_conf = sum(item["confidence"] for item in matches) / len(matches)
        else:
            avg_conf = best_conf
        return {
            "count": len(matches),
            "matches": matches,
            "confidence": avg_conf,
            "valid": self.initial_control_cluster_is_valid(matches, controls_img.shape),
        }

    # ------------------------------------------------------------------
    # 2. 初始控件集群有效性判定
    # ------------------------------------------------------------------
    def initial_control_cluster_is_valid(self, matches, image_shape):
        if not matches or len(matches) < 2 or not image_shape:
            return False
        height, width = image_shape[:2]
        if width <= 0 or height <= 0:
            return False

        centers = [item.get("location") for item in matches if item.get("location")]
        if len(centers) < 2:
            return False

        xs = [float(point[0]) for point in centers]
        ys = [float(point[1]) for point in centers]
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)
        horizontal_layout = (
            x_span >= max(56.0, width * 0.10)
            and x_span <= max(360.0, width * 0.72)
            and y_span <= max(90.0, height * 0.24)
            and min(ys) >= height * 0.52
        )
        vertical_layout = (
            x_span <= max(130.0, width * 0.28)
            and y_span >= max(42.0, height * 0.14)
            and min(ys) >= height * 0.20
        )
        if not horizontal_layout and not vertical_layout:
            return False

        confidences = [float(item.get("confidence") or 0.0) for item in matches]
        if len(matches) >= 3:
            return sum(confidences) / len(confidences) >= 0.56
        return sum(confidences) / len(confidences) >= 0.62

    # ------------------------------------------------------------------
    # 3. 快速检测初始F键提示
    # ------------------------------------------------------------------
    def detect_initial_f_prompt_quick(self, rect, threshold=0.88):
        if self._sm.sc is None or not rect:
            return None

        f_roi = getattr(self, "roi_f_btn", (0.75, 0.75, 0.25, 0.25))
        btn_img = self._sm.sc.capture_relative(rect, *f_roi)
        if btn_img is None:
            return None

        loc, conf, matched_path, strategy_name = self._sm.vis.find_best_template_multi_strategy(
            btn_img,
            self._sm.tpl.f_button_templates(),
            (
                {"name": "f-quick-gray-mask", "threshold": threshold, "use_mask": True, "mask_threshold": 6, "early_accept": max(threshold, 0.94)},
                {"name": "f-quick-edge", "threshold": max(0.80, threshold - 0.04), "use_edge": True, "early_accept": max(threshold, 0.92)},
            ),
            threshold=threshold,
            scale_range=self._sm.tpl.scale_range(rect, 0.84, 1.20),
            scale_steps=3,
        )
        if not loc:
            return None
        return {
            "kind": "F键图标",
            "confidence": conf,
            "location": loc,
            "template": matched_path,
            "strategy": strategy_name,
        }

    # ------------------------------------------------------------------
    # 4. 是否存在初始钓鱼UI
    # ------------------------------------------------------------------
    def has_initial_fishing_ui(self, rect):
        info = self.detect_cast_prompt_after_settlement(rect)
        return bool(info and info.get("location"))

    # ------------------------------------------------------------------
    # 5. 格式化初始控件信息
    # ------------------------------------------------------------------
    def format_initial_controls(self, cluster_info):
        parts = []
        for item in (cluster_info or {}).get("matches", []):
            matched_path = item.get("template")
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            parts.append(f"{item.get('key')}:{item.get('confidence', 0):.2f}/{matched_name}/{item.get('strategy') or '默认'}")
        return "；".join(parts) if parts else "无"

    # ------------------------------------------------------------------
    # 6. 检测是否已就绪可抛竿 (最大方法)
    # ------------------------------------------------------------------
    def detect_ready_to_cast(self, rect, allow_heavy=False, require_initial_controls=False, include_f=True, include_prepare_ui=False):
        if self._sm.sc is None or not rect:
            return None

        best_conf = -1.0
        initial_cluster = None

        if include_f:
            f_roi = getattr(self, "roi_f_btn", (0.75, 0.75, 0.25, 0.25))
            btn_img = self._sm.sc.capture_relative(rect, *f_roi)
        else:
            btn_img = None

        if include_f and btn_img is not None:
            loc, conf, matched_path, strategy_name = self._sm.vis.find_best_template_multi_strategy(
                btn_img,
                self._sm.tpl.f_button_templates(),
                self._sm.tpl.f_button_fast_match_strategies(),
                threshold=0.58,
                scale_range=self._sm.tpl.scale_range(rect, 0.82, 1.18),
                scale_steps=4,
            )
            best_conf = conf
            if loc:
                if require_initial_controls:
                    initial_cluster = self.detect_initial_control_cluster(rect)
                else:
                    initial_cluster = {"count": 0, "matches": [], "confidence": 0.0}
                if require_initial_controls and (initial_cluster.get("count", 0) < 2 or not initial_cluster.get("valid")):
                    return {
                        "kind": "F键图标",
                        "confidence": conf,
                        "location": None,
                        "template": matched_path,
                        "strategy": strategy_name,
                        "initial_controls": initial_cluster,
                    }
                blocking_info = self.ready_blocking_result(rect, confidence_hint=conf)
                if blocking_info:
                    blocking_info["initial_controls"] = initial_cluster
                    return blocking_info
                return {
                    "kind": "钓鱼初始界面" if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid") else "F键图标",
                    "confidence": conf,
                    "location": loc,
                    "template": matched_path,
                    "strategy": strategy_name,
                    "initial_controls": initial_cluster,
                }

            loc, conf, matched_path, strategy_name = self._sm.vis.find_best_template_multi_strategy(
                btn_img,
                self._sm.tpl.f_button_templates(),
                self._sm.tpl.f_button_match_strategies(),
                threshold=0.58,
                scale_range=self._sm.tpl.scale_range(rect, 0.55, 1.65),
                scale_steps=11,
            )
            best_conf = conf
            if loc:
                if require_initial_controls:
                    initial_cluster = self.detect_initial_control_cluster(rect)
                else:
                    initial_cluster = {"count": 0, "matches": [], "confidence": 0.0}
                if require_initial_controls and (initial_cluster.get("count", 0) < 2 or not initial_cluster.get("valid")):
                    return {
                        "kind": "F键图标",
                        "confidence": conf,
                        "location": None,
                        "template": matched_path,
                        "strategy": strategy_name,
                        "initial_controls": initial_cluster,
                    }
                blocking_info = self.ready_blocking_result(rect, confidence_hint=conf)
                if blocking_info:
                    blocking_info["initial_controls"] = initial_cluster
                    return blocking_info
                return {
                    "kind": "钓鱼初始界面" if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid") else "F键图标",
                    "confidence": conf,
                    "location": loc,
                    "template": matched_path,
                    "strategy": strategy_name,
                    "initial_controls": initial_cluster,
                }

        if require_initial_controls or not include_f:
            initial_cluster = self.detect_initial_control_cluster(rect)
            if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid"):
                first_match = initial_cluster.get("matches", [{}])[0]
                blocking_info = self.ready_blocking_result(
                    rect,
                    confidence_hint=initial_cluster.get("confidence", 0.0),
                )
                if blocking_info:
                    blocking_info["initial_controls"] = initial_cluster
                    return blocking_info
                return {
                    "kind": "钓鱼初始界面组合控件",
                    "confidence": initial_cluster.get("confidence", 0.0),
                    "location": first_match.get("location") or (0, 0),
                    "template": first_match.get("template"),
                    "strategy": first_match.get("strategy") or "initial-controls",
                    "initial_controls": initial_cluster,
                }
            if require_initial_controls:
                return {
                    "kind": "钓鱼初始界面组合控件",
                    "confidence": initial_cluster.get("confidence", best_conf if best_conf >= 0 else 0.0),
                    "location": None,
                    "template": None,
                    "strategy": "initial-controls",
                    "initial_controls": initial_cluster,
                }

        if not include_prepare_ui:
            return {
                "kind": "",
                "confidence": best_conf,
                "location": None,
                "template": None,
            } if best_conf >= 0 else None

        start_button_roi = (0.15, 0.74, 0.70, 0.23)
        start_img = self._sm.sc.capture_relative(rect, *start_button_roi)
        if start_img is not None:
            loc, conf, matched_path, strategy_name = self._sm.vis.find_best_template_multi_strategy(
                start_img,
                self._sm.tpl.ready_start_button_templates(),
                (
                    {"name": "gray-mask", "threshold": 0.56, "use_mask": True},
                    {"name": "edge", "threshold": 0.54, "use_edge": True},
                    {"name": "plain", "threshold": 0.58},
                ),
                threshold=0.56,
                scale_range=self._sm.tpl.scale_range(rect, 0.62, 1.55),
                scale_steps=9,
            )
            if conf > best_conf:
                best_conf = conf
            if loc:
                blocking_info = self.ready_blocking_result(rect, confidence_hint=conf)
                if blocking_info:
                    return blocking_info
                return {
                    "kind": "开始钓鱼按钮",
                    "confidence": conf,
                    "location": loc,
                    "template": matched_path,
                    "strategy": strategy_name,
                }

        if allow_heavy:
            now = time.time()
            if now - getattr(self, "_ready_heavy_last_check", 0) >= 3.0:
                self._ready_heavy_last_check = now
                full_img = self._sm.sc.capture_relative(rect, 0, 0, 1, 1)
                if full_img is not None:
                    loc, conf, matched_path = self._sm.vis.find_best_template(
                        full_img,
                        self._sm.tpl.ready_panel_templates(),
                        threshold=0.70,
                        use_edge=True,
                        use_binary=False,
                        scale_range=self._sm.tpl.scale_range(rect, 0.62, 1.55),
                        scale_steps=7,
                    )
                    if conf > best_conf:
                        best_conf = conf
                    if loc:
                        blocking_info = self.ready_blocking_result(rect, confidence_hint=conf)
                        if blocking_info:
                            return blocking_info
                        return {
                            "kind": "钓鱼准备界面",
                            "confidence": conf,
                            "location": loc,
                            "template": matched_path,
                        }

        return {
            "kind": "",
            "confidence": best_conf,
            "location": None,
            "template": None,
        } if best_conf >= 0 else None

    # ------------------------------------------------------------------
    # 7. 就绪时的结算阻塞预检
    # ------------------------------------------------------------------
    def ready_blocking_result(self, rect, confidence_hint=0.0):
        blocking_info = self.detect_blocking_result_for_cast(rect)
        if not blocking_info:
            return None
        result_info = dict(blocking_info)
        result_info["blocking_result"] = dict(blocking_info.get("blocking_result") or blocking_info)
        block_reason = blocking_info.get("block_reason")
        if not block_reason:
            kind = str(blocking_info.get("kind") or "")
            if "成功" in kind:
                block_reason = "success_result"
            elif "失败" in kind:
                block_reason = "failed_result"
            else:
                block_reason = "result_visible"
        result_info["block_reason"] = block_reason
        result_info["confidence"] = max(
            float(result_info.get("confidence") or 0.0),
            float(confidence_hint or 0.0),
        )
        result_info["location"] = None
        result_info.setdefault("template", None)
        result_info.setdefault("strategy", "result-block")
        return result_info

    # ------------------------------------------------------------------
    # 8. 检测可能阻塞抛竿的结算界面
    # ------------------------------------------------------------------
    def detect_blocking_result_for_cast(self, rect):
        success_info = self._sm.result_det.detect_ultrafast_success_result(rect)
        if success_info and success_info.get("location"):
            return {
                "kind": "成功结算界面",
                "confidence": success_info.get("confidence", 0.0),
                "location": success_info.get("location"),
                "template": success_info.get("template"),
                "strategy": "success-result-block",
                "signals": success_info.get("signals", []),
                "blocking_result": success_info,
                "block_reason": "success_result",
            }

        failed_info = self._sm.result_det.detect_fast_failed_result(rect)
        if failed_info and failed_info.get("location") and self._sm.result_det.is_strong_failed_result(failed_info):
            return {
                "kind": "失败结算界面",
                "confidence": failed_info.get("confidence", 0.0),
                "location": failed_info.get("location"),
                "template": failed_info.get("template"),
                "strategy": failed_info.get("strategy") or "failed-result-block",
                "blocking_result": failed_info,
                "block_reason": "failed_result",
            }

        return None

    # ------------------------------------------------------------------
    # 9. 处理就绪时检测到的结算阻塞
    # ------------------------------------------------------------------
    def handle_ready_blocking_result(self, rect, ready_info, source_label):
        result_info = (ready_info or {}).get("blocking_result")
        if not result_info:
            return False

        reason = (ready_info or {}).get("block_reason")
        if reason == "success_result":
            self._sm._log(f"[{source_label}] 检测到成功结算界面仍未处理，优先进入结算流程。")
            self._sm.result_det.finish_fast_success_result(rect, result_info, source_label=source_label)
            return True

        if reason == "failed_result":
            return self._sm.result_det.maybe_finish_failed_result(rect, result_info, source_label=source_label)

        return False

    # ------------------------------------------------------------------
    # 10. 结算后检测抛竿提示（F键+初始控件组合）
    # ------------------------------------------------------------------
    def detect_cast_prompt_after_settlement(self, rect):
        if self._sm.sc is None or not rect:
            return None

        f_roi = getattr(self, "roi_f_btn", (0.75, 0.75, 0.25, 0.25))
        btn_img = self._sm.sc.capture_relative(rect, *f_roi)
        if btn_img is None:
            return None

        loc, conf, matched_path, strategy_name = self._sm.vis.find_best_template_multi_strategy(
            btn_img,
            self._sm.tpl.f_button_templates(),
            (
                {"name": "settlement-f-gray", "threshold": 0.60, "use_mask": True, "early_accept": 0.94},
            ),
            threshold=0.60,
            scale_range=self._sm.tpl.scale_range(rect, 0.82, 1.28),
            scale_steps=3,
        )
        if not loc:
            return None
        initial_cluster = self.detect_initial_control_cluster(rect)
        if initial_cluster.get("count", 0) < 2 or not initial_cluster.get("valid"):
            return None
        return {
            "kind": "钓鱼初始界面",
            "confidence": conf,
            "location": loc,
            "template": matched_path,
            "strategy": strategy_name,
            "initial_controls": initial_cluster,
        }
