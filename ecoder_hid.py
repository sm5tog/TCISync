#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecoder_hid.py - delad, fristaende HID-atkomst for Expert Electronics E-Coder2.

Oppnar E-Codern (default VID_1FC9 / PID_0003) direkt via Windows HID API
(hid.dll + setupapi.dll) med ctypes - inget pip-beroende, funkar pa Python 3.14.

Anvands av bade ecoder_sniff.py och ecoder_mapper.py. read(timeout_ms) ar
icke-blockerande med timeout sa en trad kan polla med en stoppflagga.
"""

import ctypes
from ctypes import wintypes

# --- konstanter ---
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
HIDP_STATUS_SUCCESS = 0x00110000

ULONG_PTR = ctypes.c_size_t


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ULONG_PTR)]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", ctypes.c_wchar * 512)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wintypes.DWORD), ("VendorID", ctypes.c_ushort),
                ("ProductID", ctypes.c_ushort), ("VersionNumber", ctypes.c_ushort)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort),
                ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort * 17),
                ("NumberLinkCollectionNodes", ctypes.c_ushort),
                ("NumberInputButtonCaps", ctypes.c_ushort),
                ("NumberInputValueCaps", ctypes.c_ushort),
                ("NumberInputDataIndices", ctypes.c_ushort),
                ("NumberOutputButtonCaps", ctypes.c_ushort),
                ("NumberOutputValueCaps", ctypes.c_ushort),
                ("NumberOutputDataIndices", ctypes.c_ushort),
                ("NumberFeatureButtonCaps", ctypes.c_ushort),
                ("NumberFeatureValueCaps", ctypes.c_ushort),
                ("NumberFeatureDataIndices", ctypes.c_ushort)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ULONG_PTR), ("InternalHigh", ULONG_PTR),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

hid.HidD_GetHidGuid.restype = None
hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                          wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                                 ctypes.POINTER(GUID), wintypes.DWORD,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                  wintypes.LPCWSTR]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.CancelIo.restype = wintypes.BOOL
kernel32.CancelIo.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

hid.HidD_GetAttributes.restype = ctypes.c_ubyte
hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.restype = ctypes.c_ubyte
hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidP_GetCaps.restype = ctypes.c_long
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_FreePreparsedData.restype = ctypes.c_ubyte
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_GetProductString.restype = ctypes.c_ubyte
hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]


def find_device_paths(vid, pid):
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE_VALUE or hdev is None:
        raise OSError("SetupDiGetClassDevsW misslyckades")
    paths = []
    try:
        ifdata = SP_DEVICE_INTERFACE_DATA()
        ifdata.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        idx = 0
        while setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid),
                                                   idx, ctypes.byref(ifdata)):
            idx += 1
            detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
            detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(ifdata), ctypes.byref(detail),
                    ctypes.sizeof(detail), None, None):
                continue
            path = detail.DevicePath
            h = kernel32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                     None, OPEN_EXISTING, 0, None)
            if h == INVALID_HANDLE_VALUE or h is None:
                continue
            try:
                attr = HIDD_ATTRIBUTES()
                attr.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
                if hid.HidD_GetAttributes(h, ctypes.byref(attr)):
                    if attr.VendorID == vid and attr.ProductID == pid:
                        paths.append(path)
            finally:
                kernel32.CloseHandle(h)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


class EcoderHid:
    def __init__(self, vid=0x1FC9, pid=0x0003):
        self.vid = vid
        self.pid = pid
        self.handle = None
        self.hevent = None
        self.length = 0
        self.output_length = 0
        self.usage = 0
        self.usage_page = 0
        self.product = ""
        self._ov = None
        self._buf = None
        self._nread = None
        self._pending = False

    def open(self):
        """GENERIC_READ|GENERIC_WRITE (inte bara READ) - skrivrätten behövs
        för write() (output-rapporter, t.ex. LED-styrning, se write())."""
        paths = find_device_paths(self.vid, self.pid)
        if not paths:
            raise OSError("E-Coder hittades inte (VID_%04X/PID_%04X). Inkopplad?"
                          % (self.vid, self.pid))
        path = paths[0]
        self.handle = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                           FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                           OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.handle == INVALID_HANDLE_VALUE or self.handle is None:
            raise OSError("Kunde inte oppna E-Codern (fel %d)" % ctypes.get_last_error())
        pp = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(self.handle, ctypes.byref(pp)):
            try:
                caps = HIDP_CAPS()
                if hid.HidP_GetCaps(pp, ctypes.byref(caps)) == HIDP_STATUS_SUCCESS:
                    self.length = caps.InputReportByteLength
                    self.output_length = caps.OutputReportByteLength
                    self.usage = caps.Usage
                    self.usage_page = caps.UsagePage
            finally:
                hid.HidD_FreePreparsedData(pp)
        if not self.length:
            self.length = 64
        prod = ctypes.create_unicode_buffer(128)
        if hid.HidD_GetProductString(self.handle, prod, ctypes.sizeof(prod)):
            self.product = prod.value.strip()
        self.hevent = kernel32.CreateEventW(None, True, False, None)
        self._ov = OVERLAPPED()
        self._ov.hEvent = self.hevent
        self._buf = ctypes.create_string_buffer(self.length)
        self._nread = wintypes.DWORD(0)
        self._pending = False
        return self

    def read(self, timeout_ms=200):
        """Returnera en rapport (bytes) eller None vid timeout. En pagaende
        overlapped-lasning halls over flera anrop tills den slutfors."""
        if not self._pending:
            kernel32.ResetEvent(self.hevent)
            self._ov.Internal = 0
            self._ov.InternalHigh = 0
            ok = kernel32.ReadFile(self.handle, self._buf, self.length,
                                   ctypes.byref(self._nread), ctypes.byref(self._ov))
            if ok:
                return bytes(self._buf.raw[:self._nread.value])
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError("ReadFile-fel %d" % err)
            self._pending = True
        w = kernel32.WaitForSingleObject(self.hevent, timeout_ms)
        if w == WAIT_OBJECT_0:
            self._pending = False
            if kernel32.GetOverlappedResult(self.handle, ctypes.byref(self._ov),
                                            ctypes.byref(self._nread), False):
                return bytes(self._buf.raw[:self._nread.value])
            return None
        if w == WAIT_TIMEOUT:
            return None
        self._pending = False
        return None

    def write(self, data):
        """Skickar en output-rapport (t.ex. LED-styrning, ej ännu kartlagd -
        se ecoder_led_probe.py). data paddas/klipps till output_length byte.
        Egen (synkron, kortlivad) OVERLAPPED-struktur per anrop - enkelt och
        skrivningar är sällsynta, ingen anledning att dela med read():s."""
        if not self.output_length:
            raise OSError("Enheten rapporterar ingen output-rapportlängd")
        buf = (bytes(data) + b"\x00" * self.output_length)[:self.output_length]
        ov = OVERLAPPED()
        ov.hEvent = kernel32.CreateEventW(None, True, False, None)
        try:
            nwritten = wintypes.DWORD(0)
            ok = kernel32.WriteFile(self.handle, buf, len(buf),
                                    ctypes.byref(nwritten), ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    raise OSError("WriteFile-fel %d" % err)
                kernel32.WaitForSingleObject(ov.hEvent, 500)
                if not kernel32.GetOverlappedResult(self.handle, ctypes.byref(ov),
                                                    ctypes.byref(nwritten), False):
                    raise OSError("WriteFile (overlapped)-fel %d" % ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(ov.hEvent)

    def close(self):
        try:
            if self.handle:
                kernel32.CancelIo(self.handle)
                kernel32.CloseHandle(self.handle)
        finally:
            self.handle = None
            if self.hevent:
                kernel32.CloseHandle(self.hevent)
                self.hevent = None
