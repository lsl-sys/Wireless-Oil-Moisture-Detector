/**
 * wifi_backend.h — WiFi 无线后端实现
 *
 * STA 连接（先 5G 失败再 2.4G）→ 启动 TCP Server（8080，多客户端）
 * 并周期向 255.255.255.255:8888 广播设备信息（供上位机发现）。
 */
#ifndef WIFI_BACKEND_H
#define WIFI_BACKEND_H

#include "backend.h"
#include <WiFi.h>
#include <WiFiServer.h>
#include <WiFiUdp.h>

class WifiBackend : public WirelessBackend {
public:
    bool init() override;
    void poll() override;
    int  send(const char *json) override;
    bool isReady() override;
    const char *name() const override;
    int  rssi() const override;

private:
    void acceptClients(void);
    void broadcastInfo(void);

    WiFiServer  *m_server = nullptr;
    WiFiUDP      m_udp;
    WiFiClient   m_clients[WIFI_MAX_CLIENTS];
    bool         m_running = false;
    bool         m_5g = true;       // 当前是否工作在 5G 频段
    uint32_t     m_udp_t = 0;       // 上次 UDP 广播时刻
};

#endif // WIFI_BACKEND_H
