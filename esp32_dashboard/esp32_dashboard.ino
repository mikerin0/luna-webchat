#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <lvgl.h>
#include <TFT_eSPI.h>
#include <esp_heap_caps.h>

// WIFI_SSID, WIFI_PASSWORD, LUNA_TOKEN live in secrets.h (gitignored).
// Copy secrets.h.example to secrets.h and fill in your own values.
#include "secrets.h"
const char *LUNA_URL = "https://172.31.31.106:3010";

// Backlight enable pin for this ESP32-32E module (per board pinout).
static const int TFT_BACKLIGHT_PIN = 27;

static const uint16_t SCREEN_WIDTH = 480;
static const uint16_t SCREEN_HEIGHT = 320;
static const uint32_t POLL_INTERVAL_MS = 2000;

TFT_eSPI my_lcd = TFT_eSPI();
static lv_disp_draw_buf_t draw_buf;
static const uint32_t DRAW_BUF_LINES = 20;
static lv_color_t *buffer;
static lv_obj_t *cpu_label;
static lv_obj_t *gpu_label;
static lv_obj_t *memory_label;
static lv_obj_t *status_label;
static lv_obj_t *mute_button_label;
static lv_obj_t *big_brother_button_label;
static lv_obj_t *blank_button_label;
static lv_obj_t *blank_overlay;
static lv_obj_t *gpu_chart;
static lv_chart_series_t *gpu_series;
static bool mic_muted = false;
static bool big_brother_mode = false;
static bool blanked_mode = false;
static uint32_t last_poll_ms = 0;

void display_flush(lv_disp_drv_t *display, const lv_area_t *area, lv_color_t *color_p) {
  uint32_t width = area->x2 - area->x1 + 1;
  uint32_t height = area->y2 - area->y1 + 1;
  my_lcd.setAddrWindow(area->x1, area->y1, width, height);
  my_lcd.pushColors((uint16_t *)&color_p->full, width * height, true);
  lv_disp_flush_ready(display);
}

void touch_read(lv_indev_drv_t *, lv_indev_data_t *data) {
  uint16_t touch_x = 0;
  uint16_t touch_y = 0;
  bool touched = my_lcd.getTouch(&touch_x, &touch_y, 600);
  data->state = touched ? LV_INDEV_STATE_PR : LV_INDEV_STATE_REL;
  if (touched) {
    data->point.x = touch_x;
    data->point.y = touch_y;
  }
}

void set_status(const char *text, lv_color_t color) {
  lv_label_set_text(status_label, text);
  lv_obj_set_style_text_color(status_label, color, LV_PART_MAIN);
}

bool luna_request(const char *method, const char *path, const String &body, String &response) {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  WiFiClientSecure client;
  client.setInsecure(); // Local development; use a CA certificate for an exposed server.
  HTTPClient http;
  String url = String(LUNA_URL) + path;
  if (!http.begin(client, url)) {
    Serial.printf("HTTP begin failed for %s\n", url.c_str());
    return false;
  }
  http.addHeader("Authorization", String("Bearer ") + LUNA_TOKEN);
  if (body.length() > 0) {
    http.addHeader("Content-Type", "application/json");
  }

  int code = method[0] == 'G' ? http.GET() : http.POST(body);
  if (code < 0) {
    Serial.printf("HTTP request to %s failed: %s\n", url.c_str(), http.errorToString(code).c_str());
    http.end();
    return false;
  }
  if (code < 200 || code >= 300) {
    Serial.printf("HTTP request to %s returned status %d: %s\n", url.c_str(), code, http.getString().c_str());
    http.end();
    return false;
  }
  response = http.getString();
  http.end();
  return true;
}

void update_mute_button() {
  lv_label_set_text(mute_button_label, mic_muted ? "UNMUTE MIKE" : "MUTE MIKE");
}

void update_big_brother_button() {
  lv_label_set_text(big_brother_button_label, big_brother_mode ? "BIG BRO: ON" : "BIG BRO: OFF");
}

void enter_blank_mode() {
  pinMode(TFT_BACKLIGHT_PIN, OUTPUT);
  digitalWrite(TFT_BACKLIGHT_PIN, LOW);

  String response;
  luna_request("POST", "/api/esp32/action", "{\"action\":\"led_off\"}", response);
  luna_request("POST", "/api/esp32/action", "{\"action\":\"lcd_off\"}", response);

  blanked_mode = true;
  lv_obj_clear_flag(blank_overlay, LV_OBJ_FLAG_HIDDEN);
  lv_obj_move_foreground(blank_overlay);
}

void exit_blank_mode() {
  pinMode(TFT_BACKLIGHT_PIN, OUTPUT);
  digitalWrite(TFT_BACKLIGHT_PIN, HIGH);

  String response;
  luna_request("POST", "/api/esp32/action", "{\"action\":\"led_on\"}", response);
  luna_request("POST", "/api/esp32/action", "{\"action\":\"lcd_on\"}", response);

  blanked_mode = false;
  lv_obj_add_flag(blank_overlay, LV_OBJ_FLAG_HIDDEN);
  set_status("ALL DISPLAYS ON", lv_palette_main(LV_PALETTE_GREEN));
}

void refresh_dashboard() {
  String response;
  if (!luna_request("GET", "/api/esp32/status", "", response)) {
    if (!blanked_mode) {
      set_status(WiFi.status() == WL_CONNECTED ? "LUNA OFFLINE" : "WIFI OFFLINE", lv_palette_main(LV_PALETTE_RED));
    }
    return;
  }

  JsonDocument document;
  DeserializationError error = deserializeJson(document, response);
  if (error) {
    if (!blanked_mode) {
      set_status("BAD STATUS DATA", lv_palette_main(LV_PALETTE_RED));
    }
    return;
  }

  JsonVariant cpu = document["cpu_percent"];
  JsonVariant gpu = document["gpu_percent"];
  JsonVariant memory = document["memory_free_gb"];
  lv_label_set_text_fmt(cpu_label, "CPU  %s%%", cpu.isNull() ? "N/A" : String(cpu.as<float>(), 1).c_str());
  lv_label_set_text_fmt(gpu_label, "GPU  %s%%", gpu.isNull() ? "N/A" : String(gpu.as<float>(), 1).c_str());
  lv_label_set_text_fmt(memory_label, "RAM  %s GB free", memory.isNull() ? "N/A" : String(memory.as<float>(), 1).c_str());

  mic_muted = document["mic_muted"].as<bool>();
  update_mute_button();
  big_brother_mode = document["big_brother_mode"].as<bool>();
  update_big_brother_button();
  if (!blanked_mode) {
    set_status("LUNA ONLINE", lv_palette_main(LV_PALETTE_GREEN));
  }

  JsonArray history = document["gpu_history"].as<JsonArray>();
  lv_chart_set_all_value(gpu_chart, gpu_series, LV_CHART_POINT_NONE);
  for (JsonVariant value : history) {
    lv_chart_set_next_value(gpu_chart, gpu_series, value.isNull() ? 0 : value.as<int>());
  }
  lv_chart_refresh(gpu_chart);
}

void mute_button_event(lv_event_t *event) {
  if (lv_event_get_code(event) != LV_EVENT_CLICKED) {
    return;
  }
  bool target_muted = !mic_muted;
  String body = String("{\"action\":\"") + (target_muted ? "mute" : "unmute") + "\"}";
  String response;
  if (!luna_request("POST", "/api/esp32/action", body, response)) {
    set_status("MUTE ACTION FAILED", lv_palette_main(LV_PALETTE_RED));
    return;
  }
  mic_muted = target_muted;
  update_mute_button();
  set_status(mic_muted ? "MIKE MUTED" : "MIKE LISTENING", lv_palette_main(LV_PALETTE_GREEN));
}

void big_brother_button_event(lv_event_t *event) {
  if (lv_event_get_code(event) != LV_EVENT_CLICKED) {
    return;
  }
  bool target_mode = !big_brother_mode;
  String body = String("{\"action\":\"") + (target_mode ? "big_brother_on" : "big_brother_off") + "\"}";
  String response;
  if (!luna_request("POST", "/api/esp32/action", body, response)) {
    set_status("BIG BROTHER ACTION FAILED", lv_palette_main(LV_PALETTE_RED));
    return;
  }
  big_brother_mode = target_mode;
  update_big_brother_button();
  set_status(big_brother_mode ? "BIG BROTHER ENABLED" : "BIG BROTHER DISABLED", lv_palette_main(LV_PALETTE_GREEN));
}

void blank_button_event(lv_event_t *event) {
  if (lv_event_get_code(event) != LV_EVENT_CLICKED) {
    return;
  }
  enter_blank_mode();
}

void blank_overlay_event(lv_event_t *event) {
  if (lv_event_get_code(event) != LV_EVENT_CLICKED) {
    return;
  }
  exit_blank_mode();
}

void make_dashboard() {
  lv_obj_set_style_bg_color(lv_scr_act(), lv_color_hex(0x111827), LV_PART_MAIN);

  lv_obj_t *title = lv_label_create(lv_scr_act());
  lv_label_set_text(title, "LUNA SYSTEM STATUS");
  lv_obj_set_style_text_color(title, lv_color_hex(0x7dd3fc), LV_PART_MAIN);
  lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 12);

  cpu_label = lv_label_create(lv_scr_act());
  lv_obj_set_style_text_color(cpu_label, lv_color_hex(0xfde68a), LV_PART_MAIN);
  lv_obj_align(cpu_label, LV_ALIGN_TOP_LEFT, 28, 55);

  gpu_label = lv_label_create(lv_scr_act());
  lv_obj_set_style_text_color(gpu_label, lv_color_hex(0x86efac), LV_PART_MAIN);
  lv_obj_align(gpu_label, LV_ALIGN_TOP_LEFT, 150, 55);

  memory_label = lv_label_create(lv_scr_act());
  lv_obj_set_style_text_color(memory_label, lv_color_hex(0xc4b5fd), LV_PART_MAIN);
  lv_obj_align(memory_label, LV_ALIGN_TOP_LEFT, 270, 55);

  gpu_chart = lv_chart_create(lv_scr_act());
  lv_obj_set_size(gpu_chart, 310, 180);
  lv_obj_align(gpu_chart, LV_ALIGN_TOP_LEFT, 18, 95);
  lv_chart_set_type(gpu_chart, LV_CHART_TYPE_LINE);
  lv_chart_set_range(gpu_chart, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
  lv_chart_set_point_count(gpu_chart, 60);
  lv_chart_set_update_mode(gpu_chart, LV_CHART_UPDATE_MODE_SHIFT);
  gpu_series = lv_chart_add_series(gpu_chart, lv_palette_main(LV_PALETTE_GREEN), LV_CHART_AXIS_PRIMARY_Y);
  lv_chart_set_all_value(gpu_chart, gpu_series, LV_CHART_POINT_NONE);

  lv_obj_t *chart_title = lv_label_create(lv_scr_act());
  lv_label_set_text(chart_title, "GPU % HISTORY");
  lv_obj_set_style_text_color(chart_title, lv_color_hex(0x86efac), LV_PART_MAIN);
  lv_obj_align(chart_title, LV_ALIGN_TOP_LEFT, 28, 102);

  lv_obj_t *mute_button = lv_btn_create(lv_scr_act());
  lv_obj_set_size(mute_button, 125, 55);
  lv_obj_align(mute_button, LV_ALIGN_TOP_RIGHT, -20, 112);
  lv_obj_add_event_cb(mute_button, mute_button_event, LV_EVENT_ALL, NULL);
  mute_button_label = lv_label_create(mute_button);
  lv_obj_center(mute_button_label);
  update_mute_button();

  lv_obj_t *big_brother_button = lv_btn_create(lv_scr_act());
  lv_obj_set_size(big_brother_button, 125, 55);
  lv_obj_align(big_brother_button, LV_ALIGN_TOP_RIGHT, -20, 175);
  lv_obj_add_event_cb(big_brother_button, big_brother_button_event, LV_EVENT_ALL, NULL);
  big_brother_button_label = lv_label_create(big_brother_button);
  lv_obj_center(big_brother_button_label);
  update_big_brother_button();

  lv_obj_t *blank_button = lv_btn_create(lv_scr_act());
  lv_obj_set_size(blank_button, 125, 55);
  lv_obj_align(blank_button, LV_ALIGN_TOP_RIGHT, -20, 238);
  lv_obj_set_style_bg_color(blank_button, lv_palette_main(LV_PALETTE_GREY), LV_PART_MAIN);
  lv_obj_add_event_cb(blank_button, blank_button_event, LV_EVENT_ALL, NULL);
  blank_button_label = lv_label_create(blank_button);
  lv_label_set_text(blank_button_label, "BLANK ALL");
  lv_obj_center(blank_button_label);

  status_label = lv_label_create(lv_scr_act());
  lv_obj_align(status_label, LV_ALIGN_BOTTOM_MID, 0, -12);
  set_status("STARTING", lv_palette_main(LV_PALETTE_YELLOW));

  // Full-screen invisible overlay: shown only while blanked, catches a touch anywhere to wake everything.
  blank_overlay = lv_obj_create(lv_scr_act());
  lv_obj_set_size(blank_overlay, SCREEN_WIDTH, SCREEN_HEIGHT);
  lv_obj_set_pos(blank_overlay, 0, 0);
  lv_obj_set_style_bg_color(blank_overlay, lv_color_hex(0x000000), LV_PART_MAIN);
  lv_obj_set_style_bg_opa(blank_overlay, LV_OPA_COVER, LV_PART_MAIN);
  lv_obj_set_style_border_width(blank_overlay, 0, LV_PART_MAIN);
  lv_obj_set_style_radius(blank_overlay, 0, LV_PART_MAIN);
  lv_obj_clear_flag(blank_overlay, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_flag(blank_overlay, LV_OBJ_FLAG_HIDDEN);
  lv_obj_add_event_cb(blank_overlay, blank_overlay_event, LV_EVENT_ALL, NULL);
}

void setup() {
  Serial.begin(115200);
  my_lcd.init();
  my_lcd.setRotation(1);
  // Touch calibration for this panel at rotation 1; without it the axes map inverted.
  uint16_t calData[5] = { 254, 3643, 176, 3693, 7 };
  my_lcd.setTouch(calData);
  my_lcd.fillScreen(TFT_BLACK);

  lv_init();
  // Allocate the draw buffer on the heap; a static array here overflows the fixed DRAM segment.
  buffer = (lv_color_t *)heap_caps_malloc(SCREEN_WIDTH * DRAW_BUF_LINES * sizeof(lv_color_t), MALLOC_CAP_DMA);
  lv_disp_draw_buf_init(&draw_buf, buffer, NULL, SCREEN_WIDTH * DRAW_BUF_LINES);
  static lv_disp_drv_t display_driver;
  lv_disp_drv_init(&display_driver);
  display_driver.hor_res = SCREEN_WIDTH;
  display_driver.ver_res = SCREEN_HEIGHT;
  display_driver.flush_cb = display_flush;
  display_driver.draw_buf = &draw_buf;
  lv_disp_drv_register(&display_driver);

  static lv_indev_drv_t input_driver;
  lv_indev_drv_init(&input_driver);
  input_driver.type = LV_INDEV_TYPE_POINTER;
  input_driver.read_cb = touch_read;
  lv_indev_drv_register(&input_driver);
  make_dashboard();

  set_status("CONNECTING WIFI", lv_palette_main(LV_PALETTE_YELLOW));
  Serial.printf("Connecting to WiFi SSID '%s'...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t wifi_start_ms = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifi_start_ms < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi connected, IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.printf("WiFi connect failed, status=%d\n", WiFi.status());
  }
}

void loop() {
  lv_timer_handler();
  if (millis() - last_poll_ms >= POLL_INTERVAL_MS) {
    last_poll_ms = millis();
    refresh_dashboard();
  }
  delay(5);
}
