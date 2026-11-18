/**
 * provision.cpp — 串口配网模块实现（UART0 单行 JSON 协议，921600 8N1）
 *
 * 协议（每条命令/应答独占一行）：
 *   上位机 → 设备：
 *     {"cmd":"hello"}                          握手
 *     {"cmd":"scan"}                           扫描周围 WiFi（异步，不阻塞主循环）
 *     {"cmd":"set","ssid":"...","pass":"..."}  尝试连接（5G→2.4G），成功写 NVS 并重启
 *   设备 → 上位机（均以 {"src":"prov" 开头，宿主按此前缀过滤调试日志）：
 *     {"src":"prov","ev":"hello","ok":1,"dev":"...","mac":"...","fw":"...",
 *      "connected":1,"ssid":"...","ip":"...","rssi":-55}
 *     {"src":"prov","ev":"scan","ok":1,"nets":[{"s":"asus","r":-52,"e":1},...]}
 *     {"src":"prov","ev":"set","ok":1,"ip":"192.168.x.x","reboot":1}
 *     失败时 ok:0 且带 "err":"busy|timeout|failed|bad_ssid|connect_failed"
 *
 * set 失败会尝试恢复原网络连接（旧凭据仍在 NVS，不写坏）。
 */
#include "provision.h"
#include "ota_server.h"
#include "../key/key_sm.h"

#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include <string.h>

#define PROV_MAX_NETS      16      // 扫描结果最多回传条数
#define PROV_RX_MAX        384     // 串口命令行缓冲上限（超长丢弃）
#define PROV_SCAN_TIMEOUT  25000   // 异步扫描超时 ms（C5 双频段全信道实测 ~13s）
#define PROV_CONN_MS       8000    // set 单频段连接尝试超时 ms
#define PROV_RESTORE_MS    6000    // 失败后恢复原网络的单频段超时 ms

// ============================ NVS ============================
bool provision_has_credentials(void) {
    Preferences p;
    if (!p.begin(NVS_NAMESPACE, true)) return false;
    bool has = p.isKey(NVS_KEY_SSID) && p.getString(NVS_KEY_SSID).length() > 0;
    p.end();
    return has;
}

bool provision_load_credentials(char *ssid, size_t ssid_len,
                                char *pass, size_t pass_len) {
    Preferences p;
    if (!p.begin(NVS_NAMESPACE, true)) return false;
    String s = p.getString(NVS_KEY_SSID, "");
    String w = p.getString(NVS_KEY_PASS, "");
    p.end();
    if (s.length() == 0) return false;
    snprintf(ssid, ssid_len, "%s", s.c_str());
    snprintf(pass, pass_len, "%s", w.c_str());
    return true;
}

// ============================ 按键检测（状态机） ============================
bool provision_should_enter(void) {
    key_sm_t key;
    key_sm_init(&key, PIN_KEY, WIFI_CFG_HOLD_MS);

    uint32_t t0 = millis();
    while (millis() - t0 < WIFI_CFG_WINDOW_MS) {
        key_sm_poll(&key);
        if (key_sm_triggered(&key)) return true;   // 长按达标 → 进配网
        delay(5);
    }
    return false;
}

// ============================ 串口 JSON 行协议 ============================
static String   s_rx;                  // 命令行缓冲（堆分配，避免占用栈）
static bool     s_scanning = false;    // 异步扫描进行中
static uint32_t s_scan_t0 = 0;
static char     s_old_ssid[64];        // set 失败恢复用旧凭据（静态，非重入）
static char     s_old_pass[64];

// 最近一次扫描的 AP 表（BSSID+信道），set 时用于定向直连（绕过 connect 扫描找不到 AP 的问题）
struct prov_ap_t {
    char    ssid[33];
    uint8_t bssid[6];
    uint8_t ch;
    uint8_t auth;
};
static prov_ap_t        s_aps[PROV_MAX_NETS];
static int              s_ap_n = 0;

// 从 JSON 行里取字符串字段（支持 \" \\ \n \t 转义）；找到返回 true
static bool json_get_str(const String &js, const char *key, char *out, size_t out_len) {
    String pat = "\"";
    pat += key;
    pat += "\"";
    int i = js.indexOf(pat);
    if (i < 0) return false;
    i += pat.length();
    while (i < (int)js.length() && (js[i] == ' ' || js[i] == ':')) i++;
    if (i >= (int)js.length() || js[i] != '"') return false;
    i++;
    size_t o = 0;
    while (i < (int)js.length()) {
        char c = js[i++];
        if (c == '\\' && i < (int)js.length()) {
            char e = js[i++];
            if (e == 'n') c = '\n';
            else if (e == 't') c = '\t';
            else c = e;                       // \" \\ \/ 等原样取转义后字符
        } else if (c == '"') {
            break;
        }
        if (o + 1 < out_len) out[o++] = c;
    }
    out[o] = 0;
    return true;
}

// 追加 JSON 字符串值（转义引号/反斜杠，丢弃控制字符）
static void json_escape_into(String &dst, const String &src) {
    for (size_t i = 0; i < src.length(); i++) {
        char c = src[i];
        if (c == '"' || c == '\\') { dst += '\\'; dst += c; }
        else if ((uint8_t)c >= 0x20) dst += c;
    }
}

static void prov_reply(const String &js) {
    Serial.println(js);
}

// ---------------- hello ----------------
static void prov_on_hello(void) {
    bool conn = (WiFi.status() == WL_CONNECTED);
    String js;
    js.reserve(192);
    js = "{\"src\":\"prov\",\"ev\":\"hello\",\"ok\":1,\"dev\":\"" NODE_NAME
         "\",\"mac\":\"";
    js += WiFi.macAddress();
    js += "\",\"fw\":\"";
    js += ota_fw_version();
    js += "\",\"connected\":";
    js += (conn ? 1 : 0);
    if (conn) {
        js += ",\"ssid\":\"";
        json_escape_into(js, WiFi.SSID());
        js += "\",\"ip\":\"";
        js += WiFi.localIP().toString();
        js += "\",\"rssi\":";
        js += WiFi.RSSI();
    }
    js += '}';
    prov_reply(js);
}

// ---------------- scan（异步，状态机在 provision_serial_poll 里推进） ----------------
static void prov_on_scan(void) {
    if (s_scanning) {
        prov_reply("{\"src\":\"prov\",\"ev\":\"scan\",\"ok\":0,\"err\":\"busy\"}");
        return;
    }
    if (WiFi.getMode() == WIFI_OFF || WiFi.getMode() == WIFI_AP) WiFi.mode(WIFI_STA);
    WiFi.scanDelete();
    if (WiFi.scanNetworks(true) == WIFI_SCAN_FAILED) {   // true=异步
        prov_reply("{\"src\":\"prov\",\"ev\":\"scan\",\"ok\":0,\"err\":\"failed\"}");
        return;
    }
    s_scanning = true;
    s_scan_t0 = millis();
    Serial.println("[prov] scan started");
}

static void prov_scan_poll(void) {
    if (!s_scanning) return;
    int st = WiFi.scanComplete();
    if (st == WIFI_SCAN_RUNNING) {
        if (millis() - s_scan_t0 > PROV_SCAN_TIMEOUT) {
            WiFi.scanDelete();
            s_scanning = false;
            prov_reply("{\"src\":\"prov\",\"ev\":\"scan\",\"ok\":0,\"err\":\"timeout\"}");
        }
        return;
    }
    s_scanning = false;
    if (st < 0) {
        WiFi.scanDelete();
        prov_reply("{\"src\":\"prov\",\"ev\":\"scan\",\"ok\":0,\"err\":\"failed\"}");
        return;
    }
    // 从 Arduino 扫描缓存取 SSID/BSSID/信道（须在 scanDelete 之前）
    s_ap_n = 0;
    for (int i = 0; i < st && s_ap_n < PROV_MAX_NETS; i++) {
        String ss; uint8_t enc; int32_t rssi; uint8_t *bssid; int32_t ch;
        if (!WiFi.getNetworkInfo(i, ss, enc, rssi, bssid, ch)) break;
        int k = s_ap_n++;
        snprintf(s_aps[k].ssid, sizeof(s_aps[k].ssid), "%s", ss.c_str());
        memcpy(s_aps[k].bssid, bssid, 6);
        s_aps[k].ch   = (uint8_t)ch;
        s_aps[k].auth = enc;
    }
    int n = st;
    if (n > PROV_MAX_NETS) n = PROV_MAX_NETS;
    String js;
    js.reserve(48 + n * 56);
    js = "{\"src\":\"prov\",\"ev\":\"scan\",\"ok\":1,\"nets\":[";
    for (int i = 0; i < n; i++) {   // 驱动返回顺序基本按 RSSI 降序
        if (i) js += ',';
        js += "{\"s\":\"";
        json_escape_into(js, WiFi.SSID(i));
        js += "\",\"r\":";
        js += WiFi.RSSI(i);
        js += ",\"e\":";
        js += (WiFi.encryptionType(i) == WIFI_AUTH_OPEN) ? 0 : 1;
        js += ",\"a\":";
        js += (int)WiFi.encryptionType(i);
        js += ",\"ch\":";
        js += (i < s_ap_n) ? (int)s_aps[i].ch : 0;
        js += '}';
    }
    js += "]}";
    WiFi.scanDelete();
    prov_reply(js);
    Serial.printf("[prov] scan done, %d nets\n", st);
    for (int i = 0; i < s_ap_n; i++)
        Serial.printf("[prov]   %s ch=%u auth=%u bssid=%02X:%02X:%02X:%02X:%02X:%02X\n",
                      s_aps[i].ssid, s_aps[i].ch, s_aps[i].auth,
                      s_aps[i].bssid[0], s_aps[i].bssid[1], s_aps[i].bssid[2],
                      s_aps[i].bssid[3], s_aps[i].bssid[4], s_aps[i].bssid[5]);
}

// ---------------- set（尝试连接 → 写 NVS → 重启；失败恢复原网络） ----------------
static volatile int s_last_disc_reason = -1;   // 最近一次 STA 断连原因码（诊断用）

static void prov_capture_disconnect_event(void) {
    static bool registered = false;
    if (registered) return;
    registered = true;
    WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
        s_last_disc_reason = info.wifi_sta_disconnected.reason;
    }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
}

// 按 SSID 查最近扫描表
static const prov_ap_t *prov_find_ap(const char *ssid) {
    for (int i = 0; i < s_ap_n; i++)
        if (strcmp(s_aps[i].ssid, ssid) == 0) return &s_aps[i];
    return nullptr;
}

// 单次连接尝试：有 AP 记录则按 BSSID+信道定向直连，否则 AUTO 全频段盲扫
static bool prov_connect_once(const char *ssid, const char *pass,
                              const prov_ap_t *ap, uint32_t timeout_ms) {
    prov_capture_disconnect_event();
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);
    WiFi.disconnect(true);
    delay(300);
    s_last_disc_reason = -1;   // 清在 disconnect 之后：只统计本次 begin 期间的事件
    if (ap) {
        WiFi.begin(ssid, pass, ap->ch, ap->bssid);   // 定向直连，跳过 connect 扫描
        Serial.printf("[prov] connecting %s ch=%u bssid=%02X:%02X:%02X:%02X:%02X:%02X\n",
                      ssid, ap->ch, ap->bssid[0], ap->bssid[1], ap->bssid[2],
                      ap->bssid[3], ap->bssid[4], ap->bssid[5]);
    } else {
        WiFi.begin(ssid, pass);
        Serial.printf("[prov] connecting %s (blind auto)...\n", ssid);
    }
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeout_ms) delay(200);
    if (WiFi.status() != WL_CONNECTED)
        Serial.printf("[prov] connect fail: status=%d last_reason=%d\n",
                      (int)WiFi.status(), s_last_disc_reason);
    return WiFi.status() == WL_CONNECTED;
}

static void prov_on_set(const String &line) {
    static char ssid[64], pass[64];   // 静态缓冲，非重入
    if (!json_get_str(line, "ssid", ssid, sizeof(ssid)) || ssid[0] == 0) {
        prov_reply("{\"src\":\"prov\",\"ev\":\"set\",\"ok\":0,\"err\":\"bad_ssid\"}");
        return;
    }
    if (!json_get_str(line, "pass", pass, sizeof(pass))) pass[0] = 0;  // 开放网络允许无密码

    bool had_old = provision_load_credentials(s_old_ssid, sizeof(s_old_ssid),
                                              s_old_pass, sizeof(s_old_pass));

    // 优先按扫描表定向直连 ×2，再盲扫 AUTO 兜底 ×1
    bool ok = false;
    const prov_ap_t *ap = prov_find_ap(ssid);
    for (int i = 0; i < 2 && !ok; i++)
        ok = prov_connect_once(ssid, pass, ap, PROV_CONN_MS);
    if (!ok)
        ok = prov_connect_once(ssid, pass, nullptr, PROV_CONN_MS);

    if (ok) {
        String ip = WiFi.localIP().toString();
        Preferences p;
        p.begin(NVS_NAMESPACE, false);
        p.putString(NVS_KEY_SSID, ssid);
        p.putString(NVS_KEY_PASS, pass);
        p.end();
        prov_reply("{\"src\":\"prov\",\"ev\":\"set\",\"ok\":1,\"ip\":\"" + ip +
                   "\",\"reboot\":1}");
        Serial.printf("[prov] connected ip=%s, saved, rebooting\n", ip.c_str());
        Serial.flush();
        delay(500);
        ESP.restart();   // 不返回
    }

    prov_reply("{\"src\":\"prov\",\"ev\":\"set\",\"ok\":0,\"err\":\"connect_failed\"}");
    Serial.println("[prov] connect failed");
    if (had_old) {   // 恢复原网络（旧凭据未被覆盖）
        Serial.printf("[prov] restore old ssid=%s\n", s_old_ssid);
        const prov_ap_t *old_ap = prov_find_ap(s_old_ssid);
        if (!prov_connect_once(s_old_ssid, s_old_pass, old_ap, PROV_RESTORE_MS))
            prov_connect_once(s_old_ssid, s_old_pass, nullptr, PROV_RESTORE_MS);
    }
}

// ---------------- 行分发 + 对外轮询 ----------------
static void prov_handle_line(const String &line) {
    if (!line.startsWith("{")) return;   // 只认 JSON 行，忽略杂散输入
    char cmd[16] = {0};
    if (!json_get_str(line, "cmd", cmd, sizeof(cmd))) return;
    if (strcmp(cmd, "hello") == 0)      prov_on_hello();
    else if (strcmp(cmd, "scan") == 0)  prov_on_scan();
    else if (strcmp(cmd, "set") == 0)   prov_on_set(line);
}

void provision_serial_poll(void) {
    prov_scan_poll();
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            s_rx.trim();
            if (s_rx.length() > 0) prov_handle_line(s_rx);
            s_rx = "";
        } else if ((int)s_rx.length() < PROV_RX_MAX) {
            s_rx += c;
        }
    }
}

// ============================ 阻塞配网（无凭据 / 按键触发） ============================
void provision_run(void) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    Serial.println("[prov] enter serial provisioning, waiting host commands");
    Serial.println("[prov] cmd: {\"cmd\":\"hello\"} / {\"cmd\":\"scan\"} / "
                   "{\"cmd\":\"set\",\"ssid\":\"...\",\"pass\":\"...\"}");
    for (;;) {
        provision_serial_poll();
        delay(5);
    }
}
