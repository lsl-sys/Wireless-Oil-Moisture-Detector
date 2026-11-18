/**
 * main.cpp — 油液微水监测系统 主程序（单板采集端）
 *
 * 流程：
 *   1) 上电前 3 秒检测 IO27：持续按下 ≥1 秒 → 进配网模式
 *   2) 无 NVS 凭据 → 进配网模式
 *   3) 初始化 INA219 / RS485 / 传感器 → 初始化无线后端（WifiBackend）
 *   4) 循环：读传感器（失败重试）→ 读电池 → 组 JSON → 无线发送
 *
 * 上层只依赖无线后端抽象接口 WirelessBackend，不直接引用具体实现。
 */
#include "config.h"
#include "sensor/mds680a.h"
#include "battery/battery.h"
#include "wireless/backend.h"
#include "wireless/provision.h"
#include "wireless/ota_server.h"

#include <Arduino.h>

static WirelessBackend *s_link = nullptr;
static mds680a_data_t    s_sensor = {};
static uint8_t           s_seq = 0;
static uint32_t          s_last_ok_ms = 0;   // 最后一次传感器读取成功的时间戳

// 状态灯（WS2812，单颗）：只在颜色变化时写一次，避免每帧刷 RMT
static void led_show(uint8_t r, uint8_t g, uint8_t b) {
    static uint32_t last = 0xFFFFFFFFu;
    uint32_t cur = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
    if (cur == last) return;
    last = cur;
    rgbLedWrite(PIN_LED, r, g, b);
}

void setup() {
    Serial.begin(921600);
    delay(100);
    Serial.printf("\n=== %s ===\n", NODE_NAME);

    led_show(0, 0, 24);   // 蓝：启动中

    // 1) 上电按键检测 → 配网
    if (provision_should_enter()) {
        Serial.println("[main] key held -> enter provisioning");
        led_show(24, 0, 24);  // 紫：配网模式
        provision_run();      // 阻塞，保存后重启
    }

    // 2) 无凭据 → 配网
    if (!provision_has_credentials()) {
        Serial.println("[main] no credentials -> enter provisioning");
        led_show(24, 0, 24);
        provision_run();
    }

    // 3) 硬件初始化
    battery_init();
    mds680a_init();
    s_last_ok_ms = millis();  // 上电宽限期：前 SENSOR_LOST_MS 内不误报传感器失联

    // 4) 无线后端
    s_link = wireless_backend_get();
    if (!s_link->init()) {
        Serial.println("[main] wireless init failed");
    }

    // 5) HTTP OTA Server（80端口，与 TCP 8080 / UDP 8888 并行）
    ota_server_start();

    Serial.println("[main] init done");
}

void loop() {
    // 串口配网命令常态监听（hello / scan / set，单行 JSON）
    provision_serial_poll();

    // 读取传感器（失败重试）；读数新鲜度状态机：
    //   成功 → 刷新数据 + 时间戳；失败 → 保留最后读数，
    //   超过 SENSOR_LOST_MS 未成功 → 判定传感器失联（s_ok=0）
    bool read_ok = false;
    for (int i = 0; i < MDS680A_RETRY; i++) {
        if (mds680a_read(&s_sensor)) { read_ok = true; break; }
        delay(50);
    }
    uint32_t now = millis();
    if (read_ok) {
        s_last_ok_ms = now;
    } else {
        Serial.println("[main] sensor read failed (after retries)");
    }
    bool sensor_ok = (now - s_last_ok_ms) <= SENSOR_LOST_MS;

    // 电池
    float bat_v = battery_read_voltage();
    float bat_i = battery_read_current();
    float bat_p = battery_read_power();
    uint8_t lvl = battery_level(bat_v);
    Serial.printf("[main] bat=%.2fV %.3fA %.2fW lvl=%u\n", bat_v, bat_i, bat_p, lvl);

    // 维持链路 + 广播 + OTA HTTP 服务
    s_link->poll();
    ota_server_poll();

    // 组装并按行发送 JSON（含实时 cur/pwr、固件版本 fw、传感器状态 s_ok、原始 485 收发帧）
    if (s_link->isReady()) {
        // 原始帧转 hex（tx 8B→16字符，rx ≤17B→34字符）
        const uint8_t *rtx, *rrx; int rtx_n, rrx_n;
        mds680a_last_raw(&rtx, &rtx_n, &rrx, &rrx_n);
        char tx_hex[20], rx_hex[40];
        int o = 0;
        for (int i = 0; i < rtx_n && o < (int)sizeof(tx_hex) - 3; i++)
            o += snprintf(tx_hex + o, sizeof(tx_hex) - o, "%02X", rtx[i]);
        tx_hex[o] = 0;
        o = 0;
        for (int i = 0; i < rrx_n && o < (int)sizeof(rx_hex) - 3; i++)
            o += snprintf(rx_hex + o, sizeof(rx_hex) - o, "%02X", rrx[i]);
        rx_hex[o] = 0;

        char json[448];
        snprintf(json, sizeof(json),
                 "{\"t\":%.1f,\"aw\":%.3f,\"ppm\":%.1f,\"bat\":%.2f,\"cur\":%.3f,"
                 "\"pwr\":%.2f,\"lvl\":%u,\"rssi\":%d,\"link\":\"%s\",\"seq\":%u,"
                 "\"addr\":%d,\"fw\":\"%s\",\"s_ok\":%u,\"rtx\":\"%s\",\"rrx\":\"%s\"}",
                 s_sensor.temperature, s_sensor.water_activity, s_sensor.water_content,
                 bat_v, bat_i, bat_p, lvl, s_link->rssi(), s_link->name(), s_seq,
                 MDS680A_ADDR, ota_fw_version(), sensor_ok ? 1u : 0u, tx_hex, rx_hex);

        int n = s_link->send(json);
        if (n <= 0) Serial.println("[main] no TCP client");
    } else {
        Serial.println("[main] link not ready");
    }

    s_seq++;

    // 状态灯：OTA_UPDATING → 蓝色覆盖；否则 红=传感器失联/绿=链路就绪/黄=等待连接
    if (ota_server_state() == OTA_UPDATING) {
        led_show(0, 0, 48);
    } else if (!sensor_ok) {
        led_show(32, 0, 0);
    } else if (s_link->isReady()) {
        led_show(0, 20, 0);
    } else {
        led_show(32, 16, 0);
    }

    delay(MDS680A_POLL_MS);
}
