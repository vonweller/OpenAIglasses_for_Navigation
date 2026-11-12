// ===== all_in_one_merged.ino — XIAO ESP32S3 Sense: Camera + Mic (PDM) + IMU (ICM42688 SPI) =====
// ===== 版本: v2.6-SPIIMU - 关闭喇叭播放（硬件损坏）；相机发送不阻塞缓冲 =====
// SPEAKER_ENABLED=0 时：不初始化 I2S TX，也不拉 /stream.wav，音频只在服务端播放
#define SPEAKER_ENABLED 0

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_camera.h>
#include <ArduinoWebsockets.h>
#include "ESP_I2S.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
struct WavFmt;
#include <cstring>      // memcmp
#include <WiFiUdp.h>
#include <WiFiClient.h> 
#include <SPI.h>        // ICM42688 使用 SPI
#include <esp_heap_caps.h>
using namespace websockets;

// ===== WiFi / Server =====
const char* WIFI_SSID   = "LYT";
const char* WIFI_PASS   = "lyt13509018386";
// ===== WiFi / Server =====
// 【合并-自动发现】默认 "auto"：连上 WiFi 后广播 AIGLASS_DISCOVER 到 255.255.255.255:12345，
// 后端应答 AIGLASS_SERVER:<port>，取应答包源 IP 作为服务器地址。
// 换 WiFi / 服务器 IP 变化无需改代码；需要写死时填 "192.168.x.x" 即可。
String SERVER_HOST = "auto";
const uint16_t SERVER_PORT = 8081;
volatile bool server_resolved = false;

static const char* CAM_WS_PATH = "/ws/camera";
static const char* AUD_WS_PATH = "/ws_audio";

// ===== Camera config =====
#define CAMERA_MODEL_XIAO_ESP32S3
#include "camera_pins.h"

framesize_t g_frame_size = FRAMESIZE_VGA;
uint16_t g_sensor_pid = 0;   // 【诊断】传感器型号（OV2640=0x26 / OV3660=0x3660 / OV5640=0x5640）
int g_fb_count_live = 0;     // 【修复】实际生效的 fb_count（降级后可能与编译配置不同）
#define JPEG_QUALITY  17
#define JPEG_QUALITY  12
#define FB_COUNT      2
volatile int g_target_fps = 0; // 0=不限速，>0 时按指定 FPS 限速发送
volatile int g_jpeg_quality = JPEG_QUALITY;

// 视频传输性能监控
volatile unsigned long frame_captured_count = 0;  // 采集帧计数
volatile unsigned long frame_sent_count = 0;      // 发送帧计数
volatile unsigned long frame_dropped_count = 0;   // 丢弃帧计数
volatile unsigned long last_stats_time = 0;       // 上次统计时间
volatile unsigned long ws_send_fail_count = 0;    // WebSocket 发送失败计数
volatile unsigned long ws_cam_opened_at = 0;      // 【修复】相机WS打开时刻（半开连接看门狗用）
volatile unsigned long send_time_sum_ms = 0;
volatile unsigned long send_time_samples = 0;
volatile size_t last_jpeg_size = 0;
unsigned long stats_prev_captured = 0;
unsigned long stats_prev_sent = 0;
unsigned long stats_prev_dropped = 0;
unsigned long stats_prev_send_sum = 0;
unsigned long stats_prev_send_samples = 0;

// 各通道独立重连，不使用阻塞 delay。
unsigned long wifi_next_retry_at = 0;
unsigned long cam_next_retry_at = 0;
unsigned long aud_next_retry_at = 0;
unsigned long wifi_retry_ms = 1000;
unsigned long cam_retry_ms = 800;
unsigned long aud_retry_ms = 1200;

// ===== Mic (PDM RX) =====
#define I2S_MIC_CLOCK_PIN 42
#define I2S_MIC_DATA_PIN  41
const int SAMPLE_RATE     = 16000; 
const int CHUNK_MS        = 20;
const int BYTES_PER_CHUNK = SAMPLE_RATE * CHUNK_MS / 1000 * 2;
const int AUDIO_QUEUE_DEPTH = 10;

// ===== Speaker (I2S TX → MAX98357A) =====
#define I2S_SPK_BCLK 7
#define I2S_SPK_LRCK 8
#define I2S_SPK_DIN  9
const int TTS_RATE = 16000;

// ===== IMU (ICM42688 over SPI) / UDP =====
// 使用 D0~D3 作为 SPI
#define IMU_SPI_SCK   1   // D0
#define IMU_SPI_MOSI  2   // D1
#define IMU_SPI_MISO  3   // D2
#define IMU_SPI_CS    4   // D3
// IMU 数据发送到同一台后端服务器（自动发现后取 SERVER_HOST，见 taskImuLoop）
const int   UDP_PORT  = 12345;

WiFiUDP udp;

// ===== WS / Queues / I2S =====
WebsocketsClient wsCam;
WebsocketsClient wsAud;
volatile bool cam_ws_ready = false;
volatile bool aud_ws_ready = false;
volatile bool snapshot_in_progress = false; // 抓拍期间暂停实时采集

typedef camera_fb_t* fb_ptr_t;
QueueHandle_t qFrames;

typedef struct {
  size_t n;
  uint8_t data[BYTES_PER_CHUNK];
} AudioChunk;
QueueHandle_t qAudio;

// TTS 分片通道默认不启用；队列太深会吃掉约 90KB 内部堆，压缩到 4
#define TTS_QUEUE_DEPTH 4
typedef struct { uint16_t n; uint8_t data[2048]; } TTSChunk;
QueueHandle_t qTTS;
volatile bool tts_playing = false;

I2SClass i2sIn;   // PDM RX (Mic)
I2SClass i2sOut;  // STD TX (Speaker)
volatile bool run_audio_stream = false;

// ====================================================================
// Camera
// ====================================================================
bool init_camera() {
  // 【修复】OV2640 帧缓冲分配降级阶梯：优先 PSRAM 双缓冲，失败逐级降级
  struct TryCfg { framesize_t fs; int fb; bool psram; int xclk; } tries[] = {
    { FRAMESIZE_VGA, 2, true,  20 },
    { FRAMESIZE_VGA, 1, true,  20 },
    { FRAMESIZE_VGA, 1, true,  16 },
    { FRAMESIZE_QVGA, 1, true, 20 },
    { FRAMESIZE_QVGA, 1, false, 20 },   // 最后兜底：内部RAM + QVGA，保底有画面
  };
  for (auto& c : tries) {
    if (_init_camera_with(c.fs, c.fb, c.psram, c.xclk)) {
      g_frame_size = c.fs;
      g_fb_count_live = c.fb;
      Serial.printf("[CAM] 生效配置: fs=%d fb=%d psram=%d\n", (int)c.fs, c.fb, c.psram?1:0);
      return true;
    }
  }
  Serial.println("[CAM] 所有配置均失败!");
  return false;
}

bool apply_framesize(framesize_t fs) {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return false;
  int r = s->set_framesize(s, fs);
  if (r == 0) { g_frame_size = fs; return true; }
  return false;
}

bool _init_camera_with(framesize_t fs, int fb_count, bool in_psram, int xclk_mhz) {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM; config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM; config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM; config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM; config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM; config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM; config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn  = PWDN_GPIO_NUM; config.pin_reset = RESET_GPIO_NUM;

  config.xclk_freq_hz = xclk_mhz * 1000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = fs;
  config.jpeg_quality = JPEG_QUALITY;
  config.fb_count     = fb_count;
  config.fb_location  = in_psram ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.grab_mode    = CAMERA_GRAB_LATEST;

  Serial.printf("[CAM] try fs=%d fb=%d psram=%d xclk=%dM ...", (int)fs, fb_count, in_psram ? 1 : 0, xclk_mhz);
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) { Serial.printf(" FAILED 0x%x\n", err); return false; }
  Serial.println(" OK");
  // 校验帧缓冲确实拿到了（esp_camera_init 成功但 fb 分配异常时 fb_get 会一直 NULL）
  {
    camera_fb_t* probe = esp_camera_fb_get();
    if (!probe) { Serial.println("[CAM] init OK 但 fb_get=NULL，弃用该配置"); esp_camera_deinit(); return false; }
    Serial.printf("[CAM] probe帧 %u bytes, PSRAM=%u/free=%u\n", probe->len,
                  (unsigned)ESP.getPsramSize(), (unsigned)ESP.getFreePsram());
    esp_camera_fb_return(probe);
  }

  delay(200);
  sensor_t * s = esp_camera_sensor_get();
  if (s) {
    g_sensor_pid = s->id.PID;
    Serial.printf("[CAM] sensor PID=0x%04x MIDH=0x%02x MIDL=0x%02x VER=0x%02x PSRAM=%u bytes free=%u\n",
                  s->id.PID, s->id.MIDH, s->id.MIDL, s->id.VER,
                  (unsigned)ESP.getPsramSize(), (unsigned)ESP.getFreePsram());
    if (ESP.getPsramSize() == 0) {
      Serial.println("[CAM] !!! PSRAM 未启用：请用 PSRAM=OPI 选项编译烧录，否则 VGA 帧缓冲分配失败、无画面 !!!");
    }

    s->set_hmirror(s, 1);
    s->set_vflip(s, 0);
    s->set_brightness(s, 0);
    s->set_contrast(s, 0);
    s->set_saturation(s, 0);
    s->set_gain_ctrl(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_aec2(s, 1);
    if (s->set_lenc) s->set_lenc(s, 1);
    if (s->set_wpc) s->set_wpc(s, 1);
    if (s->set_bpc) s->set_bpc(s, 1);
    if (s->set_raw_gma) s->set_raw_gma(s, 1);
    if (s->set_dcw) s->set_dcw(s, 1);

    if (s->id.PID == OV3660_PID) {
      if (s->set_ae_level) s->set_ae_level(s, 1);
    } else if (s->id.PID == OV5640_PID) {
      if (s->set_ae_level) s->set_ae_level(s, 0);
    }
  }

  return true;
}

inline void enqueue_frame(camera_fb_t* fb) {
  if (!fb) return;
  if (xQueueSend(qFrames, &fb, 0) != pdPASS) {
    // 队列长度为 1：拥塞时释放旧帧，只保留最新画面。
    fb_ptr_t drop = nullptr;
    if (xQueueReceive(qFrames, &drop, 0) == pdPASS) {
      if (drop) {
        esp_camera_fb_return(drop);
        frame_dropped_count++;  // 统计丢帧
      }
    }
    xQueueSend(qFrames, &fb, 0);
  }
}

void clear_frame_queue() {
  if (!qFrames) return;
  fb_ptr_t pending = nullptr;
  while (xQueueReceive(qFrames, &pending, 0) == pdPASS) {
    if (pending) esp_camera_fb_return(pending);
    pending = nullptr;
  }
}

void send_camera_stats_if_due() {
  if (!cam_ws_ready) return;
  unsigned long now = millis();
  unsigned long elapsed = now - last_stats_time;
  if (elapsed < 2000) return;

  unsigned long cap_now = frame_captured_count;
  unsigned long sent_now = frame_sent_count;
  unsigned long drop_now = frame_dropped_count;
  unsigned long send_sum_now = send_time_sum_ms;
  unsigned long send_samples_now = send_time_samples;
  float seconds = max(0.001f, elapsed / 1000.0f);
  float capture_fps = (cap_now - stats_prev_captured) / seconds;
  float send_fps = (sent_now - stats_prev_sent) / seconds;
  unsigned long sample_delta = send_samples_now - stats_prev_send_samples;
  float avg_send_ms = sample_delta > 0
    ? (float)(send_sum_now - stats_prev_send_sum) / (float)sample_delta
    : 0.0f;

  char payload[320];
  snprintf(
    payload,
    sizeof(payload),
    "STAT:{\"capture_fps\":%.1f,\"send_fps\":%.1f,\"dropped\":%lu,"
    "\"send_ms\":%.1f,\"jpeg_bytes\":%u,\"rssi\":%d,\"free_heap\":%u,"
    "\"target_fps\":%d,\"framesize\":%d,\"jpeg_quality\":%d}",
    capture_fps,
    send_fps,
    drop_now,
    avg_send_ms,
    (unsigned int)last_jpeg_size,
    WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127,
    (unsigned int)ESP.getFreeHeap(),
    g_target_fps,
    (int)g_frame_size,
    g_jpeg_quality
  );
  wsCam.send(payload);

  stats_prev_captured = cap_now;
  stats_prev_sent = sent_now;
  stats_prev_dropped = drop_now;
  stats_prev_send_sum = send_sum_now;
  stats_prev_send_samples = send_samples_now;
  last_stats_time = now;
}

void taskCamCapture(void*) {
  unsigned long last_log = 0;
  unsigned long capture_fail_count = 0;
  uint32_t consec_fb_fail = 0;   // 【修复】连续采集失败计数（自愈用）
  
  bool cam_ok = (g_fb_count_live > 0);
  for(;;){
    if (snapshot_in_progress) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
    
    if (!cam_ok) {   // 【修复】初始化失败的后台重试
      vTaskDelay(pdMS_TO_TICKS(5000));
      cam_ok = init_camera();
      continue;
    }
    if (cam_ws_ready) {
      camera_fb_t* fb = esp_camera_fb_get();
      if (fb) {
        consec_fb_fail = 0;
        frame_captured_count++;
        if (fb->format != PIXFORMAT_JPEG) { 
          esp_camera_fb_return(fb);
          capture_fail_count++;
        }
        else { 
          enqueue_frame(fb);
        }
      } else {
        capture_fail_count++;
        // 【修复】esp_camera_fb_get 连续失败（PSRAM/传感器异常）时自愈：
        // 清空帧队列 -> 反初始化 -> 重新初始化摄像头
        if (++consec_fb_fail >= 300) {
          consec_fb_fail = 0;
          Serial.println("[CAM] fb_get 连续失败，重新初始化摄像头...");
          fb_ptr_t d = nullptr;
          while (xQueueReceive(qFrames, &d, 0) == pdPASS) { if (d) esp_camera_fb_return(d); }
          esp_camera_deinit();
          cam_ok = init_camera();   // 失败则进入后台重试节奏
          vTaskDelay(pdMS_TO_TICKS(100));
        }
        vTaskDelay(pdMS_TO_TICKS(2));
      }
      
      // 每 5 秒打印一次采集统计
      unsigned long now = millis();
      if (now - last_log > 5000) {
        int queue_waiting = uxQueueMessagesWaiting(qFrames);
        Serial.printf("[CAM-CAP] captured=%lu, queue=%d, fail=%lu\n", 
                      frame_captured_count, queue_waiting, capture_fail_count);
        last_log = now;
        capture_fail_count = 0;  // 重置失败计数
      }
    } else {
      vTaskDelay(pdMS_TO_TICKS(20));
    }
  }
}

// JPEG 发送拷贝缓冲：发送期间不再占用相机帧缓冲（VGA 只有 2 块，占住会导致采集饿死）
static uint8_t* s_jpeg_copy = nullptr;
static size_t   s_jpeg_copy_cap = 0;
static unsigned long s_last_send_ms = 0;
const size_t JPEG_COPY_MAX = 256 * 1024;

static bool jpeg_copy_prepare(size_t n) {
  if (n == 0 || n > JPEG_COPY_MAX) return false;
  if (s_jpeg_copy && s_jpeg_copy_cap >= n) return true;
  size_t cap = n < 24 * 1024 ? 24 * 1024 : n;
  uint8_t* p = (uint8_t*)heap_caps_realloc(s_jpeg_copy, cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) p = (uint8_t*)heap_caps_realloc(s_jpeg_copy, cap, MALLOC_CAP_8BIT);
  if (!p) return false;
  s_jpeg_copy = p;
  s_jpeg_copy_cap = cap;
  return true;
}

void taskCamSend(void*) {
  static TickType_t lastTick = 0;
  unsigned long last_log = 0;
  unsigned long send_timeout_count = 0;
  unsigned long last_sent_time = 0;
  
  for(;;){
    fb_ptr_t fb = nullptr;
    if (xQueueReceive(qFrames, &fb, pdMS_TO_TICKS(100)) == pdPASS) {
      if (fb && cam_ws_ready) {
        const size_t jpeg_len = fb->len;
        last_jpeg_size = jpeg_len;

        // 上一帧发送耗时过长说明链路拥塞：丢弃本帧，优先把缓冲还给相机。
        // 【合并调优】OV2640+ArduinoWebsockets 整帧同步发送常态 130~250ms，
        // 原阈值 150ms 会把几乎每帧都丢掉（实测 dropped 1300+）；放宽到 400ms。
        if (s_last_send_ms >= 400) {
          esp_camera_fb_return(fb);
          frame_dropped_count++;
          s_last_send_ms = 150; // 下一帧再试，避免连续丢
          continue;
        }

        if (!jpeg_copy_prepare(jpeg_len)) {
          Serial.printf("[CAM-SEND] copy alloc failed size=%u heap=%u\n",
                        (unsigned)jpeg_len, (unsigned)ESP.getFreeHeap());
          esp_camera_fb_return(fb);
          frame_dropped_count++;
          continue;
        }
        memcpy(s_jpeg_copy, fb->buf, jpeg_len);
        esp_camera_fb_return(fb);
        fb = nullptr;

        // 发送节流放在归还相机缓冲之后，避免双缓冲被限速占住
        if (g_target_fps > 0) {
          const int period_ms = 1000 / g_target_fps;
          TickType_t now = xTaskGetTickCount();
          int elapsed = (now - lastTick) * portTICK_PERIOD_MS;
          if (elapsed < period_ms) vTaskDelay(pdMS_TO_TICKS(period_ms - elapsed));
          lastTick = xTaskGetTickCount();
        }
        
        unsigned long send_start = millis();
        bool ok = wsCam.sendBinary((const char*)s_jpeg_copy, jpeg_len);
        unsigned long send_time = millis() - send_start;
        s_last_send_ms = send_time;
        send_time_sum_ms += send_time;
        send_time_samples++;
        
        if (ok) {
          frame_sent_count++;
          last_sent_time = millis();
          
          // 监控发送耗时
          if (send_time > 100) {
            Serial.printf("[CAM-SEND] WARNING: send took %lu ms (size=%u)\n", send_time, (unsigned)jpeg_len);
          }
        } else {
          ws_send_fail_count++;
          Serial.println("[CAM-SEND] ERROR: WebSocket send failed, closing...");
          wsCam.close(); 
          cam_ws_ready = false;
          continue;
        }
        
        send_camera_stats_if_due();
        
        // 每 5 秒打印一次发送统计
        unsigned long now = millis();
        if (now - last_log > 5000) {
          unsigned long gap = now - last_sent_time;
          Serial.printf("[CAM-SEND] sent=%lu, dropped=%lu, ws_fail=%lu, last_gap=%lu ms, last_send=%lu ms\n", 
                        frame_sent_count, frame_dropped_count, ws_send_fail_count, gap, s_last_send_ms);
          last_log = now;
        }
        
      } else if (fb) { 
        esp_camera_fb_return(fb); 
      }
    } else {
      // 队列接收超时：上报遥测 + 检查是否长时间没有帧
      unsigned long now = millis();
      if (now - last_log > 5000) {
        last_log = now;
        if (cam_ws_ready) {
          // 【修复】STAT 遥测：服务端日志直接可见采集/发送计数，远程即可判断眼镜端状态
          char stat[192];
          snprintf(stat, sizeof(stat),
                   "STAT:{\"captured\":%lu,\"sent\":%lu,\"dropped\":%lu,\"ws_fail\":%lu,\"free_heap\":%u,\"target_fps\":%d,\"psram\":%u,\"psram_free\":%u,\"pid\":%04x}",
                   frame_captured_count, frame_sent_count, frame_dropped_count, ws_send_fail_count,
                   (unsigned)ESP.getFreeHeap(), g_target_fps,
                   (unsigned)ESP.getPsramSize(), (unsigned)ESP.getFreePsram(), g_sensor_pid);
          wsCam.send(stat);
        }
        Serial.printf("[CAM-SEND] sent=%lu, dropped=%lu, ws_fail=%lu, captured=%lu" "\n",
                      frame_sent_count, frame_dropped_count, ws_send_fail_count, frame_captured_count);
      }
      if (cam_ws_ready && last_sent_time > 0 && (now - last_sent_time) > 3000) {
        Serial.printf("[CAM-SEND] WARNING: No frame sent for %lu ms\n", now - last_sent_time);
        send_timeout_count++;
      }
      // 【修复】半开连接看门狗：连接建立 15s 且一帧未发，主动断开触发重连
      if (cam_ws_ready && frame_sent_count == 0 && ws_cam_opened_at > 0 &&
          (millis() - ws_cam_opened_at) > 15000) {
        Serial.println("[CAM-SEND] watchdog: 15s未发出任何帧，断开重连");
        wsCam.close();
      }
    }
  }
}
// ====================================================================
// Mic (PDM RX)
// ====================================================================
void init_i2s_in(){
  i2sIn.setPinsPdmRx(I2S_MIC_CLOCK_PIN, I2S_MIC_DATA_PIN);
  if (!i2sIn.begin(I2S_MODE_PDM_RX, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("[I2S IN] init failed");
    while(1) { delay(1000); }
  }
  Serial.println("[I2S IN] PDM RX @16kHz 16bit MONO ready");
}

void taskMicCapture(void*){
  const int samples_per_chunk = BYTES_PER_CHUNK / 2; // int16
  for(;;){
    if (run_audio_stream && aud_ws_ready) {
      AudioChunk ch; ch.n = BYTES_PER_CHUNK;
      int16_t* out = reinterpret_cast<int16_t*>(ch.data);
      int i = 0;
      while (i < samples_per_chunk){
        int v = i2sIn.read();
        if (v == -1) { delay(1); continue; }
        out[i++] = (int16_t)v;
      }
      if (xQueueSend(qAudio, &ch, 0) != pdPASS){
        AudioChunk dump;
        xQueueReceive(qAudio, &dump, 0);
        xQueueSend(qAudio, &ch, 0);
      }
    } else {
      vTaskDelay(pdMS_TO_TICKS(5));
    }
  }
}

void taskMicUpload(void*){
  for(;;){
    if (run_audio_stream && aud_ws_ready){
      AudioChunk ch;
      if (xQueueReceive(qAudio, &ch, pdMS_TO_TICKS(100)) == pdPASS){
        wsAud.sendBinary((const char*)ch.data, ch.n);
      }
    } else {
      vTaskDelay(pdMS_TO_TICKS(10));
    }
  }
}

// ====================================================================
// Speaker (I2S TX) + HTTP /stream.wav (chunked-safe)
// ====================================================================
void init_i2s_out(){
  i2sOut.setPins(I2S_SPK_BCLK, I2S_SPK_LRCK, I2S_SPK_DIN);
  if (!i2sOut.begin(I2S_MODE_STD, TTS_RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO)) {
    Serial.println("[I2S OUT] init failed");
    while(1){ delay(1000); }
  }
  Serial.println("[I2S OUT] STD TX @16kHz 32bit STEREO ready");
}

struct WavFmt {
  uint16_t audioFormat;   // 1=PCM
  uint16_t numChannels;   // 1=mono
  uint32_t sampleRate;    // 16000
  uint32_t byteRate;
  uint16_t blockAlign;
  uint16_t bitsPerSample; // 16
};

static inline void mono16_to_stereo32_msb(const int16_t* in, size_t nSamp, int32_t* outLR, float gain = 0.7f) {
  for (size_t i = 0; i < nSamp; ++i) {
    int32_t s = (int32_t)((float)in[i] * gain);
    int32_t v32 = s << 16;
    outLR[i*2 + 0] = v32;
    outLR[i*2 + 1] = v32;
  }
}

// === chunked 读取辅助 ===
static bool read_line(WiFiClient& cli, String& line, uint32_t timeout_ms=3000){
  line = "";
  uint32_t t0 = millis();
  while (millis() - t0 < timeout_ms){
    while (cli.available()){
      char ch = (char)cli.read();
      if (ch == '\n'){
        if (line.endsWith("\r")) line.remove(line.length()-1);
        return true;
      }
      line += ch;
    }
    delay(1);
  }
  return false;
}

static bool readN_http_body(WiFiClient& cli, uint8_t* buf, size_t n, bool chunked, size_t& chunk_left, uint32_t timeout_ms=3000){
  size_t got = 0;
  uint32_t t0 = millis();

  while (got < n){
    if (!cli.connected()) return false;
    if (!chunked){
      int avail = cli.available();
      if (avail > 0){
        int toread = (int)min((size_t)avail, n - got);
        int r = cli.read(buf + got, toread);
        if (r > 0) got += r;
      } else {
        if (millis() - t0 > timeout_ms) return false;
        delay(1);
      }
    } else {
      if (chunk_left == 0){
        String szline;
        if (!read_line(cli, szline, timeout_ms)) return false;
        int sc = szline.indexOf(';');
        if (sc >= 0) szline = szline.substring(0, sc);
        szline.trim();
        unsigned long sz = strtoul(szline.c_str(), nullptr, 16);
        if (sz == 0){
          String dummy;
          read_line(cli, dummy, 500);
          return false;
        }
        chunk_left = (size_t)sz;
      }
      int avail = cli.available();
      if (avail > 0){
        size_t want = min(n - got, chunk_left);
        int toread = (int)min((size_t)avail, want);
        int r = cli.read(buf + got, toread);
        if (r > 0){
          got += r;
          chunk_left -= (size_t)r;
          if (chunk_left == 0){
            while (cli.available() < 2) { if (millis() - t0 > timeout_ms) return false; delay(1); }
            cli.read(); cli.read();
          }
        }
      } else {
        if (millis() - t0 > timeout_ms) return false;
        delay(1);
      }
    }
  }
  return true;
}

static bool parse_wav_header(WiFiClient& cli, WavFmt& fmt, uint32_t& dataRemaining, bool chunked, size_t& chunk_left){
  uint8_t hdr12[12];
  if (!readN_http_body(cli, hdr12, 12, chunked, chunk_left)) return false;
  if (memcmp(hdr12, "RIFF", 4) != 0 || memcmp(hdr12 + 8, "WAVE", 4) != 0) return false;

  bool gotFmt = false;
  dataRemaining = 0;

  while (true) {
    uint8_t chdr[8];
    if (!readN_http_body(cli, chdr, 8, chunked, chunk_left)) return false;
    uint32_t sz = (uint32_t)chdr[4] | ((uint32_t)chdr[5] << 8) | ((uint32_t)chdr[6] << 16) | ((uint32_t)chdr[7] << 24);

    if (memcmp(chdr, "fmt ", 4) == 0) {
      if (sz < 16) return false;
      uint8_t fmtbuf[32];
      size_t toread = min(sz, (uint32_t)sizeof(fmtbuf));
      if (!readN_http_body(cli, fmtbuf, toread, chunked, chunk_left)) return false;
      uint32_t left = sz - (uint32_t)toread;
      while (left){
        uint8_t dump[64];
        size_t d = min((uint32_t)sizeof(dump), left);
        if (!readN_http_body(cli, dump, d, chunked, chunk_left)) return false;
        left -= d;
      }
      fmt.audioFormat   = (uint16_t) (fmtbuf[0] | (fmtbuf[1] << 8));
      fmt.numChannels   = (uint16_t) (fmtbuf[2] | (fmtbuf[3] << 8));
      fmt.sampleRate    = (uint32_t) (fmtbuf[4] | (fmtbuf[5] << 8) | (fmtbuf[6] << 16) | (fmtbuf[7] << 24));
      fmt.byteRate      = (uint32_t) (fmtbuf[8] | (fmtbuf[9] << 8) | (fmtbuf[10] << 16) | (fmtbuf[11] << 24));
      fmt.blockAlign    = (uint16_t) (fmtbuf[12] | (fmtbuf[13] << 8));
      fmt.bitsPerSample = (uint16_t) (fmtbuf[14] | (fmtbuf[15] << 8));
      gotFmt = true;
    }
    else if (memcmp(chdr, "data", 4) == 0) {
      if (!gotFmt) return false;
      dataRemaining = sz;
      return true;
    }
    else {
      uint32_t left = sz;
      while (left){
        uint8_t dump[128];
        size_t d = min((uint32_t)sizeof(dump), left);
        if (!readN_http_body(cli, dump, d, chunked, chunk_left)) return false;
        left -= d;
      }
    }
  }
}

// ---- HTTP 播放任务
static TaskHandle_t taskHttpPlayHandle = nullptr;
static volatile bool http_play_running = false;

void taskHttpPlay(void*){
  http_play_running = true;
  WiFiClient cli;

  auto readLine = [&](String& out, uint32_t timeout_ms)->bool {
    out = "";
    uint32_t t0 = millis();
    while (millis() - t0 < timeout_ms) {
      while (cli.available()) {
        char c = (char)cli.read();
        if (c == '\r') continue;
        if (c == '\n') return true;
        out += c;
        if (out.length() > 1024) return false;
      }
      delay(1);
    }
    return false;
  };

  auto readNRaw = [&](uint8_t* dst, size_t n, uint32_t timeout_ms)->bool {
    size_t got = 0;
    uint32_t t0 = millis();
    while (got < n) {
      if (!cli.connected()) return false;
      int avail = cli.available();
      if (avail > 0) {
        int take = (int)min((size_t)avail, n - got);
        int r = cli.read(dst + got, take);
        if (r > 0) { got += r; continue; }
      }
      if (millis() - t0 > timeout_ms) return false;
      delay(1);
    }
    return true;
  };

  auto makeBodyReader = [&](bool& is_chunked, uint32_t& chunk_left){
    return [&](uint8_t* dst, size_t n, uint32_t timeout_ms)->bool {
      size_t filled = 0;
      uint32_t t0 = millis();
      while (filled < n) {
        if (!cli.connected()) return false;
        if (is_chunked) {
          if (chunk_left == 0) {
            String szLine;
            if (!readLine(szLine, timeout_ms)) return false;
            int sc = szLine.indexOf(';');
            if (sc >= 0) szLine = szLine.substring(0, sc);
            szLine.trim();
            uint32_t sz = 0;
            if (sscanf(szLine.c_str(), "%x", &sz) != 1) return false;
            if (sz == 0) { String dummy; readLine(dummy, 200); return false; }
            chunk_left = sz;
          }
          size_t need = (size_t)min<uint32_t>(chunk_left, (uint32_t)(n - filled));
          while (cli.available() < (int)need) {
            if (millis() - t0 > timeout_ms) return false;
            if (!cli.connected()) return false;
            delay(1);
          }
          int r = cli.read(dst + filled, need);
          if (r <= 0) {
            if (millis() - t0 > timeout_ms) return false;
            delay(1); continue;
          }
          filled     += r;
          chunk_left -= r;
          if (chunk_left == 0) {
            char crlf[2];
            if (!readNRaw((uint8_t*)crlf, 2, 200)) return false;
          }
        } else {
          if (!readNRaw(dst + filled, n - filled, timeout_ms)) return false;
          filled = n;
        }
      }
      return true;
    };
  };

  static int32_t outLR[1024 * 2];
  const uint32_t BODY_TIMEOUT_MS = 1500;

  while (http_play_running) {
    if (!cli.connected()) {
      Serial.println("[AUDIO] HTTP connect...");
      if (!cli.connect(SERVER_HOST.c_str(), SERVER_PORT)) { delay(500); continue; }
      String req =
        String("GET /stream.wav HTTP/1.1\r\n") +
        "Host: " + SERVER_HOST + ":" + String(SERVER_PORT) + "\r\n" +
        "Connection: keep-alive\r\n\r\n";
      cli.print(req);
    }

    bool header_ok  = false;
    bool is_chunked = false;
    uint32_t content_len = 0;
    {
      String line; uint32_t t0 = millis();
      while (millis() - t0 < 3000) {
        if (!readLine(line, 1000)) { if (!cli.connected()) break; continue; }
        String u = line; u.toLowerCase();
        if (u.startsWith("transfer-encoding:")) { if (u.indexOf("chunked") >= 0) is_chunked = true; }
        else if (u.startsWith("content-length:")) { content_len = (uint32_t) strtoul(u.substring(strlen("content-length:")).c_str(), nullptr, 10); }
        if (line.length() == 0) { header_ok = true; break; }
      }
    }
    if (!header_ok) { cli.stop(); delay(300); continue; }

    uint32_t chunk_left = 0;
    auto readBody = makeBodyReader(is_chunked, chunk_left);

    uint8_t hdr12[12];
    if (!readBody(hdr12, 12, 1000)) { cli.stop(); delay(300); continue; }
    if (memcmp(hdr12, "RIFF", 4) != 0 || memcmp(hdr12 + 8, "WAVE", 4) != 0) { cli.stop(); delay(300); continue; }

    bool  gotFmt = false, gotData = false;
    uint8_t chdr[8];
    uint16_t audioFormat=0, numChannels=0, bitsPerSample=0;
    uint32_t sampleRate=0;

    while (!gotData) {
      if (!readBody(chdr, 8, 1000)) { cli.stop(); delay(300); goto reconnect; }
      uint32_t sz = (uint32_t)chdr[4] | ((uint32_t)chdr[5]<<8) | ((uint32_t)chdr[6]<<16) | ((uint32_t)chdr[7]<<24);

      if (memcmp(chdr, "fmt ", 4) == 0) {
        if (sz < 16) { cli.stop(); delay(300); goto reconnect; }
        uint8_t fmtbuf[32];
        size_t toread = min(sz, (uint32_t)sizeof(fmtbuf));
        if (!readBody(fmtbuf, toread, 1000)) { cli.stop(); delay(300); goto reconnect; }
        if (sz > toread) {
          size_t left = sz - toread;
          while (left) { uint8_t dump[128]; size_t d = min(left, sizeof(dump));
            if (!readBody(dump, d, 1000)) { cli.stop(); delay(300); goto reconnect; }
            left -= d;
          }
        }
        audioFormat   = (uint16_t)(fmtbuf[0] | (fmtbuf[1] << 8));
        numChannels   = (uint16_t)(fmtbuf[2] | (fmtbuf[3] << 8));
        sampleRate    = (uint32_t)(fmtbuf[4] | (fmtbuf[5] << 8) | (fmtbuf[6] << 16) | (fmtbuf[7] << 24));
        bitsPerSample = (uint16_t)(fmtbuf[14] | (fmtbuf[15] << 8));
        gotFmt = true;
      }
      else if (memcmp(chdr, "data", 4) == 0) {
        if (!gotFmt) { cli.stop(); delay(300); goto reconnect; }
        gotData = true;
      }
      else {
        size_t left = sz;
        while (left) { uint8_t dump[128]; size_t d = min(left, sizeof(dump));
          if (!readBody(dump, d, 1000)) { cli.stop(); delay(300); goto reconnect; }
          left -= d;
        }
      }
    }

    if (!(audioFormat==1 && numChannels==1 && bitsPerSample==16 && (sampleRate==8000 || sampleRate==12000 || sampleRate==16000))) {
      Serial.printf("[AUDIO] unsupported fmt: ch=%u bits=%u sr=%u af=%u\n",
                    numChannels, bitsPerSample, sampleRate, audioFormat);
      cli.stop(); delay(300); continue;
    }
    Serial.printf("[AUDIO] WAV ok: %u/16bit/mono (chunked=%d)\n", sampleRate, is_chunked ? 1 : 0);

    static uint32_t current_out_rate = 0;
    if (current_out_rate != sampleRate) {
      // 重新配置 I2S 输出采样率，以匹配服务端 WAV
      i2sOut.begin(I2S_MODE_STD, (int)sampleRate, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO);
      current_out_rate = sampleRate;
      Serial.printf("[I2S OUT] reconfig to %u Hz\n", sampleRate);
    }

    while (http_play_running) {
      uint8_t inbuf[2048];
      size_t  filled = 0;

      // 根据采样率计算 20 ms 字节数（单声道、16 bit）
      uint32_t bytes20 = (sampleRate * 2 * 20) / 1000; // 16k=640,12k=480,8k=320
      if (bytes20 < 2) bytes20 = 2;

      if (!readBody(inbuf, bytes20, BODY_TIMEOUT_MS)) { break; }
      filled = bytes20;

      while (filled + bytes20 <= sizeof(inbuf)) {
        if (!readBody(inbuf + filled, bytes20, 2)) { break; }
        filled += bytes20;
      }

      if (filled & 1) filled -= 1;
      if (filled == 0) { vTaskDelay(pdMS_TO_TICKS(1)); continue; }

      size_t samp = filled / 2;
      mono16_to_stereo32_msb((const int16_t*)inbuf, samp, outLR, 0.8f);

      size_t bytes = samp * 2 * sizeof(int32_t);
      size_t off = 0;
      while (off < bytes && http_play_running) {
        size_t wrote = i2sOut.write((uint8_t*)outLR + off, bytes - off);
        if (wrote == 0) vTaskDelay(pdMS_TO_TICKS(1));
        else off += wrote;
      }
    }

  reconnect:
    cli.stop();
    delay(200);
  }

  cli.stop();
  vTaskDelete(nullptr);
}

void startStreamWav(){
  if (taskHttpPlayHandle) return;
  xTaskCreatePinnedToCore(taskHttpPlay, "http_wav", 8192, nullptr, 2, &taskHttpPlayHandle, 0);
  Serial.println("[AUDIO] http_wav task started");
}
void stopStreamWav(){
  if (!taskHttpPlayHandle) return;
  http_play_running = false;
  vTaskDelay(pdMS_TO_TICKS(50));
  taskHttpPlayHandle = nullptr;
  Serial.println("[AUDIO] http_wav task stopped");
}

// ====================================================================
// TTS（二进制分片）保留，但默认不启用
// ====================================================================
void taskTTSPlay(void*){
  static int32_t stereo32Buf[1024*2];
  for(;;){
    if (!tts_playing){ vTaskDelay(pdMS_TO_TICKS(5)); continue; }
    TTSChunk ch;
    if (xQueueReceive(qTTS, &ch, pdMS_TO_TICKS(50)) == pdPASS){
      size_t inSamp  = ch.n / 2;
      int16_t* inPtr = (int16_t*)ch.data;
      size_t outPairs = 0;
      for (size_t i = 0; i < inSamp; ++i){
        int32_t s = (int32_t)inPtr[i];
        s = (s * 19660) / 32768;
        int32_t v32 = s << 16;
        stereo32Buf[outPairs*2 + 0] = v32;
        stereo32Buf[outPairs*2 + 1] = v32;
        outPairs++;
        if (outPairs >= 1024){
          size_t bytes = outPairs * 2 * sizeof(int32_t);
          size_t off = 0;
          while (off < bytes){
            size_t wrote = i2sOut.write((uint8_t*)stereo32Buf + off, bytes - off);
            if (wrote == 0) vTaskDelay(pdMS_TO_TICKS(1)); else off += wrote;
          }
          outPairs = 0;
        }
      }
      if (outPairs){
        size_t bytes = outPairs * 2 * sizeof(int32_t);
        size_t off = 0;
        while (off < bytes){
          size_t wrote = i2sOut.write((uint8_t*)stereo32Buf + off, bytes - off);
          if (wrote == 0) vTaskDelay(pdMS_TO_TICKS(1)); else off += wrote;
        }
      }
    }
  }
}

inline void tts_reset_queue(){ if (qTTS) xQueueReset(qTTS); }

// ====================================================================
// IMU (ICM42688 over SPI) 50Hz via UDP
// ====================================================================

// --- ICM42688-P registers (Bank0) ---
#define REG_WHO_AM_I      0x75  // expect 0x47
#define REG_BANK_SEL      0x76
#define REG_PWR_MGMT0     0x4E  // 0x0F => accel+gyro LN
#define REG_TEMP_H        0x1D  // then ACC(1F..24), GYR(25..2A)
#define BURST_FIRST       REG_TEMP_H
#define BURST_COUNT       14

// 量程（常见默认值为 ±16g / ±2000 dps）
static const float ACC_LSB_PER_G   = 2048.0f;   // 1 g = 2048 LSB
static const float GYR_LSB_PER_DPS = 16.4f;     // 1 dps = 16.4 LSB
static const float G               = 9.80665f;
static const float TEMP_SENS       = 132.48f;   // °C/LSB
static const float TEMP_OFFSET     = 25.0f;

static inline void imu_cs_low()  { digitalWrite(IMU_SPI_CS, LOW);  }
static inline void imu_cs_high() { digitalWrite(IMU_SPI_CS, HIGH); }

uint8_t imu_read8(uint8_t reg){
  imu_cs_low();
  SPI.transfer(reg | 0x80);
  uint8_t v = SPI.transfer(0x00);
  imu_cs_high();
  return v;
}
void imu_write8(uint8_t reg, uint8_t val){
  imu_cs_low();
  SPI.transfer(reg & 0x7F);
  SPI.transfer(val);
  imu_cs_high();
}
void imu_readn(uint8_t start_reg, uint8_t* dst, size_t n){
  imu_cs_low();
  SPI.transfer(start_reg | 0x80);
  for (size_t i=0;i<n;i++) dst[i] = SPI.transfer(0x00);
  imu_cs_high();
}

bool imu_init_spi(){
  SPI.begin(IMU_SPI_SCK, IMU_SPI_MISO, IMU_SPI_MOSI, IMU_SPI_CS);
  pinMode(IMU_SPI_CS, OUTPUT);
  imu_cs_high();
  delay(5);

  uint8_t who = imu_read8(REG_WHO_AM_I);
  Serial.printf("[IMU] WHO_AM_I=0x%02X (expect 0x47)\n", who);
  if (who != 0x47) return false;

  imu_write8(REG_PWR_MGMT0, 0x0F); // accel+gyro LN
  delay(10);
  return true;
}

bool imu_read_once(float& tempC, float& ax, float& ay, float& az, float& gx, float& gy, float& gz){
  uint8_t raw[BURST_COUNT];
  imu_readn(BURST_FIRST, raw, sizeof(raw));

  auto s16 = [&](int idx)->int16_t {
    return (int16_t)((raw[idx] << 8) | raw[idx+1]);
  };

  int16_t tr  = s16(0);
  int16_t axr = s16(2);
  int16_t ayr = s16(4);
  int16_t azr = s16(6);
  int16_t gxr = s16(8);
  int16_t gyr = s16(10);
  int16_t gzr = s16(12);

  tempC = (float)tr / TEMP_SENS + TEMP_OFFSET;
  ax = ((float)axr / ACC_LSB_PER_G) * G;
  ay = ((float)ayr / ACC_LSB_PER_G) * G;
  az = ((float)azr / ACC_LSB_PER_G) * G;
  gx =  (float)gxr / GYR_LSB_PER_DPS;
  gy =  (float)gyr / GYR_LSB_PER_DPS;
  gz =  (float)gzr / GYR_LSB_PER_DPS;

  return true;
}

// 轻微平滑，便于观察；不改变 UDP 字段名
static const float EMA_ALPHA = 0.20f;
bool  ema_inited = false;
float ax_f=0, ay_f=0, az_f=0;

void taskImuLoop(void*){
  for(;;){
    static bool inited = false;
    if (!inited){
      inited = imu_init_spi();
      if (!inited){ vTaskDelay(pdMS_TO_TICKS(500)); continue; }
      Serial.println("[IMU] init OK (SPI)");
    }

    float tempC, ax, ay, az, gx, gy, gz;
    if (!imu_read_once(tempC, ax, ay, az, gx, gy, gz)){
      inited = false; vTaskDelay(pdMS_TO_TICKS(50)); continue;
    }

    if (!ema_inited){ ax_f=ax; ay_f=ay; az_f=az; ema_inited=true; }
    else {
      ax_f = EMA_ALPHA*ax + (1-EMA_ALPHA)*ax_f;
      ay_f = EMA_ALPHA*ay + (1-EMA_ALPHA)*ay_f;
      az_f = EMA_ALPHA*az + (1-EMA_ALPHA)*az_f;
    }

    char buf[256];
    unsigned long ts = millis();
    int n = snprintf(buf, sizeof(buf),
      "{\"ts\":%lu,\"temp_c\":%.2f,"
      "\"accel\":{\"x\":%.3f,\"y\":%.3f,\"z\":%.3f},"
      "\"gyro\":{\"x\":%.3f,\"y\":%.3f,\"z\":%.3f}}",
      ts, tempC, ax_f, ay_f, az_f, gx, gy, gz);

    if (n > 0) {
      if (!server_resolved) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }   // 【新增】等服务器发现
      udp.beginPacket(SERVER_HOST.c_str(), UDP_PORT);
      udp.write((const uint8_t*)buf, n);
      udp.endPacket();
    }
    vTaskDelay(pdMS_TO_TICKS(20)); // 50 Hz
  }
}

// ====================================================================
// Setup / Loop
// ====================================================================

// ====================================================================
// 服务器自动发现（UDP 广播 255.255.255.255:12345）
// ====================================================================
bool discover_server(uint32_t timeout_ms = 4000) {
  if (SERVER_HOST != "auto") { server_resolved = true; return true; }  // 手写 IP 模式
  WiFiUDP d;
  d.begin(12346);
  const char* magic = "AIGLASS_DISCOVER";
  uint32_t t0 = millis();
  int attempts = 0;
  while (millis() - t0 < timeout_ms) {
    if (attempts++ % 10 == 0) {
      d.beginPacket(IPAddress(255,255,255,255), 12345);
      d.write((const uint8_t*)magic, strlen(magic));
      d.endPacket();
      Serial.println("[DISCOVER] 广播 AIGLASS_DISCOVER ...");
    }
    int pkt = d.parsePacket();
    if (pkt > 0) {
      String resp = d.readStringUntil('\0');
      resp.trim();
      IPAddress peer = d.remoteIP();
      Serial.printf("[DISCOVER] 收到 \"%s\" from %s\n", resp.c_str(), peer.toString().c_str());
      if (resp.startsWith("AIGLASS_SERVER:")) {
        SERVER_HOST = peer.toString();
        server_resolved = true;
        Serial.printf("[DISCOVER] 服务器 = %s:%u\n", SERVER_HOST.c_str(), (unsigned)SERVER_PORT);
        d.stop();
        return true;
      }
    }
    delay(120);
  }
  d.stop();
  Serial.println("[DISCOVER] 超时未发现服务器，稍后重试");
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(300);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[WiFi] connecting");
  unsigned long wifi_wait_started = millis();
  while (WiFi.status()!=WL_CONNECTED && millis() - wifi_wait_started < 12000) {
    delay(250);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" OK " + WiFi.localIP().toString());
  } else {
    Serial.println(" timeout；将在主循环中继续非阻塞重连");
    wifi_next_retry_at = millis();
  }

  discover_server(4000);   // 【新增】自动发现服务器（失败则在 loop 里持续重试）

  // 【修复】摄像头初始化失败不再 esp_restart 死循环，改为后台重试
  if (!init_camera()) { Serial.println("[CAM] init failed, 5s 后重试..."); }

  udp.begin(0);

  init_i2s_in();
#if SPEAKER_ENABLED
  init_i2s_out();
#else
  Serial.println("[AUDIO] 喇叭已禁用（SPEAKER_ENABLED=0），不启动 I2S TX");
#endif

  qFrames = xQueueCreate(1, sizeof(fb_ptr_t));  // 只保留最新帧，避免延迟不断累积
  qAudio  = xQueueCreate(AUDIO_QUEUE_DEPTH, sizeof(AudioChunk));
  qTTS    = xQueueCreate(TTS_QUEUE_DEPTH, sizeof(TTSChunk));

  xTaskCreatePinnedToCore(taskCamCapture, "cam_cap", 10240, NULL, 4, NULL, 1);
  xTaskCreatePinnedToCore(taskCamSend,    "cam_snd",  8192, NULL, 3, NULL, 1);
  xTaskCreatePinnedToCore(taskMicCapture, "mic_cap",   4096, NULL, 2, NULL, 0);
  xTaskCreatePinnedToCore(taskMicUpload,  "mic_upl",   4096, NULL, 2, NULL, 1);
  xTaskCreatePinnedToCore(taskImuLoop,    "imu_loop",  4096, NULL, 2, NULL, 0);
  xTaskCreatePinnedToCore(taskTTSPlay,    "tts_play",  4096, NULL, 2, NULL, 0);

  wsCam.onEvent([](WebsocketsEvent ev, String){
    if (ev == WebsocketsEvent::ConnectionOpened)  { 
      cam_ws_ready = true;  
      Serial.println("[WS-CAM] open");
      cam_retry_ms = 800;
      ws_cam_opened_at = millis();
      wsCam.send("HELLO:XIAO-ESP32S3-fw2.6-auto");
      // 重置统计
      frame_sent_count = 0;
      frame_dropped_count = 0;
      ws_send_fail_count = 0;
      send_time_sum_ms = 0;
      send_time_samples = 0;
      stats_prev_captured = frame_captured_count;
      stats_prev_sent = 0;
      stats_prev_dropped = 0;
      stats_prev_send_sum = 0;
      stats_prev_send_samples = 0;
      last_stats_time = millis();
    }
    if (ev == WebsocketsEvent::ConnectionClosed)  { 
      cam_ws_ready = false; 
      clear_frame_queue();
      cam_next_retry_at = millis() + cam_retry_ms;
      Serial.printf("[WS-CAM] closed (sent=%lu, dropped=%lu, fail=%lu)\n", 
                    frame_sent_count, frame_dropped_count, ws_send_fail_count);
    }
  });

  wsCam.onMessage([](WebsocketsMessage msg){
    if (msg.isText()){
      String cmd = msg.data(); cmd.trim();
      if (cmd.startsWith("SET:FRAMESIZE=")) {
        String v = cmd.substring(strlen("SET:FRAMESIZE="));
        v.toUpperCase();
        framesize_t fs = g_frame_size;
        if (v == "QQVGA") fs = FRAMESIZE_QQVGA;
        else if (v == "HQVGA") fs = FRAMESIZE_HQVGA;
        else if (v == "QVGA") fs = FRAMESIZE_QVGA;
        else if (v == "CIF") fs = FRAMESIZE_CIF;
        else if (v == "SVGA") fs = FRAMESIZE_SVGA;
        else if (v == "XGA") fs = FRAMESIZE_XGA;
        else if (v == "VGA") fs = FRAMESIZE_VGA;
        if (apply_framesize(fs)) Serial.printf("[CAM] framesize set to %s\n", v.c_str());
        else Serial.printf("[CAM] framesize set failed: %s\n", v.c_str());
      }
      else if (cmd.startsWith("SET:QUALITY=")) {     // 动态调整画质
        int q = cmd.substring(strlen("SET:QUALITY=")).toInt();
        q = constrain(q, 5, 40);
        sensor_t* s = esp_camera_sensor_get();
        if (s) {
          s->set_quality(s, q);
          g_jpeg_quality = q;
          Serial.printf("[CAM] quality=%d\n", q);
        }
      }
      else if (cmd.startsWith("SET:FPS=")) {         // 动态调整发送 FPS
        int f = cmd.substring(strlen("SET:FPS=")).toInt();
        g_target_fps = (f <= 0 ? 0 : constrain(f, 5, 60));
        Serial.printf("[CAM] target_fps=%d\n", g_target_fps);
      }

      else if (cmd == "SNAP:HQ") {
        Serial.println("[CAM] SNAP:HQ request");
        if (snapshot_in_progress) return;
        snapshot_in_progress = true;
        sensor_t* s = esp_camera_sensor_get();
        framesize_t old_fs = g_frame_size;
        int old_q = g_jpeg_quality;
        // 目标分辨率：SXGA；更高分辨率需根据 PSRAM 稳定性调整
        framesize_t target_fs = FRAMESIZE_SXGA;
        if (s) {
          s->set_framesize(s, target_fs);
          s->set_quality(s, 18); // 数值越小越清晰
        }
        vTaskDelay(pdMS_TO_TICKS(500));
        camera_fb_t* fb = esp_camera_fb_get();
        if (fb && fb->format == PIXFORMAT_JPEG) {
          wsCam.send("SNAP:BEGIN");
          bool ok = wsCam.sendBinary((const char*)fb->buf, fb->len);
          wsCam.send("SNAP:END");
          if (!ok) { Serial.println("[CAM] SNAP send failed"); }
          esp_camera_fb_return(fb);
        } else {
          if (fb) esp_camera_fb_return(fb);
          Serial.println("[CAM] SNAP: capture failed");
        }
        if (s) {
          s->set_framesize(s, old_fs);
          s->set_quality(s, old_q);
        }
        snapshot_in_progress = false;
      }
    }
  });

  wsAud.onEvent([](WebsocketsEvent ev, String){
    if (ev == WebsocketsEvent::ConnectionOpened)  {
      aud_ws_ready = true;
      aud_retry_ms = 1200;
      if (qAudio) xQueueReset(qAudio);
      run_audio_stream = true;
      wsAud.send("START");
#if SPEAKER_ENABLED
      startStreamWav();
#endif
      Serial.println("[WS-AUD] open");
    }
    if (ev == WebsocketsEvent::ConnectionClosed)  { 
      aud_ws_ready = false; 
      run_audio_stream = false;
      if (qAudio) xQueueReset(qAudio);
      aud_next_retry_at = millis() + aud_retry_ms;
      Serial.println("[WS-AUD] closed"); 
      stopStreamWav();
    }
  });

  wsAud.onMessage([](WebsocketsMessage msg){
    if (msg.isText()){
      String s = msg.data(); s.trim();
      if (s == "RESTART"){
        run_audio_stream = false; xQueueReset(qAudio); delay(50);
        wsAud.send("START"); run_audio_stream = true;
      }
    }
  });
}

static inline bool retry_due(unsigned long now, unsigned long deadline) {
  return (long)(now - deadline) >= 0;
}

void loop() {
  unsigned long now = millis();   // 【合并】now 提前声明，供发现逻辑与重试计时共用
  // 【合并-自动发现】先发现服务器，再走原有重试机制连接两路 WS
  if (!server_resolved) {
    if (!discover_server(3000)) { delay(50); return; }
    cam_next_retry_at = now;   // 发现成功，立即尝试连接
    aud_next_retry_at = now;
  }

  static bool wifi_was_connected = false;
  bool wifi_connected = WiFi.status() == WL_CONNECTED;

  if (!wifi_connected) {
    if (wifi_was_connected) {
      Serial.println("[WiFi] connection lost；pausing camera/audio links");
      cam_ws_ready = false;
      aud_ws_ready = false;
      run_audio_stream = false;
      clear_frame_queue();
      wsCam.close();
      wsAud.close();
      stopStreamWav();
      wifi_next_retry_at = now;
    }
    wifi_was_connected = false;

    if (retry_due(now, wifi_next_retry_at)) {
      Serial.printf("[WiFi] reconnecting，next backoff=%lu ms\n", wifi_retry_ms);
      WiFi.disconnect(false, false);
      WiFi.begin(WIFI_SSID, WIFI_PASS);
      wifi_next_retry_at = now + wifi_retry_ms;
      wifi_retry_ms = min((unsigned long)15000, wifi_retry_ms * 2);
    }
    delay(2);
    return;
  }

  if (!wifi_was_connected) {
    wifi_was_connected = true;
    wifi_retry_ms = 1000;
    cam_next_retry_at = now;
    aud_next_retry_at = now;
    Serial.println("[WiFi] connected " + WiFi.localIP().toString());
  }

  // 两路 WebSocket 使用独立计时器；一路失败不会阻塞另一路轮询。
  if (!wsCam.available() && retry_due(now, cam_next_retry_at)) {
    Serial.println("[WS-CAM] connecting...");
    if (!wsCam.connect(SERVER_HOST.c_str(), SERVER_PORT, CAM_WS_PATH)) {
      cam_next_retry_at = now + cam_retry_ms;
      cam_retry_ms = min((unsigned long)10000, cam_retry_ms * 2);
      Serial.printf("[WS-CAM] connect failed，retry in %lu ms\n", cam_retry_ms);
    }
  }

  if (!wsAud.available() && retry_due(now, aud_next_retry_at)) {
    Serial.println("[WS-AUD] connecting...");
    if (!wsAud.connect(SERVER_HOST.c_str(), SERVER_PORT, AUD_WS_PATH)) {
      aud_next_retry_at = now + aud_retry_ms;
      aud_retry_ms = min((unsigned long)12000, aud_retry_ms * 2);
      Serial.printf("[WS-AUD] connect failed，retry in %lu ms\n", aud_retry_ms);
    }
  }

  wsCam.poll();
  wsAud.poll();
  delay(2);
}
