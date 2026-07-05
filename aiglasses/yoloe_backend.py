# yoloe_backend.py
# -*- coding: utf-8 -*-
import os
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import torch

from .paths import MODEL_DIR as PROJECT_MODEL_DIR

try:
    from ultralytics import YOLOE as _MODEL
except Exception:
    from ultralytics import YOLO as _MODEL

DEFAULT_MODEL_PATH = os.getenv(
    "YOLOE_MODEL_PATH",
    os.path.join(str(PROJECT_MODEL_DIR), "yoloe-26s-seg.pt"),
)
TRACKER_CFG = os.getenv("YOLO_TRACKER_YAML", "bytetrack.yaml")

_CACHE_LOCK = threading.RLock()
_MODEL_CACHE: Dict[Tuple[str, str], Any] = {}
_TEXT_PE_CACHE: Dict[Tuple[str, Tuple[str, ...]], Any] = {}


def _clean_names(names: List[str]) -> List[str]:
    clean_names = []
    seen = set()
    for name in names:
        value = str(name or "").strip()
        if value and value not in seen:
            seen.add(value)
            clean_names.append(value)
    return clean_names


def is_yoloe_text_cached(names: List[str], model_path: Optional[str] = None) -> bool:
    clean_names = _clean_names(names)
    if not clean_names:
        return True
    abs_model_path = os.path.abspath(model_path or DEFAULT_MODEL_PATH)
    with _CACHE_LOCK:
        return all((abs_model_path, (name,)) in _TEXT_PE_CACHE for name in clean_names)


class YoloEBackend:
    def __init__(self, model_path: Optional[str] = None, device: Optional[Union[str, int]] = None):
        self.model_path = model_path or DEFAULT_MODEL_PATH
        if device is not None:
            self.device = str(device)
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"

        self._model_key = (os.path.abspath(self.model_path), self.device)
        with _CACHE_LOCK:
            model = _MODEL_CACHE.get(self._model_key)
            if model is None:
                model = _MODEL(self.model_path)
                try:
                    model.to(self.device)
                except Exception:
                    if self.device != "cpu":
                        self.device = "cpu"
                        self._model_key = (os.path.abspath(self.model_path), self.device)
                        model.to("cpu")
                _MODEL_CACHE[self._model_key] = model
                print(f"[YOLOE] model loaded and cached: {self.model_path} on {self.device}", flush=True)
            else:
                print(f"[YOLOE] reuse cached model: {self.model_path} on {self.device}", flush=True)
            self.model = model

    def set_text_classes(self, names: List[str]):
        normalized = tuple(str(n).strip() for n in names if str(n).strip())
        if not normalized:
            return

        cache_key = (self._model_key[0], normalized)
        with _CACHE_LOCK:
            text_pe = _TEXT_PE_CACHE.get(cache_key)
            if text_pe is None:
                text_pe = self.model.get_text_pe(list(normalized))
                _TEXT_PE_CACHE[cache_key] = text_pe
                print(f"[YOLOE] cached text features: {list(normalized)}", flush=True)
            else:
                print(f"[YOLOE] reuse text features: {list(normalized)}", flush=True)
            self.model.set_classes(list(normalized), text_pe)

    def segment(
        self,
        frame_bgr: np.ndarray,
        conf: float = 0.20,
        iou: float = 0.45,
        imgsz: int = 640,
        persist: bool = True,
    ) -> Dict[str, Any]:
        with _CACHE_LOCK:
            r = self.model.track(
                frame_bgr,
                conf=conf,
                iou=iou,
                imgsz=imgsz,
                persist=persist,
                tracker=TRACKER_CFG,
                verbose=False,
            )[0]

        out = {"masks": [], "boxes": [], "cls_ids": [], "names": [], "ids": []}
        masks_obj = getattr(r, "masks", None)
        boxes_obj = getattr(r, "boxes", None)

        if masks_obj is None or getattr(masks_obj, "data", None) is None:
            return out

        mask_arr = masks_obj.data.cpu().numpy()
        H, W = frame_bgr.shape[:2]
        id2name = r.names if hasattr(r, "names") else {}
        N = mask_arr.shape[0]

        if boxes_obj is not None:
            xyxy = boxes_obj.xyxy.cpu().numpy()
            cls = boxes_obj.cls.cpu().tolist()
            tids = boxes_obj.id.int().cpu().tolist() if boxes_obj.id is not None else [None] * N
        else:
            xyxy = [None] * N
            cls = [0] * N
            tids = [None] * N

        for i in range(N):
            bin_mask = (mask_arr[i] > 0.5).astype(np.uint8)
            if bin_mask.shape[:2] != (H, W):
                bin_mask = cv2.resize(bin_mask, (W, H), interpolation=cv2.INTER_NEAREST)
            out["masks"].append(bin_mask)
            out["boxes"].append(tuple(xyxy[i]) if xyxy[i] is not None else None)
            cid = int(cls[i]) if cls is not None else 0
            out["cls_ids"].append(cid)
            if isinstance(id2name, dict):
                name = id2name.get(cid, str(cid))
            elif isinstance(id2name, (list, tuple)) and 0 <= cid < len(id2name):
                name = id2name[cid]
            else:
                name = str(cid)
            out["names"].append(name)
            out["ids"].append(int(tids[i]) if tids[i] is not None else None)
        return out


def prewarm_yoloe(names: List[str], model_path: Optional[str] = None, device: Optional[Union[str, int]] = None) -> None:
    clean_names = _clean_names(names)
    if not clean_names:
        return

    backend = YoloEBackend(model_path=model_path, device=device)
    for name in clean_names:
        backend.set_text_classes([name])
    print(f"[YOLOE] prewarmed {len(clean_names)} text prompts", flush=True)
