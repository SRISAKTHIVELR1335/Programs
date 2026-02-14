# -*- coding: utf-8 -*-
"""
AUTO_ECU_ACTIVE_CHECK (single)

- Returns ecus_ok (1/0) so runner output_limits can fail the program
- Returns ecu_statuses list so service can persist to app.ecu_active_status
- Does NOT raise RuntimeError just because ECU is inactive (so output is preserved)
"""

from __future__ import annotations

import time
import logging
from typing import Dict, Any, Optional, List

import can

logging.getLogger("can").setLevel(logging.ERROR)

ECU_ADDRS: Dict[str, Dict[str, int]] = {
    "BMS": {"req": 0x7F0, "res": 0x7F1},
}

DEFAULT_ECUS: List[str] = ["BMS"]

try:
    from diagnostics.runner import DiagnosticNegativeResponse  # type: ignore
except Exception:
    class DiagnosticNegativeResponse(Exception):  # type: ignore
        def __init__(self, service_id: int, nrc: int, message: str = ""):
            super().__init__(f"7F {service_id:02X} {nrc:02X}: {message}")
            self.service_id = service_id
            self.nrc = nrc
            self.message = message


def _log(context, msg: str, level: str = "INFO"):
    if context is not None:
        try:
            context.log(msg, level)
            return
        except Exception:
            pass
    print(f"[{level}] {msg}")


def _open_bus(can_interface: str, bitrate: int) -> can.Bus:
    iface = (can_interface or "").strip()
    if iface.upper().startswith("PCAN"):
        return can.Bus(interface="pcan", channel=iface, bitrate=int(bitrate), fd=False)
    if iface.lower().startswith("can"):
        return can.Bus(interface="socketcan", channel=iface, bitrate=int(bitrate))
    raise ValueError(f"Unsupported CAN interface: {iface}")


def _send_tester_present(bus: can.Bus, req_id: int, context=None):
    data = bytearray([0x02, 0x3E, 0x00, 0, 0, 0, 0, 0])
    msg = can.Message(arbitration_id=req_id, data=data, is_extended_id=False)
    _log(context, f"TX {req_id:03X} " + " ".join(f"{b:02X}" for b in msg.data))
    bus.send(msg)


def _recv_sf_payload(bus: can.Bus, res_id: int, timeout_sec: float, context=None) -> Optional[bytes]:
    end = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
    while True:
        if context:
            context.checkpoint()

        remaining = end - time.monotonic()
        if remaining <= 0:
            return None

        msg = bus.recv(timeout=min(0.1, remaining))
        if msg is None:
            continue
        if msg.arbitration_id != res_id:
            continue
        if len(msg.data) < 2:
            continue

        _log(context, f"RX {res_id:03X} " + " ".join(f"{b:02X}" for b in msg.data))

        pci_type = (msg.data[0] & 0xF0) >> 4
        if pci_type != 0x0:
            continue

        ln = int(msg.data[0] & 0x0F)
        if ln <= 0:
            return b""
        return bytes(msg.data[1:1 + min(ln, len(msg.data) - 1)])


def check_all_ecus(
    can_interface: str,
    bitrate: int,
    *,
    ecus: Optional[List[str]] = None,
    ecu_addrs: Optional[Dict[str, Dict[str, int]]] = None,
    per_ecu_timeout_sec: float = 1.0,
    context=None,
    progress=None,
    **_,
) -> Dict[str, Any]:
    bus = None
    details: Dict[str, bool] = {}
    ecu_statuses: List[Dict[str, Any]] = []

    ecu_list = list(ecus) if ecus else list(DEFAULT_ECUS)
    addr_map = dict(ECU_ADDRS)
    if ecu_addrs:
        addr_map.update(ecu_addrs)

    def _progress(p: int, m: str = ""):
        if progress:
            try: progress(int(p), str(m))
            except Exception: pass
        if context:
            try: context.progress(int(p), str(m))
            except Exception: pass

    try:
        _progress(5, f"Opening CAN ({can_interface}@{bitrate})")
        bus = _open_bus(can_interface, int(bitrate))

        total = max(1, len(ecu_list))
        for idx, ecu_code in enumerate(ecu_list, start=1):
            addr = addr_map.get(ecu_code)
            if not addr:
                details[ecu_code] = False
                continue

            req_id = int(addr["req"])
            res_id = int(addr["res"])

            _progress(int(10 + (idx * 80) / total), f"ECU {ecu_code}: TesterPresent")

            _send_tester_present(bus, req_id, context=context)
            payload = _recv_sf_payload(bus, res_id, float(per_ecu_timeout_sec), context=context)

            if payload is None:
                details[ecu_code] = False
                continue

            # 7F 3E NRC
            if len(payload) >= 3 and payload[0] == 0x7F and payload[1] == 0x3E:
                raise DiagnosticNegativeResponse(0x3E, int(payload[2]), "TesterPresent NRC")

            details[ecu_code] = bool(len(payload) >= 1 and payload[0] == 0x7E)

        for ecu_code, ok in details.items():
            ecu_statuses.append({
                "ecu_code": ecu_code,
                "is_active": bool(ok),
                "error_count": 0 if ok else 1,
                "last_response": None,   # DB column is timestamp; keep None
            })

        ecus_ok = 1 if (details and all(details.values())) else 0
        _progress(100, "ECU Active Check completed")

        return {"ecus_ok": int(ecus_ok), "ecu_statuses": ecu_statuses, "details": details}

    finally:
        if bus:
            try: bus.shutdown()
            except Exception: pass