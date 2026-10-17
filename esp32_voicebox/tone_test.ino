// Luna Voice Box — pure tone test, no WiFi at all
//
// Generates a clean 440Hz sine wave entirely on-device and plays it in a
// loop. No network, no mic, no Piper audio -- there is no possible source
// of distortion in this signal. If it still sounds crunchy, the problem is
// purely in the amp/power/wiring, nothing upstream of it.
//
// Board setup required in Arduino IDE before flashing:
//   Tools > PSRAM > OPI PSRAM (not actually needed for this test, but leave
//   it enabled anyway)

#include <driver/i2s.h>
#include <math.h>

#define PIN_AMP_BCLK  5
#define PIN_AMP_LRC   6
#define PIN_AMP_DIN   7

#define I2S_PORT_AMP  I2S_NUM_1
#define SAMPLE_RATE   22050
#define TONE_HZ       440.0
#define VOLUME_PERCENT 40

int16_t sineTable[512];

void setupAmp() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
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

void setup() {
  Serial.begin(115200);
  delay(300);

  // Precompute one cycle of a sine wave, scaled to VOLUME_PERCENT of full
  // scale -- this table is mathematically perfect, no clipping possible.
  for (int i = 0; i < 512; i++) {
    float phase = (2.0 * PI * i) / 512.0;
    sineTable[i] = (int16_t)(sinf(phase) * 32767.0 * (VOLUME_PERCENT / 100.0));
  }

  setupAmp();
  Serial.println("Playing a clean 440Hz tone, no WiFi, no network, nothing upstream.");
}

void loop() {
  static int16_t stereoChunk[256 * 2];
  static double phaseAccum = 0.0;
  double step = (TONE_HZ * 512.0) / SAMPLE_RATE;

  for (int i = 0; i < 256; i++) {
    int idx = ((int)phaseAccum) % 512;
    int16_t sample = sineTable[idx];
    stereoChunk[i * 2] = sample;
    stereoChunk[i * 2 + 1] = sample;
    phaseAccum += step;
    if (phaseAccum >= 512.0) phaseAccum -= 512.0;
  }

  size_t bytesWritten = 0;
  i2s_write(I2S_PORT_AMP, stereoChunk, sizeof(stereoChunk), &bytesWritten, portMAX_DELAY);
}
