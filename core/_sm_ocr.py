import time
import cv2
import numpy as np
import os
import re
import shutil
import traceback
from pathlib import Path
from importlib import metadata
from PIL import Image, ImageDraw, ImageFont

from core.paths import resource_path

CnOcr = None

OCR_MODEL_BUNDLE_DIR = "ocr_models"
# Glob patterns for required OCR model files (avoids hardcoding exact filenames
# that change across cnocr versions, e.g. cnocr-v2.3 → cnocr-v2.4).
# Paths are relative to each package's root (e.g. APPDATA/cnocr, APPDATA/cnstd).
OCR_REQUIRED_MODEL_GLOBS = {
    "cnocr": "2.3/densenet_lite_136-gru/*.onnx",
}

# Auto-detected det model name (cached after first detection)
_auto_det_model_name = None


def _detect_det_model_name(cnstd_root):
    """Scan cnstd cache to find the first available detection model name."""
    global _auto_det_model_name
    if _auto_det_model_name is not None:
        return _auto_det_model_name
    root = Path(cnstd_root)
    if not root.exists():
        return "naive_det"
    # cnstd stores models under version/ppocr/<model_name>/ or version/<model_name>/
    for version_dir in sorted(root.iterdir()):
        if not version_dir.is_dir():
            continue
        for sub in sorted(version_dir.iterdir()):
            if not sub.is_dir():
                continue
            # ppocr sub-directory
            if sub.name == "ppocr":
                for model_dir in sorted(sub.iterdir()):
                    if model_dir.is_dir() and any(model_dir.rglob("*.onnx")):
                        _auto_det_model_name = model_dir.name
                        return _auto_det_model_name
            elif any(sub.rglob("*.onnx")):
                _auto_det_model_name = sub.name
                return _auto_det_model_name
    # Fallback: try known names in priority order
    for name in ("ch_PP-OCRv5_det", "ch_PP-OCRv4_det", "naive_det"):
        candidates = list(root.rglob(f"{name}*/*.onnx"))
        if candidates:
            _auto_det_model_name = name
            return _auto_det_model_name
    return "naive_det"


class SettlementOCR:
    """OCR 结算识别模块，从 StateMachine 中提取。"""

    def __init__(self, sm):
        self._sm = sm

        self.ocr = {}
        self.ocr_available = True
        self._ocr_import_checked = False
        self._ocr_roots = None
        self.last_ocr_init_error = ""
        self.last_ocr_init_trace = ""
        self._fish_matcher_refs = None
        self._weight_digit_templates = None
        self._last_name_ocr_candidates = []
        self._last_weight_ocr_candidates = []
        self._last_weight_corrections = []

    # ------------------------------------------------------------------
    # 静态 / 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def default_ocr_root(package_name):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / package_name
        return Path.home() / f".{package_name}"

    def copy_tree_missing(self, source, target):
        copied = 0
        source = Path(source)
        target = Path(target)
        if not source.exists():
            return copied
        for src in source.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(source)
            dst = target / rel
            try:
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                self._sm._log(f"[识别] OCR 模型文件复制失败: {dst}，原因: {exc}")
                raise
        return copied

    def package_version(self, package_name):
        try:
            ver = metadata.version(package_name)
            if ver == "0.0.0":
                return "N/A"
            return ver
        except Exception:
            return "未安装"

    # ------------------------------------------------------------------
    # OCR 运行时路径准备
    # ------------------------------------------------------------------

    def prepare_ocr_runtime_roots(self):
        """把随程序分发的 OCR 模型复制到 cnocr/cnstd 默认可写缓存目录。"""
        if self._ocr_roots is not None:
            return self._ocr_roots

        cnocr_root = Path(os.environ.get("CNOCR_HOME") or self.default_ocr_root("cnocr"))
        cnstd_root = Path(os.environ.get("CNSTD_HOME") or self.default_ocr_root("cnstd"))
        bundle_root = Path(resource_path(OCR_MODEL_BUNDLE_DIR))

        copied = 0
        if bundle_root.exists():
            copied += self.copy_tree_missing(bundle_root / "cnocr", cnocr_root)
            copied += self.copy_tree_missing(bundle_root / "cnstd", cnstd_root)
            if copied:
                self._sm._log(f"[识别] 已补齐 OCR 本地模型缓存，共复制 {copied} 个文件。")

        os.environ["CNOCR_HOME"] = str(cnocr_root)
        os.environ["CNSTD_HOME"] = str(cnstd_root)
        self._ocr_roots = {"cnocr": cnocr_root, "cnstd": cnstd_root, "bundle": bundle_root}
        return self._ocr_roots

    def missing_required_ocr_models(self):
        roots = self.prepare_ocr_runtime_roots()
        missing = []
        for package_name, pattern in OCR_REQUIRED_MODEL_GLOBS.items():
            root = roots.get(package_name)
            if root is None:
                missing.append(f"{package_name}: root not set")
                continue
            matches = list(root.glob(pattern))
            if not matches:
                missing.append(root / pattern)
        return missing

    # ------------------------------------------------------------------
    # OCR 初始化 / 错误报告
    # ------------------------------------------------------------------

    def set_ocr_init_error(self, phase, exc=None, detail=None):
        parts = [f"{phase}失败"]
        if detail:
            parts.append(detail)
        if exc is not None:
            parts.append(f"{type(exc).__name__}: {exc}")

        missing_models = self.missing_required_ocr_models()
        if missing_models:
            parts.append(
                "缺少本地 OCR 模型文件："
                + "；".join(str(path) for path in missing_models)
                + "。请使用包含 ocr_models 目录的完整发布包，或重新执行 build_release.ps1 打包。"
            )

        parts.append(
            "依赖版本："
            f"cnocr={self.package_version('cnocr')}，"
            f"cnstd={self.package_version('cnstd')}，"
            f"onnxruntime={self.package_version('onnxruntime')}，"
            f"rapidocr={self.package_version('rapidocr')}。"
        )

        self.last_ocr_init_error = " ".join(part for part in parts if part)
        if exc is not None:
            self.last_ocr_init_trace = traceback.format_exc(limit=6)
            self._sm._log(f"[识别] OCR 详细异常: {self.last_ocr_init_trace.strip()}")
        self._sm._log(f"[识别] OCR 模块{self.last_ocr_init_error}")

    def get_init_failure_message(self):
        if self.last_ocr_init_error:
            return "OCR 模块初始化失败：" + self.last_ocr_init_error
        missing_models = self.missing_required_ocr_models()
        if missing_models:
            return "OCR 模块初始化失败：本地 OCR 模型缺失，请使用完整发布包。"
        return "OCR 模块初始化失败，请检查完整发布包、cnocr/cnstd/onnxruntime 依赖与本地模型缓存。"

    def prepare_modules(self):
        """预热结算识别所需的 OCR 模块，避免首次上鱼时才加载导致卡顿。"""
        self.last_ocr_init_error = ""
        self.last_ocr_init_trace = ""
        self.prepare_ocr_runtime_roots()
        name_ocr = self.ensure_ocr("name")
        weight_ocr = self.ensure_ocr("weight")
        general_ocr = self.ensure_ocr("general")
        # 图像兜底匹配同样需要首次构建特征，放在初始化阶段完成。
        self.load_fish_matcher_refs()
        return name_ocr is not None and weight_ocr is not None and general_ocr is not None

    def ensure_ocr(self, mode="general"):
        global CnOcr
        roots = self.prepare_ocr_runtime_roots()
        if CnOcr is None and not self._ocr_import_checked:
            self._ocr_import_checked = True
            try:
                from cnocr import CnOcr as LoadedCnOcr
                CnOcr = LoadedCnOcr
            except Exception as exc:
                self.ocr_available = False
                self.set_ocr_init_error("加载 cnocr/onnxruntime 依赖", exc)
                return None
        if CnOcr is None:
            self.ocr_available = False
            return None
        if not self.ocr_available:
            return None
        missing_models = self.missing_required_ocr_models()
        if missing_models:
            self.ocr_available = False
            self.set_ocr_init_error(
                "初始化本地模型",
                detail="随程序分发的 OCR 模型未能写入当前用户缓存。"
            )
            return None
        if mode not in self.ocr:
            try:
                det_name = _detect_det_model_name(str(roots["cnstd"]))
                common_kwargs = {
                    "det_model_name": det_name,
                    "rec_root": str(roots["cnocr"]),
                    "det_root": str(roots["cnstd"]),
                }
                if mode == "name":
                    self._sm._log("[系统] 正在初始化鱼名 OCR 识别模块...")
                    self.ocr[mode] = CnOcr(**common_kwargs)
                elif mode == "weight":
                    self._sm._log("[系统] 正在初始化重量 OCR 识别模块...")
                    self.ocr[mode] = CnOcr(**common_kwargs, cand_alphabet="0123456789gG克")
                else:
                    self._sm._log("[系统] 正在初始化 OCR 单行识别模块...")
                    self.ocr[mode] = CnOcr(**common_kwargs)
            except Exception as exc:
                self.ocr_available = False
                self.set_ocr_init_error("初始化 OCR 模型", exc)
                self.ocr.pop(mode, None)
        return self.ocr.get(mode)

    # ------------------------------------------------------------------
    # OCR 候选收集
    # ------------------------------------------------------------------

    def collect_ocr_candidates(self, image, mode="general"):
        ocr = self.ensure_ocr(mode)
        if ocr is None or image is None or image.size == 0:
            return []

        candidates = []
        t0 = time.perf_counter()
        try:
            result = ocr.ocr_for_single_line(image)
        except Exception as exc:
            self._sm._log(f"[识别] OCR 执行失败: {exc}")
            return []
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if isinstance(result, dict):
            cleaned = (result.get("text") or "").strip()
            if cleaned:
                candidates.append((cleaned, float(result.get("score") or 0.0)))
        elif result:
            cleaned = str(result).strip()
            if cleaned:
                candidates.append((cleaned, 0.0))

        if mode in {"name", "weight"}:
            candidates.sort(key=lambda item: item[1], reverse=True)
            try:
                from core.tracker import EventTracker
                best_text = candidates[0][0] if candidates else ""
                best_conf = candidates[0][1] if candidates else 0.0
                EventTracker.get().ocr_result(best_text, best_conf, elapsed_ms, bool(candidates))
            except Exception:
                pass
            return candidates

        if getattr(ocr, "det_model", None) is not None:
            try:
                results = ocr.ocr(image)
            except Exception:
                results = []
            for item in results or []:
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                score = item.get("score", 0.0) if isinstance(item, dict) else 0.0
                cleaned = (text or "").strip()
                if cleaned:
                    candidates.append((cleaned, float(score or 0.0)))

        candidates.sort(key=lambda item: item[1], reverse=True)
        try:
            from core.tracker import EventTracker
            best_text = candidates[0][0] if candidates else ""
            best_conf = candidates[0][1] if candidates else 0.0
            EventTracker.get().ocr_result(best_text, best_conf, elapsed_ms, bool(candidates))
        except Exception:
            pass
        return candidates

    def collect_ocr_texts(self, image):
        return [text for text, _ in self.collect_ocr_candidates(image)]

    # ------------------------------------------------------------------
    # 图像裁剪
    # ------------------------------------------------------------------

    def crop_name_text_region(self, image):
        if image is None or image.size == 0:
            return image

        # 结算鱼名是白色描边字，背景常有高亮光效；优先只框选中心标题行的低饱和高亮文字。
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 0, 150), (179, 80, 255))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        height, width = image.shape[:2]
        boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h < 20 or h < max(6, int(height * 0.10)) or w < 4:
                continue
            if w > width * 0.45 or h > height * 0.75:
                continue
            boxes.append((x, y, w, h))

        if not boxes:
            return image

        center_x = width / 2
        center_y = height / 2
        boxes.sort(key=lambda item: abs((item[0] + item[2] / 2) - center_x) + abs((item[1] + item[3] / 2) - center_y) * 0.55)
        row_y = boxes[0][1] + boxes[0][3] / 2
        row_boxes = [
            box for box in boxes
            if abs((box[1] + box[3] / 2) - row_y) < max(18, int(height * 0.20))
        ]

        x1 = min(x for x, _, _, _ in row_boxes)
        y1 = min(y for _, y, _, _ in row_boxes)
        x2 = max(x + w for x, _, w, _ in row_boxes)
        y2 = max(y + h for _, y, _, h in row_boxes)

        pad_x = max(8, int((x2 - x1) * 0.18))
        pad_y = max(6, int((y2 - y1) * 0.40))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)

        if x2 <= x1 or y2 <= y1:
            return image
        if (x2 - x1) * (y2 - y1) > width * height * 0.72:
            return image
        return image[y1:y2, x1:x2]

    def crop_weight_digits_region(self, image):
        if image is None or image.size == 0:
            return image

        # 重量数字比单位 g 更高更粗；先按亮色主体分割，再只保留数字高度等级的连通区域。
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 0, 135), (179, 115, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1)
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        height, width = image.shape[:2]
        boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h < 18 or h < max(12, int(height * 0.24)) or w < 4:
                continue
            if w > width * 0.40 or h > height * 0.92:
                continue
            boxes.append((x, y, w, h))

        if not boxes:
            return image

        max_height = max(h for _, _, _, h in boxes)
        top_y = min(y for _, y, _, h in boxes if h >= max_height * 0.70)
        digit_boxes = [
            box for box in boxes
            if box[3] >= max_height * 0.68 and box[1] <= top_y + max(8, int(max_height * 0.24))
        ]
        if not digit_boxes:
            return image

        x1 = min(x for x, _, _, _ in digit_boxes)
        y1 = min(y for _, y, _, _ in digit_boxes)
        x2 = max(x + w for x, _, w, _ in digit_boxes)
        y2 = max(y + h for _, y, _, h in digit_boxes)

        pad_x = max(4, int((x2 - x1) * 0.08))
        pad_y = max(4, int((y2 - y1) * 0.18))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)

        if x2 <= x1 or y2 <= y1:
            return image
        return image[y1:y2, x1:x2]

    def crop_text_region(self, image, mode):
        if image is None or image.size == 0:
            return image

        if mode == "name":
            return self.crop_name_text_region(image)
        if mode == "weight":
            return self.crop_weight_digits_region(image)

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        threshold = 115 if mode == "name" else 135
        mask = cv2.inRange(gray, threshold, 255)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        height, width = gray.shape[:2]
        min_area = max(12, int(width * height * 0.0007))
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h < min_area or h < max(6, int(height * 0.10)):
                continue
            boxes.append((x, y, w, h))

        if not boxes:
            return image

        x1 = min(x for x, _, _, _ in boxes)
        y1 = min(y for _, y, _, _ in boxes)
        x2 = max(x + w for x, _, w, _ in boxes)
        y2 = max(y + h for _, y, _, h in boxes)

        pad_x = max(8, int((x2 - x1) * 0.16))
        pad_y = max(5, int((y2 - y1) * 0.28))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)

        if x2 <= x1 or y2 <= y1:
            return image
        if (x2 - x1) * (y2 - y1) > width * height * 0.88:
            return image
        return image[y1:y2, x1:x2]

    # ------------------------------------------------------------------
    # OCR 变体构建
    # ------------------------------------------------------------------

    def build_ocr_variants(self, image, mode):
        if image is None or image.size == 0:
            return []

        variants = []
        sources = [image]
        cropped = self.crop_text_region(image, mode)
        if cropped is not image and cropped is not None and cropped.size > 0:
            sources.insert(0, cropped)

        scales = (2.0, 3.0, 4.0) if mode == "name" else (2.0,)
        for source in sources:
            for scale in scales:
                enlarged = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)

                variants.append(enlarged)

                if mode == "name":
                    _, binary = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
                    variants.append(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR))
                    continue

                denoised = cv2.GaussianBlur(gray, (3, 3), 0)
                _, binary = cv2.threshold(denoised, 165, 255, cv2.THRESH_BINARY)
                inverted = cv2.bitwise_not(binary)
                variants.append(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR))
                variants.append(cv2.cvtColor(inverted, cv2.COLOR_GRAY2BGR))

        return variants

    # ------------------------------------------------------------------
    # 重量文本解析
    # ------------------------------------------------------------------

    def parse_weight_text(self, text):
        raw_text = str(text or "").strip()
        if not raw_text:
            return 0

        normalized = raw_text.translate(str.maketrans({
            "O": "0",
            "o": "0",
            "〇": "0",
            "I": "1",
            "l": "1",
            "|": "1",
            "S": "5",
            "s": "5",
            "B": "8",
        }))
        compact = re.sub(r"\s+", "", normalized)

        explicit_match = re.search(r"(\d{1,5})(?:[gG克])", compact)
        if explicit_match:
            value = int(explicit_match.group(1))
            return value if 0 < value < 50000 else 0

        if not re.fullmatch(r"\d{1,6}", compact):
            loose_match = re.search(r"(\d{1,6})", compact)
            if not loose_match:
                return 0
            compact = loose_match.group(1)

        value = int(compact)
        return value if 0 < value < 50000 else 0

    def extract_weight_value(self, texts):
        for text in texts:
            value = self.parse_weight_text(text)
            if value > 0:
                return value
        return 0

    def is_plausible_name(self, text):
        cleaned = re.sub(r"\s+", "", text or "")
        if len(cleaned) < 2:
            return False
        banned = ["点击空白区域关闭", "获得钓鱼经验", "等级", "LEVEL", "RESULT", "MASTER"]
        return not any(token in cleaned for token in banned)

    # ------------------------------------------------------------------
    # ROI 文本读取
    # ------------------------------------------------------------------

    def read_roi_text(self, rect, rois, mode):
        best_text = ""
        weight_candidates = []
        known_fishes = self._sm.record_mgr.get_encyclopedia() if mode == "name" else {}
        name_candidates = []

        for roi in rois:
            image = self._sm.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            for variant in self.build_ocr_variants(image, mode):
                candidates = self.collect_ocr_candidates(variant, mode)
                if not candidates:
                    continue
                if mode == "weight":
                    for text, score in candidates:
                        self._last_weight_ocr_candidates.append((text, score))
                        if score < 0.12:
                            continue
                        value = self.parse_weight_text(text)
                        if value <= 0:
                            continue
                        digit_count = len(str(value))
                        compact = re.sub(r"\s+", "", str(text or "").translate(str.maketrans({
                            "O": "0",
                            "o": "0",
                            "〇": "0",
                            "I": "1",
                            "l": "1",
                            "|": "1",
                            "S": "5",
                            "s": "5",
                            "B": "8",
                        })))
                        has_unit = 1 if re.search(r"\d{1,5}(?:[gG克])", compact) else 0
                        weight_candidates.append((value, float(score or 0.0), has_unit, digit_count, text))
                else:
                    for text, score in candidates:
                        if mode == "name":
                            self._last_name_ocr_candidates.append((text, score))
                            name_candidates.append((text, score))
                            if score >= 0.88:
                                resolved, resolved_score, _ = self._sm.record_mgr.resolve_fish_name_candidates([(text, score)])
                                if resolved in known_fishes and resolved_score >= 1.0:
                                    return resolved, 0
                        if score < 0.16:
                            continue
                        if len(text) > len(best_text):
                            best_text = text

        if mode == "weight":
            if not weight_candidates:
                return "", 0

            explicit_candidates = [item for item in weight_candidates if item[2]]
            pure_candidates = [item for item in weight_candidates if not item[2]]
            explicit_best_score = max((item[1] for item in explicit_candidates), default=-1.0)
            pure_best_score = max((item[1] for item in pure_candidates), default=-1.0)

            if explicit_candidates and explicit_best_score >= pure_best_score - 0.18:
                pool = explicit_candidates
            else:
                pool = weight_candidates

            best_score = max(item[1] for item in pool)
            near_best = [item for item in pool if item[1] >= max(0.12, best_score - 0.08)]
            near_best.sort(key=lambda item: (min(item[3], 5), item[1]), reverse=True)
            return "", near_best[0][0]
        resolved, score, raw_text = self._sm.record_mgr.resolve_fish_name_candidates(name_candidates)
        if resolved in known_fishes:
            if raw_text and raw_text != resolved:
                self._sm._log(f"[识别] 鱼名 OCR 已按图鉴词典修正: {raw_text} -> {resolved} ({score:.2f})")
            return resolved, 0
        return "", 0

    # ------------------------------------------------------------------
    # 图像兜底匹配
    # ------------------------------------------------------------------

    def load_fish_matcher_refs(self):
        if self._fish_matcher_refs is not None:
            return self._fish_matcher_refs

        refs = []
        orb = cv2.ORB_create(nfeatures=300)
        encyclopedia = self._sm.record_mgr.get_encyclopedia()
        for name, data in encyclopedia.items():
            image_path = data.get("image_path", "")
            if not image_path or not os.path.exists(image_path):
                continue
            try:
                image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            except Exception:
                continue
            if image is None:
                continue
            if len(image.shape) == 3 and image.shape[2] == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)

            h, w = image.shape[:2]
            crop = image[int(h * 0.12):int(h * 0.82), int(w * 0.12):int(w * 0.88)]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            _, descriptors = orb.detectAndCompute(gray, None)
            if descriptors is None:
                continue
            refs.append((name, descriptors))

        self._fish_matcher_refs = refs
        return refs

    def match_fish_by_image(self, rect, rois):
        refs = self.load_fish_matcher_refs()
        if not refs:
            return ""

        orb = cv2.ORB_create(nfeatures=350)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        best_name = ""
        best_score = 0
        second_score = 0

        for roi in rois:
            image = self._sm.sc.capture_relative(rect, *roi)
            if image is None or image.size == 0:
                continue
            h, w = image.shape[:2]
            crop = image[int(h * 0.12):int(h * 0.88), int(w * 0.12):int(w * 0.88)]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            _, query_desc = orb.detectAndCompute(gray, None)
            if query_desc is None:
                continue

            for name, ref_desc in refs:
                matches = matcher.knnMatch(query_desc, ref_desc, k=2)
                good_matches = [
                    m for pair in matches if len(pair) == 2 for m, n in [pair] if m.distance < 0.72 * n.distance
                ]
                score = len(good_matches)
                if score > best_score:
                    second_score = best_score
                    best_score = score
                    best_name = name
                elif score > second_score:
                    second_score = score

        if best_score >= 28 and best_score >= int(second_score * 1.4):
            return best_name
        return ""

    # ------------------------------------------------------------------
    # 模板数字识别（重量兜底）
    # ------------------------------------------------------------------

    def build_weight_digit_templates(self):
        if self._weight_digit_templates is not None:
            return self._weight_digit_templates

        font_candidates = [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\bahnschrift.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\impact.ttf",
        ]
        templates = {digit: [] for digit in "0123456789"}

        for font_path in font_candidates:
            if not os.path.exists(font_path):
                continue
            try:
                font = ImageFont.truetype(font_path, 92)
            except Exception:
                continue

            for digit in "0123456789":
                canvas = Image.new("L", (120, 140), 0)
                drawer = ImageDraw.Draw(canvas)
                bbox = drawer.textbbox((0, 0), digit, font=font, stroke_width=7)
                text_x = (120 - (bbox[2] - bbox[0])) // 2 - bbox[0]
                text_y = (140 - (bbox[3] - bbox[1])) // 2 - bbox[1]
                drawer.text(
                    (text_x, text_y),
                    digit,
                    font=font,
                    fill=255,
                    stroke_width=7,
                    stroke_fill=0,
                )
                arr = np.array(canvas)
                _, binary = cv2.threshold(arr, 110, 255, cv2.THRESH_BINARY)
                coords = cv2.findNonZero(binary)
                if coords is None:
                    continue
                x, y, w, h = cv2.boundingRect(coords)
                crop = binary[y:y + h, x:x + w]
                crop = cv2.resize(crop, (52, 84), interpolation=cv2.INTER_AREA)
                templates[digit].append(crop)

        self._weight_digit_templates = templates
        return templates

    def classify_digit_image(self, image):
        templates = self.build_weight_digit_templates()
        if image is None or image.size == 0:
            return "", -1.0

        resized = cv2.resize(image, (52, 84), interpolation=cv2.INTER_AREA)
        best_digit = ""
        best_score = -1.0
        for digit, variants in templates.items():
            for template in variants:
                score = cv2.matchTemplate(resized, template, cv2.TM_CCOEFF_NORMED)[0][0]
                if score > best_score:
                    best_score = score
                    best_digit = digit
        return best_digit, best_score

    def read_weight_by_template(self, rect, rois):
        for roi in rois:
            image = self._sm.sc.capture_relative(rect, *roi)
            if image is None or image.size == 0:
                continue
            digit_image = self.crop_weight_digits_region(image)
            value = self.extract_weight_from_image_by_template(
                digit_image if digit_image is not None and digit_image.size > 0 else image
            )
            if value > 0:
                return value
        return 0

    def extract_weight_from_image_by_template(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binary = cv2.threshold(gray, 175, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        h, w = binary.shape[:2]
        for cnt in contours:
            x, y, cw, ch = cv2.boundingRect(cnt)
            if ch < h * 0.38 or cw < 8 or cw > w * 0.28:
                continue
            if y > h * 0.78:
                continue
            boxes.append((x, y, cw, ch))

        if not boxes:
            return 0

        boxes.sort(key=lambda item: item[0])
        top_y = min(box[1] for box in boxes)
        max_height = max(box[3] for box in boxes)
        digits = []
        for x, y, cw, ch in boxes:
            if y > top_y + max_height * 0.12:
                continue
            pad = 4
            crop = binary[max(0, y - pad):min(h, y + ch + pad), max(0, x - pad):min(w, x + cw + pad)]
            digit, score = self.classify_digit_image(crop)
            if digit and score >= 0.18:
                digits.append(digit)

        if not digits:
            return 0

        try:
            return int("".join(digits))
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # 候选格式化 / 调试保存
    # ------------------------------------------------------------------

    def format_name_ocr_candidates(self):
        unique = []
        seen = set()
        for text, score in sorted(self._last_name_ocr_candidates, key=lambda item: item[1], reverse=True):
            cleaned = str(text or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(f"{cleaned}({score:.2f})")
            if len(unique) >= 8:
                break
        return "、".join(unique)

    def format_weight_ocr_candidates(self):
        unique = []
        seen = set()
        for text, score in sorted(self._last_weight_ocr_candidates, key=lambda item: item[1], reverse=True):
            cleaned = str(text or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(f"{cleaned}({score:.2f})")
            if len(unique) >= 6:
                break
        return "、".join(unique)

    def save_unknown_settlement_debug(self, rect, name_rois):
        if self._sm.sc is None:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        screenshot_dir = Path("screenshot")
        try:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            screenshot_dir = Path(".")
        full_image = self._sm.sc.capture_relative(rect, 0, 0, 1, 1)
        if full_image is not None and full_image.size > 0:
            path = screenshot_dir / f"debug_settlement_unknown_{timestamp}.png"
            cv2.imwrite(str(path), full_image)
            self._sm._log(f"[排错] 已保存未知鱼类结算截图: {path}")
        if not self._sm.config.get("debug_mode", False):
            return
        for index, roi in enumerate(name_rois, start=1):
            roi_image = self._sm.sc.capture_relative(rect, *roi)
            if roi_image is not None and roi_image.size > 0:
                path = screenshot_dir / f"debug_settlement_unknown_name_roi_{timestamp}_{index}.png"
                cv2.imwrite(str(path), roi_image)

    # ------------------------------------------------------------------
    # 结算信息读取（主入口）
    # ------------------------------------------------------------------

    def read_settlement_info(self, rect, save_unknown_debug=True):
        fish_name = ""
        weight_g = 0
        self._last_name_ocr_candidates = []
        self._last_weight_ocr_candidates = []
        self._last_weight_corrections = []

        name_rois = [
            (0.30, 0.14, 0.40, 0.12),
            (0.26, 0.12, 0.48, 0.15),
            (0.34, 0.16, 0.32, 0.10),
            (0.28, 0.18, 0.44, 0.11),
            (0.24, 0.10, 0.52, 0.20),
        ]
        fish_image_rois = [
            (0.33, 0.24, 0.34, 0.34),
            (0.30, 0.22, 0.40, 0.38),
            (0.36, 0.26, 0.28, 0.30),
        ]
        weight_rois = [
            (0.33, 0.62, 0.34, 0.14),
            (0.30, 0.60, 0.40, 0.16),
            (0.36, 0.64, 0.28, 0.12),
        ]
        sample_offsets = [0.0, 0.22, 0.46, 0.75, 1.05]

        elapsed = 0.0
        for target_offset in sample_offsets:
            sleep_for = max(0.0, target_offset - elapsed)
            if sleep_for > 0:
                if not self._sm._sleep_interruptible(sleep_for):
                    return fish_name or "未知鱼类", weight_g
            elapsed = target_offset

            if not fish_name:
                candidate_name, _ = self.read_roi_text(rect, name_rois, "name")
                if candidate_name:
                    fish_name = candidate_name

            if weight_g <= 0:
                _, candidate_weight = self.read_roi_text(rect, weight_rois, "weight")
                if candidate_weight > 0:
                    weight_g = candidate_weight

            if weight_g <= 0:
                candidate_weight = self.read_weight_by_template(rect, weight_rois)
                if candidate_weight > 0:
                    weight_g = candidate_weight

            if fish_name and weight_g > 0:
                break

        if not fish_name and not self.ocr_available:
            candidate_name = self.match_fish_by_image(rect, fish_image_rois)
            if candidate_name:
                fish_name = candidate_name

        if fish_name:
            self._sm._log(f"[识别] 结算鱼名识别结果: {fish_name}")
        else:
            candidates = self.format_name_ocr_candidates()
            if candidates:
                self._sm._log(f"[识别] 鱼名 OCR 候选未命中图鉴: {candidates}")
            if save_unknown_debug:
                self.save_unknown_settlement_debug(rect, name_rois)
            fish_name = "未知鱼类"
            self._sm._log("[识别] 未能稳定识别到鱼名，已按未知鱼类记录。")

        if weight_g > 0:
            if self._last_weight_corrections:
                raw_text, corrected = self._last_weight_corrections[-1]
                self._sm._log(f"[识别] 重量 OCR 候选疑似把单位 g 识别为数字，已修正: {raw_text} -> {corrected} g")
            self._sm._log(f"[识别] 结算重量识别结果: {weight_g} g")
        else:
            candidates = self.format_weight_ocr_candidates()
            if candidates:
                self._sm._log(f"[识别] 重量 OCR 候选未能稳定解析: {candidates}")
            self._sm._log("[识别] 未能稳定识别到重量，已按 0 g 记录。")

        return fish_name, weight_g
