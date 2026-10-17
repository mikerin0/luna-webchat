// Luna Voice Box — offline speech test, no WiFi at all
//
// Plays a real baked-in speech clip (same TTS/compression pipeline Luna
// actually uses) straight from flash, in a loop. No WiFi, no mic, no
// network -- this is the cleanest possible baseline for what the amp/
// speaker chain does with real speech content, independent of any
// WiFi-radio noise.
//
// Needs sample_audio.h in this same sketch folder.
//
// Board setup required in Arduino IDE before flashing:
//   Tools > PSRAM > OPI PSRAM (not required for this test, but leave it on)

#include <driver/i2s.h>
#include "sample_audio.h"

#define PIN_AMP_BCLK  5
#define PIN_AMP_LRC   6
#define PIN_AMP_DIN   7

#define I2S_PORT_AMP  I2S_NUM_1
#define VOLUME_PERCENT 40

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
  Serial.printf("Loaded %u samples at %u Hz (%.1fs). No WiFi, no network.\n",
    (unsigned)SAMPLE_COUNT, (unsigned)SAMPLE_RATE_HZ, SAMPLE_COUNT / (float)SAMPLE_RATE_HZ);
}

void loop() {
  Serial.println("Playing...");
  playSample();
  Serial.println("Done.\n");
  delay(1000);
}
