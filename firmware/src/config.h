/**
 * config.h — 油液微水监测系统 全局配置
 *
 * 单板架构：采集端（ESP32-C5）直接接入电脑所在网络/热点，
 * 上位机通过网络（TCP/UDP）直接取数据；取消原电脑端接收板。
 */
#ifndef CONFIG_H
#define CONFIG_H

#include <Arduino.h>

// ============================ 设备名 ============================
#define NODE_NAME "OilSensor"

// ============================ RS485 / MAX485 ============================
#define PIN_485_TX      10    // UART1 TX → MAX485 DI
#define PIN_485_RX      9     // UART1 RX ← MAX485 RO
#define PIN_485_DE      8     // MAX485 DE/RE 控制：HIGH=发送, LOW=接收
#define RS485_BAUD      9600
#define RS485_SERIAL    Serial1

// DE 控制时序（us）
#define MAX485_TX_SETUP_US   10     // 拉高到发送首字节的延时
#define MAX485_TX_HOLD_US    1000   // 最后一字节发送完后保持发送态的时长（按波特率算）

// ============================ MDS-680A 传感器 ============================
#define MDS680A_ADDR        1      // 默认从机地址（用于 JSON addr 字段）
#define MDS680A_REG_TEMP    0x0000 // 温度
#define MDS680A_REG_AW      0x0002 // 水活性
#define MDS680A_REG_WATER   0x0004 // 含水量
#define MDS680A_REG_COUNT   6      // 读 6 个寄存器 = 12 字节数据
#define MDS680A_REQ_LEN     8      // 请求帧长度
#define MDS680A_RESP_LEN    17     // 响应帧长度（1地址+1功能码+1字节数+12数据+2CRC）

// 采集周期（同时作为 TCP 数据帧与 UDP/上位机的发送周期）
#define MDS680A_POLL_MS     500
// 采样失败重试次数
#define MDS680A_RETRY       3
// 传感器失联判定：超过该时长未读到有效数据 → s_ok=0（读数保持最后一次值）
#define SENSOR_LOST_MS      3000

// ============================ 电池检测：INA219（I2C） ============================
// 采样电阻 10mΩ 接在电池 24V 与负载 VIN 之间；A0/A1 均接 3.3V → 器件地址 0x45
#define PIN_I2C_SDA         4      // I2C SDA（硬件连接：IO4）
#define PIN_I2C_SCL         5      // I2C SCL（硬件连接：IO5）
#define INA219_ADDR         0x45   // 器件地址（7bit）
#define INA219_SHUNT_OHM    0.06f  // 采样电阻（Ω）
#define INA219_MAX_AMP      3.2f   // 校准用最大预期电流（A）

// INA219 寄存器
#define INA219_REG_CONFIG   0x00
#define INA219_REG_SHUNT_V  0x01
#define INA219_REG_BUS_V    0x02
#define INA219_REG_POWER    0x03
#define INA219_REG_CURRENT  0x04
#define INA219_REG_CALIB    0x05

// 24V 铅酸/锂电 报警阈值（V）
#define BAT_FULL            24.0f  // 满电
#define BAT_HALF            22.2f
#define BAT_LOW             21.6f
#define BAT_CRIT            21.0f  // 临界

// ============================ 状态指示 / 按键 ============================
// 状态灯为单颗 WS2812（LED2，数据网络 2812DI 接模组 U1 第 19 脚 = GPIO15）
#define PIN_LED             15     // WS2812 数据脚（GPIO15）
#define PIN_KEY             27     // 配网按键（外接按键，低电平有效，INPUT_PULLUP，状态机轮询）
#define LED_BLINK_MS        500

// 配网按键检测时序
#define WIFI_CFG_WINDOW_MS  3000   // 上电后检测窗口
#define WIFI_CFG_HOLD_MS    1000   // 持续按下阈值 → 进配网

// ============================ 配网（串口 JSON 行协议） ============================
#define NVS_NAMESPACE        "oilcfg"    // Preferences 命名空间
#define NVS_KEY_SSID         "ssid"
#define NVS_KEY_PASS         "pass"

// ============================ 网络通信 ============================
#define WIFI_TCP_PORT        8080      // 数据 TCP 端口（采集端为 Server）
#define WIFI_UDP_PORT        8888      // UDP 广播端口（设备发现）
#define UDP_BROADCAST_MS     1000      // UDP 设备信息广播周期
#define WIFI_STA_TIMEOUT     15000     // STA 连接超时 ms
#define WIFI_MAX_CLIENTS     4         // 最大 TCP 客户端数

#endif // CONFIG_H
