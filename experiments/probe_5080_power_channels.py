"""Read-only RM power-device/channel metadata on the measured R580 ABI."""
import ctypes as C
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n


def capture_transport(gpu):
    gdi = gpu._legacy_clk_gdi()
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.VirtualProtect.argtypes = [C.c_void_p, C.c_size_t, n.u32, C.POINTER(n.u32)]
    kernel.GetCurrentProcess.restype = C.c_void_p
    kernel.FlushInstructionCache.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
    process = kernel.GetCurrentProcess()
    address = C.cast(gdi.D3DKMTEscape, C.c_void_p).value
    original = C.string_at(address, 14)
    proto = C.WINFUNCTYPE(C.c_long, C.c_void_p)
    found = []

    def restore():
        C.memmove(address, original, 14)
        kernel.FlushInstructionCache(process, address, 14)

    def callback(ptr):
        restore()
        escape = n.GPU._Escape.from_address(ptr)
        if escape.pPrivateDriverData and escape.PrivateDriverDataSize >= 68:
            data = C.cast(escape.pPrivateDriverData, C.POINTER(n.u32))
            if data[14] == 0x2080a612 and data[15] == 11680:
                found.append((list(data[:17]), {k: getattr(escape, k) for k in
                             ("hAdapter", "hDevice", "Type", "Flags", "hContext")}))
        result = proto(address)(ptr)
        C.memmove(address, patch, 14)
        kernel.FlushInstructionCache(process, address, 14)
        return result

    keep = proto(callback)
    patch = b"\xff\x25\0\0\0\0" + struct.pack("<Q", C.cast(keep, C.c_void_p).value)
    old = n.u32()
    if not kernel.VirtualProtect(address, 14, 0x40, C.byref(old)):
        raise C.WinError(C.get_last_error())
    try:
        C.memmove(address, patch, 14)
        kernel.FlushInstructionCache(process, address, 14)
        block = n._PwrPolInfo(version=gpu.nvapi.ver(n._PwrPolInfo, 1))
        rc = gpu.nvapi.PowerPolInfo(gpu.nvapi.gpu, C.byref(block))
    finally:
        restore()
        unused = n.u32()
        kernel.VirtualProtect(address, 14, old.value, C.byref(unused))
    assert rc == 0 and len(found) == 1
    return found[0]


def read(gpu, transport, command, size, values=()):
    # An explicit whitelist excludes every setter. Parameter sizes come from
    # the installed driver's own RM dispatcher and captured getter requests.
    assert (command, size) in {(0x2080a610, 0x7b90), (0x2080a612, 11680),
                               (0x2080a613, 16916), (0x2080a61a, 4432)}
    header, fields = transport
    packet = (n.u32 * (17 + size // 4))()
    packet[:17] = header
    packet[2], packet[14], packet[15], packet[16] = C.sizeof(packet), command, size, 0
    packet[17:17 + len(values)] = values
    status = gpu._legacy_clk_escape(packet, fields)
    assert status == 0 and packet[16] == 0, (hex(command), status, packet[16])
    return list(packet[17:])


if __name__ == "__main__":
    gpu = n.GPU("0000:01:00.0")
    assert (gpu.static["name"], gpu.static["driver"]) == ("NVIDIA GeForce RTX 5080", "580.97")
    transport = capture_transport(gpu)
    info = read(gpu, transport, 0x2080a612, 11680)
    devices = read(gpu, transport, 0x2080a610, 0x7b90)
    status = read(gpu, transport, 0x2080a613, 16916, (0, info[2]))
    control = read(gpu, transport, 0x2080a61a, 4432, (0, 0, 0, 0, 0x3ffff))
    result = {"identity": gpu.static, "read_only": True, "devices_info": devices,
              "channels_info": info, "channels_status": status,
              "policy_control": control}
    path = ROOT / "experiments/power-5080-20260908/channels.json"
    path.write_text(json.dumps(result, indent=2))
    print("device info header:", devices[:4], "channel mask:", hex(info[2]))
    for index in range(15):
        if not info[2] & (1 << index):
            continue
        start = (0x1c + index * 0x13c) // 4
        meta = info[start:start + 0x13c // 4]
        start = (0xc + index * 0x204) // 4
        values = status[start:start + 0x204 // 4]
        print("channel", index, "metadata", meta[:10], "status", values[:16])
    for index in range(32):
        if not devices[2] & (1 << index):
            continue
        start = (0x10 + index * 0x3dc) // 4
        print("device", index, "prefix", devices[start:start + 24])
