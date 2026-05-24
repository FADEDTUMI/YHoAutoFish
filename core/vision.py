import cv2
import numpy as np
import os

class VisionCore:
    def __init__(self):
        # 初始化默认的HSV阈值，后续可由GUI配置传入覆盖
        self.hsv_config = {
            "green": {"min": [40, 50, 50], "max": [80, 255, 255]},
            "yellow": {"min": [15, 100, 100], "max": [35, 255, 255]}
        }
        self._template_cache = {}
        self._processed_template_cache = {}
        # 预分配固定尺寸形态学核，避免每帧重复创建
        self._kernel_3x3 = np.ones((3, 3), np.uint8)
        self._kernel_2x3 = np.ones((2, 3), np.uint8)
        
    def update_hsv_config(self, color_name, min_val, max_val):
        """用于GUI动态调节HSV参数"""
        if color_name in self.hsv_config:
            self.hsv_config[color_name]["min"] = min_val
            self.hsv_config[color_name]["max"] = max_val

    def _read_template(self, template_path):
        path = os.fspath(template_path)
        if path in self._template_cache:
            return self._template_cache[path]

        if not os.path.exists(path):
            self._template_cache[path] = None
            return None

        template = cv2.imdecode(np.fromfile(path, dtype=np.uint8), -1)
        self._template_cache[path] = template
        return template

    def _to_gray(self, image):
        if image is None:
            return None
        if len(image.shape) == 2:
            return image.copy()
        if len(image.shape) == 3 and image.shape[2] == 4:
            alpha_channel = image[:, :, 3]
            rgb_channels = image[:, :, :3]
            background = np.zeros_like(rgb_channels, dtype=np.uint8)
            alpha_factor = alpha_channel[:, :, np.newaxis] / 255.0
            bgr = (rgb_channels * alpha_factor + background * (1 - alpha_factor)).astype(np.uint8)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    def _prepare_for_match(self, image, use_edge=False, use_binary=False, binary_threshold=200):
        gray = self._to_gray(image)
        if gray is None:
            return None
        if use_binary:
            _, gray = cv2.threshold(gray, binary_threshold, 255, cv2.THRESH_BINARY)
        elif use_edge:
            v = np.median(gray)
            lower = int(max(0, (1.0 - 0.33) * v))
            upper = int(min(255, (1.0 + 0.33) * v))
            gray = cv2.Canny(gray, lower, upper)
        return gray

    def _template_for_match(self, template_path, use_edge=False, use_binary=False, binary_threshold=200):
        path = os.fspath(template_path)
        cache_key = (path, bool(use_edge), bool(use_binary), int(binary_threshold))
        if cache_key in self._processed_template_cache:
            return self._processed_template_cache[cache_key]

        template = self._read_template(path)
        if template is None:
            print(f"[Vision] 无法解析图片数据: {path}")
            self._processed_template_cache[cache_key] = None
            return None

        prepared = self._prepare_for_match(template, use_edge=use_edge, use_binary=use_binary, binary_threshold=binary_threshold)
        self._processed_template_cache[cache_key] = prepared
        return prepared

    def _template_mask_for_match(
        self,
        template_path,
        use_edge=False,
        use_binary=False,
        binary_threshold=200,
        mask_threshold=8,
    ):
        path = os.fspath(template_path)
        cache_key = ("mask", path, bool(use_edge), bool(use_binary), int(binary_threshold), int(mask_threshold))
        if cache_key in self._processed_template_cache:
            return self._processed_template_cache[cache_key]

        template = self._read_template(path)
        if template is None:
            self._processed_template_cache[cache_key] = None
            return None

        if len(template.shape) == 3 and template.shape[2] == 4:
            mask = cv2.inRange(template[:, :, 3], 12, 255)
        else:
            gray = self._to_gray(template)
            if gray is None:
                self._processed_template_cache[cache_key] = None
                return None
            if use_edge:
                v = np.median(gray)
                lower = int(max(0, (1.0 - 0.33) * v))
                upper = int(min(255, (1.0 + 0.33) * v))
                mask = cv2.Canny(gray, lower, upper)
                mask = cv2.dilate(mask, self._kernel_3x3, iterations=1)
            elif use_binary:
                threshold = max(1, min(245, int(binary_threshold) - 25))
                _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
            else:
                border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
                background = int(np.median(border))
                diff = cv2.absdiff(gray, np.full_like(gray, background))
                _, mask = cv2.threshold(diff, int(mask_threshold), 255, cv2.THRESH_BINARY)
                if cv2.countNonZero(mask) < 5:
                    _, mask = cv2.threshold(gray, 35, 255, cv2.THRESH_BINARY)

        if cv2.countNonZero(mask) < 5:
            mask = None
        self._processed_template_cache[cache_key] = mask
        return mask

    def _build_scales(self, scale_range=None, scale_steps=11):
        if scale_range is None:
            low, high = 0.5, 1.5
        else:
            low, high = float(scale_range[0]), float(scale_range[1])
        if low > high:
            low, high = high, low
        low = max(0.20, low)
        high = max(low, min(4.00, high))
        steps = max(1, int(scale_steps))
        if steps == 1 or abs(high - low) < 0.001:
            return [low]
        scales = list(np.linspace(high, low, steps))
        common_scales = (
            0.50, 0.625, 0.75, 0.80, 0.90, 1.0, 1.10, 1.25, 1.50, 1.75, 2.0, 3.0,
        )
        if steps >= 7:
            scales.extend(scale for scale in common_scales if low <= scale <= high)
        if low <= 1.0 <= high and all(abs(scale - 1.0) > 0.015 for scale in scales):
            scales.append(1.0)
        merge_tolerance = max(0.012, min(0.08, (high - low) / max(steps * 2.0, 1.0)))
        unique_scales = []
        for scale in sorted(scales, reverse=True):
            if all(abs(scale - existing) > merge_tolerance for existing in unique_scales):
                unique_scales.append(scale)
        return unique_scales

    def find_template(
        self,
        screen_img,
        template_path,
        threshold=0.75,
        use_edge=False,
        use_binary=False,
        scale_range=None,
        scale_steps=11,
        binary_threshold=200,
        use_mask=False,
        mask_threshold=8,
    ):
        """
        在屏幕截图中寻找模板图片 (支持中文路径)
        use_edge: 是否使用 Canny 边缘检测匹配（排除光照干扰）
        use_binary: 是否使用二值化提取高亮特征匹配（适用于白天水面强光下的纯白 UI 图标）
        返回 (x, y) 坐标，如果没有找到返回 (None, None)
        """
        try:
            screen_gray = self._prepare_for_match(
                screen_img,
                use_edge=use_edge,
                use_binary=use_binary,
                binary_threshold=binary_threshold,
            )
            template_gray = self._template_for_match(
                template_path,
                use_edge=use_edge,
                use_binary=use_binary,
                binary_threshold=binary_threshold,
            )
            template_mask = None
            if use_mask:
                template_mask = self._template_mask_for_match(
                    template_path,
                    use_edge=use_edge,
                    use_binary=use_binary,
                    binary_threshold=binary_threshold,
                    mask_threshold=mask_threshold,
                )

            if screen_gray is None or template_gray is None:
                return None, 0.0
            if use_binary and cv2.countNonZero(screen_gray) < 5:
                return None, 0.0
            
            best_match = None
            best_val = -1
            best_loc = None
            
            for scale in self._build_scales(scale_range=scale_range, scale_steps=scale_steps):
                # 缩放模板
                width = int(round(template_gray.shape[1] * scale))
                height = int(round(template_gray.shape[0] * scale))
                
                # 如果缩放后的模板比截图还要大，就跳过
                if width < 4 or height < 4 or width > screen_gray.shape[1] or height > screen_gray.shape[0]:
                    continue

                interpolation = cv2.INTER_NEAREST if use_binary else (cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
                resized_template = cv2.resize(template_gray, (width, height), interpolation=interpolation)
                if use_binary:
                    _, resized_template = cv2.threshold(resized_template, 127, 255, cv2.THRESH_BINARY)
                    if cv2.countNonZero(resized_template) < 5:
                        continue
                if float(np.std(resized_template)) < 1.0:
                    continue

                resized_mask = None
                match_method = cv2.TM_CCOEFF_NORMED
                if template_mask is not None:
                    resized_mask = cv2.resize(template_mask, (width, height), interpolation=cv2.INTER_NEAREST)
                    _, resized_mask = cv2.threshold(resized_mask, 1, 255, cv2.THRESH_BINARY)
                    if cv2.countNonZero(resized_mask) >= 5:
                        match_method = cv2.TM_CCORR_NORMED
                    else:
                        resized_mask = None
                
                # 进行匹配
                if resized_mask is not None:
                    res = cv2.matchTemplate(screen_gray, resized_template, match_method, mask=resized_mask)
                else:
                    res = cv2.matchTemplate(screen_gray, resized_template, match_method)
                res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
                max_val = max(-1.0, min(1.0, float(max_val)))
                
                if max_val > best_val:
                    best_val = max_val
                    best_loc = max_loc
                    best_match = resized_template

            if best_val >= threshold and best_match is not None:
                h, w = best_match.shape[:2]
                center_x = best_loc[0] + w // 2
                center_y = best_loc[1] + h // 2
                return (center_x, center_y), best_val
                
            return None, best_val
        except Exception as e:
            print(f"[Vision] Template matching error: {e}")
            return None, 0.0

    def find_best_template(self, screen_img, template_paths, threshold=0.75, **kwargs):
        """在多个模板中返回置信度最高的匹配。"""
        best_loc = None
        best_conf = -1.0
        best_path = None
        early_accept = kwargs.pop("early_accept", None)

        for template_path in template_paths or []:
            loc, conf = self.find_template(screen_img, template_path, threshold=threshold, **kwargs)
            if conf > best_conf:
                best_loc = loc
                best_conf = conf
                best_path = template_path
            if loc is not None and early_accept is not None and conf >= float(early_accept):
                return loc, conf, template_path

        if best_loc is not None and best_conf >= threshold:
            return best_loc, best_conf, best_path
        return None, best_conf, best_path

    def find_template_matches(
        self,
        screen_img,
        template_path,
        threshold=0.75,
        max_matches=12,
        min_distance=24,
        **kwargs,
    ):
        matches = []
        if screen_img is None or not template_path:
            return matches

        use_edge = bool(kwargs.get("use_edge", False))
        use_binary = bool(kwargs.get("use_binary", False))
        binary_threshold = int(kwargs.get("binary_threshold", 200))
        scale_range = kwargs.get("scale_range")
        scale_steps = kwargs.get("scale_steps", 7)
        use_mask = bool(kwargs.get("use_mask", False))
        mask_threshold = int(kwargs.get("mask_threshold", 8))

        try:
            screen_gray = self._prepare_for_match(
                screen_img,
                use_edge=use_edge,
                use_binary=use_binary,
                binary_threshold=binary_threshold,
            )
            template_gray = self._template_for_match(
                template_path,
                use_edge=use_edge,
                use_binary=use_binary,
                binary_threshold=binary_threshold,
            )
            if screen_gray is None or template_gray is None:
                return matches

            template_mask = None
            if use_mask:
                template_mask = self._template_mask_for_match(
                    template_path,
                    use_edge=use_edge,
                    use_binary=use_binary,
                    binary_threshold=binary_threshold,
                    mask_threshold=mask_threshold,
                )

            max_matches = max(1, int(max_matches))
            min_distance = max(1, int(min_distance))
            for scale in self._build_scales(scale_range=scale_range, scale_steps=scale_steps):
                width = int(round(template_gray.shape[1] * scale))
                height = int(round(template_gray.shape[0] * scale))
                if width < 4 or height < 4 or width > screen_gray.shape[1] or height > screen_gray.shape[0]:
                    continue

                interpolation = cv2.INTER_NEAREST if use_binary else (cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
                resized_template = cv2.resize(template_gray, (width, height), interpolation=interpolation)
                if float(np.std(resized_template)) < 1.0:
                    continue

                resized_mask = None
                match_method = cv2.TM_CCOEFF_NORMED
                if template_mask is not None:
                    resized_mask = cv2.resize(template_mask, (width, height), interpolation=cv2.INTER_NEAREST)
                    _, resized_mask = cv2.threshold(resized_mask, 1, 255, cv2.THRESH_BINARY)
                    if cv2.countNonZero(resized_mask) >= 5:
                        match_method = cv2.TM_CCORR_NORMED
                    else:
                        resized_mask = None

                if resized_mask is not None:
                    res = cv2.matchTemplate(screen_gray, resized_template, match_method, mask=resized_mask)
                else:
                    res = cv2.matchTemplate(screen_gray, resized_template, match_method)
                res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)

                while len(matches) < max_matches:
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    max_val = max(-1.0, min(1.0, float(max_val)))
                    if max_val < threshold:
                        break
                    center = (max_loc[0] + width // 2, max_loc[1] + height // 2)
                    if all((center[0] - item["location"][0]) ** 2 + (center[1] - item["location"][1]) ** 2 >= min_distance ** 2 for item in matches):
                        matches.append({
                            "location": center,
                            "confidence": max_val,
                            "template": template_path,
                            "scale": scale,
                            "size": (width, height),
                        })
                    x1 = max(0, max_loc[0] - min_distance)
                    y1 = max(0, max_loc[1] - min_distance)
                    x2 = min(res.shape[1], max_loc[0] + width + min_distance)
                    y2 = min(res.shape[0], max_loc[1] + height + min_distance)
                    res[y1:y2, x1:x2] = -1.0

            matches.sort(key=lambda item: item["confidence"], reverse=True)
            return matches[:max_matches]
        except Exception as e:
            print(f"[Vision] Template multi-match error: {e}")
            return []

    def find_best_template_multi_strategy(self, screen_img, template_paths, strategies, threshold=0.75, **base_kwargs):
        """使用多种预处理策略匹配模板，返回最可靠的命中。"""
        best_raw = (None, -1.0, None, "")
        best_pass = (None, -1.0, None, "", -999.0)

        for strategy in strategies or ():
            params = dict(base_kwargs)
            params.update({k: v for k, v in strategy.items() if k not in {"name", "threshold"}})
            local_threshold = float(strategy.get("threshold", threshold))
            loc, conf, matched_path = self.find_best_template(
                screen_img,
                template_paths,
                threshold=local_threshold,
                **params,
            )
            strategy_name = strategy.get("name", "")
            if conf > best_raw[1]:
                best_raw = (loc, conf, matched_path, strategy_name)
            if loc is not None and conf >= local_threshold:
                margin = conf - local_threshold
                if margin > best_pass[4]:
                    best_pass = (loc, conf, matched_path, strategy_name, margin)
                early_accept = strategy.get("early_accept")
                if early_accept is not None and conf >= float(early_accept):
                    return loc, conf, matched_path, strategy_name

        if best_pass[0] is not None:
            return best_pass[0], best_pass[1], best_pass[2], best_pass[3]
        return None, best_raw[1], best_raw[2], best_raw[3]

    def _target_color_profile(self, reference_paths):
        paths = tuple(os.fspath(path) for path in (reference_paths or ()) if path)
        cache_key = ("target-color-profile", paths)
        if cache_key in self._processed_template_cache:
            return self._processed_template_cache[cache_key]

        samples = []
        for path in paths:
            template = self._read_template(path)
            if template is None or template.size == 0:
                continue
            if len(template.shape) == 3 and template.shape[2] == 4:
                alpha_mask = template[:, :, 3] > 20
                bgr = template[:, :, :3]
            else:
                alpha_mask = None
                bgr = template[:, :, :3] if len(template.shape) == 3 else cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)

            hsv_ref = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            valid = (hsv_ref[:, :, 1] >= 70) & (hsv_ref[:, :, 2] >= 70)
            if alpha_mask is not None:
                valid &= alpha_mask
            if np.count_nonzero(valid) < 12:
                continue
            samples.append(hsv_ref[valid])

        if samples:
            values = np.concatenate(samples, axis=0)
            hue = values[:, 0]
            sat = values[:, 1]
            val = values[:, 2]
            h_low = int(max(0, np.percentile(hue, 1) - 4))
            h_high = int(min(179, np.percentile(hue, 99) + 6))
            s_low = int(max(145, np.percentile(sat, 10) - 18))
            v_low = int(max(96, np.percentile(val, 5) - 38))
            profile = {
                "strict": (h_low, h_high, s_low, v_low),
                "relaxed": (
                    max(0, h_low - 4),
                    min(179, h_high + 5),
                    max(122, s_low - 20),
                    max(78, v_low - 24),
                ),
            }
        else:
            profile = {
                "strict": (78, 109, 145, 96),
                "relaxed": (74, 114, 122, 78),
            }

        self._processed_template_cache[cache_key] = profile
        return profile

    def _target_reference_mask(self, hsv, reference_paths, relaxed=False):
        if hsv is None:
            return None
        profile = self._target_color_profile(reference_paths)
        h_low, h_high, s_low, v_low = profile["relaxed" if relaxed else "strict"]
        lower = np.array([h_low, s_low, v_low], dtype=np.uint8)
        upper = np.array([h_high, 255, 255], dtype=np.uint8)
        return cv2.inRange(hsv, lower, upper)

    def _cursor_color_profile(self, reference_paths):
        paths = tuple(os.fspath(path) for path in (reference_paths or ()) if path)
        cache_key = ("cursor-color-profile", paths)
        if cache_key in self._processed_template_cache:
            return self._processed_template_cache[cache_key]

        samples = []
        for path in paths:
            template = self._read_template(path)
            if template is None or template.size == 0:
                continue
            if len(template.shape) == 3 and template.shape[2] == 4:
                alpha_mask = template[:, :, 3] > 20
                bgr = template[:, :, :3]
            else:
                alpha_mask = None
                bgr = template[:, :, :3] if len(template.shape) == 3 else cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)

            hsv_ref = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            valid = (
                (hsv_ref[:, :, 0] >= 12)
                & (hsv_ref[:, :, 0] <= 45)
                & (hsv_ref[:, :, 1] >= 70)
                & (hsv_ref[:, :, 2] >= 110)
            )
            if alpha_mask is not None:
                valid &= alpha_mask
            if np.count_nonzero(valid) < 8:
                continue
            samples.append(hsv_ref[valid])

        if samples:
            values = np.concatenate(samples, axis=0)
            hue = values[:, 0]
            sat = values[:, 1]
            val = values[:, 2]
            h_low = int(max(0, np.percentile(hue, 1) - 4))
            h_high = int(min(179, np.percentile(hue, 99) + 4))
            s_low = int(max(72, np.percentile(sat, 5) - 22))
            v_low = int(max(104, np.percentile(val, 1) - 18))
            profile = {
                "strict": (h_low, h_high, s_low, v_low),
                "relaxed": (
                    max(0, h_low - 3),
                    min(179, h_high + 3),
                    max(60, s_low - 16),
                    max(88, v_low - 18),
                ),
            }
        else:
            profile = {
                "strict": (18, 35, 72, 104),
                "relaxed": (15, 38, 60, 88),
            }

        self._processed_template_cache[cache_key] = profile
        return profile

    def _cursor_reference_mask(self, hsv, reference_paths, relaxed=False):
        if hsv is None or not reference_paths:
            return None
        profile = self._cursor_color_profile(reference_paths)
        h_low, h_high, s_low, v_low = profile["relaxed" if relaxed else "strict"]
        lower = np.array([h_low, s_low, v_low], dtype=np.uint8)
        upper = np.array([h_high, 255, 255], dtype=np.uint8)
        return cv2.inRange(hsv, lower, upper)

    def analyze_fishing_bar(
        self,
        roi_img,
        cursor_template_paths=None,
        cursor_color_reference_paths=None,
        target_template_paths=None,
        target_color_reference_paths=None,
        cursor_scale_range=None,
        cursor_scale_steps=5,
        target_scale_range=None,
        target_scale_steps=5,
        allow_target_template=False,
        draw_debug=True,
    ):
        """
        解析上方耐力条区域。
        先定位黄色游标所在的 HUD 水平带，再只在同一带内寻找横向绿色目标条，
        避免树林、水草等大面积绿色背景参与目标中心计算。
        """
        if roi_img is None or roi_img.size == 0:
            return None, None, None, roi_img, 0.0

        debug_img = roi_img.copy() if draw_debug else None
        roi_h, roi_w = roi_img.shape[:2]
        hsv = cv2.cvtColor(roi_img, cv2.COLOR_BGR2HSV)

        reference_paths = target_color_reference_paths if target_color_reference_paths is not None else target_template_paths
        has_reference_color = bool(reference_paths)
        cursor_reference_paths = cursor_color_reference_paths if cursor_color_reference_paths is not None else cursor_template_paths
        target_mask = None
        initial_target = None
        if has_reference_color:
            green_candidates = []
            initial_reference_mask = self._target_reference_mask(hsv, reference_paths, relaxed=False)
            if initial_reference_mask is not None and cv2.countNonZero(initial_reference_mask) >= max(8, int(roi_w * roi_h * 0.00020)):
                initial_reference_mask = cv2.morphologyEx(initial_reference_mask, cv2.MORPH_CLOSE, np.ones((3, max(3, int(roi_w * 0.008))), np.uint8))
                initial_reference_mask = cv2.morphologyEx(initial_reference_mask, cv2.MORPH_OPEN, self._kernel_2x3)
                initial_target = self._select_reference_color_bar_component(
                    initial_reference_mask,
                    {"cy": roi_h * 0.5},
                    roi_w,
                    roi_h,
                    hsv=hsv,
                )
                if initial_target is not None:
                    green_candidates = [initial_target]
        else:
            green_cfg = self.hsv_config.get("green", {})
            lower_green = np.array(green_cfg.get("min", [35, 45, 45]), dtype=np.uint8)
            upper_green = np.array(green_cfg.get("max", [95, 255, 255]), dtype=np.uint8)
            lower_green = np.maximum(lower_green, np.array([35, 45, 70], dtype=np.uint8))
            upper_green = np.maximum(upper_green, np.array([95, 255, 255], dtype=np.uint8))
            target_mask = cv2.inRange(hsv, lower_green, upper_green)

            green_probe_mask = target_mask.copy()
            probe_values = hsv[:, :, 2][green_probe_mask > 0]
            if probe_values.size:
                probe_v_floor = max(int(lower_green[2]), min(155, int(np.percentile(probe_values, 65)) + 6))
                refined_probe = green_probe_mask.copy()
                refined_probe[hsv[:, :, 2] < probe_v_floor] = 0
                if cv2.countNonZero(refined_probe) >= max(8, int(roi_w * roi_h * 0.0004)):
                    green_probe_mask = refined_probe
            green_probe_mask = cv2.morphologyEx(green_probe_mask, cv2.MORPH_CLOSE, np.ones((3, max(7, int(roi_w * 0.025))), np.uint8))
            green_probe_mask = cv2.morphologyEx(green_probe_mask, cv2.MORPH_OPEN, self._kernel_2x3)
            green_candidates = self._collect_green_bar_candidates(green_probe_mask, roi_w, roi_h)

        cursor_mask = self._cursor_reference_mask(hsv, cursor_reference_paths, relaxed=False)
        if cursor_mask is None or cv2.countNonZero(cursor_mask) < max(4, int(roi_w * roi_h * 0.00010)):
            yellow_cfg = self.hsv_config.get("yellow", {})
            lower_yellow = np.array(yellow_cfg.get("min", [15, 80, 130]), dtype=np.uint8)
            upper_yellow = np.array(yellow_cfg.get("max", [45, 255, 255]), dtype=np.uint8)
            lower_yellow = np.minimum(lower_yellow, np.array([15, 80, 130], dtype=np.uint8))
            upper_yellow = np.maximum(upper_yellow, np.array([45, 255, 255], dtype=np.uint8))
            cursor_mask = cv2.inRange(hsv, lower_yellow, upper_yellow)

        cursor_kernel = self._kernel_3x3
        cursor_mask = cv2.morphologyEx(cursor_mask, cv2.MORPH_OPEN, cursor_kernel)
        cursor_mask = cv2.morphologyEx(cursor_mask, cv2.MORPH_CLOSE, cursor_kernel)

        cursor_candidates = self._collect_cursor_components(cursor_mask, roi_w, roi_h, hsv_roi=hsv)
        cursor = self._select_cursor_candidate(cursor_candidates, green_candidates, roi_w, roi_h)
        needs_template = cursor is None or cursor.get("confidence", 0.0) < 0.55 or cursor.get("score", 0.0) < 0.58
        if cursor_template_paths and needs_template:
            template_cursor = self._cursor_template_candidate(
                roi_img,
                cursor_template_paths or (),
                roi_w,
                roi_h,
                cursor_scale_range,
                cursor_scale_steps,
            )
            if template_cursor is not None:
                cursor_candidates.append(template_cursor)
                cursor = self._select_cursor_candidate(cursor_candidates, green_candidates, roi_w, roi_h)
        if cursor is None:
            if debug_img is not None:
                cv2.putText(debug_img, "cursor missing", (4, max(12, roi_h - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 255), 1)
            return None, None, None, debug_img, 0.0

        band_half = max(6, int(cursor["h"] * 0.85), int(roi_h * 0.22))
        band_y1 = max(0, int(cursor["cy"]) - band_half)
        band_y2 = min(roi_h, int(cursor["cy"]) + band_half + 1)

        target = None
        if has_reference_color:
            reference_mask = initial_reference_mask  # 复用经形态学处理后的结果（第574-575行）
            if reference_mask is not None:
                reference_band_mask = np.zeros_like(reference_mask)
                reference_band_mask[band_y1:band_y2, :] = reference_mask[band_y1:band_y2, :]
                if cv2.countNonZero(reference_band_mask[band_y1:band_y2, :]) >= max(8, int(roi_w * roi_h * 0.00020)):
                    ref_close_w = max(3, int(roi_w * 0.008))
                    reference_band_mask = cv2.morphologyEx(reference_band_mask, cv2.MORPH_CLOSE, np.ones((3, ref_close_w), np.uint8))
                    reference_band_mask = cv2.morphologyEx(reference_band_mask, cv2.MORPH_OPEN, self._kernel_2x3)
                    target = self._select_horizontal_run_green_bar(reference_band_mask, cursor, roi_w, roi_h, hsv=hsv)
                    if target is None:
                        target = self._select_reference_color_bar_component(reference_band_mask, cursor, roi_w, roi_h, hsv=hsv)

            if target is None:
                relaxed_reference_mask = self._target_reference_mask(hsv, reference_paths, relaxed=True)
                if relaxed_reference_mask is not None:
                    relaxed_reference_band_mask = np.zeros_like(relaxed_reference_mask)
                    relaxed_reference_band_mask[band_y1:band_y2, :] = relaxed_reference_mask[band_y1:band_y2, :]
                    if cv2.countNonZero(relaxed_reference_band_mask[band_y1:band_y2, :]) >= max(8, int(roi_w * roi_h * 0.00020)):
                        relaxed_ref_close_w = max(3, int(roi_w * 0.010))
                        relaxed_reference_band_mask = cv2.morphologyEx(relaxed_reference_band_mask, cv2.MORPH_CLOSE, np.ones((3, relaxed_ref_close_w), np.uint8))
                        relaxed_reference_band_mask = cv2.morphologyEx(relaxed_reference_band_mask, cv2.MORPH_OPEN, self._kernel_2x3)
                        target = self._select_reference_color_bar_component(
                            relaxed_reference_band_mask,
                            cursor,
                            roi_w,
                            roi_h,
                            hsv=hsv,
                            relaxed=True,
                        )
        if target is None and initial_target is not None:
            target = initial_target

        if target is None and not has_reference_color:
            band_mask = np.zeros_like(target_mask)
            band_mask[band_y1:band_y2, :] = target_mask[band_y1:band_y2, :]
            relaxed_band_mask = band_mask.copy()
            band_values = hsv[band_y1:band_y2, :, 2][band_mask[band_y1:band_y2, :] > 0]
            if band_values.size:
                adaptive_v_floor = max(int(lower_green[2]), min(155, int(np.percentile(band_values, 65)) + 6))
                refined_band_mask = band_mask.copy()
                refined_band_mask[hsv[:, :, 2] < adaptive_v_floor] = 0
                if cv2.countNonZero(refined_band_mask[band_y1:band_y2, :]) >= max(8, int(roi_w * roi_h * 0.0004)):
                    band_mask = refined_band_mask
            close_w = max(5, int(roi_w * 0.018))
            band_mask = cv2.morphologyEx(band_mask, cv2.MORPH_CLOSE, np.ones((3, close_w), np.uint8))
            band_mask = cv2.morphologyEx(band_mask, cv2.MORPH_OPEN, self._kernel_2x3)

            target = self._select_green_bar_component(band_mask, roi_w, roi_h, band_y1, band_y2, cursor, hsv=hsv)
            if target is None:
                target = self._select_split_green_bar_near_cursor(band_mask, cursor, roi_w, roi_h, hsv=hsv)
            if target is None:
                relaxed_close_w = max(5, int(roi_w * 0.020))
                relaxed_band_mask = cv2.morphologyEx(relaxed_band_mask, cv2.MORPH_CLOSE, np.ones((3, relaxed_close_w), np.uint8))
                relaxed_band_mask = cv2.morphologyEx(relaxed_band_mask, cv2.MORPH_OPEN, self._kernel_2x3)
                target = self._select_green_bar_component(
                    relaxed_band_mask,
                    roi_w,
                    roi_h,
                    band_y1,
                    band_y2,
                    cursor,
                    hsv=hsv,
                    relaxed=True,
                )
                if target is None:
                    target = self._select_split_green_bar_near_cursor(
                        relaxed_band_mask,
                        cursor,
                        roi_w,
                        roi_h,
                        hsv=hsv,
                        relaxed=True,
                    )
            if target is None:
                target = self._select_green_candidate_near_cursor(green_candidates, cursor, roi_w, roi_h, hsv=hsv)
        if target is None and allow_target_template and target_template_paths:
            target = self._target_bar_template_candidate(
                roi_img,
                target_template_paths or (),
                cursor,
                roi_w,
                roi_h,
                target_scale_range,
                target_scale_steps,
            )

        cursor_x = int(cursor["cx"])
        target_x = int(target["cx"]) if target else None
        target_w = int(target["w"]) if target else None
        confidence = 0.0
        if target:
            confidence = min(0.98, cursor["confidence"] * 0.42 + target["confidence"] * 0.58)

        if debug_img is not None:
            cv2.rectangle(debug_img, (0, band_y1), (roi_w - 1, max(band_y1, band_y2 - 1)), (255, 120, 0), 1)
            cv2.rectangle(
                debug_img,
                (int(cursor["x"]), int(cursor["y"])),
                (int(cursor["x"] + cursor["w"]), int(cursor["y"] + cursor["h"])),
                (0, 255, 255),
                1,
            )
            cv2.line(debug_img, (cursor_x, 0), (cursor_x, roi_h), (0, 255, 255), 2)
            if target:
                cv2.rectangle(
                    debug_img,
                    (int(target["x"]), int(target["y"])),
                    (int(target["x"] + target["w"]), int(target["y"] + target["h"])),
                    (0, 180, 0),
                    1,
                )
                cv2.line(debug_img, (target_x, 0), (target_x, roi_h), (0, 255, 0), 2)
            source = cursor.get("source", "color")
            if target:
                track_score = target.get("track_score", 0.0)
                cv2.putText(debug_img, f"conf {confidence:.2f} {source} rail {track_score:.2f}", (4, max(12, roi_h - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            else:
                cv2.putText(debug_img, f"conf {confidence:.2f} {source}", (4, max(12, roi_h - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

        return target_x, cursor_x, target_w, debug_img, confidence

    def _cursor_template_candidate(self, roi_img, cursor_template_paths, roi_w, roi_h, scale_range=None, scale_steps=5):
        if not cursor_template_paths:
            return None

        strategies = (
            {"name": "cursor-gray-mask", "threshold": 0.70, "use_mask": True, "mask_threshold": 5},
            {"name": "cursor-binary-mask", "threshold": 0.64, "use_binary": True, "binary_threshold": 150, "use_mask": True},
            {"name": "cursor-edge", "threshold": 0.56, "use_edge": True},
        )
        loc, conf, matched_path, strategy = self.find_best_template_multi_strategy(
            roi_img,
            cursor_template_paths,
            strategies,
            threshold=0.60,
            scale_range=scale_range or (0.70, 1.55),
            scale_steps=max(3, int(scale_steps)),
        )
        if loc is None:
            return None

        template = self._read_template(matched_path)
        if template is not None:
            template_h, template_w = template.shape[:2]
        else:
            template_h, template_w = max(6, int(roi_h * 0.45)), max(3, int(roi_h * 0.16))
        h = max(int(template_h), int(roi_h * 0.35))
        h = min(max(4, h), roi_h)
        w = max(int(template_w), int(h * 0.28), 3)
        w = min(max(3, w), max(4, int(roi_w * 0.12)))
        cx, cy = float(loc[0]), float(loc[1])
        x = int(round(cx - w / 2))
        y = int(round(cy - h / 2))
        x = max(0, min(roi_w - w, x))
        y = max(0, min(roi_h - h, y))
        return {
            "x": x,
            "y": y,
            "w": int(w),
            "h": int(h),
            "area": int(w * h),
            "cx": cx,
            "cy": cy,
            "confidence": max(0.0, min(0.98, float(conf))),
            "score": max(0.0, min(0.98, float(conf))),
            "source": "template",
            "strategy": strategy,
        }

    def _target_bar_template_candidate(self, roi_img, target_template_paths, cursor, roi_w, roi_h, scale_range=None, scale_steps=5):
        if not target_template_paths:
            return None

        strategies = (
            {"name": "target-gray-mask", "threshold": 0.54, "use_mask": True, "mask_threshold": 6},
        )
        loc, conf, matched_path, strategy = self.find_best_template_multi_strategy(
            roi_img,
            target_template_paths,
            strategies,
            threshold=0.52,
            scale_range=scale_range or (0.55, 1.35),
            scale_steps=max(1, int(scale_steps)),
        )
        if loc is None:
            return None

        template = self._read_template(matched_path)
        if template is not None:
            template_h, template_w = template.shape[:2]
        else:
            template_h, template_w = max(4, int(roi_h * 0.42)), max(12, int(roi_w * 0.14))

        scale_w = max(8.0, min(float(roi_w) * 0.42, float(template_w)))
        scale_h = max(3.0, min(float(roi_h) * 0.45, float(template_h)))
        cx, cy = float(loc[0]), float(loc[1])
        if cursor is not None:
            y_delta = abs(cy - float(cursor.get("cy", cy)))
            if y_delta > max(5.0, roi_h * 0.16):
                return None

        w = int(round(scale_w))
        h = int(round(scale_h))
        x = int(round(cx - w / 2))
        y = int(round(cy - h / 2))
        x = max(0, min(max(0, roi_w - w), x))
        y = max(0, min(max(0, roi_h - h), y))

        hsv = cv2.cvtColor(roi_img, cv2.COLOR_BGR2HSV)
        color_quality = self._bar_color_quality(hsv, x, y, w, h)
        if color_quality < 0.42:
            return None
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        target_like = (
            (hue >= 48)
            & (hue <= 102)
            & (sat >= 125)
            & (val >= 88)
        )
        bad_wide_rows = 0
        checked_rows = 0
        row_start = max(0, int(y))
        row_end = min(roi_h, int(y + h))
        center_col = max(0, min(roi_w - 1, int(round(cx))))
        for row in range(row_start, row_end):
            row_mask = target_like[row]
            if not row_mask[center_col]:
                continue
            left = center_col
            while left > 0 and row_mask[left - 1]:
                left -= 1
            right = center_col
            while right < roi_w - 1 and row_mask[right + 1]:
                right += 1
            run_w = right - left + 1
            checked_rows += 1
            edge_touch = left <= 1 or right >= roi_w - 2
            if (
                run_w > max(roi_w * 0.44, w * 2.20)
                or (edge_touch and run_w > max(roi_w * 0.22, w * 1.45))
                or (left <= 1 and right >= roi_w - 2)
            ):
                bad_wide_rows += 1
        if checked_rows and bad_wide_rows >= max(2, int(checked_rows * 0.45)):
            return None

        return {
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "area": int(w * h),
            "cx": cx,
            "cy": cy,
            "confidence": max(0.0, min(0.92, float(conf))),
            "score": max(0.0, min(1.05, float(conf) + 0.10)),
            "track_score": max(0.0, min(1.0, color_quality)),
            "source": "target-template",
            "strategy": strategy,
        }

    def _collect_cursor_components(self, mask, roi_w, roi_h, hsv_roi=None):
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        min_area = max(10, int(roi_w * roi_h * 0.0004))

        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < min_area or h < max(4, int(roi_h * 0.16)):
                continue
            if w > max(12, int(h * 0.44)):
                continue
            aspect = h / max(w, 1)
            if aspect < 1.8:
                continue
            rect_area = w * h
            extent = area / max(rect_area, 1)
            if extent < 0.55:
                continue
            cx, cy = centroids[index]
            vertical_score = min(1.0, aspect / 3.0)
            height_score = min(1.0, h / max(1, roi_h * 0.55))
            area_score = min(1.0, area / max(1, roi_w * roi_h * 0.012))
            center_score = 1.0 - min(1.0, abs(cy - roi_h * 0.5) / max(1.0, roi_h * 0.65))
            color_concentration = 0.5
            if hsv_roi is not None:
                region_mask = (labels[y:y+h, x:x+w] == index)
                region_hsv = hsv_roi[y:y+h, x:x+w][region_mask]
                if len(region_hsv) > 10:
                    h_std = float(np.std(region_hsv[:, 0].astype(float)))
                    color_concentration = max(0.0, min(1.0, 1.0 - (h_std / 20.0)))
            confidence = max(0.0, min(0.98,
                vertical_score * 0.28 + height_score * 0.25 +
                area_score * 0.15 + center_score * 0.12 + color_concentration * 0.20))
            score = confidence + area_score * 0.15
            candidates.append({
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "area": int(area),
                "cx": float(cx),
                "cy": float(cy),
                "confidence": confidence,
                "color_concentration": color_concentration,
                "score": score,
                "source": "color",
            })
        return candidates

    def _collect_green_bar_candidates(self, mask, roi_w, roi_h):
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        min_width = max(10, int(roi_w * 0.030))
        min_area = max(8, int(roi_w * roi_h * 0.00035))
        max_height = max(5, int(roi_h * 0.58))

        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < min_area or w < min_width or h < 2 or h > max_height:
                continue
            aspect = w / max(1, h)
            if aspect < 2.0:
                continue
            fill_ratio = area / max(1, w * h)
            if fill_ratio < 0.12:
                continue
            cx, cy = centroids[index]
            edge_penalty = 0.12 if (x <= 1 or x + w >= roi_w - 1) else 0.0
            score = min(1.0, aspect / 8.0) * 0.35 + min(1.0, w / max(1.0, roi_w * 0.18)) * 0.35
            score += min(1.0, fill_ratio / 0.55) * 0.20
            score += (1.0 - min(1.0, abs(cy - roi_h * 0.5) / max(1.0, roi_h * 0.55))) * 0.10
            score = max(0.0, score - edge_penalty)
            candidates.append({
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "area": int(area),
                "cx": float(cx),
                "cy": float(cy),
                "confidence": max(0.0, min(0.98, score)),
                "score": score,
            })
        return sorted(candidates, key=lambda item: item["score"], reverse=True)[:6]

    def _select_cursor_candidate(self, candidates, green_candidates, roi_w, roi_h):
        best = None
        for candidate in candidates:
            cx = float(candidate["cx"])
            cy = float(candidate["cy"])
            h = max(1, int(candidate["h"]))
            w = max(1, int(candidate["w"]))
            edge_distance = min(cx, max(0.0, roi_w - 1 - cx))
            edge_score = min(1.0, edge_distance / max(1.0, roi_w * 0.055))
            if candidate.get("source") != "template" and green_candidates and edge_score < 0.75:
                continue
            center_score = 1.0 - min(1.0, abs(cy - roi_h * 0.5) / max(1.0, roi_h * 0.62))
            slender_score = min(1.0, (h / max(w, 1)) / 3.2)
            band_score = 0.50
            if green_candidates:
                green_scores = []
                for green in green_candidates:
                    y_score = 1.0 - min(1.0, abs(cy - green["cy"]) / max(2.0, roi_h * 0.16, h * 0.9))
                    expected_h = max(4.0, green["h"] * 2.8)
                    height_score = 1.0 - min(1.0, abs(h - expected_h) / max(3.0, expected_h * 1.10))
                    margin = max(roi_w * 0.10, green["w"] * 0.75, 24.0)
                    left = float(green["x"]) - margin
                    right = float(green["x"] + green["w"]) + margin
                    if left <= cx <= right:
                        x_score = 1.0
                    else:
                        distance = left - cx if cx < left else cx - right
                        x_score = 1.0 - min(1.0, distance / max(8.0, margin * 0.80))
                    green_scores.append(y_score * 0.56 + height_score * 0.18 + x_score * 0.26)
                band_score = max(0.0, max(green_scores))
                if band_score < 0.36 and candidate.get("source") != "template":
                    continue

            template_bonus = 0.18 if candidate.get("source") == "template" else 0.0
            confidence = float(candidate.get("confidence", 0.0))
            color_c = float(candidate.get("color_concentration", 0.5))
            score = confidence * 0.30 + color_c * 0.16 + band_score * 0.22 + center_score * 0.10 + slender_score * 0.10 + edge_score * 0.10 + template_bonus
            if candidate.get("source") == "template" and confidence >= 0.78:
                score += 0.06
            candidate["score"] = max(0.0, min(1.20, score))
            candidate["confidence"] = max(0.0, min(0.98, confidence * 0.70 + band_score * 0.20 + edge_score * 0.10))
            if best is None or candidate["score"] > best["score"]:
                best = candidate

        return best

    def _green_track_score(self, hsv, x, y, w, h, cursor=None):
        """评估绿色候选是否嵌在 HUD 深色轨道内，而不是树林背景。"""
        if hsv is None or w <= 0 or h <= 0:
            return 0.0

        roi_h, roi_w = hsv.shape[:2]
        pad_x = max(4, int(h * 1.2))
        pad_y = max(3, int(h * 1.4))
        x1 = max(0, int(x) - pad_x)
        x2 = min(roi_w, int(x + w) + pad_x)
        top_y1 = max(0, int(y) - pad_y)
        top_y2 = max(0, int(y) - 1)
        bottom_y1 = min(roi_h, int(y + h) + 1)
        bottom_y2 = min(roi_h, int(y + h) + pad_y + 1)

        value = hsv[:, :, 2]
        saturation = hsv[:, :, 1]
        scene_brightness = float(np.median(value))
        dark_thresh = min(105, max(55, scene_brightness * 0.75))
        dark_mask = (value < dark_thresh) | ((value < dark_thresh + 35) & (saturation < 130))

        ratios = []
        for y1, y2 in ((top_y1, top_y2), (bottom_y1, bottom_y2)):
            region = dark_mask[y1:y2, x1:x2]
            if region.size:
                ratios.append(float(np.count_nonzero(region)) / float(region.size))
        adjacent_dark = sum(ratios) / len(ratios) if ratios else 0.0
        balanced_dark = min(ratios) if len(ratios) >= 2 else 0.0

        thin_limit = max(4.0, roi_h * 0.20)
        thin_score = 1.0 - min(1.0, max(0.0, float(h) - thin_limit) / max(1.0, roi_h * 0.22))

        cursor_score = 0.5
        if cursor is not None:
            cy = float(y) + float(h) / 2.0
            y_delta = abs(cy - float(cursor.get("cy", cy)))
            cursor_score = 1.0 - min(1.0, y_delta / max(3.0, roi_h * 0.14))

        return max(0.0, min(1.0, balanced_dark * 0.46 + adjacent_dark * 0.18 + thin_score * 0.22 + cursor_score * 0.14))

    def _bar_color_quality(self, hsv, x, y, w, h):
        """评估候选区域是否像真实青绿色耐力条，而不是普通树林背景。"""
        if hsv is None or w <= 0 or h <= 0:
            return 0.0

        roi_h, roi_w = hsv.shape[:2]
        x1 = max(0, int(x))
        y1 = max(0, int(y))
        x2 = min(roi_w, int(x + w))
        y2 = min(roi_h, int(y + h))
        patch = hsv[y1:y2, x1:x2]
        if patch.size == 0:
            return 0.0

        hue = patch[:, :, 0]
        sat = patch[:, :, 1]
        val = patch[:, :, 2]
        cyan_green = (hue >= 48) & (hue <= 102) & (sat >= 135) & (val >= 96)
        bright_core = (hue >= 56) & (hue <= 96) & (sat >= 155) & (val >= 108)
        cyan_ratio = float(np.count_nonzero(cyan_green)) / float(cyan_green.size)
        core_ratio = float(np.count_nonzero(bright_core)) / float(bright_core.size)
        sat_score = min(1.0, max(0.0, (float(np.mean(sat)) - 45.0) / 120.0))
        val_score = min(1.0, max(0.0, (float(np.mean(val)) - 75.0) / 115.0))
        return max(0.0, min(1.0, cyan_ratio * 0.42 + core_ratio * 0.32 + sat_score * 0.14 + val_score * 0.12))

    def _select_reference_color_bar_component(self, mask, cursor, roi_w, roi_h, hsv=None, relaxed=False):
        if mask is None or cursor is None:
            return None

        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        best = None
        cursor_cy = float(cursor.get("cy", roi_h * 0.5))
        min_width = max(36 if relaxed else 48, int(roi_w * (0.075 if relaxed else 0.095)))
        max_width = max(min_width + 1, int(roi_w * (0.44 if relaxed else 0.39)))
        max_height = max(6, int(roi_h * (0.92 if relaxed else 0.88)))
        max_y_delta = max(5.0 if relaxed else 4.0, roi_h * (0.18 if relaxed else 0.14))

        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < max(8, int(roi_w * roi_h * 0.00028)):
                continue
            if w < min_width or w > max_width or h < 2 or h > max_height:
                continue
            if x <= 1 and x + w >= roi_w - 2:
                continue
            aspect = w / max(1, h)
            if aspect < (2.0 if relaxed else 2.6):
                continue
            fill_ratio = area / max(1, w * h)
            if fill_ratio < (0.10 if relaxed else 0.14):
                continue

            cx, cy = centroids[index]
            y_delta = abs(float(cy) - cursor_cy)
            if y_delta > max_y_delta:
                continue

            color_quality = self._bar_color_quality(hsv, x, y, w, h)
            if color_quality < (0.42 if relaxed else 0.50):
                continue

            width_score = min(1.0, w / max(1.0, roi_w * 0.18))
            y_score = 1.0 - min(1.0, y_delta / max(1.0, roi_h * 0.35))
            fill_score = min(1.0, fill_ratio / 0.42)
            height_score = 1.0 - min(1.0, max(0.0, h - roi_h * 0.52) / max(1.0, roi_h * 0.20))
            confidence = color_quality * 0.36 + width_score * 0.25 + y_score * 0.21 + fill_score * 0.10 + height_score * 0.08
            score = confidence + width_score * 0.08 + color_quality * 0.08

            if best is None or score > best["score"]:
                best = {
                    "x": int(x),
                    "y": int(y),
                    "w": int(w),
                    "h": int(h),
                    "area": int(area),
                    "cx": float(cx),
                    "cy": float(cy),
                    "confidence": max(0.0, min(0.92, confidence)),
                    "score": max(0.0, min(1.08, score)),
                    "track_score": max(0.0, min(1.0, color_quality)),
                    "source": "reference-color",
                }

        return best

    def _select_horizontal_run_green_bar(self, mask, cursor, roi_w, roi_h, hsv=None, relaxed=False):
        if hsv is None or mask is None or cursor is None:
            return None

        cursor_cx = float(cursor.get("cx", 0.0))
        cursor_cy = float(cursor.get("cy", roi_h * 0.5))
        cursor_h = float(cursor.get("h", max(4, roi_h * 0.5)))
        y_half = max(5, int(roi_h * (0.26 if relaxed else 0.22)), int(cursor_h * 0.45))
        y1 = max(0, int(round(cursor_cy)) - y_half)
        y2 = min(roi_h, int(round(cursor_cy)) + y_half + 1)
        if y2 <= y1:
            return None

        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        target_like = (
            (mask > 0)
            & (hue >= 48)
            & (hue <= 102)
            & (sat >= (125 if relaxed else 135))
            & (val >= (88 if relaxed else 96))
        )

        min_piece_w = max(8 if relaxed else 10, int(roi_w * (0.014 if relaxed else 0.018)))
        min_full_w = max(32 if relaxed else 42, int(roi_w * (0.075 if relaxed else 0.095)))
        max_full_w = max(min_full_w + 1, int(roi_w * (0.44 if relaxed else 0.38)))
        gap_limit = max(12.0, roi_w * (0.085 if relaxed else 0.065), float(cursor.get("w", 4)) * 4.5)

        row_candidates = []

        def add_row_candidate(row, x1, x2, source):
            width = int(x2 - x1 + 1)
            if width < min_full_w or width > max_full_w:
                return
            if x1 <= 1 and x2 >= roi_w - 2:
                return
            cx = x1 + width / 2.0
            center_score = 1.0 - min(1.0, abs(float(row) - cursor_cy) / max(1.0, roi_h * 0.28))
            width_score = min(1.0, width / max(1.0, roi_w * 0.18))
            color_quality = self._bar_color_quality(hsv, x1, row, width, 1)
            if color_quality < (0.44 if relaxed else 0.52):
                return
            score = color_quality * 0.46 + width_score * 0.32 + center_score * 0.22
            row_candidates.append({
                "row": int(row),
                "x1": int(x1),
                "x2": int(x2),
                "cx": float(cx),
                "w": int(width),
                "score": float(score),
                "source": source,
            })

        for row in range(y1, y2):
            xs = np.flatnonzero(target_like[row])
            if xs.size == 0:
                continue

            runs = []
            start = int(xs[0])
            prev = int(xs[0])
            for value in xs[1:]:
                value = int(value)
                if value == prev + 1:
                    prev = value
                    continue
                if prev - start + 1 >= min_piece_w:
                    runs.append((start, prev))
                start = prev = value
            if prev - start + 1 >= min_piece_w:
                runs.append((start, prev))

            if not runs:
                continue

            for run_x1, run_x2 in runs:
                run_w = run_x2 - run_x1 + 1
                cursor_inside = run_x1 + run_w * 0.10 <= cursor_cx <= run_x2 - run_w * 0.10
                if run_w >= min_full_w and (cursor_inside or run_w >= int(roi_w * 0.13)):
                    add_row_candidate(row, run_x1, run_x2, "row-single-color")

            for left_index, left in enumerate(runs):
                left_x1, left_x2 = left
                if left_x1 > cursor_cx:
                    continue
                for right in runs[left_index + 1:]:
                    right_x1, right_x2 = right
                    if right_x2 < cursor_cx:
                        continue
                    gap = right_x1 - left_x2 - 1
                    if gap < 0 or gap > gap_limit:
                        continue
                    x1 = min(left_x1, right_x1)
                    x2 = max(left_x2, right_x2)
                    if x1 - gap_limit * 0.25 <= cursor_cx <= x2 + gap_limit * 0.25:
                        add_row_candidate(row, x1, x2, "row-split-color")

        if not row_candidates:
            return None

        row_candidates.sort(key=lambda item: item["score"], reverse=True)
        min_support = 2 if relaxed else 3
        for base in row_candidates[:12]:
            support = []
            for item in row_candidates:
                if abs(item["row"] - base["row"]) > max(3, int(roi_h * 0.26)):
                    continue
                if abs(item["cx"] - base["cx"]) > max(5.0, base["w"] * 0.12):
                    continue
                if abs(item["w"] - base["w"]) > max(10, int(base["w"] * 0.22)):
                    continue
                overlap = min(base["x2"], item["x2"]) - max(base["x1"], item["x1"]) + 1
                if overlap / max(1, min(base["w"], item["w"])) < 0.62:
                    continue
                support.append(item)

            if len(support) < min_support:
                continue

            support = sorted(support, key=lambda item: item["row"])
            x1 = int(round(float(np.median([item["x1"] for item in support]))))
            x2 = int(round(float(np.median([item["x2"] for item in support]))))
            bar_y1 = support[0]["row"]
            bar_y2 = support[-1]["row"] + 1
            width = x2 - x1 + 1
            height = max(2, bar_y2 - bar_y1)
            color_quality = self._bar_color_quality(hsv, x1, bar_y1, width, height)
            if color_quality < (0.44 if relaxed else 0.52):
                continue

            score_mean = sum(item["score"] for item in support) / len(support)
            support_score = min(1.0, len(support) / max(3.0, roi_h * 0.32))
            confidence = color_quality * 0.42 + min(1.0, width / max(1.0, roi_w * 0.18)) * 0.24 + support_score * 0.22 + score_mean * 0.12
            return {
                "x": int(x1),
                "y": int(bar_y1),
                "w": int(width),
                "h": int(height),
                "area": int(width * height),
                "cx": float(x1 + width / 2.0),
                "cy": float(bar_y1 + height / 2.0),
                "confidence": max(0.0, min(0.92, confidence)),
                "score": max(0.0, min(1.08, confidence + support_score * 0.10)),
                "track_score": max(0.0, min(1.0, color_quality)),
                "source": base["source"],
            }

        return None

    def _select_split_green_bar_near_cursor(self, mask, cursor, roi_w, roi_h, hsv=None, relaxed=False):
        """在游标同一水平带内合并被黄色游标切开的左右耐力条段。"""
        if mask is None or cursor is None:
            return None

        run_candidate = self._select_horizontal_run_green_bar(mask, cursor, roi_w, roi_h, hsv=hsv, relaxed=relaxed)
        if run_candidate is not None:
            return run_candidate

        work_mask = mask
        if hsv is not None:
            hue = hsv[:, :, 0]
            sat = hsv[:, :, 1]
            val = hsv[:, :, 2]
            target_like = (
                ((hue >= 48) & (hue <= 102) & (sat >= 135) & (val >= 96))
                | ((hue >= 56) & (hue <= 96) & (sat >= 155) & (val >= 108))
            )
            strict_mask = np.zeros_like(mask)
            strict_mask[(mask > 0) & target_like] = 255
            if cv2.countNonZero(strict_mask) >= max(8, int(roi_w * roi_h * 0.00025)):
                close_w = max(5, int(roi_w * (0.010 if relaxed else 0.008)))
                work_mask = cv2.morphologyEx(strict_mask, cv2.MORPH_CLOSE, np.ones((3, close_w), np.uint8))
                work_mask = cv2.morphologyEx(work_mask, cv2.MORPH_OPEN, self._kernel_2x3)

        count, labels, stats, centroids = cv2.connectedComponentsWithStats(work_mask, 8)
        pieces = []
        cursor_cx = float(cursor.get("cx", 0.0))
        cursor_cy = float(cursor.get("cy", roi_h * 0.5))
        min_piece_w = max(8 if relaxed else 10, int(roi_w * (0.016 if relaxed else 0.020)))
        min_full_w = max(28 if relaxed else 36, int(roi_w * (0.085 if relaxed else 0.105)))
        max_full_w = max(min_full_w + 1, int(roi_w * (0.44 if relaxed else 0.38)))
        max_h = max(6, int(roi_h * (0.62 if relaxed else 0.54)))
        max_y_delta = max(4.5 if relaxed else 3.5, roi_h * (0.14 if relaxed else 0.105))

        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if w < min_piece_w or w > max_full_w or h < 2 or h > max_h:
                continue
            if area < max(6, int(roi_w * roi_h * 0.00022)):
                continue
            aspect = w / max(1, h)
            if aspect < (1.35 if relaxed else 1.55):
                continue
            fill_ratio = area / max(1, w * h)
            if fill_ratio < (0.08 if relaxed else 0.12):
                continue
            if x <= 1 and x + w >= roi_w - 1:
                continue

            cx, cy = centroids[index]
            y_delta = abs(float(cy) - cursor_cy)
            if y_delta > max_y_delta:
                continue

            color_quality = self._bar_color_quality(hsv, x, y, w, h)
            if color_quality < (0.42 if relaxed else 0.50):
                continue

            pieces.append({
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "area": int(area),
                "cx": float(cx),
                "cy": float(cy),
                "fill_ratio": float(fill_ratio),
                "color_quality": color_quality,
            })

        if not pieces:
            return None

        best = None

        def make_candidate(x1, y1, x2, y2, area, color_quality, fill_ratio, source):
            w = int(max(1, x2 - x1))
            h = int(max(1, y2 - y1))
            if w < min_full_w or w > max_full_w or h > max_h:
                return None
            cy = y1 + h / 2.0
            y_score = 1.0 - min(1.0, abs(cy - cursor_cy) / max(1.0, roi_h * 0.22))
            width_score = min(1.0, w / max(1.0, roi_w * 0.18))
            height_score = 1.0 - min(1.0, max(0.0, h - roi_h * 0.50) / max(1.0, roi_h * 0.22))
            confidence = color_quality * 0.34 + width_score * 0.24 + y_score * 0.22 + min(1.0, fill_ratio / 0.45) * 0.12 + height_score * 0.08
            return {
                "x": int(x1),
                "y": int(y1),
                "w": w,
                "h": h,
                "area": int(area),
                "cx": float(x1 + w / 2.0),
                "cy": float(cy),
                "confidence": max(0.0, min(0.90, confidence)),
                "score": max(0.0, min(1.05, confidence + width_score * 0.08 + color_quality * 0.08)),
                "track_score": max(0.0, min(1.0, color_quality)),
                "source": source,
            }

        gap_limit = max(18.0, roi_w * (0.10 if relaxed else 0.075), float(cursor.get("w", 4)) * 5.0)
        for left in pieces:
            left_x2 = left["x"] + left["w"]
            if left["cx"] > cursor_cx + gap_limit * 0.35:
                continue
            for right in pieces:
                if right is left or right["cx"] < cursor_cx - gap_limit * 0.35:
                    continue
                right_x1 = right["x"]
                if right_x1 < left["x"]:
                    continue
                gap = max(0.0, right_x1 - left_x2)
                if gap > gap_limit:
                    continue
                x1 = min(left["x"], right["x"])
                x2 = max(left_x2, right["x"] + right["w"])
                if not (x1 - gap_limit * 0.25 <= cursor_cx <= x2 + gap_limit * 0.25):
                    continue
                y1 = min(left["y"], right["y"])
                y2 = max(left["y"] + left["h"], right["y"] + right["h"])
                union_area = left["area"] + right["area"]
                union_fill = union_area / max(1, (x2 - x1) * (y2 - y1))
                color_quality = (left["color_quality"] * left["area"] + right["color_quality"] * right["area"]) / max(1, union_area)
                candidate = make_candidate(x1, y1, x2, y2, union_area, color_quality, union_fill, "split-color")
                if candidate is not None and (best is None or candidate["score"] > best["score"]):
                    best = candidate

        if best is not None:
            return best

        for piece in pieces:
            cursor_inside = piece["x"] + piece["w"] * 0.12 <= cursor_cx <= piece["x"] + piece["w"] * 0.88
            if piece["w"] < max(min_full_w, int(roi_w * (0.12 if relaxed else 0.14))) and not cursor_inside:
                continue
            candidate = make_candidate(
                piece["x"],
                piece["y"],
                piece["x"] + piece["w"],
                piece["y"] + piece["h"],
                piece["area"],
                piece["color_quality"],
                piece["fill_ratio"],
                "single-color",
            )
            if candidate is not None and (best is None or candidate["score"] > best["score"]):
                best = candidate

        return best

    def _select_green_bar_component(self, mask, roi_w, roi_h, band_y1, band_y2, cursor, hsv=None, relaxed=False):
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        best = None
        band_h = max(1, band_y2 - band_y1)
        min_width = max(8 if relaxed else 12, int(roi_w * (0.026 if relaxed else 0.035)))
        max_width = max(min_width + 1, int(roi_w * (0.42 if relaxed else 0.34)))
        min_area = max(7 if relaxed else 10, int(roi_w * roi_h * (0.00030 if relaxed else 0.00045)))
        max_height = max(5, int(min(roi_h * (0.30 if relaxed else 0.22), band_h * (0.62 if relaxed else 0.48))))

        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < min_area or w < min_width or w > max_width or h < 2 or h > max_height:
                continue
            aspect = w / max(h, 1)
            if aspect < (1.65 if relaxed else 2.2):
                continue
            fill_ratio = area / max(1, w * h)
            if fill_ratio < (0.10 if relaxed else 0.16):
                continue
            cx, cy = centroids[index]
            y_delta = abs(cy - cursor["cy"])
            if y_delta > max(4.0 if relaxed else 3.0, roi_h * (0.11 if relaxed else 0.08)):
                continue
            if w > roi_w * 0.95 and h > roi_h * 0.42:
                continue

            track_score = self._green_track_score(hsv, x, y, w, h, cursor=cursor)
            min_track_score = 0.22 if relaxed else 0.30
            if track_score < min_track_score:
                continue

            edge_touch_penalty = 0.0
            if x <= 1 or (x + w) >= roi_w - 1:
                edge_touch_penalty += 0.12
            if y <= band_y1 + 1 or (y + h) >= band_y2 - 1:
                edge_touch_penalty += 0.10

            aspect_score = min(1.0, aspect / 8.0)
            width_score = min(1.0, w / max(1.0, roi_w * 0.18))
            fill_score = min(1.0, fill_ratio / 0.55)
            y_score = 1.0 - min(1.0, y_delta / max(1.0, band_h * 0.65))
            height_score = 1.0 - min(1.0, abs(h - max(3.0, cursor["h"] * 0.32)) / max(3.0, cursor["h"]))
            confidence = aspect_score * 0.20 + width_score * 0.20 + fill_score * 0.16 + y_score * 0.20 + height_score * 0.06 + track_score * 0.18
            confidence = max(0.0, min(0.98, confidence - edge_touch_penalty))
            score = confidence + width_score * 0.08 + y_score * 0.08 + track_score * 0.12

            if best is None or score > best["score"]:
                best = {
                    "x": int(x),
                    "y": int(y),
                    "w": int(w),
                    "h": int(h),
                    "area": int(area),
                    "cx": float(cx),
                    "cy": float(cy),
                    "confidence": confidence,
                    "score": score,
                    "track_score": track_score,
                }
        return best

    def _select_green_candidate_near_cursor(self, candidates, cursor, roi_w, roi_h, hsv=None):
        best = None
        for candidate in candidates or []:
            y_delta = abs(float(candidate["cy"]) - float(cursor["cy"]))
            if y_delta > max(4.0, roi_h * 0.10):
                continue
            if candidate["w"] > roi_w * 0.42:
                continue
            if candidate["w"] > roi_w * 0.92 and candidate["h"] > roi_h * 0.38:
                continue
            track_score = self._green_track_score(
                hsv,
                candidate["x"],
                candidate["y"],
                candidate["w"],
                candidate["h"],
                cursor=cursor,
            )
            if track_score < 0.24:
                continue
            width_score = min(1.0, candidate["w"] / max(1.0, roi_w * 0.16))
            y_score = 1.0 - min(1.0, y_delta / max(1.0, roi_h * 0.38))
            base_conf = float(candidate.get("confidence", 0.0))
            score = base_conf * 0.42 + width_score * 0.22 + y_score * 0.18 + track_score * 0.18
            if best is None or score > best["score"]:
                best = dict(candidate)
                best["score"] = score
                best["track_score"] = track_score
                best["confidence"] = max(0.0, min(0.88, base_conf * 0.58 + y_score * 0.16 + width_score * 0.10 + track_score * 0.16))
        return best

    def _get_center_x(self, mask, is_vertical=False, strict_shape=True, return_width=False):
        """从二值化掩码中找到最大的合法轮廓，并返回中心X坐标 (以及可选的宽度)"""
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours: return None
        
        # 按照面积从大到小排序，只取最大的那个，防止被背景的小噪点干扰
        contours = sorted(contours, key=cv2.contourArea, reverse=True)
        
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            
            # 忽略过小的噪点
            if area < 5: 
                continue
                
            if strict_shape:
                # 宽容的形态学过滤：
                # 黄色游标 (is_vertical=True) 应该是竖着的，高大于宽，放宽要求
                if is_vertical and w > h * 1.8: 
                    continue
                    
                # 绿色目标条 (is_vertical=False) 应该是横着的，宽大于高
                if not is_vertical and h > w * 1.8:
                    continue
                
            if return_width:
                return x + w // 2, w
            return x + w // 2
            
        return None
