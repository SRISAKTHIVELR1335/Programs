# -*- coding: utf-8 -*-
"""
Auto-run: VIN Read (UDS DID F190)

program_id: AUTO_VIN_READ
module_name: vin_read
function_name: read_vin

Returns: {"vin": "<VIN>", "raw": {...}}
Raises RuntimeError on failure so UI can fallback to manual VIN input.
"""

from __future__ import annotations

import time
import logging
from typing import Dict, Any, Optional

import can

logging.getLogger("can").setLevel(logging.ERROR)

TESTER_ID = 0x7F0
ECU_RESPONSE_ID = 0x7F1


def _log_tx(msg: can.Message, context=None):
    data = " ".join(f"{b:02X}" for b in msg.data)
    line = f"Tx {msg.arbitration_id:03X} {msg.dlc} {data}"
    if context:
        context.log(line)
    else:
        print(line)


def _log_rx(msg: can.Message, context=None):
    if msg.arbitration_id == ECU_RESPONSE_ID:
        data = " ".join(f"{b:02X}" for b in msg.data)
        line = f"Rx {msg.arbitration_id:03X} {msg.dlc} {data}"
        if context:
            context.log(line)
        else:
            print(line)


def _open_bus(can_interface: str, bitrate: int) -> can.Bus:
    iface = can_interface.strip()
    if iface.upper().startswith("PCAN"):
        return can.Bus(interface="pcan", channel=iface, bitrate=bitrate)
    if iface.lower().startswith("can"):
        return can.Bus(interface="socketcan", channel=iface, bitrate=bitrate)
    raise ValueError(f"Unsupported CAN interface: {iface}")


def _send_can_frame(bus: can.Bus, arbitration_id: int, data: list, context=None):
    padded_data = data[:]
    while len(padded_data) < 8:
        padded_data.append(0x00)
    msg = can.Message(arbitration_id=arbitration_id, data=bytearray(padded_data[:8]), is_extended_id=False)
    _log_tx(msg, context)
    bus.send(msg)


def _receive_single_can_frame(bus: can.Bus, response_id: int, timeout: float = 0.5, context=None) -> Optional[can.Message]:
    start_time = time.time()
    while time.time() - start_time < timeout:
        msg = bus.recv(timeout=timeout - (time.time() - start_time))
        if msg and msg.arbitration_id == response_id:
            _log_rx(msg, context)
            return msg
    return None


def _receive_isotp_response(bus: can.Bus, response_id: int, timeout: float = 5.0, context=None) -> Optional[list]:
    start_time = time.time()
    full_response_data: list[int] = []
    expect_consecutive_frames = False
    seq_number_expected = 1
    total_uds_length = 0

    while time.time() - start_time < timeout:
        msg = _receive_single_can_frame(bus, response_id, timeout=0.5, context=context)
        if not msg:
            continue

        pci_type = (msg.data[0] & 0xF0) >> 4

        if pci_type == 0x0:  # Single Frame
            uds_length = msg.data[0] & 0x0F
            full_response_data = list(msg.data[1:1 + uds_length])
            break

        elif pci_type == 0x1:  # First Frame
            total_uds_length = ((msg.data[0] & 0x0F) << 8) + msg.data[1]
            full_response_data = list(msg.data[2:])
            expect_consecutive_frames = True
            seq_number_expected = 1

            # Flow Control (CTS)
            fc_frame = [0x30, 0x00, 0x00] + [0x00] * 5
            time.sleep(0.01)
            _send_can_frame(bus, TESTER_ID, fc_frame, context)
            continue

        elif expect_consecutive_frames and pci_type == 0x2:  # Consecutive Frame
            seq_number = msg.data[0] & 0x0F
            if seq_number != seq_number_expected:
                return None

            full_response_data.extend(msg.data[1:])
            seq_number_expected = (seq_number_expected + 1) % 16

            if len(full_response_data) >= total_uds_length:
                full_response_data = full_response_data[:total_uds_length]
                break

    return full_response_data or None


def _send_isotp_request(bus: can.Bus, arbitration_id: int, data: list, context=None) -> bool:
    total_len = len(data)
    if total_len <= 7:
        sf = [total_len] + data + [0x00] * (7 - total_len)
        _send_can_frame(bus, arbitration_id, sf, context)
        return True

    ff_data = [0x10 | ((total_len >> 8) & 0x0F), total_len & 0xFF] + data[:6]
    _send_can_frame(bus, arbitration_id, ff_data, context)

    fc_msg = _receive_single_can_frame(bus, ECU_RESPONSE_ID, timeout=1.0, context=context)
    if not fc_msg or ((fc_msg.data[0] & 0xF0) >> 4) != 0x3:
        return False

    remaining_data = data[6:]
    seq = 1
    while remaining_data:
        cf_data_len = min(7, len(remaining_data))
        cf = [0x20 | (seq % 16)] + remaining_data[:cf_data_len]
        _send_can_frame(bus, arbitration_id, cf, context)
        remaining_data = remaining_data[cf_data_len:]
        seq += 1
        time.sleep(0.01)

    return True


def _send_uds_request(bus: can.Bus, sid: int, sub_payload: list, expected_positive_sid: int,
                      timeout: float = 5.0, context=None) -> Optional[list]:
    uds_payload = [sid] + sub_payload

    if len(uds_payload) <= 7:
        sf = [len(uds_payload)] + uds_payload + [0x00] * (7 - len(uds_payload))
        _send_can_frame(bus, TESTER_ID, sf, context)
    else:
        if not _send_isotp_request(bus, TESTER_ID, uds_payload, context):
            return None

    response = _receive_isotp_response(bus, ECU_RESPONSE_ID, timeout, context)
    if response and response[0] == expected_positive_sid:
        return response
    return None


def _extended_diagnostic_session(bus: can.Bus, context=None) -> bool:
    response = _send_uds_request(bus, 0x10, [0x03], 0x50, timeout=2.0, context=context)
    return bool(response and len(response) >= 2 and response[1] == 0x03)


def read_vin(
    can_interface: str,
    bitrate: int,
    context=None,
    progress=None,
    timeout: float = 5.0,
    **_,   # <-- important
) -> Dict[str, Any]:
    bus = None
    raw: Dict[str, Any] = {}

    def _log_line(msg: str):
        if context:
            context.log(msg)
        else:
            print(msg)

    try:
        if progress:
            progress(5, f"Opening CAN ({can_interface}, {bitrate})")
        bus = _open_bus(can_interface, bitrate)
        raw["bus"] = {"interface": can_interface, "bitrate": bitrate}

        if context:
            context.checkpoint()
            context.log("Starting extended diagnostic session (0x10 03)")

        if not _extended_diagnostic_session(bus, context=context):
            raise RuntimeError("Extended diagnostic session failed")

        if progress:
            progress(40, "Requesting VIN (22 F1 90)")
        if context:
            context.log("Sending UDS request 22 F1 90")

        response = _send_uds_request(
            bus,
            sid=0x22,
            sub_payload=[0xF1, 0x90],
            expected_positive_sid=0x62,
            timeout=timeout,
            context=context,
        )

        raw["uds_response"] = response

        if response and len(response) >= 3 and response[1] == 0xF1 and response[2] == 0x90:
            vin_bytes = response[3:]
            vin_str = "".join(chr(b) if 32 <= b <= 126 else "?" for b in vin_bytes).strip().upper()

            if context:
                context.log(f"VIN read: {vin_str}")
                context.checkpoint()
            if progress:
                progress(100, f"VIN: {vin_str}")

            return {"vin": vin_str, "raw": raw}

        raise RuntimeError("No valid VIN (F190) response from ECU")

    except can.CanError as e:
        _log_line(f"CAN error: {e}")
        raise RuntimeError(f"CAN error: {e}") from e
    except Exception as e:
        _log_line(f"Error: {e}")
        raise
    finally:
        if bus:
            try:
                bus.shutdown()
                _log_line("CAN bus shutdown.")
            except Exception:
                pass