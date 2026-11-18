/**
 * wifi_backend.cpp — WiFi 无线后端实现
 *
 * 连接策略：先 5G，失败再切 2.4G
 * 数据通道：TCP Server（8080），向每个已连接客户端按行发送 JSON
 * 发现通道：UDP 每 1 秒向 255.255.255.255:8888 广播设备信息
 */
#include "wifi_backend.h"
#include "provision.h"
#include "ota_server.h"

#include <Arduino.h>
#include <WiFiClient.h>
#include <string.h>
#include <esp_wifi.h>
#include <lwip/sockets.h>

// 已连接客户端套接字加固：
//   1) SO_SNDTIMEO 200ms —— 僵尸/满窗客户端的 write 最多阻塞 200ms，
//      短写由 send() 检测后立刻丢弃，绝不允许拖死整个 loop
//   2) TCP keepalive 5s/2s/3 次 —— 半开连接（对端进程消失、FIN 丢失）
//      约 11s 内被 lwIP 主动拆除
static void tune_client(WiFiClient &c) {
    int fd = c.fd();
    if (fd < 0) return;
    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 200 * 1000;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    int one = 1, idle = 5, intvl = 2, cnt = 3;
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
}

// 尝试连接指定频段；成功返回 true
static bool connect_sta(const char *ssid, const char *pass, bool five_g) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    esp_wifi_set_band_mode(five_g ? WIFI_BAND_MODE_5G_ONLY : WIFI_BAND_MODE_2G_ONLY);
    WiFi.disconnect(true);
    delay(100);
    WiFi.begin(ssid, pass);

    Serial.printf("[wifi] STA connecting %s band=%s...\n", ssid, five_g ? "5G" : "2.4G");
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_STA_TIMEOUT) {
        delay(200);
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[wifi] connected IP=%s\n", WiFi.localIP().toString().c_str());
        return true;
    }
    WiFi.disconnect(true);
    return false;
}

bool WifiBackend::init() {
    char ssid[64] = {0};
    char pass[64] = {0};
    if (!provision_load_credentials(ssid, sizeof(ssid), pass, sizeof(pass))) {
        Serial.println("[wifi] no credentials in NVS");
        return false;
    }

    if (connect_sta(ssid, pass, true)) {
        m_5g = true;
    } else if (connect_sta(ssid, pass, false)) {
        m_5g = false;
    } else {
        Serial.println("[wifi] STA connect failed on both bands");
        return false;
    }

    m_server = new WiFiServer(WIFI_TCP_PORT);
    m_server->begin();
    m_udp.begin(WIFI_UDP_PORT);

    m_running = true;
    m_udp_t = 0;
    Serial.printf("[wifi] ready: TCP=%d UDP=%d link=%s\n",
                  WIFI_TCP_PORT, WIFI_UDP_PORT, name());
    return true;
}

void WifiBackend::acceptClients(void) {
    if (!m_server) return;
    for (;;) {
        WiFiClient c = m_server->accept();
        if (!c) break;
        int slot = -1;
        for (int i = 0; i < WIFI_MAX_CLIENTS; i++) {
            if (!m_clients[i] || !m_clients[i].connected()) { slot = i; break; }
        }
        if (slot < 0) {
            c.stop();  // 客户端已满
            continue;
        }
        if (m_clients[slot]) m_clients[slot].stop();  // 显式释放旧连接
        m_clients[slot] = c;
        m_clients[slot].setNoDelay(true);
        tune_client(m_clients[slot]);
        Serial.printf("[wifi] client connected: %s\n", c.remoteIP().toString().c_str());
    }
}

void WifiBackend::broadcastInfo(void) {
    char info[220];
    snprintf(info, sizeof(info),
             "{\"discover\":\"OILMOIST\",\"dev\":\"%s\",\"mac\":\"%s\",\"ip\":\"%s\","
             "\"port\":%d,\"addr\":%d,\"rssi\":%d,\"link\":\"%s\",\"fw\":\"%s\"}",
             NODE_NAME, WiFi.macAddress().c_str(), WiFi.localIP().toString().c_str(),
             WIFI_TCP_PORT, MDS680A_ADDR, WiFi.RSSI(), name(), ota_fw_version());
    m_udp.beginPacket(IPAddress(255, 255, 255, 255), WIFI_UDP_PORT);
    m_udp.write((const uint8_t *)info, strlen(info));
    m_udp.endPacket();
}

void WifiBackend::poll() {
    if (!m_running) return;
    if (WiFi.status() != WL_CONNECTED) return;  // 断线：暂不处理重连

    acceptClients();

    uint32_t now = millis();
    if (now - m_udp_t >= UDP_BROADCAST_MS) {
        m_udp_t = now;
        broadcastInfo();
    }
}

int WifiBackend::send(const char *json) {
    if (!m_running) return -1;
    int n = 0;
    size_t want = strlen(json) + 1;  // json + '\n'
    for (int i = 0; i < WIFI_MAX_CLIENTS; i++) {
        if (!m_clients[i]) continue;
        if (!m_clients[i].connected()) {
            m_clients[i].stop();  // 回收半死连接
            continue;
        }
        // 防僵尸客户端拖死主循环：短写/写失败立刻 stop 该客户端，
        // 绝不允许单个客户端阻塞整个 loop（广播/HTTP/帧发送全靠它）
        size_t w1 = m_clients[i].write((const uint8_t *)json, want - 1);
        size_t w2 = (w1 == want - 1) ? m_clients[i].write((const uint8_t *)"\n", 1) : 0;
        if (w1 + w2 != want) {
            Serial.printf("[wifi] client %d short write %u/%u, drop\n",
                          i, (unsigned)(w1 + w2), (unsigned)want);
            m_clients[i].stop();
            continue;
        }
        n++;
    }
    return n;  // 成功发送的客户端数量
}

bool WifiBackend::isReady() {
    return m_running && WiFi.status() == WL_CONNECTED;
}

const char *WifiBackend::name() const {
    return m_5g ? "WIFI5G" : "WIFI2.4G";
}

int WifiBackend::rssi() const {
    return WiFi.RSSI();
}
