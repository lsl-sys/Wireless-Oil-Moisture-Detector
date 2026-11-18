/**
 * max485.h — MAX485 收发控制
 *
 * DE（脚3）与 /RE（脚2）短接，由 GPIO 统一控制：
 *   HIGH = 发送模式
 *   LOW  = 接收模式（默认，闲置时不阻塞总线）
 *
 * 使用：
 *   max485_begin();
 *   max485_tx_mode();
 *   Serial1.write(buf, len);
 *   Serial1.flush();
 *   delayMicroseconds(MAX485_TX_HOLD_US);
 *   max485_rx_mode();
 */
#ifndef MAX485_H
#define MAX485_H

#include "config.h"

static inline void max485_begin(void) {
    pinMode(PIN_485_DE, OUTPUT);
    digitalWrite(PIN_485_DE, LOW);  // 默认接收
}

static inline void max485_tx_mode(void) {
    digitalWrite(PIN_485_DE, HIGH);
    delayMicroseconds(MAX485_TX_SETUP_US);
}

static inline void max485_rx_mode(void) {
    // 等待最后一字节移位完成（flush 只保证数据进发送 FIFO）
    // 9600 8N1 每字节 ≈ 1.04ms，由调用方在 flush 后加延时
    digitalWrite(PIN_485_DE, LOW);
}

#endif // MAX485_H
