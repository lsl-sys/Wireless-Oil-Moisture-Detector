/**
 * battery.h — 电池检测（INA219，I2C）
 *
 * 采样电阻 10mΩ 串在电池与负载之间；INA219 直接采集总线电压（= 电池电压）
 * 与电流/功率。使用 Arduino Wire 库直接读写 INA219 寄存器，不引入第三方库。
 */
#ifndef BATTERY_H
#define BATTERY_H

#include "config.h"

// 初始化 I2C 与 INA219
void battery_init(void);

// 电池（总线）电压，单位 V
float battery_read_voltage(void);

// 负载电流，单位 A
float battery_read_current(void);

// 功率，单位 W
float battery_read_power(void);

// 返回电量百分比 0-100（按电压线性映射 BAT_CRIT..BAT_FULL → 0..100）
uint8_t battery_level(float voltage);

#endif // BATTERY_H
