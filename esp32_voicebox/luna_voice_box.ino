// Luna Voice Box — push-to-talk + wake-word over WiFi
//
// Hold the button, talk, release it: records to a WAV, POSTs it to Luna
// (STT -> chat -> TTS on ai-host), and plays the spoken reply back through
// the speaker. Flip the toggle to arm wake-word mode instead: it keeps
// recording short chunks and asking the server if "Luna" was said in each
// one (no local keyword-spotting model -- just reuses the server's existing
// Whisper STT), and on a hit, listens for a command the same way a button
// press would. Button still works even while armed.
//
// Needs secrets.h in this same sketch folder (WIFI_SSID, WIFI_PASSWORD,
// LUNA_TOKEN -- same file used by the CYD dashboard sketch).
//
// Board setup required in Arduino IDE before flashing:
//   Tools > PSRAM > OPI PSRAM

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <string.h>
#include "secrets.h"

const char *LUNA_URL = "https://172.31.31.106:3010";

// Static IP so this device is always reachable at the same address.
// Make sure .107 is outside your router's DHCP pool, or confirm the
// gateway/subnet below actually match your network before flashing.
IPAddress staticIP(172, 31, 31, 107);
IPAddress gateway(172, 31, 31, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress dns(172, 31, 31, 1);

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

// How much audio to capture per wake-word check while armed. Short enough to
// feel responsive, long enough to usually catch "Luna" said at a normal pace.
#define WAKE_CHUNK_SECONDS 3
#define WAKE_SAMPLES  (SAMPLE_RATE * WAKE_CHUNK_SECONDS)

// INMP441 gain shift: raw I2S samples are 32-bit, mic data left-justified.
// >>14 is a common starting point; raise it if audio clips, lower it if quiet.
#define MIC_GAIN_SHIFT 14

// Piper's TTS output is compressed server-side already; this just gives a
// final trim before the amp. Percent of full digital scale, tuned by ear.
#define REPLY_VOLUME_PERCENT 20

int32_t *rawBuffer;    // raw 32-bit mic samples, from PSRAM
int16_t *pcmBuffer;    // converted 16-bit mono samples, from PSRAM

void setupMic() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
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
    .sample_rate = 22050, // reconfigured per-reply to whatever the WAV header reports
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
  WiFi.config(staticIP, gateway, subnet, dns);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi connected, IP: %s\n", WiFi.localIP().toString().c_str());
    WiFi.setSleep(true); // power-saving on by default -- only disabled
                          // briefly around actual playback (see sendAndPlay),
                          // since that's the only window where modem-sleep
                          // transitions were actually audible as noise.
                          // Leaving it off all the time drains the battery
                          // for no benefit while just sitting idle.
  } else {
    Serial.printf("WiFi connect failed, status=%d\n", WiFi.status());
  }
}

// Writes a standard 44-byte PCM WAV header for the given sample count.
void writeWavHeader(uint8_t *out, uint32_t numSamples, uint32_t sampleRate) {
  uint32_t dataBytes = numSamples * 2; // 16-bit mono
  uint32_t byteRate = sampleRate * 2;

  memcpy(out, "RIFF", 4);
  uint32_t riffLen = 36 + dataBytes;
  memcpy(out + 4, &riffLen, 4);
  memcpy(out + 8, "WAVE", 4);
  memcpy(out + 12, "fmt ", 4);
  uint32_t fmtLen = 16;
  memcpy(out + 16, &fmtLen, 4);
  uint16_t audioFormat = 1; // PCM
  memcpy(out + 20, &audioFormat, 2);
  uint16_t numChannels = 1;
  memcpy(out + 22, &numChannels, 2);
  memcpy(out + 24, &sampleRate, 4);
  memcpy(out + 28, &byteRate, 4);
  uint16_t blockAlign = 2;
  memcpy(out + 32, &blockAlign, 2);
  uint16_t bitsPerSample = 16;
  memcpy(out + 34, &bitsPerSample, 2);
  memcpy(out + 36, "data", 4);
  memcpy(out + 40, &dataBytes, 4);
}

// Records while the button is held, returns the number of samples captured.
size_t recordWhileHeld() {
  size_t samplesRecorded = 0;
  size_t bytesRead = 0;
  while (digitalRead(PIN_BUTTON) == LOW && samplesRecorded < MAX_SAMPLES) {
    size_t toRead = min((size_t)1024, (size_t)(MAX_SAMPLES - samplesRecorded));
    i2s_read(I2S_PORT_MIC, &rawBuffer[samplesRecorded], toRead * sizeof(int32_t), &bytesRead, portMAX_DELAY);
    samplesRecorded += bytesRead / sizeof(int32_t);
  }
  for (size_t i = 0; i < samplesRecorded; i++) {
    pcmBuffer[i] = (int16_t)(rawBuffer[i] >> MIC_GAIN_SHIFT);
  }
  return samplesRecorded;
}

// Records exactly numSamples, no button gating -- used for wake-word check
// chunks and for the command recording that follows a detected wake word.
size_t recordFixedSamples(size_t numSamples) {
  if (numSamples > MAX_SAMPLES) numSamples = MAX_SAMPLES;
  size_t samplesRecorded = 0;
  size_t bytesRead = 0;
  while (samplesRecorded < numSamples) {
    size_t toRead = min((size_t)1024, numSamples - samplesRecorded);
    i2s_read(I2S_PORT_MIC, &rawBuffer[samplesRecorded], toRead * sizeof(int32_t), &bytesRead, portMAX_DELAY);
    samplesRecorded += bytesRead / sizeof(int32_t);
  }
  for (size_t i = 0; i < samplesRecorded; i++) {
    pcmBuffer[i] = (int16_t)(rawBuffer[i] >> MIC_GAIN_SHIFT);
  }
  return samplesRecorded;
}

// Reads a little-endian uint32 from a byte buffer.
uint32_t readLE32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// Reads exactly `len` bytes, explicitly waiting for data to actually be
// available rather than trusting a single readBytes() call -- there can be
// a lag right after POST() returns before the body is readable on this
// connection. Returns bytes actually read (may be < len on timeout/EOF).
size_t waitAndRead(WiFiClient *stream, uint8_t *buf, size_t len, uint32_t timeoutMs) {
  size_t gotTotal = 0;
  uint32_t start = millis();
  while (gotTotal < len && millis() - start < timeoutMs) {
    if (stream->available() > 0) {
      int n = stream->read(buf + gotTotal, len - gotTotal);
      if (n > 0) {
        gotTotal += n;
        start = millis(); // reset the timeout on forward progress
      }
    } else if (!stream->connected() && stream->available() == 0) {
      break; // connection closed, nothing left to read
    } else {
      delay(5);
    }
  }
  return gotTotal;
}

// Builds a multipart/form-data body wrapping numSamples of pcmBuffer as a
// WAV file, ready to POST. Caller must free() the returned buffer. Shared by
// sendAndPlay() and checkWakeWord() so there's one copy of this, not two.
uint8_t *buildMultipartWav(size_t numSamples, size_t *outLen) {
  const size_t wavLen = 44 + numSamples * 2;
  uint8_t *wav = (uint8_t *)ps_malloc(wavLen);
  if (!wav) {
    Serial.println("Failed to allocate WAV buffer.");
    return NULL;
  }
  writeWavHeader(wav, numSamples, SAMPLE_RATE);
  memcpy(wav + 44, pcmBuffer, numSamples * 2);

  const char *boundary = "LunaVoiceBoxBoundary";
  String head = String("--") + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"file\"; filename=\"talk.wav\"\r\n"
    "Content-Type: audio/wav\r\n\r\n";
  String tail = String("\r\n--") + boundary + "--\r\n";
  size_t bodyLen = head.length() + wavLen + tail.length();

  uint8_t *body = (uint8_t *)ps_malloc(bodyLen);
  if (!body) {
    Serial.println("Failed to allocate upload buffer.");
    free(wav);
    return NULL;
  }
  size_t offset = 0;
  memcpy(body + offset, head.c_str(), head.length()); offset += head.length();
  memcpy(body + offset, wav, wavLen); offset += wavLen;
  memcpy(body + offset, tail.c_str(), tail.length());
  free(wav);

  *outLen = bodyLen;
  return body;
}

// Records a short chunk and asks Luna's server whether "Luna" was said in
// it. Returns true on a hit. Mirrors sendAndPlay()'s POST pattern but reads
// back a tiny JSON reply instead of a WAV.
bool checkWakeWord(size_t numSamples) {
  if (numSamples == 0) return false;

  size_t bodyLen = 0;
  uint8_t *body = buildMultipartWav(numSamples, &bodyLen);
  if (!body) return false;

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  if (!http.begin(client, String(LUNA_URL) + "/api/esp32/voicebox/wake_check")) {
    Serial.println("HTTP begin failed.");
    free(body);
    return false;
  }
  http.addHeader("Authorization", String("Bearer ") + LUNA_TOKEN);
  http.addHeader("Content-Type", "multipart/form-data; boundary=LunaVoiceBoxBoundary");
  http.setTimeout(15000);

  int code = http.POST(body, bodyLen);
  free(body);
  if (code != 200) {
    Serial.printf("Wake check failed: HTTP %d\n", code);
    http.end();
    return false;
  }

  String resp = http.getString();
  http.end();
  Serial.println(resp); // transcript visible for false-positive/negative tuning
  return resp.indexOf("\"detected\":true") >= 0;
}

// Sends the recorded WAV to Luna and plays the spoken reply back.
void sendAndPlay(size_t numSamples) {
  if (numSamples == 0) {
    Serial.println("Nothing recorded.");
    return;
  }

  size_t bodyLen = 0;
  uint8_t *body = buildMultipartWav(numSamples, &bodyLen);
  if (!body) return;

  const char *boundary = "LunaVoiceBoxBoundary";
  WiFiClientSecure client;
  client.setInsecure(); // local self-signed cert, same as the CYD dashboard
  HTTPClient http;
  if (!http.begin(client, String(LUNA_URL) + "/api/esp32/voicebox/talk")) {
    Serial.println("HTTP begin failed.");
    free(body);
    return;
  }
  http.addHeader("Authorization", String("Bearer ") + LUNA_TOKEN);
  http.addHeader("Content-Type", String("multipart/form-data; boundary=") + boundary);
  http.setTimeout(30000);

  Serial.println("Sending to Luna...");
  int code = http.POST(body, bodyLen);
  free(body);
  Serial.printf("HTTP code: %d, declared size: %d\n", code, http.getSize());

  if (code != 200) {
    Serial.printf("Voicebox request failed: HTTP %d\n", code);
    if (code > 0) {
      Serial.println(http.getString());
    }
    http.end();
    return;
  }

  // Read the response WAV header to get the actual sample rate, then
  // stream the rest straight into the amp.
  WiFiClient *stream = http.getStreamPtr();
  uint8_t header[44];
  size_t got = waitAndRead(stream, header, 44, 10000);
  Serial.printf("Read %u header bytes: ", (unsigned)got);
  for (size_t i = 0; i < got; i++) {
    Serial.printf("%02X ", header[i]);
  }
  Serial.println();
  if (got != 44 || memcmp(header, "RIFF", 4) != 0) {
    Serial.println("Reply doesn't look like a WAV file.");
    http.end();
    return;
  }
  uint32_t replyRate = readLE32(header + 24);
  uint32_t dataBytes = readLE32(header + 40);
  Serial.printf("Reply: %u Hz, %u bytes. Downloading...\n", replyRate, dataBytes);

  // Buffer the whole reply in PSRAM before playing any of it, so network
  // jitter during download can never starve the I2S write loop mid-sample
  // (that stall is what caused the crunchy/rough playback).
  uint8_t *replyBuf = (uint8_t *)ps_malloc(dataBytes);
  if (!replyBuf) {
    Serial.println("Failed to allocate reply buffer.");
    http.end();
    return;
  }
  size_t receivedBytes = waitAndRead(stream, replyBuf, dataBytes, 30000);
  http.end();
  if (receivedBytes < dataBytes) {
    Serial.printf("Reply incomplete: got %u of %u bytes.\n", (unsigned)receivedBytes, dataBytes);
  }

  Serial.println("Playing...");
  WiFi.setSleep(false); // only disable modem-sleep for this window -- it's
                         // the sole reason it was ever disabled in the first
                         // place (noise during playback), not needed outside it
  i2s_start(I2S_PORT_AMP); // wake the amp up only for the duration of playback
  i2s_set_sample_rates(I2S_PORT_AMP, replyRate);

  int16_t stereoChunk[512 * 2];
  size_t samplesTotal = receivedBytes / 2; // 16-bit mono samples
  for (size_t i = 0; i < samplesTotal; i += 512) {
    size_t chunkSamples = min((size_t)512, samplesTotal - i);
    for (size_t j = 0; j < chunkSamples; j++) {
      size_t byteIdx = (i + j) * 2;
      int16_t sample = (int16_t)(((int32_t)(int16_t)(replyBuf[byteIdx] | (replyBuf[byteIdx + 1] << 8)) * REPLY_VOLUME_PERCENT) / 100);
      stereoChunk[j * 2] = sample;
      stereoChunk[j * 2 + 1] = sample;
    }
    size_t bytesWritten = 0;
    i2s_write(I2S_PORT_AMP, stereoChunk, chunkSamples * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
  }
  i2s_zero_dma_buffer(I2S_PORT_AMP); // flush any trailing samples before going quiet
  i2s_stop(I2S_PORT_AMP);
  WiFi.setSleep(true); // back to power-saving now that playback is done

  free(replyBuf);
  Serial.println("Playback done.\n");
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_TOGGLE, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  rawBuffer = (int32_t *)ps_malloc(MAX_SAMPLES * sizeof(int32_t));
  pcmBuffer = (int16_t *)ps_malloc(MAX_SAMPLES * sizeof(int16_t));
  if (!rawBuffer || !pcmBuffer) {
    Serial.println("FAILED to allocate audio buffers in PSRAM.");
    Serial.println("Check Tools > PSRAM > OPI PSRAM is enabled, then re-flash.");
    while (true) { delay(1000); }
  }

  setupMic();
  setupAmp();
  i2s_stop(I2S_PORT_AMP); // idle the amp between replies so it isn't live
                          // and picking up WiFi-radio noise as ticking
  connectWiFi();

  Serial.println("Luna Voice Box ready.");
  Serial.println("Hold the button and talk (up to 5s), release to send it to Luna.");
  Serial.println("Or flip the toggle to arm wake-word listening for \"Luna\".");
}

void loop() {
  bool toggleOn = (digitalRead(PIN_TOGGLE) == LOW);
  digitalWrite(PIN_LED, toggleOn ? HIGH : LOW);

  if (digitalRead(PIN_BUTTON) == LOW) {
    // Push-to-talk, unchanged -- works whether the toggle is armed or not.
    Serial.println("Recording...");
    size_t samples = recordWhileHeld();
    Serial.printf("Captured %u samples (%.1fs).\n", (unsigned)samples, samples / (float)SAMPLE_RATE);
    sendAndPlay(samples);
  } else if (toggleOn) {
    // Armed: listen for "Luna" in short chunks, and on a hit, record a
    // command the same way a button press would. Each cycle tears down and
    // rebuilds a fresh TLS connection -- a deliberate pause between cycles
    // (on top of the 2s capture itself) keeps that churn from piling up
    // too fast on the chip's internal RAM over a long armed session.
    size_t n = recordFixedSamples(WAKE_SAMPLES);
    if (checkWakeWord(n)) {
      Serial.println("Wake word detected, listening for command...");
      for (int i = 0; i < 3; i++) {
        digitalWrite(PIN_LED, LOW);
        delay(80);
        digitalWrite(PIN_LED, HIGH);
        delay(80);
      }
      size_t cmdSamples = recordFixedSamples(MAX_SAMPLES);
      sendAndPlay(cmdSamples);
    }
    delay(3000);
  }

  delay(20);
}
