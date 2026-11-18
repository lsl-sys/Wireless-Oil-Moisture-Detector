/**
 * battery.cpp — 电池检测实现（INA219，I2C 直接读写寄存器）
 *
 * 配置寄存器 0x399F：32V 总线量程 / ±320mV 分流压降 / 12bit ADC / 连续采样
 * 校准：Cal = 0.04096 / (current_LSB * R_shunt)
 *   current_LSB = INA219_MAX_AMP / 32768
 */
#include "battery.h"
#include <Arduino.h>
#include <Wire.h>

// 电流分辨率（A/LSB）
static const float INA219_CURRENT_LSB = INA219_MAX_AMP / 32768.0f;
// INA219 是否在线（上电扫描结果）；不在线时读函数直接返回 0，避免每周期 I2C 超时拖慢主循环
static bool s_present = false;

// 写 16bit 寄存器
static bool ina_write16(uint8_t reg, uint16_t val) {
    Wire.beginTransmission(INA219_ADDR);
    Wire.write(reg);
    Wire.write((val >> 8) & 0xFF);
    Wire.write(val & 0xFF);
    return Wire.endTransmission() == 0;
}

// 读 16bit 寄存器
static bool ina_read16(uint8_t reg, uint16_t *out) {
    Wire.beginTransmission(INA219_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((uint8_t)INA219_ADDR, (uint8_t)2) != 2) return false;
    uint8_t hi = Wire.read();
    uint8_t lo = Wire.read();
    *out = ((uint16_t)hi << 8) | lo;
    return true;
}

void battery_init(void) {
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    Wire.setClock(100000);

    // 上电自检：扫描 I2C 总线，确认 INA219 是否在线（诊断用，一次性）
    Serial.printf("[battery] i2c scan (sda=%d scl=%d):", PIN_I2C_SDA, PIN_I2C_SCL);
    int found = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) {
            Serial.printf(" 0x%02X", addr);
            found++;
            if (addr == INA219_ADDR) s_present = true;
        }
    }
    Serial.printf("%s\n", found ? "" : " (无设备)");
    if (!s_present) {
        Serial.printf("[battery] INA219 @0x%02X 不在线，电池量测跳过\n", INA219_ADDR);
        return;
    }

    ina_write16(INA219_REG_CONFIG, 0x399F);
    uint16_t cal = (uint16_t)(0.04096f / (INA219_CURRENT_LSB * INA219_SHUNT_OHM));
    ina_write16(INA219_REG_CALIB, cal);

    Serial.printf("[battery] INA219 @0x%02X init (R=%.3f ohm, cal=%u)\n",
                  INA219_ADDR, INA219_SHUNT_OHM, cal);
}

float battery_read_voltage(void) {
    if (!s_present) return 0.0f;
    uint16_t raw = 0;
    if (!ina_read16(INA219_REG_BUS_V, &raw)) return 0.0f;
    // 总线电压寄存器：bit15 CNVR, bit1 OVF, bit0 CNVR；bit[15:3] 有效，4mV/LSB
    return (float)(raw >> 3) * 0.004f;
}

float battery_read_current(void) {
    if (!s_present) return 0.0f;
    uint16_t raw = 0;
    if (!ina_read16(INA219_REG_CURRENT, &raw)) return 0.0f;
    // 电流寄存器为有符号 16bit，单位 current_LSB
    return (float)(int16_t)raw * INA219_CURRENT_LSB;
}

float battery_read_power(void) {
    if (!s_present) return 0.0f;
    uint16_t raw = 0;
    if (!ina_read16(INA219_REG_POWER, &raw)) return 0.0f;
    // 功率寄存器：Power = 20 * current_LSB * raw
    return (float)raw * 20.0f * INA219_CURRENT_LSB;
}

uint8_t battery_level(float voltage) {
    // 线性映射到 0-100
    if (voltage >= BAT_FULL) return 100;
    if (voltage <= BAT_CRIT) return 0;
    return (uint8_t)((voltage - BAT_CRIT) / (BAT_FULL - BAT_CRIT) * 100.0f);
}
