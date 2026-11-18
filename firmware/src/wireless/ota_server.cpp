/**
 * ota_server.cpp — HTTP OTA 固件升级服务
 *
 * POST handler 空函数（让 WebServer 进入 upload 阶段）；
 * upload handler 完整处理 START/WRITE/END，END 分支 send() + 重启。
 * 唯一 send() 点 = upload handler END 分支，不会有重复 Content-Length。
 */
#include "ota_server.h"
#include "config.h"

#include <Arduino.h>
#include <WebServer.h>
#include <WiFiClient.h>
#include <Update.h>

#define FW_VERSION "1.0.5"
#define HTTP_PORT  80

static WebServer *s_http = nullptr;
static enum ota_state s_state = OTA_IDLE;
static int s_progress = 0;
static String s_errmsg;

// -------------------- 状态 JSON --------------------
static String build_status_json() {
    const char *st = "idle";
    if (s_state == OTA_UPDATING) st = "updating";
    else if (s_state == OTA_SUCCESS) st = "success";
    else if (s_state == OTA_ERROR) st = "error";
    char buf[192];
    snprintf(buf, sizeof(buf),
             "{\"state\":\"%s\",\"progress\":%d,\"msg\":\"%s\"}",
             st, s_progress, s_errmsg.c_str());
    return String(buf);
}

// -------------------- 路由处理 --------------------
static void handle_version() {
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"fw\":\"%s\"}", FW_VERSION);
    s_http->send(200, "application/json", String(buf));
}

static void handle_status() {
    s_http->send(200, "application/json", build_status_json());
}

/** POST handler — 空函数，让 WebServer 进入 upload 阶段处理 body */
static void handle_update_post() { /* 空 */ }

/** Upload handler — 唯一处理 body 和发响应的地方 */
static void handle_update_upload() {
    HTTPUpload &upload = s_http->upload();

    if (upload.status == UPLOAD_FILE_START) {
        Serial.printf("[ota] FW update start, len=%u (multipart totalSize, using UNKNOWN)\n", upload.totalSize);
        s_state = OTA_UPDATING;
        s_progress = 0;
        s_errmsg = "";

        // multipart 的 upload.totalSize 是整个 body（含 boundary），不是纯 bin 大小
        // 用 UPDATE_SIZE_UNKNOWN，让 Update.end() 根据实际写入校验
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
            Update.abort();
            s_state = OTA_ERROR;
            s_errmsg = Update.errorString();
            Serial.printf("[ota] Update.begin fail: %s\n", s_errmsg.c_str());
            s_http->send(400, "application/json",
                         String("{\"error\":\"") + s_errmsg + "\"}");
            s_http->client().stop();
        }
    }
    else if (upload.status == UPLOAD_FILE_WRITE) {
        size_t wn = Update.write(upload.buf, upload.currentSize);
        if (wn != upload.currentSize) {
            Serial.printf("[ota] Update.write short: want %u got %u\n",
                          (unsigned)upload.currentSize, (unsigned)wn);
        }
        s_progress = (int)(upload.totalSize > 0 ?
                           (upload.currentSize * 100 / upload.totalSize) : 0);
    }
    else if (upload.status == UPLOAD_FILE_END) {
        Serial.printf("[ota] FW update end, size=%u\n", upload.totalSize);
        if (Update.end(true)) {
            s_state = OTA_SUCCESS;
            s_progress = 100;
            Serial.println("[ota] FW update OK, rebooting");
            s_http->send(200, "application/json", String("{\"ok\":true}"));
            // 手动 stop client，防止 WebServer 框架再加额外 Content-Length header
            s_http->client().stop();
            delay(500);
            ESP.restart();
        } else {
            s_state = OTA_ERROR;
            s_errmsg = Update.errorString();
            Serial.printf("[ota] Update.end fail: %s\n", s_errmsg.c_str());
            s_http->send(500, "application/json",
                         String("{\"error\":\"") + s_errmsg + "\"}");
            s_http->client().stop();
        }
    }
}

// -------------------- 对外接口 --------------------
const char *ota_fw_version(void) { return FW_VERSION; }

bool ota_server_start(void) {
    if (s_http) return true;
    s_http = new WebServer(HTTP_PORT);

    s_http->on("/version", HTTP_GET, handle_version);
    s_http->on("/status",  HTTP_GET, handle_status);
    // POST handler 空函数 → WebServer 进入 upload 阶段 → upload handler 处理一切
    s_http->on("/update", HTTP_POST, handle_update_post, handle_update_upload);

    s_http->onNotFound([]() {
        s_http->send(404, "text/plain", "not found");
    });

    s_http->begin();
    Serial.printf("[ota] HTTP server started on :%d\n", HTTP_PORT);
    return true;
}

void ota_server_poll(void) {
    if (s_http) s_http->handleClient();
}

enum ota_state ota_server_state(void) { return s_state; }
