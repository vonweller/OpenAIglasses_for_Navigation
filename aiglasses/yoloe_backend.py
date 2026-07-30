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
_TEXT_PE_CACHE: Dict[Tuple[str, str, str, Tuple[str, ...]], Any] = {}


def _fp16_enabled(device: str) -> bool:
    return str(device).startswith("cuda") and os.getenv("AIGLASS_FP16", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _regular_tensor(
    value: torch.Tensor,
    *,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> torch.Tensor:
    """Copy an inference tensor into a normal tensor that is safe to cache."""
    with torch.inference_mode(False):
        result = value.detach().clone()
        if device is not None or dtype is not None:
            result = result.to(
                device=device if device is not None else result.device,
                dtype=dtype if dtype is not None else result.dtype,
            )
    return result


def _clean_names(names: List[str]) -> List[str]:
    clean_names = []
    seen = set()
    for name in names:
        value = str(name or "").strip()
        if value and value not in seen:
            seen.add(value)
            clean_names.append(value)
    return clean_names


def is_yoloe_text_cached(
    names: List[str],
    model_path: Optional[str] = None,
    device: Optional[Union[str, int]] = None,
) -> bool:
    clean_names = _clean_names(names)
    if not clean_names:
        return True
    abs_model_path = os.path.abspath(model_path or DEFAULT_MODEL_PATH)
    resolved_device = str(device) if device is not None else ("cuda" if torch.cuda.is_available() else "cpu")
    precision = "fp16" if _fp16_enabled(resolved_device) else "fp32"
    with _CACHE_LOCK:
        return all(
            (abs_model_path, resolved_device, precision, (name,)) in _TEXT_PE_CACHE
            for name in clean_names
        )


class YoloEBackend:
    def __init__(self, model_path: Optional[str] = None, device: Optional[Union[str, int]] = None):
        self.model_path = model_path or DEFAULT_MODEL_PATH
        if device is not None:
            self.device = str(device)
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        self.half = _fp16_enabled(self.device)
        self._classes: Tuple[str, ...] = ()
        self._text_pe: Optional[torch.Tensor] = None

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
                        self.half = False
                        self._model_key = (os.path.abspath(self.model_path), self.device)
                        model.to("cpu")
                _MODEL_CACHE[self._model_key] = model
                print(f"[YOLOE] model loaded and cached: {self.model_path} on {self.device}", flush=True)
            else:
                print(f"[YOLOE] reuse cached model: {self.model_path} on {self.device}", flush=True)
            self.model = model

    def _network(self):
        return getattr(self.model, "model", None)

    def _target_dtype(self) -> torch.dtype:
        return torch.float16 if self.half and self.device.startswith("cuda") else torch.float32

    def _align_text_features(self) -> None:
        """Keep YOLOE's plain-tensor text prompt aligned with inference precision."""
        network = self._network()
        text_pe = self._text_pe
        if not torch.is_tensor(text_pe):
            return
        try:
            parameter = next(network.parameters())
            target_device = parameter.device
        except Exception:
            target_device = text_pe.device
        target_dtype = self._target_dtype()
        if (
            torch.is_inference(text_pe)
            or text_pe.device != target_device
            or text_pe.dtype != target_dtype
        ):
            text_pe = _regular_tensor(text_pe, device=target_device, dtype=target_dtype)
            self._text_pe = text_pe
        if self._classes:
            self.model.set_classes(list(self._classes), text_pe)
        network.pe = text_pe

    def _ensure_predictor_precision(self) -> None:
        predictor = getattr(self.model, "predictor", None)
        auto_backend = getattr(predictor, "model", None)
        if auto_backend is None:
            return
        if bool(getattr(auto_backend, "fp16", False)) != bool(self.half):
            self.model.predictor = None

    def _switch_to_fp32(self) -> None:
        """Reset the Ultralytics predictor after an FP16 failure and retry safely."""
        self.half = False
        network = self._network()
        if network is not None:
            text_pe = self._text_pe
            if torch.is_tensor(text_pe):
                try:
                    parameter = next(network.parameters())
                    text_pe = _regular_tensor(
                        text_pe,
                        device=parameter.device,
                        dtype=torch.float32,
                    )
                except Exception:
                    text_pe = _regular_tensor(text_pe, dtype=torch.float32)
                self._text_pe = text_pe
                network.pe = text_pe
        # AutoBackend stores its own fp16 flag, so rebuilding is required.
        self.model.predictor = None

    def set_text_classes(self, names: List[str]):
        normalized = tuple(str(n).strip() for n in names if str(n).strip())
        if not normalized:
            return

        precision = "fp16" if self.half else "fp32"
        cache_key = (self._model_key[0], self.device, precision, normalized)
        with _CACHE_LOCK:
            text_pe = _TEXT_PE_CACHE.get(cache_key)
            if text_pe is None:
                text_pe = self.model.get_text_pe(list(normalized))
                network = self._network()
                try:
                    parameter = next(network.parameters())
                    text_pe = _regular_tensor(
                        text_pe,
                        device=parameter.device,
                        dtype=self._target_dtype(),
                    )
                except Exception:
                    text_pe = _regular_tensor(text_pe, dtype=self._target_dtype())
                _TEXT_PE_CACHE[cache_key] = text_pe
                print(f"[YOLOE] cached text features: {list(normalized)}", flush=True)
            else:
                if torch.is_inference(text_pe):
                    text_pe = _regular_tensor(text_pe)
                    _TEXT_PE_CACHE[cache_key] = text_pe
                print(f"[YOLOE] reuse text features: {list(normalized)}", flush=True)
            self.model.set_classes(list(normalized), text_pe)
            # Ultralytics skips assignment when class names are unchanged. The
            # embedding is not a parameter/buffer, so assign it explicitly.
            network = self._network()
            if network is not None:
                network.pe = text_pe
            self._classes = normalized
            self._text_pe = text_pe

    def segment(
        self,
        frame_bgr: np.ndarray,
        conf: float = 0.20,
        iou: float = 0.45,
        imgsz: int = 640,
        persist: bool = True,
    ) -> Dict[str, Any]:
        with _CACHE_LOCK:
            self._ensure_predictor_precision()
            self._align_text_features()
            kwargs = dict(
                conf=conf,
                iou=iou,
                imgsz=imgsz,
                persist=persist,
                tracker=TRACKER_CFG,
                verbose=False,
                device=self.device,
                half=self.half,
            )
            try:
                with torch.inference_mode():
                    r = self.model.track(frame_bgr, **kwargs)[0]
            except Exception:
                if not self.half:
                    raise
                self._switch_to_fp32()
                kwargs["half"] = False
                print("[YOLOE] FP16 推理失败，已自动切换到 FP32 并重试", flush=True)
                with torch.inference_mode():
                    r = self.model.track(frame_bgr, **kwargs)[0]

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
