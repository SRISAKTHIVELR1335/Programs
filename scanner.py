# diagnostics/scanner.py
# -*- coding: utf-8 -*-
"""
NIRIX STATION SCANNER (BACKEND – OpenCV)

RESPONSIBILITY
──────────────
This module is used ONLY by the backend `/api/scan/*` endpoints to talk to a
fixed station camera (USB / built‑in) using OpenCV.

- Front-end scanner (phone/tablet/desktop browser) uses html5-qrcode in tests.html.
- Backend scanner is used mainly on PC stations where OpenCV has direct access
  to a local camera and you want scanning without browser camera permission.

PUBLIC API
──────────
- start_scan(kind: "text"|"vin"|"hex" = "text", timeout_sec: int|None = None) -> ScanSession
- get_scan(scan_id: str) -> Optional[ScanSession]
- cancel_scan(scan_id: str) -> bool
- cleanup_scans(max_age_sec: int = 300) -> int

OPTIONAL (PREVIEW SUPPORT)
────────────────────────
This version also stores the latest JPEG frame in memory (if enabled) so your
web app can implement a "live preview" endpoint if you want it:

- get_scan_frame_jpeg(scan_id: str) -> Optional[bytes]

NOTE: Your current Website_With_DB.py does not expose /api/scan/<id>/frame.
If you want live preview on PC using backend OpenCV, add that endpoint.

ScanSession.status:
  "running"   – scan in progress
  "found"     – value found
  "timeout"   – no code found within timeout
  "cancelled" – cancelled by user
  "error"     – internal error
  "busy"      – camera already in use

ENV
───
- NIRIX_SCAN_CAMERA_INDEX (default "0")
- NIRIX_SCAN_TIMEOUT_SEC (default "20")
- NIRIX_SCAN_PREVIEW_ENABLED ("true"/"false", default "true")
- NIRIX_SCAN_PREVIEW_WIDTH (default "640")      # resize preview frames
- NIRIX_SCAN_PREVIEW_QUALITY (default "75")     # JPEG quality 1..100
- NIRIX_SCAN_PREVIEW_MAX_FPS (default "8")      # limit preview encode frequency
- NIRIX_SCAN_CAP_BACKEND (optional; e.g. "DSHOW", "MSMF", "V4L2")
"""

from __future__ import annotations

import os
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

import cv2


# =============================================================================
# CONFIG
# =============================================================================

CAM_INDEX = int(os.getenv("NIRIX_SCAN_CAMERA_INDEX", "0"))
DEFAULT_TIMEOUT_SEC = int(os.getenv("NIRIX_SCAN_TIMEOUT_SEC", "20"))

PREVIEW_ENABLED = os.getenv("NIRIX_SCAN_PREVIEW_ENABLED", "true").lower() in ("1", "true", "yes")
PREVIEW_WIDTH = int(os.getenv("NIRIX_SCAN_PREVIEW_WIDTH", "640"))
PREVIEW_QUALITY = int(os.getenv("NIRIX_SCAN_PREVIEW_QUALITY", "75"))
PREVIEW_MAX_FPS = float(os.getenv("NIRIX_SCAN_PREVIEW_MAX_FPS", "8"))

# Enforce single camera access process-wide
_CAMERA_LOCK = threading.Lock()


def _cv_cap_backend() -> int:
    """
    Optional VideoCapture backend selection for Windows/Linux camera quirks.
    """
    name = (os.getenv("NIRIX_SCAN_CAP_BACKEND", "") or "").strip().upper()
    mapping = {
        "DSHOW": getattr(cv2, "CAP_DSHOW", 0),
        "MSMF": getattr(cv2, "CAP_MSMF", 0),
        "V4L2": getattr(cv2, "CAP_V4L2", 0),
        "ANY": getattr(cv2, "CAP_ANY", 0),
    }
    return mapping.get(name, 0)


# Prefer OpenCV barcode module if available (opencv-contrib-python)
_BARCODE_DETECTOR = None
try:
    # Depending on build, one of these exists
    if hasattr(cv2, "barcode_BarcodeDetector"):
        _BARCODE_DETECTOR = cv2.barcode_BarcodeDetector()  # type: ignore[attr-defined]
    elif hasattr(cv2, "barcode") and hasattr(cv2.barcode, "BarcodeDetector"):
        _BARCODE_DETECTOR = cv2.barcode.BarcodeDetector()  # type: ignore[attr-defined]
except Exception:
    _BARCODE_DETECTOR = None

_QR_DETECTOR = cv2.QRCodeDetector()


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class ScanSession:
    """In-memory scan session state."""
    scan_id: str
    status: str = "running"  # running|found|timeout|cancelled|error|busy
    value: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    # Optional preview support (latest JPEG frame)
    last_frame_jpeg: Optional[bytes] = None
    last_frame_at: Optional[float] = None

    # Internal lock for safe updates
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_status(self, status: str, *, error: Optional[str] = None):
        with self._lock:
            self.status = status
            if error is not None:
                self.error = error

    def set_value_found(self, value: str):
        with self._lock:
            self.value = value
            self.status = "found"

    def set_preview_frame(self, jpg: bytes):
        with self._lock:
            self.last_frame_jpeg = jpg
            self.last_frame_at = time.time()


_SCANS: Dict[str, ScanSession] = {}
_SCANS_LOCK = threading.Lock()


# =============================================================================
# HELPERS
# =============================================================================

def _postprocess(value: str, kind: str) -> str:
    """
    Post-process decoded text based on `kind`:
      - "vin": uppercase, strip spaces
      - "hex": uppercase, strip 0x prefix and spaces
      - default: strip
    """
    s = (value or "").strip()
    kind = (kind or "text").strip().lower()

    if kind == "vin":
        s = s.upper().replace(" ", "")
    elif kind == "hex":
        s = s.strip().upper()
        if s.startswith("0X"):
            s = s[2:]
        s = s.replace(" ", "")
    else:
        s = s.strip()
    return s


def _try_decode_barcode(frame) -> Optional[str]:
    """
    Decode using OpenCV barcode detector (if available).
    Handles minor API differences across builds.
    """
    if _BARCODE_DETECTOR is None:
        return None

    try:
        # Most builds return: ok, decoded_info, decoded_type, points
        out = _BARCODE_DETECTOR.detectAndDecode(frame)  # type: ignore
        if not isinstance(out, tuple) or len(out) < 2:
            return None

        ok = bool(out[0])
        decoded_info = out[1]

        if ok and decoded_info:
            # decoded_info is typically a list of strings
            if isinstance(decoded_info, (list, tuple)):
                for v in decoded_info:
                    if v:
                        return str(v)
            # some builds might return a single string
            if isinstance(decoded_info, str) and decoded_info:
                return decoded_info
    except Exception:
        return None

    return None


def _try_decode_qr(frame) -> Optional[str]:
    """Decode using OpenCV QRCodeDetector (always available)."""
    try:
        data, points, _ = _QR_DETECTOR.detectAndDecode(frame)
        if data:
            return str(data)
    except Exception:
        return None
    return None


def _decode_frame(frame) -> Optional[str]:
    """
    Decode a single frame using:
      1) OpenCV barcode detector (if available)
      2) OpenCV QRCodeDetector (always available)
    """
    v = _try_decode_barcode(frame)
    if v:
        return v
    return _try_decode_qr(frame)


def _encode_preview_jpeg(frame) -> Optional[bytes]:
    """
    Encode frame as JPEG for optional preview.
    Resizes for bandwidth/CPU control.
    """
    if frame is None:
        return None

    try:
        img = frame

        if PREVIEW_WIDTH and PREVIEW_WIDTH > 0:
            h, w = img.shape[:2]
            if w > PREVIEW_WIDTH:
                scale = PREVIEW_WIDTH / float(w)
                new_w = PREVIEW_WIDTH
                new_h = int(h * scale)
                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

        quality = max(10, min(95, int(PREVIEW_QUALITY)))
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return buf.tobytes()
    except Exception:
        return None


# =============================================================================
# PUBLIC API
# =============================================================================

def start_scan(kind: str = "text", timeout_sec: Optional[int] = None) -> ScanSession:
    """
    Start a new scan session.

    Args:
        kind: "text" | "vin" | "hex"
        timeout_sec: optional timeout override (seconds)

    Returns:
        ScanSession with a unique scan_id.
    """
    scan_id = f"scan_{uuid.uuid4().hex[:10]}"
    session = ScanSession(scan_id=scan_id)

    with _SCANS_LOCK:
        _SCANS[scan_id] = session

    try:
        tsec = int(timeout_sec) if timeout_sec is not None else DEFAULT_TIMEOUT_SEC
    except Exception:
        tsec = DEFAULT_TIMEOUT_SEC
    tsec = max(1, tsec)

    def worker():
        # Enforce single camera access process-wide
        if not _CAMERA_LOCK.acquire(blocking=False):
            session.set_status("busy", error="Camera is busy")
            return

        cap = None
        try:
            backend = _cv_cap_backend()
            if backend:
                cap = cv2.VideoCapture(CAM_INDEX, backend)
            else:
                cap = cv2.VideoCapture(CAM_INDEX)

            if not cap or not cap.isOpened():
                session.set_status("error", error=f"Cannot open camera index {CAM_INDEX}")
                return

            end = time.time() + tsec
            last_preview_emit = 0.0
            preview_min_interval = 1.0 / max(1.0, float(PREVIEW_MAX_FPS))

            while time.time() < end and not session.cancel_event.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue

                # Optional preview capture
                if PREVIEW_ENABLED:
                    now = time.time()
                    if (now - last_preview_emit) >= preview_min_interval:
                        jpg = _encode_preview_jpeg(frame)
                        if jpg:
                            session.set_preview_frame(jpg)
                        last_preview_emit = now

                # Decode
                val = _decode_frame(frame)
                if val:
                    session.set_value_found(_postprocess(val, kind))
                    return

                time.sleep(0.02)

            if session.cancel_event.is_set():
                session.set_status("cancelled")
            else:
                session.set_status("timeout")

        except Exception as e:
            session.set_status("error", error=str(e))

        finally:
            try:
                if cap is not None:
                    cap.release()
            finally:
                _CAMERA_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return session


def get_scan(scan_id: str) -> Optional[ScanSession]:
    """Get scan session state by ID."""
    with _SCANS_LOCK:
        return _SCANS.get(scan_id)


def get_scan_frame_jpeg(scan_id: str) -> Optional[bytes]:
    """
    Return latest preview JPEG bytes if available (PREVIEW_ENABLED must be true).
    Safe to call even if preview is disabled (returns None).
    """
    s = get_scan(scan_id)
    if not s:
        return None
    with s._lock:
        return s.last_frame_jpeg


def cancel_scan(scan_id: str) -> bool:
    """Request cancellation of a scan session."""
    s = get_scan(scan_id)
    if not s:
        return False
    s.cancel_event.set()
    return True


def cleanup_scans(max_age_sec: int = 300) -> int:
    """
    Remove old scan sessions from memory.
    """
    now = time.time()
    removed = 0
    with _SCANS_LOCK:
        to_del = [sid for sid, s in _SCANS.items() if (now - s.created_at) > max_age_sec]
        for sid in to_del:
            _SCANS.pop(sid, None)
            removed += 1
    return removed


__all__ = [
    "ScanSession",
    "start_scan",
    "get_scan",
    "get_scan_frame_jpeg",
    "cancel_scan",
    "cleanup_scans",
]