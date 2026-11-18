/**
 * modbus.h — Modbus RTU 帧构造与 CRC16 校验
 *
 * 多项式：x16 + x15 + x2 + 1  (0xA001)
 * 校验范围：从地址码到数据结束
 */
#ifndef MODBUS_H
#define MODBUS_H

#include <stdint.h>

// CRC16/MODBUS
static inline uint16_t modbus_crc16(const uint8_t *data, uint16_t len) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t j = 0; j < 8; j++) {
            if (crc & 0x0001)
                crc = (crc >> 1) ^ 0xA001;
            else
                crc >>= 1;
        }
    }
    return crc;
}

// 构造 0x04 读输入寄存器请求帧
// buf: 至少 8 字节; 返回帧长度
static inline uint16_t modbus_build_read_hold(uint8_t *buf, uint8_t addr,
                                               uint16_t reg, uint16_t count) {
    buf[0] = addr;
    buf[1] = 0x04;
    buf[2] = (reg >> 8) & 0xFF;
    buf[3] = reg & 0xFF;
    buf[4] = (count >> 8) & 0xFF;
    buf[5] = count & 0xFF;
    uint16_t crc = modbus_crc16(buf, 6);
    buf[6] = crc & 0xFF;         // CRC Lo
    buf[7] = (crc >> 8) & 0xFF;  // CRC Hi
    return 8;
}

// 校验响应帧 CRC
static inline int modbus_check_crc(const uint8_t *buf, uint16_t len) {
    if (len < 4) return 0;
    uint16_t calc = modbus_crc16(buf, len - 2);
    uint16_t recv = (uint16_t)buf[len - 1] << 8 | buf[len - 2];
    return calc == recv;
}

#endif // MODBUS_H
