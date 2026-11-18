/**
 * backend.cpp — 无线后端选择工厂
 *
 * 当前固定使用 WifiBackend；待蓝牙调通后在此切换为 BleBackend。
 * 上层只需调用 wireless_backend_get()。
 */
#include "backend.h"
#include "wifi_backend.h"

static WifiBackend s_backend;

WirelessBackend *wireless_backend_get(void) {
    return &s_backend;
}
