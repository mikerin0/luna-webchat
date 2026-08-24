# Luna ESP32 dashboard

This is the first ESP32-32E dashboard for the Luna project. It shows live CPU, GPU, and free-memory values, plots recent GPU usage, and provides a touch button to mute or unmute Mike's microphone through the Pi.

## Arduino libraries

Install these libraries in Arduino IDE:

- TFT_eSPI, using the LCDWiki configuration for the E32R40T/E32N40T module
- LVGL 8.x, matching the LCDWiki example
- ArduinoJson 7.x

The sketch uses the LVGL 8 driver API (`lv_disp_drv_t`, `lv_disp_draw_buf_t`, and `lv_timer_handler`). It is not an LVGL 9 sketch.

## Configure the sketch

Edit the values at the top of `esp32_dashboard.ino`:

```cpp
const char *WIFI_SSID = "YOUR_WIFI_NAME";
const char *WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char *LUNA_URL = "https://172.31.31.106:3010";
const char *LUNA_TOKEN = "YOUR_ESP32_TOKEN";
```

The sketch uses `WiFiClientSecure::setInsecure()` for the local self-signed HTTPS certificate. That is suitable for initial testing on the local network. A CA certificate should be configured before exposing the device beyond the trusted LAN.

## Configure Luna

Set the same random token in the Luna service environment:

```text
LUNA_ESP32_TOKEN=use-the-same-random-token-as-the-sketch
```

Restart the `luna-webchat.service` user service after changing the environment.

## API used by the sketch

```text
GET  /api/esp32/status
POST /api/esp32/action  {"action":"mute"}
POST /api/esp32/action  {"action":"unmute"}
```

All requests use:

```text
Authorization: Bearer <token>
```

The status endpoint returns current CPU/GPU/free-memory values, up to 60 GPU samples, microphone state, speaking state, Pi availability, and LED power state.
