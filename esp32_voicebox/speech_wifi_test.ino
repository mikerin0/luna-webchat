// Luna Voice Box — same offline speech clip as speech_offline_test.ino,
// but with WiFi connected and idle in the background. Same audio, same
// playback code -- the only difference from the offline test is whether
// the WiFi radio is active, so any change in sound is attributable to that.
//
// Also lets you dial down WiFi transmit power with one line (TX_POWER
// below) to test whether that specifically reduces the noise.
//
// Needs secrets.h in this same sketch folder (WIFI_SSID, WIFI_PASSWORD only
// -- no token needed, this doesn't talk to Luna's server at all).
//
// Board setup required in Arduino IDE before flashing:
//   Tools > PSRAM > OPI PSRAM (not required for this test, but leave it on)

#include <WiFi.h>
#include <driver/i2s.h>
#include "sample_audio.h"
#include "secrets.h"

#define PIN_AMP_BCLK  5
#define PIN_AMP_LRC   6
#define PIN_AMP_DIN   7

#define I2S_PORT_AMP  I2S_NUM_1
#define VOLUME_PERCENT 40

// Try WIFI_POWER_19_5dBm (max, the default) first, then step down through
// WIFI_POWER_11dBm, WIFI_POWER_8_5dBm, WIFI_POWER_5dBm, WIFI_POWER_2dBm to
// see if lowering transmit power reduces the noise.
#define TX_POWER WIFI_POWER_19_5dBm

void setupAmp() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE_HZ,
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
    WiFi.setTxPower(TX_POWER);
    Serial.printf("TX power set, now reporting: %d (units of 0.25dBm)\n", WiFi.getTxPower());
  } else {
    Serial.printf("WiFi connect failed, status=%d\n", WiFi.status());
  }
}

void playSample() {
  int16_t stereoChunk[512 * 2];
  for (size_t i = 0; i < SAMPLE_COUNT; i += 512) {
    size_t chunkSamples = min((size_t)512, (size_t)(SAMPLE_COUNT - i));
    for (size_t j = 0; j < chunkSamples; j++) {
      int16_t sample = (int16_t)(((int32_t)SAMPLE_PCM[i + j] * VOLUME_PERCENT) / 100);
      stereoChunk[j * 2] = sample;
      stereoChunk[j * 2 + 1] = sample;
    }
    size_t bytesWritten = 0;
    i2s_write(I2S_PORT_AMP, stereoChunk, chunkSamples * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
  }
  i2s_zero_dma_buffer(I2S_PORT_AMP);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  setupAmp();
  connectWiFi();
  Serial.printf("Loaded %u samples at %u Hz (%.1fs). WiFi connected and idle.\n",
    (unsigned)SAMPLE_COUNT, (unsigned)SAMPLE_RATE_HZ, SAMPLE_COUNT / (float)SAMPLE_RATE_HZ);
}

void loop() {
  Serial.println("Playing...");
  playSample();
  Serial.println("Done.\n");
  delay(1000);
}
