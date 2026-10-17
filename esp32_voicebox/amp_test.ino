// Luna Voice Box — amp/speaker test only
//
// No mic, no button, no recording. Connects to WiFi, fetches a short spoken
// sample from Luna every few seconds, and plays it on the amp. Just something
// to make noise repeatedly while you poke at wiring.
//
// Needs secrets.h in this same sketch folder (WIFI_SSID, WIFI_PASSWORD only —
// no token needed, this endpoint isn't auth-gated).
//
// Board setup required in Arduino IDE before flashing:
//   Tools > PSRAM > OPI PSRAM

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <string.h>
#include "secrets.h"

const char *LUNA_URL = "https://172.31.31.106:3010/api/voicebox/sample";

#define PIN_AMP_BCLK  5
#define PIN_AMP_LRC   6
#define PIN_AMP_DIN   7

#define I2S_PORT_AMP  I2S_NUM_1
#define REPLY_VOLUME_PERCENT 40

void setupAmp() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = 22050,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
  };
  i2s_pin_config_t pins = {
    .bck_io_num = PIN_AMP_BCLK,
    .ws_io_num = PIN_AMP_LRC,
    .data_out_num = PIN_AMP_DIN,
    .data_in_num = I2S_PIN_NO_CHANGE,
  };
  i2s_driver_install(I2S_PORT_AMP, &cfg, 0, NULL);
  i2s_set_pin(I2S_PORT_AMP, &pins);
  i2s_stop(I2S_PORT_AMP); // idle until we actually play something
}

void connectWiFi() {
  Serial.printf("Connecting to WiFi SSID '%s'...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi connected, IP: %s\n", WiFi.localIP().toString().c_str());
    WiFi.setSleep(false);
  } else {
    Serial.printf("WiFi connect failed, status=%d\n", WiFi.status());
  }
}

uint32_t readLE32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// Polls stream->available() with a timeout instead of trusting a single
// readBytes() call -- there can be a lag right after the response starts
// before the body is actually readable on this connection.
size_t waitAndRead(WiFiClient *stream, uint8_t *buf, size_t len, uint32_t timeoutMs) {
  size_t gotTotal = 0;
  uint32_t start = millis();
  while (gotTotal < len && millis() - start < timeoutMs) {
    if (stream->available() > 0) {
      int n = stream->read(buf + gotTotal, len - gotTotal);
      if (n > 0) {
        gotTotal += n;
        start = millis();
      }
    } else if (!stream->connected() && stream->available() == 0) {
      break;
    } else {
      delay(5);
    }
  }
  return gotTotal;
}

void fetchAndPlay() {
  WiFiClientSecure client;
  client.setInsecure(); // local self-signed cert
  HTTPClient http;
  if (!http.begin(client, LUNA_URL)) {
    Serial.println("HTTP begin failed.");
    return;
  }
  http.setTimeout(30000);

  Serial.println("Fetching sample...");
  int code = http.GET();
  Serial.printf("HTTP code: %d\n", code);
  if (code != 200) {
    http.end();
    return;
  }

  WiFiClient *stream = http.getStreamPtr();
  uint8_t header[44];
  size_t got = waitAndRead(stream, header, 44, 10000);
  if (got != 44 || memcmp(header, "RIFF", 4) != 0) {
    Serial.println("Doesn't look like a WAV file.");
    http.end();
    return;
  }
  uint32_t rate = readLE32(header + 24);
  uint32_t dataBytes = readLE32(header + 40);
  Serial.printf("Sample: %u Hz, %u bytes.\n", rate, dataBytes);

  uint8_t *buf = (uint8_t *)ps_malloc(dataBytes);
  if (!buf) {
    Serial.println("Failed to allocate buffer.");
    http.end();
    return;
  }
  size_t received = waitAndRead(stream, buf, dataBytes, 30000);
  http.end();
  if (received < dataBytes) {
    Serial.printf("Incomplete: got %u of %u bytes.\n", (unsigned)received, dataBytes);
  }

  Serial.println("Playing...");
  i2s_start(I2S_PORT_AMP);
  i2s_set_sample_rates(I2S_PORT_AMP, rate);

  int16_t stereoChunk[512 * 2];
  size_t samplesTotal = received / 2; // 16-bit mono samples
  for (size_t i = 0; i < samplesTotal; i += 512) {
    size_t chunkSamples = min((size_t)512, samplesTotal - i);
    for (size_t j = 0; j < chunkSamples; j++) {
      size_t byteIdx = (i + j) * 2;
      int16_t sample = (int16_t)(((int32_t)(int16_t)(buf[byteIdx] | (buf[byteIdx + 1] << 8)) * REPLY_VOLUME_PERCENT) / 100);
      stereoChunk[j * 2] = sample;
      stereoChunk[j * 2 + 1] = sample;
    }
    size_t bytesWritten = 0;
    i2s_write(I2S_PORT_AMP, stereoChunk, chunkSamples * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
  }
  i2s_zero_dma_buffer(I2S_PORT_AMP);
  i2s_stop(I2S_PORT_AMP);

  free(buf);
  Serial.println("Done.\n");
}

void setup() {
  Serial.begin(115200);
  delay(300);
  setupAmp();
  connectWiFi();
  Serial.println("Amp test ready. Will fetch and play a sample every few seconds.");
}

void loop() {
  fetchAndPlay();
  delay(500);
}
