# -*- coding: utf-8 -*-
"""
CAN UTILITIES (FINAL – PRODUCTION READY, APP.SCHEMA SAFE)

Responsibilities:
✔ Resolve CAN configuration from DB (app.config)
✔ Open / close CAN bus safely
✔ Provide reusable CAN helpers for diagnostic services & test programs
✔ No test-specific logic (VIN/DTC/etc.)
✔ Compatible with python-can
✔ PostgreSQL UPSERT safe

Expected DB table:
  app.config(key_name TEXT UNIQUE, value_text TEXT)

Expected keys:
  - can_backend   : PCAN | SOCKETCAN   (optional; default PCAN)
  - can_interface : PCAN_USBBUS1 | can0 (optional; default PCAN_USBBUS1)
  - can_bitrate   : 500000             (optional; default 500000)
"""

from __future__ import annotations

from typing import Optional, Dict, Any

from can import interface, Bus  # python-can
from database import query_one, execute


# =============================================================================
# CONFIG HELPERS (POSTGRESQL SAFE)
# =============================================================================

def get_config_value(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read a configuration value from DB (app.config).
    """
    try:
        row = query_one(
            "SELECT value_text FROM app.config WHERE key_name = :k",
            {"k": key},
        )
        if row and row.get("value_text") is not None:
            return row["value_text"]
        return default
    except Exception:
        # If DB not available or table missing, fall back
        return default


def set_config_value(key: str, value: str) -> None:
    """
    Insert or update config value (PostgreSQL UPSERT).
    Requires UNIQUE constraint on app.config.key_name.
    """
    try:
        execute(
            """
            INSERT INTO app.config (key_name, value_text)
            VALUES (:k, :v)
            ON CONFLICT (key_name)
            DO UPDATE SET value_text = EXCLUDED.value_text
            """,
            {"k": key, "v": str(value)},
        )
    except Exception:
        # silently ignore (caller may run without DB during tests)
        pass


# =============================================================================
# CAN CONFIG RESOLUTION
# =============================================================================

def get_can_config() -> Dict[str, Any]:
    """
    Resolve CAN configuration from DB.

    Returns:
      {
        "backend": "pcan" | "socketcan",
        "channel": "PCAN_USBBUS1" | "can0",
        "bitrate": 500000
      }
    """
    backend_raw = (get_config_value("can_backend", "PCAN") or "").strip().upper()

    interface_name = (get_config_value("can_interface", "") or "").strip()
    bitrate_text = (get_config_value("can_bitrate", "500000") or "500000").strip()

    # Defaults if not configured
    if not interface_name:
        # keep backward compatibility: if vci_mode is present, infer
        vci_mode = (get_config_value("vci_mode", "") or "").strip().lower()
        if vci_mode == "socketcan":
            interface_name = "can0"
            backend_raw = "SOCKETCAN"
        else:
            interface_name = "PCAN_USBBUS1"
            backend_raw = backend_raw or "PCAN"

    try:
        bitrate = int(bitrate_text)
    except Exception:
        bitrate = 500000

    if backend_raw == "SOCKETCAN":
        return {"backend": "socketcan", "channel": interface_name, "bitrate": bitrate}

    # Default: PCAN
    return {"backend": "pcan", "channel": interface_name, "bitrate": bitrate}


# =============================================================================
# CAN BUS MANAGEMENT
# =============================================================================

def open_can_bus(
    *,
    channel: Optional[str] = None,
    bitrate: Optional[int] = None,
    backend: Optional[str] = None,
) -> Bus:
    """
    Open CAN bus safely.

    If parameters are omitted, values are resolved from DB (app.config).
    """
    cfg = get_can_config()

    channel = (channel or cfg["channel"] or "").strip()
    bitrate = int(bitrate or cfg["bitrate"] or 500000)
    backend = (backend or cfg["backend"] or "pcan").strip().lower()

    if not channel:
        raise ValueError("CAN channel is required")

    # python-can: interface.Bus(channel=..., bustype=..., bitrate=...)
    if backend == "pcan":
        return interface.Bus(
            channel=channel,
            bustype="pcan",
            bitrate=bitrate,
        )

    if backend == "socketcan":
        return interface.Bus(
            channel=channel,
            bustype="socketcan",
            bitrate=bitrate,
        )

    # Add more backends if needed
    raise ValueError(f"Unsupported CAN backend: {backend}")


def close_can_bus(bus: Optional[Bus]) -> None:
    """
    Safely shutdown CAN bus.
    """
    try:
        if bus:
            bus.shutdown()
    except Exception:
        pass


# =============================================================================
# GENERIC DIAGNOSTIC HELPERS
# =============================================================================

def send_can_frame(
    bus: Bus,
    arbitration_id: int,
    data: bytes,
    *,
    is_extended_id: bool = False,
) -> None:
    """
    Send one CAN frame.
    """
    from can import Message

    msg = Message(
        arbitration_id=arbitration_id,
        data=data,
        is_extended_id=is_extended_id,
    )
    bus.send(msg)


def recv_can_frame(bus: Bus, timeout: float = 1.0):
    """
    Receive one CAN frame.
    """
    return bus.recv(timeout)


# =============================================================================
# CONTEXT MANAGER (RECOMMENDED USAGE)
# =============================================================================

class CanSession:
    """
    Context manager for CAN session handling.

    Usage:
        with CanSession() as bus:
            ...
    """

    def __init__(
        self,
        *,
        channel: Optional[str] = None,
        bitrate: Optional[int] = None,
        backend: Optional[str] = None,
    ):
        self._params = {"channel": channel, "bitrate": bitrate, "backend": backend}
        self.bus: Optional[Bus] = None

    def __enter__(self) -> Bus:
        self.bus = open_can_bus(**self._params)
        return self.bus

    def __exit__(self, exc_type, exc, tb) -> None:
        close_can_bus(self.bus)
        self.bus = None