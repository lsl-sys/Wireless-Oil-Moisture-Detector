/**
 * provision.h — 配网模块（串口 JSON 行协议 + NVS 持久化）
 *
 * 方案：上位机经 USB 串口（UART0 / CH343，921600 8N1）下发单行 JSON 命令，
 *       设备逐行应答（应答行均以 {"src":"prov" 开头，与调试日志区分）：
 *         {"cmd":"hello"}                         → 握手应答（dev/mac/fw/当前连接）
 *         {"cmd":"scan"}                          → 异步扫描，应答周围 WiFi 列表
 *         {"cmd":"set","ssid":"...","pass":"..."} → 尝试连接，成功写 NVS 并重启
 *
 * 串口监听常态运行（主 loop 每轮调用 provision_serial_poll），无需按键；
 * 无凭据 / 上电长按按键时进入 provision_run 阻塞配网循环（只响应串口命令）。
 */
#ifndef PROVISION_H
#define PROVISION_H

#include "config.h"

// NVS 中是否已有 WiFi 凭据
bool provision_has_credentials(void);

// 从 NVS 读取凭据，返回 true=成功
bool provision_load_credentials(char *ssid, size_t ssid_len,
                                char *pass, size_t pass_len);

// 上电前 3 秒检测 IO27：按键状态机（消抖+长按），持续按下 ≥1 秒返回 true（进入配网）
bool provision_should_enter(void);

// 进入配网模式（阻塞；set 成功后 ESP.restart，正常情况下不返回）
void provision_run(void);

// 串口配网命令监听（主 loop 每轮调用；含异步扫描状态机推进）
void provision_serial_poll(void);

#endif // PROVISION_H
