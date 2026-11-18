/**
 * mds680a.h — MDS-680A 油液微水传感器读取
 */
#ifndef MDS680A_H
#define MDS680A_H

#include "config.h"

typedef struct {
    float temperature;     // ℃
    float water_activity;  // aw
    float water_content;   // ppm
    int   valid;           // 1=数据有效
} mds680a_data_t;

// 初始化 RS485 + UART
void mds680a_init(void);

// 读取一次传感器数据（阻塞，含收发）
// 返回 1=成功, 0=失败
int mds680a_read(mds680a_data_t *out);

// 最近一次读操作的原始 485 收发帧（供上位机"原始收发数据"模式展示）
// tx_len 固定 8；rx_len 成功=17，失败=实际收到字节数（可能为 0）
void mds680a_last_raw(const uint8_t **tx, int *tx_len, const uint8_t **rx, int *rx_len);

#endif // MDS680A_H
