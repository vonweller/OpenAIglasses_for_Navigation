// k10_glasses.ino — UNIHIKER K10 (行空板) 自动发现 + 摄像头推流客户端
// 用途：把 K10 当作"眼镜端"接入本仓库后端（http://<PC>:8081）。
// 特性：
//   1) 服务器自动发现：广播 AIGLASS_DISCOVER 到 255.255.255.255:12345，
//      后端应答 AIGLASS_SERVER:<port>，取应答源 IP —— 换 WiFi/换电脑都不用改代码。
//   2) esp32-camera 驱动 GC2145 出 JPEG，经 /ws/camera 推流；支持后端
//      SET:FRAMESIZE / SET:QUALITY / SET:FPS 遥控与 STAT 遥测。
//   3) 屏幕显示连接状态（不依赖串口）。
// 依赖：ArduinoWebsockets(gilmaimon) 库 + UNIHIKER:esp32:k10 BSP

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_camera.h>
#include <ArduinoWebsockets.h>
#include "unihiker_k10.h"
using namespace websockets;

// ===== WiFi（改成你的 2.4G WiFi；服务器地址不需要填——自动发现）=====
// 烧录前改成你自己的 2.4G WiFi（真实密码不要提交进 Git）
const char* WIFI_SSID = "YOUR_2_4_GHZ_WIFI";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

String SERVER_HOST = "auto";
const uint16_t SERVER_PORT = 8081;
volatile bool server_resolved = false;

static const char* CAM_WS_PATH = "/ws/camera";

// ===== K10 GC2145 camera pins（ESP32S3_EYE 家族映射，PWDN/RESET=-1）=====
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     15
#define SIOD_GPIO_NUM      4
#define SIOC_GPIO_NUM      5
#define Y9_GPIO_NUM       16
#define Y8_GPIO_NUM       17
#define Y7_GPIO_NUM       18
#define Y6_GPIO_NUM       12
#define Y5_GPIO_NUM       10
#define Y4_GPIO_NUM        8
#define Y3_GPIO_NUM        9
#define Y2_GPIO_NUM       11
#define VSYNC_GPIO_NUM     6
#define HREF_GPIO_NUM      7
#define PCLK_GPIO_NUM     13

// ===== Camera config（K230 profile：SVGA 15fps，与后端 k230_* 档对齐；也可 VGA 20fps）=====
framesize_t g_frame_size = FRAMESIZE_SVGA;
#define JPEG_QUALITY  14
#define FB_COUNT      2
volatile int g_target_fps = 15;
framesize_t fs_from_name(const String& v);

UNIHIKER_K10 k10;
uint8_t screen_dir = 2;
WebsocketsClient wsCam;
volatile bool cam_ws_ready = false;

volatile unsigned long frame_captured = 0, frame_sent = 0, frame_dropped = 0;
volatile unsigned long s_last_send_ms = 0;
unsigned long last_stat_ms = 0;
static uint8_t* s_jpeg_copy = nullptr;
static size_t   s_jpeg_copy_cap = 0;
const size_t JPEG_COPY_MAX = 256 * 1024;

// JPEG 发送拷贝缓冲：归还相机 fb 后再发，避免双缓冲被网络阻塞占死
static bool jpeg_copy_prepare(size_t n) {
  if (n == 0 || n > JPEG_COPY_MAX) return false;
  if (s_jpeg_copy && s_jpeg_copy_cap >= n) return true;
  size_t cap = n < 24 * 1024 ? 24 * 1024 : n;
  uint8_t* p = (uint8_t*)heap_caps_realloc(s_jpeg_copy, cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) p = (uint8_t*)heap_caps_malloc(cap, MALLOC_CAP_DEFAULT | MALLOC_CAP_8BIT);
  if (!p) return false;
  s_jpeg_copy = p;
  s_jpeg_copy_cap = cap;
  return true;
}

void uiMsg(const String& line1, const String& line2 = "") {
  k10.canvas->canvasClear();
  k10.canvas->canvasText("K10 AI Glasses", 1, 0x00FFAA);
  k10.canvas->canvasText("WiFi: " + String(WIFI_SSID), 2, 0xAAAAAA);
  k10.canvas->canvasText(line1, 3, 0xFFFFFF);
  if (line2.length()) k10.canvas->canvasText(line2, 4, 0xAAAAAA);
  k10.canvas->updateCanvas();
}

framesize_t fs_from_name(const String& v) {
  if (v == "VGA") return FRAMESIZE_VGA;
  if (v == "SVGA") return FRAMESIZE_SVGA;
  if (v == "XGA") return FRAMESIZE_XGA;
  if (v == "QVGA") return FRAMESIZE_QVGA;
  return g_frame_size;
}

bool apply_framesize(framesize_t fs) {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return false;
  if (s->set_framesize(s, fs) == 0) { g_frame_size = fs; return true; }
  return false;
}

bool init_camera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM; config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM; config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM; config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = g_frame_size;
  config.jpeg_quality = JPEG_QUALITY;
  config.fb_count     = FB_COUNT;
  config.fb_location  = CAMERA_FB_IN_PSRAM;
  config.grab_mode    = CAMERA_GRAB_LATEST;
  Serial.printf("[CAM] init fs=%d fb=2 psram=1 ...", (int)g_frame_size);
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf(" FAILED 0x%x\n", err);
    // 降级重试：SVGA->VGA->QVGA（PSRAM）->QVGA(DRAM)
    struct T { framesize_t fs; bool ps; } tries[] = {{FRAMESIZE_VGA,true},{FRAMESIZE_QVGA,true},{FRAMESIZE_QVGA,false}};
    for (auto& c : tries) {
      esp_camera_deinit();
      config.frame_size = c.fs;
      config.fb_location = c.ps ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
      Serial.printf("[CAM] retry fs=%d ps=%d ...", (int)c.fs, c.ps?1:0);
      if (esp_camera_init(&config) == ESP_OK) {
        camera_fb_t* probe = esp_camera_fb_get();
        if (probe) { esp_camera_fb_return(probe); g_frame_size = c.fs; Serial.println(" OK(degraded)"); return true; }
        esp_camera_deinit();
      }
    }
    return false;
  }
  camera_fb_t* probe = esp_camera_fb_get();
  if (!probe) { esp_camera_deinit(); Serial.println(" probe=NULL"); return false; }
  Serial.printf(" OK probe=%uB PSRAM=%u\n", probe->len, (unsigned)ESP.getPsramSize());
  esp_camera_fb_return(probe);
  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_hmirror(s, 0); s->set_vflip(s, 1);   // K10 摄像头物理朝向：垂直翻转按需调
    s->set_whitebal(s, 1); s->set_gain_ctrl(s, 1);
  }
  return true;
}

// ===== 服务器自动发现 =====
bool discover_server(uint32_t timeout_ms = 4000) {
  if (SERVER_HOST != "auto") { server_resolved = true; return true; }
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
      String resp = d.readStringUntil('\0'); resp.trim();
      IPAddress peer = d.remoteIP();
      Serial.printf("[DISCOVER] 收到 \"%s\" from %s\n", resp.c_str(), peer.toString().c_str());
      if (resp.startsWith("AIGLASS_SERVER:")) {
        SERVER_HOST = peer.toString();
        server_resolved = true;
        Serial.printf("[DISCOVER] 服务器 = %s:%u\n", SERVER_HOST.c_str(), (unsigned)SERVER_PORT);
        uiMsg("Server: " + SERVER_HOST, "Port: " + String(SERVER_PORT));
        d.stop();
        return true;
      }
    }
    delay(120);
  }
  d.stop();
  Serial.println("[DISCOVER] 超时，稍后重试");
  return false;
}

// ===== 相机发送任务 =====
QueueHandle_t qFrames;
void taskCamCapture(void*) {
  for(;;) {
    if (!cam_ws_ready) { vTaskDelay(pdMS_TO_TICKS(20)); continue; }
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) { vTaskDelay(pdMS_TO_TICKS(2)); continue; }
    frame_captured++;
    if (fb->format != PIXFORMAT_JPEG) { esp_camera_fb_return(fb); continue; }
    if (xQueueSend(qFrames, &fb, 0) != pdPASS) {
      camera_fb_t* drop = nullptr;
      if (xQueueReceive(qFrames, &drop, 0) == pdPASS && drop) { esp_camera_fb_return(drop); frame_dropped++; }
      xQueueSend(qFrames, &fb, 0);
    }
  }
}

void taskCamSend(void*) {
  static TickType_t lastTick = 0;
  for(;;) {
    camera_fb_t* fb = nullptr;
    if (xQueueReceive(qFrames, &fb, pdMS_TO_TICKS(100)) == pdPASS && fb) {
      if (!cam_ws_ready) { esp_camera_fb_return(fb); continue; }
      const size_t jpeg_len = fb->len;
      if (s_last_send_ms >= 400) { esp_camera_fb_return(fb); frame_dropped++; s_last_send_ms = 150; continue; }
      if (!jpeg_copy_prepare(jpeg_len)) { esp_camera_fb_return(fb); frame_dropped++; continue; }
      memcpy(s_jpeg_copy, fb->buf, jpeg_len);
      esp_camera_fb_return(fb);
      if (g_target_fps > 0) {
        const int period_ms = 1000 / g_target_fps;
        TickType_t now = xTaskGetTickCount();
        int elapsed = (now - lastTick) * portTICK_PERIOD_MS;
        if (elapsed < period_ms) vTaskDelay(pdMS_TO_TICKS(period_ms - elapsed));
        lastTick = xTaskGetTickCount();
      }
      unsigned long t0 = millis();
      bool ok = wsCam.sendBinary((const char*)s_jpeg_copy, jpeg_len);
      s_last_send_ms = millis() - t0;
      if (ok) frame_sent++;
      else { wsCam.close(); cam_ws_ready = false; }
    }
  }
}

void setup() {
  Serial.begin(115200);
  k10.begin();
  k10.initScreen(screen_dir);
  k10.creatCanvas();
  k10.rgb->brightness(3);
  k10.rgb->write(-1, 0x0000FF);
  uiMsg("Booting...");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[WiFi] connecting");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) { delay(250); Serial.print("."); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" OK " + WiFi.localIP().toString());
    uiMsg("WiFi OK: " + WiFi.localIP().toString(), "发现服务器中...");
  } else {
    Serial.println(" FAIL（循环重试）");
    uiMsg("WiFi FAIL", String(WIFI_SSID));
  }

  discover_server(4000);
  if (!init_camera()) Serial.println("[CAM] init failed，稍后重试");

  qFrames = xQueueCreate(1, sizeof(camera_fb_t*));
  xTaskCreatePinnedToCore(taskCamCapture, "cam_cap", 8192, nullptr, 4, nullptr, 1);
  xTaskCreatePinnedToCore(taskCamSend,    "cam_snd", 8192, nullptr, 3, nullptr, 1);

  wsCam.onEvent([](WebsocketsEvent ev, String){
    if (ev == WebsocketsEvent::ConnectionOpened) {
      cam_ws_ready = true;
      wsCam.send("HELLO:UNIHIKER-K10-fw1.0");
      Serial.println("[WS-CAM] open");
    }
    if (ev == WebsocketsEvent::ConnectionClosed) { cam_ws_ready = false; Serial.println("[WS-CAM] closed"); }
  });
  wsCam.onMessage([](WebsocketsMessage msg){
    if (!msg.isText()) return;
    String cmd = msg.data(); cmd.trim();
    if (cmd.startsWith("SET:FRAMESIZE=")) {
      String v = cmd.substring(14); v.toUpperCase();
      if (apply_framesize(fs_from_name(v))) Serial.println("[CAM] framesize=" + v);
    } else if (cmd.startsWith("SET:QUALITY=")) {
      int q = constrain(cmd.substring(12).toInt(), 5, 40);
      sensor_t* s = esp_camera_sensor_get();
      if (s) s->set_quality(s, q);
    } else if (cmd.startsWith("SET:FPS=")) {
      int f = cmd.substring(8).toInt();
      g_target_fps = (f <= 0) ? 0 : constrain(f, 5, 30);
    }
  });
}

void loop() {
  // WiFi 掉线自动重连
  if (WiFi.status() != WL_CONNECTED) { delay(500); WiFi.reconnect(); return; }
  // 自动发现（未解析时持续尝试）
  if (!server_resolved && !discover_server(3000)) return;

  if (!wsCam.available()) {
    Serial.println("[WS-CAM] connecting...");
    if (wsCam.connect(SERVER_HOST.c_str(), SERVER_PORT, CAM_WS_PATH)) {
      Serial.printf("[WS-CAM] connected -> %s:%u\n", SERVER_HOST.c_str(), (unsigned)SERVER_PORT);
    } else { delay(1000); return; }
  }

  // STAT 遥测（5s 一次）
  if (cam_ws_ready && millis() - last_stat_ms > 5000) {
    last_stat_ms = millis();
    char stat[160];
    snprintf(stat, sizeof(stat),
      "STAT:{\"device\":\"k10\",\"capture_fps\":%.1f,\"send_fps\":%.1f,\"dropped\":%lu,\"send_ms\":%.1f,\"jpeg_bytes\":%u,\"rssi\":%d,\"free_heap\":%u,\"target_fps\":%d}",
      0.0, 0.0, frame_dropped, (double)s_last_send_ms, 0, WiFi.RSSI(), (unsigned)ESP.getFreeHeap(), g_target_fps);
    wsCam.send(stat);
    uiMsg("Streaming", "sent=" + String(frame_sent) + " drop=" + String(frame_dropped));
  }

  wsCam.poll();
  delay(2);
}
