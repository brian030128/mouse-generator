#include <Arduino.h>
#include <USB.h>
#include <USBHID.h>
#include <esp_timer.h>
#include <errno.h>
#include "config.h"

#if !CONFIG_IDF_TARGET_ESP32S3 || ARDUINO_USB_MODE != 0
#error Select ESP32-S3 and USB Mode = USB-OTG (TinyUSB)
#endif
#if ARDUINO_USB_CDC_ON_BOOT || ARDUINO_USB_MSC_ON_BOOT || ARDUINO_USB_DFU_ON_BOOT
#error Disable USB CDC, MSC and DFU on boot for a mouse-only device
#endif

// Generic Desktop / Mouse, five buttons, relative signed 16-bit X/Y,
// signed 8-bit wheel. Report protocol only; no keyboard or vendor collection.
static const uint8_t REPORT_DESCRIPTOR[] = {
  0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01,
  0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01,
  0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01,
  0x95, 0x05, 0x81, 0x02, 0x75, 0x03, 0x95, 0x01,
  0x81, 0x03, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
  0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10,
  0x95, 0x02, 0x81, 0x06, 0x09, 0x38, 0x15, 0x81,
  0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
  0xC0, 0xC0
};

USBHID hid;
class RelayMouse : public USBHIDDevice {
public:
  RelayMouse() { hid.addDevice(this, sizeof(REPORT_DESCRIPTOR)); }
  uint16_t _onGetDescriptor(uint8_t *dst) override {
    memcpy(dst, REPORT_DESCRIPTOR, sizeof(REPORT_DESCRIPTOR));
    return sizeof(REPORT_DESCRIPTOR);
  }
} mouse;

struct __attribute__((packed)) MouseReport {
  uint8_t buttons;
  int16_t dx, dy;
  int8_t wheel;
};
static_assert(sizeof(MouseReport) == 6, "HID report layout mismatch");
struct Event { uint32_t due; MouseReport report; };
Event events[MAX_EVENTS];
unsigned expected = 0, loaded = 0, cursor = 0;
bool running = false, releasePending = true;
uint8_t heldButtons = 0;
int64_t epoch = 0;
uint32_t lastContact = 0, worstLate = 0;
char line[128];
unsigned lineLength = 0;
bool overflow = false;

uint16_t crc16(const char *s) {
  uint16_t crc = 0xFFFF;
  while (*s) {
    crc ^= uint16_t(uint8_t(*s++)) << 8;
    for (unsigned b = 0; b < 8; ++b)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}

void releaseButtons() {
  if (releasePending && hid.ready()) {
    MouseReport report = {0, 0, 0, 0};
    if (hid.SendReport(1, &report, sizeof(report), 20)) {
      heldButtons = 0;
      releasePending = false;
    }
  }
}

void stopRun(const char *reason) {
  running = false;
  expected = loaded = cursor = 0;
  releasePending = true;
  releaseButtons();
  if (reason) Serial0.println(reason);
}

// Parse exactly n decimal integers; reject overflow and trailing garbage.
bool numbers(char *s, int64_t *values, unsigned n) {
  for (unsigned i = 0; i < n; ++i) {
    while (*s == ' ') ++s;
    if (!*s) return false;
    char *end;
    errno = 0;
    values[i] = strtoll(s, &end, 10);
    if (end == s || errno || (*end && *end != ' ')) return false;
    s = end;
  }
  while (*s == ' ') ++s;
  return !*s;
}

void command(char *s) {
  // Unframed STOP is always accepted, including during playback.
  if (!strcmp(s, "STOP")) { stopRun("OK STOP"); return; }
  char *star = strrchr(s, '*');
  if (!star || strlen(star + 1) != 4) { stopRun("ERR FRAME"); return; }
  for (unsigned i = 1; i <= 4; ++i)
    if (!isxdigit((unsigned char)star[i])) { stopRun("ERR FRAME"); return; }
  uint16_t received = strtoul(star + 1, nullptr, 16);
  *star = 0;
  if (crc16(s) != received) { stopRun("ERR CRC"); return; }
  lastContact = millis();
  if (!strcmp(s, "HELLO")) {
    Serial0.printf("OK HELLO 1 %u %u %u %u USB=%u\n", MAX_EVENTS,
                   MIN_GAP_US, MAX_DURATION_US, WATCHDOG_MS, hid.ready());
  } else if (!strcmp(s, "PING")) {
    Serial0.println("OK PING");
  } else if (!strncmp(s, "LOAD ", 5) && !running) {
    int64_t v[1];
    if (!numbers(s + 5, v, 1) || v[0] < 1 || v[0] > MAX_EVENTS) {
      stopRun("ERR COUNT"); return;
    }
    stopRun(nullptr);
    expected = v[0];
    Serial0.println("OK LOAD");
  } else if (!strncmp(s, "E ", 2) && !running) {
    int64_t v[6];
    if (!numbers(s + 2, v, 6) || !expected || loaded >= expected ||
        v[0] != loaded || v[1] < 0 || v[1] > MAX_DURATION_US ||
        (loaded && v[1] < events[loaded - 1].due + MIN_GAP_US) ||
        v[2] < -32767 || v[2] > 32767 || v[3] < -32767 || v[3] > 32767 ||
        v[4] < 0 || v[4] > 31 || v[5] < -127 || v[5] > 127) {
      stopRun("ERR EVENT"); return;
    }
    events[loaded] = {uint32_t(v[1]), {uint8_t(v[4]), int16_t(v[2]),
                                      int16_t(v[3]), int8_t(v[5])}};
    Serial0.printf("OK E %u\n", loaded++);
  } else if (!strcmp(s, "RUN") && !running) {
    releaseButtons();
    if (!expected || loaded != expected || events[loaded - 1].report.buttons ||
        !hid.ready() || releasePending || digitalRead(STOP_PIN) == LOW) {
      stopRun("ERR NOT_READY"); return;
    }
    cursor = worstLate = 0;
    epoch = esp_timer_get_time() + 10000;  // time to return the RUN acknowledgement
    running = true;
    Serial0.println("OK RUN");
  } else {
    stopRun("ERR COMMAND");
  }
}

void setup() {
  pinMode(STOP_PIN, INPUT_PULLUP);
  Serial0.setRxBufferSize(2048);
  Serial0.begin(CONTROL_BAUD, SERIAL_8N1, CONTROL_RX_PIN, CONTROL_TX_PIN);
  USB.productName(PRODUCT_NAME);
  USB.manufacturerName(MANUFACTURER_NAME);
  char serial[17];
  snprintf(serial, sizeof(serial), "%012llX", (unsigned long long)ESP.getEfuseMac());
  USB.serialNumber(serial);
  USB.usbClass(0); USB.usbSubClass(0); USB.usbProtocol(0);
  USB.usbAttributes(0x80); USB.usbPower(100); USB.webUSB(false);
  hid.begin();
  USB.begin();
  lastContact = millis();
  Serial0.println("READY MOUSE_RELAY 1");
}

void loop() {
  // Bounded input work prevents an incoming serial flood from starving motion.
  for (unsigned budget = 0; budget < 128 && Serial0.available(); ++budget) {
    char c = Serial0.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLength] = 0;
      if (overflow) stopRun("ERR LINE_TOO_LONG");
      else if (lineLength) command(line);
      lineLength = 0; overflow = false;
    } else if (!overflow && lineLength < sizeof(line) - 1) {
      line[lineLength++] = c;
    } else { overflow = true; }
  }
  releaseButtons();
  if (!running) { delay(1); return; }
  if (digitalRead(STOP_PIN) == LOW) { stopRun("ERR SWITCH"); return; }
  if (uint32_t(millis() - lastContact) > WATCHDOG_MS) {
    stopRun("ERR WATCHDOG"); return;
  }
  if (!USB) { stopRun("ERR USB_DISCONNECTED"); return; }
  int64_t elapsed = esp_timer_get_time() - epoch;
  if (elapsed < events[cursor].due) { delayMicroseconds(100); return; }
  uint32_t late = elapsed - events[cursor].due;
  if (late > MAX_LATENESS_US || !hid.ready()) {
    stopRun("ERR USB_OR_LATE"); return;
  }
  worstLate = max(worstLate, late);
  heldButtons = events[cursor].report.buttons;
  if (!hid.SendReport(1, &events[cursor].report, sizeof(MouseReport), 20)) {
    // Never retry displacement: a timeout may occur after transmission.
    stopRun("ERR HID_SEND"); return;
  }
  if (++cursor == loaded) {
    Serial0.printf("DONE %u %u\n", cursor, worstLate);
    running = false;
    expected = loaded = cursor = 0;
  }
}
