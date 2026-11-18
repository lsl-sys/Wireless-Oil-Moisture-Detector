/**
 * mds680a.cpp — MDS-680A 油液微水传感器读取实现
 *
 * 协议：Modbus RTU, 9600 8N1
 * 读取 0x0000 起 6 个寄存器 → 温度(float) + 水活性(float) + 含水量(float)
 * float 字节序：ABCD（大端，IEEE754）
 */
#include "mds680a.h"
#include "rs485/max485.h"
#include "rs485/modbus.h"

#include <Arduino.h>
#include <string.h>

void mds680a_init(void) {
    max485_begin();
    RS485_SERIAL.begin(RS485_BAUD, SERIAL_8N1, PIN_485_RX, PIN_485_TX);
    Serial.printf("[mds680a] init, baud=%d, tx=%d rx=%d de=%d\n",
                  RS485_BAUD, PIN_485_TX, PIN_485_RX, PIN_485_DE);
}

// 从 4 字节大端 float（ABCD）解析
static float parse_float_be(const uint8_t *p) {
    uint32_t u = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
                 ((uint32_t)p[2] << 8)  | (uint32_t)p[3];
    float f;
    memcpy(&f, &u, 4);
    return f;
}

// 最近一次收发原始帧缓存（rx 上限 64 字节，与本函数内 resp 缓冲一致）
static uint8_t s_last_tx[MDS680A_REQ_LEN];
static uint8_t s_last_rx[64];
static int     s_last_tx_len = 0;
static int     s_last_rx_len = 0;

void mds680a_last_raw(const uint8_t **tx, int *tx_len, const uint8_t **rx, int *rx_len) {
    if (tx) *tx = s_last_tx;
    if (tx_len) *tx_len = s_last_tx_len;
    if (rx) *rx = s_last_rx;
    if (rx_len) *rx_len = s_last_rx_len;
}

int mds680a_read(mds680a_data_t *out) {
    out->valid = 0;

    // 构造请求帧
    uint8_t req[MDS680A_REQ_LEN];
    modbus_build_read_hold(req, MDS680A_ADDR, MDS680A_REG_TEMP, MDS680A_REG_COUNT);
    memcpy(s_last_tx, req, MDS680A_REQ_LEN);
    s_last_tx_len = MDS680A_REQ_LEN;
    s_last_rx_len = 0;

    // 清空接收缓冲
    while (RS485_SERIAL.available()) RS485_SERIAL.read();

    // 发送
    max485_tx_mode();
    RS485_SERIAL.write(req, MDS680A_REQ_LEN);
    RS485_SERIAL.flush();
    // 等待最后一字节移位完成（9600bps: 10bit ≈ 1.04ms）
    delayMicroseconds(MAX485_TX_HOLD_US);
    max485_rx_mode();

    // 等待响应（最多 200ms）
    uint8_t resp[64];
    int rlen = 0;
    uint32_t t0 = millis();
    while (rlen < MDS680A_RESP_LEN && millis() - t0 < 200) {
        if (RS485_SERIAL.available()) {
            resp[rlen++] = RS485_SERIAL.read();
        }
    }

    if (rlen < MDS680A_RESP_LEN) {
        memcpy(s_last_rx, resp, rlen);   // 保存实际收到的（可能不完整）
        s_last_rx_len = rlen;
        Serial.printf("[mds680a] timeout, got %d bytes\n", rlen);
        return 0;
    }
    memcpy(s_last_rx, resp, rlen);
    s_last_rx_len = rlen;

    // 校验：地址、功能码、字节数、CRC
    if (resp[0] != MDS680A_ADDR || resp[1] != 0x04 || resp[2] != 12) {
        Serial.printf("[mds680a] bad resp header: %02X %02X %02X\n",
                      resp[0], resp[1], resp[2]);
        return 0;
    }
    if (!modbus_check_crc(resp, MDS680A_RESP_LEN)) {
        Serial.println("[mds680a] CRC error");
        return 0;
    }

    // 解析三个 float
    out->temperature    = parse_float_be(resp + 3);
    out->water_activity = parse_float_be(resp + 7);
    out->water_content  = parse_float_be(resp + 11);
    out->valid = 1;
    return 1;
}
