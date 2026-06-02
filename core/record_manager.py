import json
import os
import random
import re
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from core.paths import ensure_writable_file, resource_path

_RECORD_FILE_LOCK = threading.RLock()
ENCYCLOPEDIA_RESOURCE_DIR = "异环鱼类图鉴资源"
ENCYCLOPEDIA_RESOURCE_DIR_ASCII = "fish_encyclopedia"
OCR_CONFUSABLE_CHARS = str.maketrans({
    "賽": "紫",
    "赛": "紫",
    "慈": "斑",
    "班": "斑",
    "部": "斑",
    "幔": "鳗",
    "鳄": "鲷",
    "勰": "鲷",
    "魚": "鱼",
    "魯": "鱼",
    "鲁": "鱼",
    "食": "鱼",
    # --- 繁简转换 ---
    "鱗": "鳞",
    "鯨": "鲸",
    "燈": "灯",
    "條": "条",
    "鰻": "鳗",
    "鰭": "鳍",
    "鯛": "鲷",
    "繡": "绣",
    "鰠": "鳋",
    "鳳": "凤",
})


class RecordManager:
    DEFAULT_STATS = {
        "total_caught": 0,
        "total_time_seconds": 0,
        "total_attempts": 0,
        "consecutive_empty": 0,
    }
    DEFAULT_SUMMARY = {
        "last_record_id": 0,
        "last_history_len": 0,
        "last_time": "",
    }

    def __init__(self, record_file=None, encyclopedia_dir=None):
        self.record_file = record_file or ensure_writable_file("records.json")
        self.encyclopedia_dir = encyclopedia_dir or self._default_encyclopedia_dir()
        self._query_cache = {}
        self._cache_version = 0
        self.records = {
            "stats": dict(self.DEFAULT_STATS),
            "encyclopedia": {},
            "history": [],
            "summary": dict(self.DEFAULT_SUMMARY),
            "next_record_id": 1,
        }
        self._load_failed = False
        self._migration_needed = False
        self.load_records()
        if not self._load_failed:
            self._sync_encyclopedia_images()
            self._ensure_thumbnail_cache()
            if self._migration_needed:
                self.save_records()

    @staticmethod
    def _default_encyclopedia_dir():
        primary = resource_path(ENCYCLOPEDIA_RESOURCE_DIR)
        if os.path.exists(primary):
            return primary
        return resource_path(ENCYCLOPEDIA_RESOURCE_DIR_ASCII)

    def _touch_cache(self):
        self._cache_version += 1
        self._query_cache.clear()

    def load_records(self):
        if not os.path.exists(self.record_file):
            return

        data = None
        for attempt in range(3):
            try:
                with _RECORD_FILE_LOCK:
                    with open(self.record_file, "r", encoding="utf-8") as file:
                        data = json.load(file)
                break
            except json.JSONDecodeError as exc:
                if attempt == 2:
                    self._load_failed = True
                    print(f"Failed to load records: {exc}")
                    return
                time.sleep(0.08)
            except Exception as exc:
                self._load_failed = True
                print(f"Failed to load records: {exc}")
                return

        self.records["stats"].update(data.get("stats", {}))
        self.records["history"] = data.get("history", [])
        self.records["encyclopedia"] = data.get("encyclopedia", {})
        raw_summary = data.get("summary", {})
        summary = dict(self.DEFAULT_SUMMARY)
        summary.update(raw_summary)
        if isinstance(raw_summary, dict) and "last_record_id" not in raw_summary:
            summary.pop("last_record_id", None)
        self.records["summary"] = summary
        self.records["next_record_id"] = data.get("next_record_id", 1)
        self._migrate_record_ids()
        self._touch_cache()

    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _migrate_record_ids(self):
        history = self.records.get("history", [])
        if not isinstance(history, list):
            self.records["history"] = []
            history = self.records["history"]
            self._migration_needed = True

        used_ids = set()
        next_candidate = 1
        for record in history:
            if not isinstance(record, dict):
                continue
            record_id = self._safe_int(record.get("record_id"), 0)
            if record_id <= 0 or record_id in used_ids:
                while next_candidate in used_ids:
                    next_candidate += 1
                record["record_id"] = next_candidate
                used_ids.add(next_candidate)
                next_candidate += 1
                self._migration_needed = True
            else:
                record["record_id"] = record_id
                used_ids.add(record_id)

        max_record_id = max(used_ids, default=0)
        next_record_id = self._safe_int(self.records.get("next_record_id"), 1)
        normalized_next_id = max(next_record_id, max_record_id + 1, 1)
        if self.records.get("next_record_id") != normalized_next_id:
            self.records["next_record_id"] = normalized_next_id
            self._migration_needed = True

        summary = self.records.setdefault("summary", dict(self.DEFAULT_SUMMARY))
        last_record_id = self._safe_int(summary.get("last_record_id"), -1)
        if last_record_id < 0:
            last_record_id = 0

        if "last_record_id" not in summary:
            cursor = self._safe_int(summary.get("last_history_len"), 0)
            if 0 < cursor <= len(history):
                candidate = history[cursor - 1]
                if isinstance(candidate, dict):
                    last_record_id = self._safe_int(candidate.get("record_id"), 0)
            elif cursor <= 0:
                last_record_id = 0
            else:
                last_time = summary.get("last_time", "")
                if last_time:
                    matched_ids = [
                        self._safe_int(record.get("record_id"), 0)
                        for record in history
                        if isinstance(record, dict) and record.get("time", "") <= last_time
                    ]
                    last_record_id = max(matched_ids, default=max_record_id)
                else:
                    last_record_id = max_record_id
            self._migration_needed = True

        normalized_summary = {
            "last_record_id": max(0, last_record_id),
            "last_history_len": self._safe_int(summary.get("last_history_len"), 0),
            "last_time": summary.get("last_time", ""),
        }
        if summary != normalized_summary:
            self.records["summary"] = normalized_summary
            self._migration_needed = True

    def _next_record_id(self):
        record_id = max(1, self._safe_int(self.records.get("next_record_id"), 1))
        self.records["next_record_id"] = record_id + 1
        return record_id

    def save_records(self):
        try:
            record_dir = os.path.dirname(os.path.abspath(self.record_file)) or "."
            with _RECORD_FILE_LOCK:
                fd, temp_path = tempfile.mkstemp(prefix=".records.", suffix=".json", dir=record_dir, text=True)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as file:
                        json.dump(self.records, file, ensure_ascii=False, indent=4)
                    os.replace(temp_path, self.record_file)
                except Exception:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    raise
            try:
                from core.tracker import EventTracker
                EventTracker.get().sync_records({
                    "stats": dict(self.records.get("stats", {})),
                    "encyclopedia": dict(self.records.get("encyclopedia", {})),
                    "history": list(self.records.get("history", [])),
                })
            except Exception:
                pass
        except Exception as exc:
            print(f"Failed to save records: {exc}")

    def _decode_mojibake(self, text):
        if not isinstance(text, str) or not text:
            return text

        candidates = [text]
        for codec in ("gbk", "gb18030", "utf-8"):
            try:
                repaired = text.encode(codec, errors="ignore").decode("utf-8", errors="ignore").strip()
                if repaired and repaired not in candidates:
                    candidates.append(repaired)
            except Exception:
                continue
        return candidates

    def _canonical_name_candidates(self, name, image_path=""):
        candidates = set()
        if name:
            for item in self._decode_mojibake(name):
                if item:
                    candidates.add(item)
        if image_path:
            basename = os.path.splitext(os.path.basename(image_path))[0]
            for item in self._decode_mojibake(basename):
                if item:
                    candidates.add(item)
        return candidates

    def _normalize_name_text(self, text):
        text = (text or "").strip()
        if not text:
            return ""
        text = re.sub(r"\s+", "", text)
        text = text.replace("·", "").replace("•", "")
        text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
        text = text.translate(OCR_CONFUSABLE_CHARS)
        return text

    def get_fish_name_alphabet(self):
        chars = set()
        for name, data in self.records.get("encyclopedia", {}).items():
            for candidate in self._canonical_name_candidates(name, data.get("image_path", "")):
                chars.update(self._normalize_name_text(candidate))
        return "".join(sorted(chars))

    def _levenshtein_distance(self, left, right):
        if left == right:
            return 0
        if not left:
            return len(right)
        if not right:
            return len(left)

        previous = list(range(len(right) + 1))
        for i, left_char in enumerate(left, start=1):
            current = [i]
            for j, right_char in enumerate(right, start=1):
                cost = 0 if left_char == right_char else 1
                current.append(min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                ))
            previous = current
        return previous[-1]

    def _lcs_length(self, left, right):
        if not left or not right:
            return 0
        previous = [0] * (len(right) + 1)
        for left_char in left:
            current = [0]
            for j, right_char in enumerate(right, start=1):
                if left_char == right_char:
                    current.append(previous[j - 1] + 1)
                else:
                    current.append(max(previous[j], current[j - 1]))
            previous = current
        return previous[-1]

    def _fish_match_threshold(self, normalized):
        if len(normalized) <= 2:
            return 0.98
        if len(normalized) == 3:
            return 0.78
        return 0.68

    def rank_fish_name(self, raw_name, ocr_score=0.0, loose=False):
        normalized = self._normalize_name_text(raw_name)
        if not normalized:
            return "", 0.0, 0.0

        encyclopedia = self.records.get("encyclopedia", {})
        best_name = raw_name.strip()
        best_score = 0.0
        second_score = 0.0
        ocr_score = max(0.0, min(float(ocr_score or 0.0), 1.0))

        for name, data in encyclopedia.items():
            candidates = self._canonical_name_candidates(name, data.get("image_path", ""))
            for candidate in candidates:
                candidate_norm = self._normalize_name_text(candidate)
                if not candidate_norm:
                    continue

                if normalized == candidate_norm:
                    score = 1.12 + ocr_score * 0.08
                else:
                    max_len = max(len(normalized), len(candidate_norm))
                    edit_distance = self._levenshtein_distance(normalized, candidate_norm)
                    edit_score = 1.0 - (edit_distance / max_len)
                    lcs_score = self._lcs_length(normalized, candidate_norm) / max_len
                    lexical_score = SequenceMatcher(None, normalized, candidate_norm).ratio()
                    score = edit_score * 0.50 + lcs_score * 0.28 + lexical_score * 0.14 + ocr_score * 0.08

                    if len(normalized) >= 3 and (normalized in candidate_norm or candidate_norm in normalized):
                        score = max(score, 0.90 - abs(len(normalized) - len(candidate_norm)) * 0.025)

                    if loose and 2 <= len(normalized) == len(candidate_norm) <= 6:
                        diff_count = sum(1 for left, right in zip(normalized, candidate_norm) if left != right)
                        if diff_count == 1 and (len(normalized) > 2 or normalized[0] == candidate_norm[0]):
                            score = max(score, 0.86 + ocr_score * 0.04)

                if score > best_score:
                    second_score = best_score
                    best_score = score
                    best_name = name
                elif score > second_score:
                    second_score = score

        return best_name, best_score, second_score

    def resolve_fish_name(self, raw_name, loose=False):
        normalized = self._normalize_name_text(raw_name)
        best_name, best_score, second_score = self.rank_fish_name(raw_name, 1.0 if loose else 0.5, loose)
        if not best_name:
            return ""
        threshold = self._fish_match_threshold(normalized)
        if best_score >= 1.0 or (best_score >= threshold and best_score - second_score >= 0.035):
            return best_name
        return raw_name.strip()

    def resolve_fish_name_candidates(self, candidates):
        grouped = {}

        for raw_text, ocr_score in candidates:
            normalized = self._normalize_name_text(raw_text)
            if not normalized:
                continue
            name, score, local_second = self.rank_fish_name(raw_text, ocr_score, loose=True)
            if not name:
                continue
            if score < 1.0 and score - local_second < 0.035:
                continue
            current = grouped.setdefault(name, {"score": 0.0, "raw": raw_text, "norm": normalized, "hits": 0})
            current["hits"] += 1
            if score > current["score"]:
                current["score"] = score
                current["raw"] = raw_text
                current["norm"] = normalized

        if not grouped:
            return "", 0.0, ""

        ranked = []
        for name, data in grouped.items():
            score = data["score"] + min(0.06, max(0, data["hits"] - 1) * 0.012)
            ranked.append((score, name, data["raw"], data["norm"]))
        ranked.sort(reverse=True)

        best_score, best_name, best_raw, best_norm = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        threshold = self._fish_match_threshold(best_norm)
        if best_score >= 1.0 or (best_score >= threshold and best_score - second_score >= 0.035):
            return best_name, best_score, best_raw
        return "", best_score, best_raw

    def _scan_resource_catalog(self):
        catalog = {}
        if not os.path.isdir(self.encyclopedia_dir):
            return catalog

        for rarity_dir in os.listdir(self.encyclopedia_dir):
            rarity_path = os.path.join(self.encyclopedia_dir, rarity_dir)
            if not os.path.isdir(rarity_path):
                continue
            for filename in os.listdir(rarity_path):
                if not filename.lower().endswith(".png"):
                    continue
                fish_name = os.path.splitext(filename)[0]
                catalog[fish_name] = {
                    "caught_count": 0,
                    "max_weight": 0,
                    "rarity": rarity_dir,
                    "image_path": os.path.join(rarity_path, filename),
                    "first_caught_at": "",
                    "last_caught_at": "",
                }
        return catalog

    def _sync_encyclopedia_images(self):
        catalog = self._scan_resource_catalog()
        if not catalog:
            return

        old_encyclopedia = self.records.get("encyclopedia", {})
        remapped = {}

        for fish_name, base_data in catalog.items():
            merged = dict(base_data)
            for old_name, old_data in old_encyclopedia.items():
                candidates = self._canonical_name_candidates(old_name, old_data.get("image_path", ""))
                if fish_name in candidates:
                    merged["caught_count"] = max(0, int(old_data.get("caught_count", 0)))
                    merged["max_weight"] = max(0, int(old_data.get("max_weight", 0)))
                    merged["first_caught_at"] = old_data.get("first_caught_at", "")
                    merged["last_caught_at"] = old_data.get("last_caught_at", "")
                    break
            remapped[fish_name] = merged

        for old_name, old_data in old_encyclopedia.items():
            if any(old_name == name or old_name in self._canonical_name_candidates(name, info.get("image_path", "")) for name, info in remapped.items()):
                continue
            repaired_names = [candidate for candidate in self._canonical_name_candidates(old_name, old_data.get("image_path", "")) if candidate not in remapped]
            fallback_name = repaired_names[0] if repaired_names else old_name
            remapped[fallback_name] = {
                "caught_count": max(0, int(old_data.get("caught_count", 0))),
                "max_weight": max(0, int(old_data.get("max_weight", 0))),
                "rarity": old_data.get("rarity", "未知稀有度"),
                "image_path": old_data.get("image_path", ""),
                "first_caught_at": old_data.get("first_caught_at", ""),
                "last_caught_at": old_data.get("last_caught_at", ""),
            }

        repaired_history = []
        for record in self.records.get("history", []):
            fixed = dict(record)
            name_candidates = self._canonical_name_candidates(record.get("fish_name", ""), record.get("image_path", ""))
            matched_name = next((name for name in remapped if name in name_candidates), None)
            if matched_name:
                fixed["fish_name"] = matched_name
                fixed["rarity"] = remapped[matched_name]["rarity"]
                fixed["image_path"] = remapped[matched_name]["image_path"]
            repaired_history.append(fixed)

        if self.records.get("encyclopedia", {}) == remapped and self.records.get("history", []) == repaired_history:
            return

        self.records["encyclopedia"] = remapped
        self.records["history"] = repaired_history
        self._touch_cache()
        self.save_records()

    def _ensure_thumbnail_cache(self):
        """为缺失 .cache 缩略图的图鉴资源自动生成 160px 缩略图。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            return
        import logging
        _log = logging.getLogger(__name__).debug
        cache_size = 160
        for fish_name, data in self.records.get("encyclopedia", {}).items():
            image_path = data.get("image_path", "")
            if not image_path or not os.path.exists(image_path):
                continue
            rarity_dir = os.path.dirname(image_path)
            cache_dir = os.path.join(rarity_dir, ".cache")
            cache_file = os.path.join(cache_dir, f"{fish_name}_{cache_size}.png")
            if os.path.exists(cache_file):
                continue
            try:
                # cv2.imread 不支持 Windows 中文路径，用 imdecode 替代
                with open(image_path, "rb") as f:
                    buf = np.frombuffer(f.read(), dtype=np.uint8)
                img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
                if img is None:
                    continue
                h, w = img.shape[:2]
                if max(h, w) <= 0:
                    continue
                scale = cache_size / max(h, w)
                new_w, new_h = int(w * scale), int(h * scale)
                resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                os.makedirs(cache_dir, exist_ok=True)
                # cv2.imwrite 同样不支持中文路径
                ok, encoded = cv2.imencode(".png", resized)
                if ok:
                    with open(cache_file, "wb") as f:
                        f.write(encoded.tobytes())
            except Exception as exc:
                _log("缩略图生成失败 [%s]: %s", fish_name, exc)

    def generate_sample_records(self):
        encyclopedia = {}
        for name, data in self._scan_resource_catalog().items():
            encyclopedia[name] = {
                "caught_count": 0,
                "max_weight": 0,
                "rarity": data.get("rarity", "未知稀有度"),
                "image_path": data.get("image_path", ""),
                "first_caught_at": "",
                "last_caught_at": "",
            }

        fish_names = list(encyclopedia.keys())
        history = []
        randomizer = random.Random(20260424)
        rarity_weight = {
            "绿色稀有度": (25, 280),
            "蓝色稀有度": (40, 420),
            "紫色稀有度": (60, 560),
            "金色稀有度": (80, 760),
            "废品": (5, 60),
            "未知稀有度": (15, 160),
        }

        selected = []
        for rarity in ["绿色稀有度", "蓝色稀有度", "紫色稀有度", "金色稀有度", "废品"]:
            same_rarity = [name for name, data in encyclopedia.items() if data.get("rarity") == rarity]
            randomizer.shuffle(same_rarity)
            selected.extend(same_rarity[: min(len(same_rarity), 8 if rarity != "废品" else 2)])

        selected = selected[:34] if len(selected) > 34 else selected

        for index in range(132):
            fish_name = selected[index % len(selected)]
            fish_data = encyclopedia[fish_name]
            rarity = fish_data["rarity"]
            weight_range = rarity_weight.get(rarity, (15, 160))
            weight = randomizer.randint(*weight_range)

            day = 1 + (index % 18)
            hour = 8 + (index * 3) % 12
            minute = (index * 7) % 60
            timestamp = f"2026-04-{day:02d} {hour:02d}:{minute:02d}:00"

            fish_data["caught_count"] += 1
            fish_data["max_weight"] = max(fish_data["max_weight"], weight)
            if not fish_data["first_caught_at"]:
                fish_data["first_caught_at"] = timestamp
            fish_data["last_caught_at"] = timestamp

            history.append(
                {
                    "time": timestamp,
                    "fish_name": fish_name,
                    "weight": weight,
                    "rarity": rarity,
                    "image_path": fish_data["image_path"],
                }
            )

        stats = {
            "total_caught": len(history),
            "total_time_seconds": 6 * 3600 + 42 * 60,
            "total_attempts": len(history) + 19,
            "consecutive_empty": 2,
        }

        return {
            "stats": stats,
            "encyclopedia": encyclopedia,
            "history": history,
        }

    def add_empty_catch(self):
        self.records["stats"]["total_attempts"] += 1
        self.records["stats"]["consecutive_empty"] += 1
        self._touch_cache()
        self.save_records()

    def add_catch(self, fish_name, weight_g, rarity=None):
        self.records["stats"]["total_caught"] += 1
        self.records["stats"]["total_attempts"] += 1
        self.records["stats"]["consecutive_empty"] = 0

        canonical_name = fish_name
        if fish_name not in self.records["encyclopedia"]:
            for name in self.records["encyclopedia"]:
                if fish_name in self._canonical_name_candidates(name, self.records["encyclopedia"][name].get("image_path", "")):
                    canonical_name = name
                    break

        is_unknown_entry = canonical_name in {"", "未知鱼类", "未识别鱼类"}
        if is_unknown_entry:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.records["history"].append(
                {
                    "record_id": self._next_record_id(),
                    "time": timestamp,
                    "fish_name": canonical_name or "未知鱼类",
                    "weight": int(weight_g or 0),
                    "rarity": rarity or "未知稀有度",
                    "image_path": "",
                }
            )
            self._touch_cache()
            self.save_records()
            return

        if canonical_name not in self.records["encyclopedia"]:
            self.records["encyclopedia"][canonical_name] = {
                "caught_count": 0,
                "max_weight": 0,
                "rarity": rarity or "未知稀有度",
                "image_path": "",
                "first_caught_at": "",
                "last_caught_at": "",
            }

        fish_data = self.records["encyclopedia"][canonical_name]
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        fish_data["caught_count"] += 1
        fish_data["max_weight"] = max(int(weight_g or 0), int(fish_data.get("max_weight", 0)))
        if not fish_data.get("first_caught_at"):
            fish_data["first_caught_at"] = timestamp
        fish_data["last_caught_at"] = timestamp

        self.records["history"].append(
            {
                "record_id": self._next_record_id(),
                "time": timestamp,
                "fish_name": canonical_name,
                "weight": int(weight_g or 0),
                "rarity": fish_data.get("rarity", rarity or "未知稀有度"),
                "image_path": fish_data.get("image_path", ""),
            }
        )

        self._touch_cache()
        self.save_records()

    def add_catch_count_only(self):
        """极简模式：仅增加统计数据，不记录历史详情。"""
        self.records["stats"]["total_caught"] += 1
        self.records["stats"]["total_attempts"] += 1
        self.records["stats"]["consecutive_empty"] = 0
        self._touch_cache()
        self.save_records()

    def add_runtime(self, duration_seconds):
        self.records["stats"]["total_time_seconds"] += max(0, int(duration_seconds))
        self._touch_cache()
        self.save_records()

    def get_stats(self):
        return dict(self.records["stats"])

    def get_history(self):
        return list(self.records["history"])

    def get_unsummarized_history(self):
        history = self.records.get("history", [])
        summary = self.records.setdefault("summary", dict(self.DEFAULT_SUMMARY))
        last_record_id = self._safe_int(summary.get("last_record_id"), 0)

        if last_record_id >= 0:
            return [
                record for record in history
                if self._safe_int(record.get("record_id"), 0) > last_record_id
            ]

        last_time = summary.get("last_time", "")
        if last_time:
            return [record for record in history if record.get("time", "") > last_time]
        return list(history)

    def mark_summary_completed(self):
        history = self.records.get("history", [])
        last_record_id = max((self._safe_int(record.get("record_id"), 0) for record in history), default=0)
        self.records["summary"] = {
            "last_record_id": last_record_id,
            "last_history_len": len(history),
            "last_time": history[-1].get("time", "") if history else "",
        }
        self._touch_cache()
        self.save_records()

    def get_encyclopedia(self):
        return dict(self.records["encyclopedia"])

    def get_all_fishes_by_rarity(self):
        grouped = defaultdict(dict)
        for name, data in self.records["encyclopedia"].items():
            grouped[data.get("rarity", "未知稀有度")][name] = data
        return dict(grouped)

    def query_history(self, keyword="", rarity="全部稀有度", period="全部时间", weight_bucket="全部重量"):
        keyword = (keyword or "").strip().lower()
        period = period or "全部时间"
        weight_bucket = weight_bucket or "全部重量"
        cache_key = (self._cache_version, keyword, rarity, period, weight_bucket)
        if cache_key in self._query_cache:
            return list(self._query_cache[cache_key])

        now = datetime.now()
        start_date = None
        if period == "今日":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "最近24小时":
            start_date = now - timedelta(hours=24)
        elif period == "最近7天":
            start_date = now - timedelta(days=7)
        elif period == "最近30天":
            start_date = now - timedelta(days=30)

        results = []
        for record in self.records["history"]:
            if keyword and keyword not in record.get("fish_name", "").lower():
                continue
            if rarity and rarity != "全部稀有度" and record.get("rarity") != rarity:
                continue
            if start_date is not None:
                try:
                    record_time = datetime.strptime(record.get("time", "")[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if record_time < start_date:
                    continue
            weight = int(record.get("weight", 0) or 0)
            if weight_bucket == "小于100g" and weight >= 100:
                continue
            if weight_bucket == "100-999g" and not (100 <= weight <= 999):
                continue
            if weight_bucket == "1000g以上" and weight < 1000:
                continue
            if weight_bucket == "1000-9999g" and not (1000 <= weight <= 9999):
                continue
            if weight_bucket == "10000g以上" and weight < 10000:
                continue
            results.append(record)
        self._query_cache[cache_key] = list(results)
        return list(results)

    def get_rarity_distribution(self, history=None):
        source = history if history is not None else self.records["history"]
        distribution = defaultdict(int)
        for record in source:
            distribution[record.get("rarity", "未知稀有度")] += 1
        return dict(distribution)

    def get_daily_trend(self, days=7):
        points = defaultdict(int)
        for record in self.records["history"]:
            day = record.get("time", "")[:10]
            if day:
                points[day] += 1
        days = max(1, int(days))
        ordered_days = sorted(points.keys())[-days:]
        return [(day, points[day]) for day in ordered_days]

    def get_summary(self):
        encyclopedia = self.records["encyclopedia"]
        history = self.records["history"]
        stats = self.records["stats"]

        unlocked_count = sum(1 for data in encyclopedia.values() if data.get("caught_count", 0) > 0)
        total_species = len(encyclopedia)
        max_weight = max((int(data.get("max_weight", 0)) for data in encyclopedia.values()), default=0)
        rarest_count = self.get_rarity_distribution(history).get("金色稀有度", 0)
        success_rate = 0.0
        if stats.get("total_attempts", 0) > 0:
            success_rate = stats.get("total_caught", 0) / stats["total_attempts"] * 100

        return {
            "total_species": total_species,
            "unlocked_species": unlocked_count,
            "max_weight": max_weight,
            "gold_caught": rarest_count,
            "success_rate": success_rate,
        }
