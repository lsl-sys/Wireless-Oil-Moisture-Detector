/**
 * backend.h — 无线后端统一抽象接口
 *
 * 上层（main.cpp）只依赖本接口，不直接引用任何具体实现。
 * 当前实现：WifiBackend（见 wifi_backend.h/.cpp）
 * （蓝牙方案已放弃，全链路仅 WiFi）
 */
#ifndef WIRELESS_BACKEND_H
#define WIRELESS_BACKEND_H

#include "config.h"

class WirelessBackend {
public:
    virtual ~WirelessBackend() {}

    // 初始化后端（建立连接、启动服务等），返回 true=成功
    virtual bool init() = 0;

    // 周期轮询：维持连接、接收/接受新客户端、发送后台广播等
    virtual void poll() = 0;

    // 发送一帧 JSON（无需自带换行；实现方按行补齐 \n）。
    // 返回 >0 表示至少发送成功；<=0 表示失败/无接收方
    virtual int send(const char *json) = 0;

    // 后端是否就绪（可发送数据）
    virtual bool isReady() = 0;

    // 当前链路名（写入 JSON link 字段），如 "WIFI5G"
    virtual const char *name() const = 0;

    // 当前信号强度（dBm）
    virtual int rssi() const = 0;
};

// 获取当前无线后端实例
WirelessBackend *wireless_backend_get(void);

#endif // WIRELESS_BACKEND_H
