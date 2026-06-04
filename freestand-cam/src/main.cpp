#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <HTTPClient.h>
#include <esp_camera.h>
#include <esp_task_wdt.h>
#include <mbedtls/base64.h>
#include <time.h>
#include "config.h"

#define WDT_TIMEOUT_S 60

// Camera pins for AI-Thinker ESP32-CAM (OV2640)
#define PWDN_GPIO_NUM  32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM   0
#define SIOD_GPIO_NUM  26
#define SIOC_GPIO_NUM  27
#define Y9_GPIO_NUM    35
#define Y8_GPIO_NUM    34
#define Y7_GPIO_NUM    39
#define Y6_GPIO_NUM    36
#define Y5_GPIO_NUM    21
#define Y4_GPIO_NUM    19
#define Y3_GPIO_NUM    18
#define Y2_GPIO_NUM     5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  23
#define PCLK_GPIO_NUM  22

static uint32_t seq;

void initCamera() {
    camera_config_t config = {};
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = Y2_GPIO_NUM;
    config.pin_d1       = Y3_GPIO_NUM;
    config.pin_d2       = Y4_GPIO_NUM;
    config.pin_d3       = Y5_GPIO_NUM;
    config.pin_d4       = Y6_GPIO_NUM;
    config.pin_d5       = Y7_GPIO_NUM;
    config.pin_d6       = Y8_GPIO_NUM;
    config.pin_d7       = Y9_GPIO_NUM;
    config.pin_xclk     = XCLK_GPIO_NUM;
    config.pin_pclk     = PCLK_GPIO_NUM;
    config.pin_vsync    = VSYNC_GPIO_NUM;
    config.pin_href     = HREF_GPIO_NUM;
    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn     = PWDN_GPIO_NUM;
    config.pin_reset    = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.frame_size   = FRAMESIZE_VGA;  // 640x480
    config.jpeg_quality = JPEG_QUALITY;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("[camera] init failed: 0x%x\n", err);
    } else {
        Serial.println("[camera] ready");
        // Uncomment if camera is mounted upside-down:
        // sensor_t *s = esp_camera_sensor_get();
        // s->set_vflip(s, 1);
        // s->set_hmirror(s, 1);
    }
}

void connectWiFi() {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[wifi] connecting");
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
        delay(500);
        Serial.print(".");
        attempts++;
        esp_task_wdt_reset();
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\n[wifi] timeout — rebooting");
        esp_restart();
    }
    Serial.printf("\n[wifi] connected: %s\n", WiFi.localIP().toString().c_str());
}

void initOTA() {
    ArduinoOTA.setHostname(OTA_HOSTNAME);
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart([]() {
        esp_task_wdt_delete(NULL);
        Serial.println("[ota] starting — watchdog disabled");
    });
    ArduinoOTA.onEnd([]() { Serial.println("\n[ota] done"); });
    ArduinoOTA.onError([](ota_error_t e) { Serial.printf("[ota] error %u\n", e); });
    ArduinoOTA.begin();
    Serial.println("[ota] ready — hostname: " OTA_HOSTNAME);
}

String nowISO() {
    time_t now;
    time(&now);
    struct tm *t = gmtime(&now);
    char buf[30];
    strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", t);
    return String(buf);
}

void postFrame(camera_fb_t *fb) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[post] wifi down, skipping");
        return;
    }

    size_t b64_len = ((fb->len + 2) / 3) * 4 + 1;
    char *b64 = (char *)malloc(b64_len);
    if (!b64) {
        Serial.println("[post] out of memory for base64");
        return;
    }
    size_t out_len = 0;
    mbedtls_base64_encode((uint8_t *)b64, b64_len, &out_len, fb->buf, fb->len);
    b64[out_len] = '\0';
    esp_task_wdt_reset();

    char header[256];
    snprintf(header, sizeof(header),
        "{\"device_id\":\"" DEVICE_ID "\","
        "\"capture_ts\":\"%s\","
        "\"seq\":%u,"
        "\"width\":%u,"
        "\"height\":%u,"
        "\"jpeg_quality\":%d,"
        "\"image_b64\":\"",
        nowISO().c_str(), seq++, fb->width, fb->height, JPEG_QUALITY);

    size_t total = strlen(header) + out_len + 3;
    char *body = (char *)malloc(total);
    if (!body) {
        Serial.println("[post] out of memory for body");
        free(b64);
        return;
    }
    strcpy(body, header);
    strcat(body, b64);
    strcat(body, "\"}");
    free(b64);
    esp_task_wdt_reset();

    HTTPClient http;
    http.begin(BACKEND_URL);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-DEVICE-KEY", DEVICE_KEY);
    http.setTimeout(15000);
    int code = http.POST((uint8_t *)body, strlen(body));
    Serial.printf("[post] seq=%u size=%uB http=%d\n", seq - 1, fb->len, code);
    http.end();
    free(body);
    esp_task_wdt_reset();
}

void setup() {
    Serial.begin(115200);
    delay(500);
    seq = esp_random();
    Serial.println("\n[boot] freestand-cam starting");

    esp_task_wdt_init(WDT_TIMEOUT_S, true);
    esp_task_wdt_add(NULL);

    connectWiFi();

    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    Serial.print("[ntp] syncing");
    time_t now = 0;
    int ntp_attempts = 0;
    while (now < 1000000000 && ntp_attempts < 20) {
        delay(500);
        Serial.print(".");
        time(&now);
        ntp_attempts++;
        esp_task_wdt_reset();
    }
    Serial.println(now >= 1000000000 ? " ok" : " timeout (continuing)");

    initOTA();
    initCamera();
}

static unsigned long lastCapture = 0;

void loop() {
    esp_task_wdt_reset();
    ArduinoOTA.handle();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[wifi] reconnecting...");
        WiFi.reconnect();
        delay(2000);
        return;
    }

    if (millis() - lastCapture >= CAPTURE_INTERVAL_MS) {
        lastCapture = millis();
        esp_task_wdt_reset();
        camera_fb_t *fb = esp_camera_fb_get();
        esp_task_wdt_reset();
        if (!fb) {
            Serial.println("[camera] capture failed");
            return;
        }
        postFrame(fb);
        esp_camera_fb_return(fb);
        esp_task_wdt_reset();
    }
}
