// Luna Voice Box — hardware bring-up / loopback test
//
// Hold the PTT button and talk, release it, and it plays your voice back
// through the speaker. Flip the toggle and the status LED follows it.
// This only exercises the wired hardware locally — no WiFi/server yet,
// that comes once the battery circuit and the ai-host side are ready.
//
// Board setup required in Arduino IDE before flashing:
//   Tools > PSRAM > OPI PSRAM   (the recording buffer lives in PSRAM —
//   this board's 8MB won't be usable otherwise, and the sketch will
//   fail to allocate the buffer without this enabled)

#include <driver/i2s.h>

// ---- Pins, matching the wiring diagram ----
#define PIN_MIC_SCK   1
#define PIN_MIC_WS    2
#define PIN_MIC_SD    4

#define PIN_AMP_BCLK  5
#define PIN_AMP_LRC   6
#define PIN_AMP_DIN   7

#define PIN_BUTTON    41   // push-to-talk — internal pull-up, active LOW
#define PIN_TOGGLE    42   // wake-word arm switch — internal pull-up, active LOW
#define PIN_LED       40   // status LED

#define I2S_PORT_MIC  I2S_NUM_0
#define I2S_PORT_AMP  I2S_NUM_1
#define SAMPLE_RATE   16000
#define RECORD_SECONDS 5
#define MAX_SAMPLES   (SAMPLE_RATE * RECORD_SECONDS)

// INMP441 gain shift: raw I2S samples are 32-bit, mic data left-justified.
// >>14 is a common starting point; raise it (e.g. >>15) if audio clips/distorts,
// lower it (e.g. >>12) if playback sounds too quiet.
#define MIC_GAIN_SHIFT 14

int32_t *audioBuffer;  // allocated from PSRAM in setup()

void setupMic() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,   // mic's L/R pin is tied to GND = left
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
  };
  i2s_pin_config_t pins = {
    .bck_io_num = PIN_MIC_SCK,
    .ws_io_num = PIN_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = PIN_MIC_SD,
  };
  i2s_driver_install(I2S_PORT_MIC, &cfg, 0, NULL);
  i2s_set_pin(I2S_PORT_MIC, &pins);
}

void setupAmp() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,  // stereo frame; we duplicate mono into both slots below
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

  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_TOGGLE, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  audioBuffer = (int32_t *)ps_malloc(MAX_SAMPLES * sizeof(int32_t));
  if (audioBuffer == NULL) {
    Serial.println("FAILED to allocate recording buffer in PSRAM.");
    Serial.println("Check Tools > PSRAM > OPI PSRAM is enabled, then re-flash.");
    while (true) { delay(1000); }
  }

  setupMic();
  setupAmp();

  Serial.println("Luna Voice Box -- hardware test ready.");
  Serial.println("Hold the button and talk (up to 5s), release to hear it played back.");
}

void loop() {
  // Toggle -> LED, direct passthrough
  bool toggleOn = (digitalRead(PIN_TOGGLE) == LOW);
  digitalWrite(PIN_LED, toggleOn ? HIGH : LOW);

  // Button -> record while held, play back on release
  if (digitalRead(PIN_BUTTON) == LOW) {
    Serial.println("Recording...");
    size_t samplesRecorded = 0;
    size_t bytesRead = 0;

    while (digitalRead(PIN_BUTTON) == LOW && samplesRecorded < MAX_SAMPLES) {
      size_t toRead = min((size_t)1024, (size_t)(MAX_SAMPLES - samplesRecorded));
      i2s_read(I2S_PORT_MIC, &audioBuffer[samplesRecorded], toRead * sizeof(int32_t), &bytesRead, portMAX_DELAY);
      samplesRecorded += bytesRead / sizeof(int32_t);
    }

    Serial.printf("Captured %u samples (%.1fs). Playing back...\n",
                   (unsigned)samplesRecorded, samplesRecorded / (float)SAMPLE_RATE);

    int16_t playBuffer[1024 * 2]; // stereo, interleaved L/R
    size_t bytesWritten = 0;
    for (size_t i = 0; i < samplesRecorded; i += 1024) {
      size_t chunk = min((size_t)1024, samplesRecorded - i);
      for (size_t j = 0; j < chunk; j++) {
        int16_t sample = (int16_t)(audioBuffer[i + j] >> MIC_GAIN_SHIFT);
        playBuffer[j * 2]     = sample; // left
        playBuffer[j * 2 + 1] = sample; // right
      }
      i2s_write(I2S_PORT_AMP, playBuffer, chunk * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
    }

    Serial.println("Playback done.\n");
  }

  delay(20);
}
