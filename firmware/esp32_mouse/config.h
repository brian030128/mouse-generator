#pragma once

// UART0: the USB-to-UART bridge on many S3 development boards.
// For an external 3.3 V adapter: adapter TX -> RX, RX -> TX, GND -> GND.
constexpr int CONTROL_RX_PIN = 44;
constexpr int CONTROL_TX_PIN = 43;
constexpr unsigned long CONTROL_BAUD = 115200;
// Ground this pin to abort. Change it if GPIO4 is occupied on your board.
constexpr int STOP_PIN = 4;
constexpr unsigned MAX_EVENTS = 4096;
constexpr unsigned MAX_DURATION_US = 60000000;
constexpr unsigned MIN_GAP_US = 1000;
constexpr unsigned WATCHDOG_MS = 1500;
constexpr unsigned MAX_LATENESS_US = 20000;

// Keep the Arduino core's development VID/PID. For distribution, obtain an
// assigned VID/PID or explicit permission from its owner; do not copy a mouse.
constexpr char PRODUCT_NAME[] = "Mouse Generator Relay";
constexpr char MANUFACTURER_NAME[] = "Mouse Generator Project";
