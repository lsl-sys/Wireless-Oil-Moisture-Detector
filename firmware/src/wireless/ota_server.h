/**
 * ota_server.h — HTTP 固件 OTA 升级服务
 *
 * 对外提供：
 *   POST /update  Content-Type: application/octet-stream  直接发 bin 原始字节流
 *   GET  /version 返回 {"fw":"0.0.0","ver":"1.0.4"}
 *   GET  /status  {"state":"idle|updating|success|error","progress":0-100,"msg":""}
 *
 * 上位机流程：
 *   1) POST http://<ip>:80/update （Content-Length 为 bin 总长，body 直接是固件 bin）
 *   2) 轮询 GET /status 直到 state=success
 *   3) 设备自动重启，重启后读取新 OTA 分区
 */
#ifndef OTA_SERVER_H
#define OTA_SERVER_H

#include <Arduino.h>

#ifdef __cplusplus
extern "C" {
#endif

// 返回当前固件版本号，用于 UDP 广播 + TCP 数据帧里携带
const char *ota_fw_version(void);

// 启动 HTTP OTA Server（监听 80 端口）。失败返回 false
bool ota_server_start(void);

// 轮询（在主 loop 里调用，让 HTTP Server 处理请求）
void ota_server_poll(void);

// 当前状态查询（供上层 WS2812 指示用）
enum ota_state { OTA_IDLE = 0, OTA_UPDATING, OTA_SUCCESS, OTA_ERROR };
enum ota_state ota_server_state(void);

#ifdef __cplusplus
}
#endif

#endif // OTA_SERVER_H
