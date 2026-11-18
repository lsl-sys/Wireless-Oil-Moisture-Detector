/**
 * key_sm.h — 按键状态机（轮询式，不使用外部中断）
 *
 * 状态迁移：
 *   IDLE ──按下──> DEBOUNCE ──消抖通过──> HELD ──保持达标──> TRIGGERED(锁存)
 *     ↑______释放______|__________释放__________|                  |
 *     ↑____________________释放_________________|__________________|
 *
 * 用法：
 *   key_sm_t k;  key_sm_init(&k, PIN_KEY, 1000);
 *   周期调用 key_sm_poll(&k)（5~10ms 一次）；
 *   key_sm_triggered(&k) 取走「长按达标」事件（一次性）。
 */
#ifndef KEY_SM_H
#define KEY_SM_H

#include <Arduino.h>

#define KEY_DEBOUNCE_MS  30   // 消抖时间

typedef enum {
    KEY_ST_IDLE = 0,   // 未按下
    KEY_ST_DEBOUNCE,   // 按下消抖中
    KEY_ST_HELD,       // 已确认按下，保持计时中
    KEY_ST_TRIGGERED   // 长按达标（锁存，释放后回 IDLE）
} key_state_t;

typedef struct {
    uint8_t     pin;
    uint32_t    hold_ms;     // 长按阈值
    key_state_t state;
    uint32_t    t_state;     // 进入当前状态的 millis
    bool        event;       // 触发标志（key_sm_triggered 读取后清除）
} key_sm_t;

static inline bool key_sm_pressed(const key_sm_t *k) {
    return digitalRead(k->pin) == LOW;   // 低电平有效（INPUT_PULLUP）
}

static inline void key_sm_init(key_sm_t *k, uint8_t pin, uint32_t hold_ms) {
    k->pin = pin;
    k->hold_ms = hold_ms;
    k->state = KEY_ST_IDLE;
    k->t_state = millis();
    k->event = false;
    pinMode(pin, INPUT_PULLUP);
}

static inline void key_sm_poll(key_sm_t *k) {
    uint32_t now = millis();
    bool down = key_sm_pressed(k);

    switch (k->state) {
    case KEY_ST_IDLE:
        if (down) { k->state = KEY_ST_DEBOUNCE; k->t_state = now; }
        break;

    case KEY_ST_DEBOUNCE:
        if (!down) {
            k->state = KEY_ST_IDLE;                 // 抖动/误触，丢弃
        } else if (now - k->t_state >= KEY_DEBOUNCE_MS) {
            k->state = KEY_ST_HELD; k->t_state = now;
        }
        break;

    case KEY_ST_HELD:
        if (!down) {
            k->state = KEY_ST_IDLE;                 // 未达阈值就松开
        } else if (now - k->t_state >= k->hold_ms) {
            k->state = KEY_ST_TRIGGERED; k->event = true;
        }
        break;

    case KEY_ST_TRIGGERED:
        if (!down) k->state = KEY_ST_IDLE;          // 事件锁存到被读取
        break;
    }
}

// 取走触发事件：有事件返回 true 并清除
static inline bool key_sm_triggered(key_sm_t *k) {
    if (k->event) { k->event = false; return true; }
    return false;
}

#endif // KEY_SM_H
