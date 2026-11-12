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
    # YOLOE text prompts + MobileCLIP are unstable in FP16 on this stack.
    # Keep FP32 by default; set AIGLASS_FP16=1 only if you explicitly want it.
    return str(device).startswith("cuda") and os.getenv("AIGLASS_FP16", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _regular_tensor(
    value: torch.Tensor,
    *,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> torch.Tensor:
    """Copy any tensor, including inference tensors, into a normal cacheable tensor."""
    target_device = device if device is not None else value.device
    target_dtype = dtype if dtype is not None else value.dtype
    array = np.array(value.detach().to(dtype=target_dtype, device="cpu").numpy(), copy=True)
    with torch.inference_mode(False):
        return torch.from_numpy(array).to(device=target_device, dtype=target_dtype)


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
    with _CACHE_LOCK:
        return all(
            (abs_model_path, resolved_device, "fp32", (name,)) in _TEXT_PE_CACHE
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

    def _restore_fp32_weights(self) -> None:
        """CLIP/MobileCLIP text encoding must run in FP32, even after FP16 tracking."""
        network = self._network()
        if network is not None:
            try:
                network.float()
            except Exception:
                pass
        # AutoBackend may still wrap the same module in FP16; rebuild on next track().
        self.model.predictor = None

    def _encode_text_pe(self, names: Tuple[str, ...]) -> torch.Tensor:
        self._restore_fp32_weights()
        try:
            text_pe = self.model.get_text_pe(list(names))
        except RuntimeError as exc:
            message = str(exc)
            if "same dtype" not in message and "Half" not in message:
                raise
            self._restore_fp32_weights()
            text_pe = self.model.get_text_pe(list(names))
        return _regular_tensor(text_pe, dtype=torch.float32)

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
        aligned = text_pe
        if (
            torch.is_inference(aligned)
            or aligned.device != target_device
            or aligned.dtype != target_dtype
        ):
            aligned = _regular_tensor(text_pe, device=target_device, dtype=target_dtype)
        # Keep self._text_pe as the canonical FP32 copy; only the model sees the aligned tensor.
        if self._classes:
            self.model.set_classes(list(self._classes), aligned)
        if network is not None:
            network.pe = aligned

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
        self._restore_fp32_weights()
        if torch.is_tensor(self._text_pe):
            self._text_pe = _regular_tensor(self._text_pe, dtype=torch.float32)
        self._align_text_features()

    def set_text_classes(self, names: List[str]):
        normalized = tuple(str(n).strip() for n in names if str(n).strip())
        if not normalized:
            return

        # Always cache FP32 encodings. Tracking may later convert a copy to FP16.
        cache_key = (self._model_key[0], self.device, "fp32", normalized)
        with _CACHE_LOCK:
            text_pe = _TEXT_PE_CACHE.get(cache_key)
            if text_pe is None:
                text_pe = self._encode_text_pe(normalized)
                _TEXT_PE_CACHE[cache_key] = text_pe
                print(f"[YOLOE] cached text features: {list(normalized)}", flush=True)
            else:
                if torch.is_inference(text_pe) or text_pe.dtype != torch.float32:
                    text_pe = _regular_tensor(text_pe, dtype=torch.float32)
                    _TEXT_PE_CACHE[cache_key] = text_pe
                print(f"[YOLOE] reuse text features: {list(normalized)}", flush=True)
            self._classes = normalized
            self._text_pe = text_pe
            self._align_text_features()

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
            except Exception as exc:
                message = str(exc)
                can_retry = self.half or any(
                    token in message
                    for token in ("Inference tensors", "same dtype", "Half", "version counter")
                )
                if not can_retry:
                    raise
                self._switch_to_fp32()
                kwargs["half"] = False
                print(f"[YOLOE] 推理失败，已切换到 FP32 并重试: {message}", flush=True)
                try:
                    with torch.inference_mode():
                        r = self.model.track(frame_bgr, **kwargs)[0]
                except Exception as retry_exc:
                    print(f"[YOLOE] FP32 重试失败，本帧跳过: {retry_exc}", flush=True)
                    return {"masks": [], "boxes": [], "cls_ids": [], "names": [], "ids": []}

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
