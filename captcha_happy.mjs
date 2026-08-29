#!/usr/bin/env node
// captcha_happy.mjs — in-process happy-dom Aliyun captcha solver for chat.z.ai.
//
// Ported from TriDefender/zcode-api src/proxy/captcha-happy.ts (proven Node
// happy-dom solver). Target adapted from zcode.z.ai to chat.z.ai:
//   - origin / referer: https://chat.z.ai/
//   - default SceneId: didk33e0 (chat.z.ai), region sgp, prefix no8xfe
//
// No browser, no canvas/playwright/Chromium. One solve ≈ 1-3s.
//
// Mechanics:
//  1. cookie priming of https://chat.z.ai/ (5-min cache)
//  2. CDN disk cache at ~/.zai-captcha-cdn-cache/<sha1(url)> + in-mem cache
//  3. installNativeToString (mask JS-implemented platform APIs as native)
//  4. per-request client-hint / UA / origin / referer injection (interceptor)
//  5. guest-side patches (Event.isTrusted, HTMLDocument naming)
//  6. solve contract: initAliyunCaptcha + getInstance().startTracelessVerification()
//
// CLI:
//   node captcha_happy.mjs [--scene didk33e0] [--region sgp] [--prefix no8xfe]
//                          [--timeout-ms 30000] [--attempts 3]
// Prints exactly one JSON line to stdout on completion:
//   {"ok":true,"captcha":"<base64 captcha_verify_param>","elapsed_ms":1234,"attempts":1}
//   {"ok":false,"error":"..."}
// All diagnostics go to stderr.

import { Browser, PropertySymbol } from "happy-dom";
import WindowBrowserContext from "happy-dom/lib/window/WindowBrowserContext.js";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Worker } from "node:worker_threads";

const ORIGIN = "https://chat.z.ai";
const CDN_CACHE_DIR = path.join(os.homedir(), ".zai-captcha-cdn-cache");
const _memCdnCache = new Map();
let _cookieCache = { cookies: [], ts: 0 };
const COOKIE_CACHE_TTL_MS = 5 * 60 * 1000;
const _DEBUG = /^(1|true|yes)$/i.test(process.env.CAPTCHA_DEBUG || "");

// ── Blocking fetch for sync XHR ──────────────────────────────────────────────
// happy-dom implements sync XHR by spawning `process.argv[0] -e <script>`.
// That works in plain Node but is slow and fragile; run the request on a
// worker thread instead and wake the blocked host thread via Atomics.
const SYNC_FETCH_BUF_BYTES = 8 * 1024 * 1024;
const SYNC_FETCH_HEADER_BYTES = 64;
let _syncFetchWorker = null;

const SYNC_WORKER_SRC = `
  const { parentPort } = require("node:worker_threads");
  const enc = new TextEncoder();
  parentPort.on("message", (m) => {
    (async () => {
      const i32 = new Int32Array(m.sab);
      const u8 = new Uint8Array(m.sab);
      const payloadAt = 64;
      const fail = (msg) => {
        const b = enc.encode(msg);
        u8.set(b, payloadAt);
        i32[5] = b.length; i32[1] = 0; i32[2] = 0; i32[3] = 0; i32[4] = 0;
        i32[0] = 2; Atomics.notify(i32, 0);
      };
      try {
        const res = await fetch(m.url, m.init);
        const body = Buffer.from(await res.arrayBuffer());
        const headers = {};
        for (const [k, v] of res.headers) headers[k] = v;
        const setCookie = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
        const statusText = enc.encode(res.statusText || "");
        const headersJson = enc.encode(JSON.stringify(headers));
        const setCookieJson = enc.encode(JSON.stringify(setCookie));
        let off = payloadAt;
        u8.set(statusText, off); i32[2] = statusText.length; off += statusText.length;
        u8.set(headersJson, off); i32[3] = headersJson.length; off += headersJson.length;
        u8.set(setCookieJson, off); i32[4] = setCookieJson.length; off += setCookieJson.length;
        u8.set(body, off); i32[5] = body.length;
        i32[1] = res.status;
        i32[0] = 1; Atomics.notify(i32, 0);
      } catch (err) {
        fail(String((err && err.message) || err));
      }
    })();
  });
`;

function ensureSyncFetchWorker() {
  if (_syncFetchWorker) return _syncFetchWorker;
  _syncFetchWorker = new Worker(SYNC_WORKER_SRC, { eval: true });
  return _syncFetchWorker;
}

function syncFetchBlocking(url, init, timeoutMs = 30_000) {
  try {
    const worker = ensureSyncFetchWorker();
    const sab = new SharedArrayBuffer(SYNC_FETCH_HEADER_BYTES + SYNC_FETCH_BUF_BYTES);
    const i32 = new Int32Array(sab);
    const u8 = new Uint8Array(sab);
    worker.postMessage({ sab, url, init });
    const waitResult = Atomics.wait(i32, 0, 0, timeoutMs);
    if (waitResult === "timed-out") return { error: "sync fetch timeout" };
    const dec = new TextDecoder();
    let off = SYNC_FETCH_HEADER_BYTES;
    const readSlice = (len) => {
      const slice = u8.subarray(off, off + len);
      off += len;
      return slice;
    };
    const statusText = dec.decode(readSlice(i32[2]));
    const headers = i32[3] ? JSON.parse(dec.decode(readSlice(i32[3]))) : {};
    const setCookie = i32[4] ? JSON.parse(dec.decode(readSlice(i32[4]))) : [];
    const body = Buffer.from(readSlice(i32[5]));
    if (i32[0] === 2) return { error: dec.decode(u8.subarray(SYNC_FETCH_HEADER_BYTES, SYNC_FETCH_HEADER_BYTES + i32[5])) || "sync fetch failed" };
    return { status: i32[1], statusText, headers, setCookie, body };
  } catch (err) {
    try { _syncFetchWorker?.terminate(); } catch (_) {}
    _syncFetchWorker = null;
    return { error: `sync fetch error: ${err?.message ?? err}` };
  }
}

function shutdownSyncFetchWorker() {
  try { _syncFetchWorker?.terminate(); } catch (_) {}
  _syncFetchWorker = null;
}

// ── Fingerprint ──────────────────────────────────────────────────────────────
function generateFingerprint() {
  const userAgent =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36";
  const uaMajor = "151";
  const uaFull = "151.0.7922.109";
  const platform = "Win32";
  const screen = { w: 1280, h: 720, aw: 1280, ah: 720 };
  const webglUnmaskedVendor = "Google Inc. (Google)";
  const webglUnmaskedRenderer =
    "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)";
  const canvasImage =
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
  return { userAgent, uaMajor, uaFull, platform, screen, webglUnmaskedVendor, webglUnmaskedRenderer, canvasImage };
}

const fp = generateFingerprint();

const HTML = `<!DOCTYPE html><html><head></head><body>
<div id="cap"></div><button id="btn"></button>
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
</body></html>`;

function debug(msg) {
  if (_DEBUG) process.stderr.write(`${msg}\n`);
}

function diskPathFor(url) {
  return path.join(CDN_CACHE_DIR, crypto.createHash("sha1").update(String(url)).digest("hex"));
}

function sniffMime(url) {
  if (/\.js(\?|$)/i.test(url)) return "application/javascript";
  if (/\.css(\?|$)/i.test(url)) return "text/css";
  if (/\.png(\?|$)/i.test(url)) return "image/png";
  if (/\.(jpg|jpeg)(\?|$)/i.test(url)) return "image/jpeg";
  if (/\.json(\?|$)/i.test(url)) return "application/json";
  return "application/octet-stream";
}

// ── Request log / stall tracking ─────────────────────────────────────────────
const _requestLog = [];

// ── CDN cache access ─────────────────────────────────────────────────────────
function getCachedBody(url) {
  const mem = _memCdnCache.get(url);
  if (mem) return mem;
  try {
    const p = diskPathFor(url);
    if (fs.existsSync(p)) {
      const body = fs.readFileSync(p);
      _memCdnCache.set(url, body);
      return body;
    }
  } catch (_) {}
  return null;
}

async function fetchAndStore(url) {
  try {
    const res = await fetch(url, { headers: { "user-agent": fp.userAgent } });
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.length > 0) {
      _memCdnCache.set(url, buf);
      try {
        fs.mkdirSync(CDN_CACHE_DIR, { recursive: true });
        fs.writeFileSync(diskPathFor(url), buf);
      } catch (err) {
        debug(`[cache-write-err] ${url}: ${err.message}`);
      }
    }
    return buf;
  } catch (err) {
    debug(`[loader-fetch-err] ${url}: ${err.message}`);
    return null;
  }
}

// ── Request header injection (every frame request: XHR, fetch, scripts) ─────
function injectRequestHeaders(request) {
  const h = request.headers;
  try {
    h.set("sec-ch-ua", '"Chromium";v="' + fp.uaMajor + '", "Not)A;Brand";v="24"');
    h.set("sec-ch-ua-mobile", "?0");
    h.set("sec-ch-ua-platform", '"Windows"');
    h.set("user-agent", fp.userAgent);
    h.set("accept-language", "en-US,en;q=0.9");
    h.set("referer", `${ORIGIN}/`);
    let origin = null;
    try {
      const u = new URL(request.url);
      const method = String(request.method || "GET").toUpperCase();
      const crossOrigin = u.origin !== ORIGIN;
      if (crossOrigin || (method !== "GET" && method !== "HEAD")) {
        origin = ORIGIN;
      }
    } catch (_) {}
    if (origin) h.set("origin", origin);
  } catch (_) {}
}

function cookieHeader(request) {
  try {
    const ctx = global.__browserFrame.page.context;
    const u = new URL(request.url);
    if (request.credentials === "omit") return null;
    const cookies = ctx.cookieContainer.getCookies(u, false);
    if (cookies.length > 0) {
      return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
    }
  } catch (_) {}
  return null;
}

function parseSetCookie(raw, url) {
  const u = new URL(url);
  const parts = raw.split(";");
  const pair = parts[0].split("=");
  const cookie = {
    name: pair[0].trim(),
    value: pair.slice(1).join("=").trim(),
    url: u.origin,
    domain: u.hostname,
    path: "/",
  };
  for (const p of parts.slice(1)) {
    const kv = p.trim().split(/=(.*)/s);
    const k = (kv[0] || "").toLowerCase();
    if (k === "domain" && kv[1]) cookie.domain = kv[1];
    if (k === "path" && kv[1]) cookie.path = kv[1];
    if (k === "expires") cookie.expires = new Date(kv[1]).getTime();
    if (k === "max-age") cookie.maxAge = parseInt(kv[1], 10);
    if (k === "httponly") cookie.httpOnly = true;
    if (k === "secure") cookie.secure = true;
    if (k === "samesite") cookie.sameSite = kv[1];
  }
  return cookie;
}

function storeSetCookies(res, url) {
  try {
    const list = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
    if (list.length && global.__cookieContainer) {
      for (const raw of list) {
        try { global.__cookieContainer.addCookies([parseSetCookie(raw, url)]); } catch (_) {}
      }
    }
  } catch (_) {}
}

// ── The interceptor: replaces happy-dom's network layer completely ───────────
function makeInterceptor() {
  return {
    async beforeAsyncRequest({ request, window: w }) {
      const url = request.url;
      _requestLog.push({ at: Date.now(), method: request.method, url });
      injectRequestHeaders(request);
      if (/\balicdn\.com/i.test(url)) {
        const body = getCachedBody(url);
        if (body) {
          return new w.Response(body, {
            status: 200,
            statusText: "OK",
            headers: { "content-type": sniffMime(url) },
          });
        }
      }
      // Passthrough via global fetch; cache alicdn bodies for next time.
      try {
        const init = { method: request.method, headers: {} };
        request.headers.forEach((value, key) => {
          init.headers[key] = value;
        });
        const cookie = cookieHeader(request);
        if (cookie) init.headers.cookie = cookie;
        let hasBody = false;
        try {
          if (request.body) {
            const ab = await request.arrayBuffer();
            if (ab && ab.byteLength > 0) {
              init.body = ab;
              hasBody = true;
            }
          }
        } catch (_) {}
        const res = await fetch(url, init);
        const buf = Buffer.from(await res.arrayBuffer());
        storeSetCookies(res, url);
        if (/\balicdn\.com/i.test(url) && !hasBody && buf.length > 0) {
          _memCdnCache.set(url, buf);
          try {
            fs.mkdirSync(CDN_CACHE_DIR, { recursive: true });
            fs.writeFileSync(diskPathFor(url), buf);
          } catch (_) {}
        }
        debug(`[xhr] ${request.method} ${new URL(url).hostname}${new URL(url).pathname} -> ${res.status} (${buf.length}b)`);
        const headers = {};
        const ct = res.headers.get("content-type");
        if (ct) headers["content-type"] = ct;
        return new w.Response(buf, {
          status: res.status,
          statusText: res.statusText || "",
          headers,
        });
      } catch (err) {
        debug(`[xhr-err] ${url}: ${err.message}`);
        return new w.Response("", { status: 503, statusText: "passthrough failed" });
      }
    },
    beforeSyncRequest({ request, window: w }) {
      const url = request.url;
      _requestLog.push({ at: Date.now(), method: request.method, url, sync: true });
      injectRequestHeaders(request);
      let body = null;
      if (/\balicdn\.com/i.test(url)) {
        body = getCachedBody(url);
      }
      if (body) {
        return {
          status: 200,
          statusText: "OK",
          ok: true,
          url,
          redirected: false,
          headers: new w.Headers({ "content-type": sniffMime(url) }),
          body: Buffer.from(body),
          [PropertySymbol.virtualServerFile]: null,
        };
      }
      const init = { method: request.method, headers: {} };
      request.headers.forEach((value, key) => {
        init.headers[key] = value;
      });
      const cookie = cookieHeader(request);
      if (cookie) init.headers.cookie = cookie;
      try {
        if (request.body) {
          const ab = request.body;
          if (ab && ab.byteLength > 0) init.body = ab;
        }
      } catch (_) {}
      const res = syncFetchBlocking(url, init);
      if (res.error) {
        process.stderr.write(`[sync-xhr-err] ${url}: ${res.error}\n`);
        return new w.Response("", { status: 503, statusText: "sync fetch failed" });
      }
      try {
        if (global.__cookieContainer) {
          for (const raw of res.setCookie || []) {
            try { global.__cookieContainer.addCookies([parseSetCookie(raw, url)]); } catch (_) {}
          }
        }
      } catch (_) {}
      const hdrs = {};
      for (const [k, v] of Object.entries(res.headers || {})) hdrs[k] = String(v);
      return {
        status: res.status,
        statusText: res.statusText || "",
        ok: res.status >= 200 && res.status < 300,
        url,
        redirected: false,
        headers: new w.Headers(hdrs),
        body: Buffer.from(res.body),
        [PropertySymbol.virtualServerFile]: null,
      };
    },
  };
}

// ── Parse-fail instrumentation (host side) ───────────────────────────────────
function installEvalInstrumentation(w) {
  const sym = PropertySymbol && PropertySymbol.evaluateScript;
  if (!sym || typeof w[sym] !== "function") return;
  const orig = w[sym];
  w[sym] = function (code, options) {
    try {
      return orig.call(this, code, options);
    } catch (err) {
      try {
        const src = String(code || "");
        const filename = (options && options.filename) || "?";
        process.stderr.write(
          `\n[EVAL-PARSE-FAIL] file=${filename} len=${src.length}\n` +
            `  head300: ${JSON.stringify(src.slice(0, 300))}\n` +
            `  err: ${err && err.message}\n`,
        );
        if (/^https?:/.test(filename)) {
          (async () => {
            try {
              const res = await fetch(filename, { headers: { "user-agent": fp.userAgent } });
              const fresh = Buffer.from(await res.arrayBuffer());
              if (fresh.length > 0 && fresh.length !== src.length) {
                process.stderr.write(`[EVAL-CACHE-MISMATCH] deleting ${diskPathFor(filename)}\n`);
                try { fs.unlinkSync(diskPathFor(filename)); } catch (_) {}
                _memCdnCache.delete(filename);
              }
            } catch (_) {}
          })();
        }
      } catch (_) {}
      throw err;
    }
  };
}

// ── Mask JS-implemented platform APIs as native ──────────────────────────────
function installNativeToString(w) {
  const realToString = Function.prototype.toString;
  const nativeRe = /\[native code\]/;
  const mask = (fn) => {
    if (typeof fn !== "function") return;
    try {
      if (nativeRe.test(realToString.call(fn))) return;
      const name = fn.name || "";
      const nativeStr = `function ${name}() { [native code] }`;
      Object.defineProperty(fn, "toString", {
        value: () => nativeStr,
        configurable: true,
        writable: true,
      });
    } catch (_) {}
  };
  const seen = new w.Set();
  const maskObj = (obj, depth) => {
    if (!obj || (typeof obj !== "object" && typeof obj !== "function") || depth > 5) return;
    try {
      if (obj.constructor && obj.constructor.prototype !== Object.prototype) {
        const ctorName = obj.constructor.name;
        if (/^(WriteStream|ReadStream|Socket|Process|Timeout|Immediate)$/.test(ctorName)) return;
      }
    } catch (_) {}
    if (seen.has(obj)) return;
    try { seen.add(obj); } catch (_) { return; }
    let names = [];
    try {
      names = Object.getOwnPropertyNames(obj);
    } catch (_) { return; }
    for (const name of names) {
      if (name === "toString" || name === "constructor") continue;
      let desc;
      try {
        desc = Object.getOwnPropertyDescriptor(obj, name);
      } catch (_) { continue; }
      if (!desc) continue;
      if (typeof desc.value === "function") {
        mask(desc.value);
      } else if (typeof desc.get === "function") {
        mask(desc.get);
        try {
          const v = desc.get.call(obj);
          if (typeof v === "function") mask(v);
        } catch (_) {}
      }
      if (depth < 3) {
        try {
          const v = desc.value;
          if (v && (typeof v === "function" || typeof v === "object")) maskObj(v, depth + 1);
        } catch (_) {}
      }
    }
  };
  const targets = [
    w,
    w.navigator,
    w.document,
    w.Document && w.Document.prototype,
    w.Element && w.Element.prototype,
    w.HTMLElement && w.HTMLElement.prototype,
    w.Node && w.Node.prototype,
    w.EventTarget && w.EventTarget.prototype,
    w.HTMLCanvasElement && w.HTMLCanvasElement.prototype,
    w.XMLHttpRequest && w.XMLHttpRequest.prototype,
    w.Event && w.Event.prototype,
    w.Window && w.Window.prototype,
  ].filter(Boolean);
  for (const t of targets) {
    try { maskObj(t, 0); } catch (_) {}
  }
}

// ── Guest-context patches (run via window.eval inside the VM realm) ──────────
const GUEST_EVAL_PATCH = `
(function() {
  try {
    Object.defineProperty(Event.prototype, "isTrusted", {
      get() { return true; },
      configurable: true
    });
  } catch (e) {}
  try {
    if (window.HTMLDocument) {
      Object.defineProperty(window.HTMLDocument, "name", { value: "HTMLDocument", configurable: true });
      Object.defineProperty(window.HTMLDocument.prototype, Symbol.toStringTag, { value: "HTMLDocument", configurable: true });
    }
  } catch (e) {}
  try {
    Object.defineProperty(window.Document.prototype, Symbol.toStringTag, { value: "HTMLDocument", configurable: true });
  } catch (e) {}
  try {
    window.addEventListener("unhandledrejection", function(e) {
      if (${_DEBUG ? "true" : "false"}) {
        var r = e && e.reason;
        try { console.error("[UH-REASON]", typeof r, r && r.message); } catch (e2) {}
      }
    });
  } catch (e) {}
})();
`;

// ── Browser-ish polyfills ────────────────────────────────────────────────────
function applyPolyfills(w) {
  if (typeof w.Option !== "function") {
    w.Option = class Option extends w.HTMLOptionElement {
      constructor(text, value, defaultSelected, selected) {
        super();
        if (text !== undefined) {
          const el = w.document.createElement("option");
          el.text = text;
          if (value !== undefined) el.value = value;
          if (defaultSelected) el.defaultSelected = true;
          if (selected) el.selected = true;
          return el;
        }
      }
    };
  }
  if (typeof w.Video !== "function" && w.HTMLVideoElement) {
    w.Video = class Video extends w.HTMLVideoElement {
      constructor() { return w.document.createElement("video"); }
    };
  }

  if (typeof w.alert !== "function") w.alert = () => {};
  if (typeof w.prompt !== "function") w.prompt = () => null;
  if (typeof w.confirm !== "function") w.confirm = () => false;
  if (typeof w.open !== "function") w.open = () => null;
  if (typeof w.close !== "function") w.close = () => {};
  try { Object.defineProperty(w, "alert", { value: w.alert, configurable: true, writable: true }); } catch (_) {}
  try { Object.defineProperty(w, "prompt", { value: w.prompt, configurable: true, writable: true }); } catch (_) {}
  try { Object.defineProperty(w, "confirm", { value: w.confirm, configurable: true, writable: true }); } catch (_) {}
  // happy-dom's own open()/close() are destructive; the risk engine probes them.
  try { Object.defineProperty(w, "open", { value: () => null, configurable: true, writable: true }); } catch (_) {}
  try { Object.defineProperty(w, "close", { value: () => {}, configurable: true, writable: true }); } catch (_) {}

  const extraGlobals = {
    print: () => {},
    stop: () => {},
    moveTo: () => {},
    moveBy: () => {},
    showModalDialog: () => null,
    find: () => false,
  };
  for (const [k, v] of Object.entries(extraGlobals)) {
    try { Object.defineProperty(w, k, { value: v, configurable: true, writable: true }); } catch (_) {}
  }

  if (!w.EventSource) {
    w.EventSource = class {
      constructor() {
        this.readyState = 2;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
      }
      close() { this.readyState = 2; }
      addEventListener() {}
      removeEventListener() {}
    };
  }

  if (!w.Beacon) w.Beacon = class {};

  if (!w.RTCPeerConnection) {
    w.RTCPeerConnection = class {
      constructor() {}
      createDataChannel() { return {}; }
      close() {}
      createOffer() { return Promise.resolve({}); }
      setLocalDescription() { return Promise.resolve(); }
      addEventListener() {}
      removeEventListener() {}
    };
  }

  if (!w.MessageChannel) {
    w.MessageChannel = class {
      constructor() {
        this.port1 = { onmessage: null, postMessage() {}, start() {}, close() {}, addEventListener() {}, removeEventListener() {} };
        this.port2 = { onmessage: null, postMessage() {}, start() {}, close() {}, addEventListener() {}, removeEventListener() {} };
      }
    };
  }

  w.IntersectionObserver =
    w.IntersectionObserver ||
    class {
      constructor(cb) { this.cb = cb; }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return []; }
    };

  w.ResizeObserver =
    w.ResizeObserver ||
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };

  w.DeviceOrientationEvent =
    w.DeviceOrientationEvent ||
    class extends w.Event {
      constructor(type, opts) { super(type, opts); }
    };
  w.DeviceMotionEvent =
    w.DeviceMotionEvent ||
    class extends w.Event {
      constructor(type, opts) { super(type, opts); }
    };

  w.requestIdleCallback = w.requestIdleCallback || ((cb) => setTimeout(() => cb({ didTimeout: false, timeRemaining: () => 10 }), 1));
  w.cancelIdleCallback = w.cancelIdleCallback || ((id) => clearTimeout(id));

  w.matchMedia =
    w.matchMedia ||
    (() => ({
      matches: false,
      media: "",
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent() { return false; },
    }));

  if (!w.visualViewport) {
    const VisualViewport = function () {};
    VisualViewport.prototype = {
      width: fp.screen.w - 16,
      height: fp.screen.h - 120,
      scale: 1,
      offsetLeft: 0,
      offsetTop: 0,
      pageLeft: 0,
      pageTop: 0,
      onresize: null,
      onscroll: null,
      onscrollend: null,
    };
    w.VisualViewport = VisualViewport;
    w.visualViewport = Object.create(w.VisualViewport.prototype);
  }

  if (!w.indexedDB) {
    const IDBFactory = function () {};
    IDBFactory.prototype = {
      open: () => ({ onupgradeneeded: null, onsuccess: null, onerror: null }),
      deleteDatabase: () => ({}),
      databases: () => Promise.resolve([]),
    };
    w.IDBFactory = IDBFactory;
    w.indexedDB = Object.create(w.IDBFactory.prototype);
  }

  if (!w.speechSynthesis) {
    const SpeechSynthesis = function () {};
    SpeechSynthesis.prototype = {
      speak() {},
      cancel() {},
      pause() {},
      resume() {},
      getVoices: () => [],
    };
    w.SpeechSynthesis = SpeechSynthesis;
    w.speechSynthesis = Object.create(w.SpeechSynthesis.prototype);
    w.SpeechSynthesisUtterance = function () {};
  }

  w.Worker =
    w.Worker ||
    class {
      postMessage() {}
      terminate() {}
      addEventListener() {}
      removeEventListener() {}
    };

  w.Notification =
    w.Notification ||
    class {
      static permission = "default";
      static requestPermission() { return Promise.resolve("default"); }
      close() {}
    };

  // Canvas / WebGL
  const proto = w.HTMLCanvasElement.prototype;
  const nativeGetContext = typeof proto.getContext === "function" ? proto.getContext : null;
  proto.getContext = function (type, ...rest) {
    if (/webgl/i.test(type)) {
      return makeWebGLMock(this);
    }
    if (nativeGetContext) {
      try {
        const ctx = nativeGetContext.call(this, type, ...rest);
        if (ctx) return ctx;
      } catch (_) {}
    }
    return make2DStub(this);
  };

  function makeWebGLMock(canvas) {
    return {
      canvas,
      getParameter(p) {
        if (p === 7936) return "WebKit";
        if (p === 7937) return "WebKit WebGL";
        if (p === 7938) return "WebGL 1.0 (OpenGL ES 2.0 Chromium)";
        if (p === 35724) return "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)";
        if (p === 0x9245) return fp.webglUnmaskedVendor;
        if (p === 0x9246) return fp.webglUnmaskedRenderer;
        return "Intel Inc.";
      },
      getExtension(name) {
        if (name === "WEBGL_debug_renderer_info") {
          return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
        }
        return null;
      },
      getSupportedExtensions() {
        return [
          "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
          "EXT_disjoint_timer_query", "EXT_float_blend", "EXT_frag_depth",
          "EXT_texture_compression_bptc", "EXT_texture_compression_rgtc",
          "EXT_texture_filter_anisotropic", "EXT_sRGB", "KHR_parallel_shader_compile",
          "OES_element_index_uint", "OES_fbo_render_mipmap", "OES_standard_derivatives",
          "OES_texture_float", "OES_texture_float_linear", "OES_texture_half_float",
          "OES_texture_half_float_linear", "OES_vertex_array_object",
          "WEBGL_color_buffer_float", "WEBGL_compressed_texture_astc",
          "WEBGL_compressed_texture_etc", "WEBGL_compressed_texture_etc1",
          "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
          "WEBGL_debug_renderer_info", "WEBGL_debug_shaders", "WEBGL_depth_texture",
          "WEBGL_draw_buffers", "WEBGL_lose_context", "WEBGL_multi_draw",
        ];
      },
      getContextAttributes() {
        return {
          alpha: true, antialias: true, depth: true,
          failIfMajorPerformanceCaveat: false, powerPreference: "default",
          premultipliedAlpha: true, preserveDrawingBuffer: false,
          stencil: false, desynchronized: false,
        };
      },
      getShaderPrecisionFormat() {
        return { precision: 23, rangeMin: 127, rangeMax: 127 };
      },
    };
  }

  function make2DStub(canvas) {
    return {
      canvas,
      fillRect() {},
      clearRect() {},
      getImageData: (_x, _y, w2 = 1, h2 = 1) => new w.ImageData(w2, h2),
      putImageData() {},
      createImageData: (w2 = 1, h2 = 1) => new w.ImageData(w2, h2),
      setTransform() {},
      transform() {},
      drawImage() {},
      save() {},
      restore() {},
      beginPath() {},
      moveTo() {},
      lineTo() {},
      bezierCurveTo() {},
      quadraticCurveTo() {},
      closePath() {},
      clip() {},
      stroke() {},
      fill() {},
      arc() {},
      rect() {},
      ellipse() {},
      translate() {},
      scale() {},
      rotate() {},
      fillText() {},
      strokeText() {},
      measureText: (t) => ({ width: String(t).length * 8 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      createRadialGradient: () => ({ addColorStop() {} }),
      createPattern: () => ({}),
      isPointInPath: () => false,
      font: "10px sans-serif",
      textBaseline: "alphabetic",
      textAlign: "start",
      fillStyle: "#000",
      strokeStyle: "#000",
      globalAlpha: 1,
      lineWidth: 1,
      shadowBlur: 0,
      shadowColor: "",
    };
  }

  const nativeToDataURL = typeof proto.toDataURL === "function" ? proto.toDataURL : null;
  proto.toDataURL = function (...a) {
    try {
      if (nativeToDataURL) return nativeToDataURL.apply(this, a);
    } catch (_) {}
    return fp.canvasImage;
  };
  if (typeof proto.toBlob !== "function") {
    proto.toBlob = (cb) => cb && cb(new w.Blob());
  }

  w.OffscreenCanvas =
    w.OffscreenCanvas ||
    class {
      constructor(width, height) {
        this.width = width;
        this.height = height;
      }
      getContext() {
        return proto.getContext.call(this);
      }
    };

  const audioMock = class {
    constructor() {
      this.sampleRate = 44100;
      this.currentTime = 0;
      this.state = "suspended";
    }
    createOscillator() {
      return {
        type: "sine",
        frequency: { value: 440, setValueAtTime() {} },
        connect() {},
        start() {},
        stop() {},
      };
    }
    createDynamicsCompressor() {
      return {
        threshold: { value: -24, setValueAtTime() {} },
        knee: { value: 30, setValueAtTime() {} },
        ratio: { value: 12, setValueAtTime() {} },
        attack: { value: 0.003, setValueAtTime() {} },
        release: { value: 0.25, setValueAtTime() {} },
        connect() {},
      };
    }
    createAnalyser() {
      return {
        fftSize: 2048,
        frequencyBinCount: 1024,
        getByteFrequencyData() {},
        getByteTimeDomainData() {},
        connect() {},
      };
    }
    createGain() {
      return { gain: { value: 1 }, connect() {} };
    }
    destination = {};
    resume() {
      this.state = "running";
      return Promise.resolve();
    }
    close() {
      this.state = "closed";
      return Promise.resolve();
    }
  };
  w.AudioContext = w.AudioContext || audioMock;
  w.OfflineAudioContext =
    w.OfflineAudioContext ||
    class extends audioMock {
      constructor(_channels, length, sampleRate) {
        super();
        this.length = length;
        this.sampleRate = sampleRate;
      }
      startRendering() {
        const len = this.length || 44100;
        const sr = this.sampleRate || 44100;
        const buf = new Float32Array(len);
        for (let i = 0; i < len; i += 1) {
          const t = i / sr;
          buf[i] =
            Math.sin(2 * Math.PI * 1000 * t) * Math.exp(-t * 1.2) * 0.6 +
            Math.sin(2 * Math.PI * 3000 * t) * Math.exp(-t * 1.5) * 0.25 +
            Math.sin(2 * Math.PI * 5000 * t) * Math.exp(-t * 2.0) * 0.12;
        }
        return Promise.resolve({
          numberOfChannels: 1,
          length: len,
          sampleRate: sr,
          getChannelData: () => buf,
        });
      }
    };

  w.requestAnimationFrame = w.requestAnimationFrame || ((cb) => setTimeout(() => cb(Date.now()), 16));
  w.cancelAnimationFrame = w.cancelAnimationFrame || ((id) => clearTimeout(id));

  try {
    Object.defineProperty(w.document, "hidden", { value: false, configurable: true });
    Object.defineProperty(w.document, "visibilityState", { value: "visible", configurable: true });
  } catch (_) {}

  if (!w.document.fonts) {
    w.document.fonts = {
      ready: Promise.resolve(),
      check: () => true,
      addEventListener() {},
      removeEventListener() {},
    };
  }

  if (!w.chrome) {
    w.chrome = {
      app: {
        isInstalled: false,
        InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
        RunningState: { CANNOT_RUN: "cannot_run", CAN_RUN: "can_run", RUNNING: "running" },
        getDetails() { return null; },
        getIsInstalled() { return false; },
        installState(cb) { if (cb) cb("not_installed"); },
        runningState(cb) { if (cb) cb("cannot_run"); },
      },
      csi() {
        const now = Date.now();
        return { startE: now - 100, onloadT: now, pageT: 100, tran: 15 };
      },
      loadTimes() {
        const now = Date.now() / 1000;
        return {
          requestTime: now - 0.1, startLoadTime: now - 0.1,
          commitLoadTime: now - 0.05, finishDocumentLoadTime: now,
          finishLoadTime: now, firstPaintTime: now - 0.02,
          firstPaintAfterLoadTime: 0, navigationType: "Other",
          wasFetchedViaSpdy: true, wasNpnNegotiated: true,
          npnNegotiatedProtocol: "h2", wasAlternateProtocolAvailable: false,
          connectionInfo: "h2",
        };
      },
    };
  }

  // navigator patch
  const nav = w.navigator;
  const plugins = createNavigatorPlugins(w);
  const navPatch = {
    userAgent: fp.userAgent,
    platform: fp.platform,
    language: "en-US",
    languages: ["en-US", "en"],
    vendor: "Google Inc.",
    webdriver: false,
    hardwareConcurrency: 12,
    deviceMemory: 8,
    maxTouchPoints: 0,
    cookieEnabled: true,
    plugins: plugins.plugins,
    mimeTypes: plugins.mimeTypes,
    appVersion: fp.userAgent.replace(/^Mozilla\//, ""),
    appName: "Netscape",
    appCodeName: "Mozilla",
    product: "Gecko",
    productSub: "20030107",
    vendorSub: "",
    oscpu: undefined,
    doNotTrack: null,
    sendBeacon: (url, data) => {
      try {
        const xhr = new w.XMLHttpRequest();
        xhr.open("POST", url, true);
        xhr.send(data);
        return true;
      } catch (_) {
        return false;
      }
    },
  };
  for (const [k, v] of Object.entries(navPatch)) {
    try { Object.defineProperty(nav, k, { value: v, configurable: true }); } catch (_) {}
  }

  const makeNS = (protoObj) => {
    const C = new w.Function();
    C.prototype = protoObj;
    return new C();
  };

  if (!nav.connection) {
    const NetInfo = () => {};
    NetInfo.prototype = { onchange: null, effectiveType: "4g", rtt: 50, downlink: 10, saveData: false };
    w.NetworkInformation = NetInfo;
    try { Object.defineProperty(nav, "connection", { value: makeNS(NetInfo.prototype), configurable: true }); } catch (_) {}
  }
  if (!nav.userAgentData) {
    const UAData = function () {};
    UAData.prototype = {
      brands: [
        { brand: "Not=A?Brand", version: "99" },
        { brand: "Google Chrome", version: fp.uaMajor },
        { brand: "Chromium", version: fp.uaMajor },
      ],
      mobile: false,
      platform: "Windows",
      getHighEntropyValues: () =>
        Promise.resolve({
          brands: [
            { brand: "Not=A?Brand", version: "99" },
            { brand: "Google Chrome", version: fp.uaMajor },
            { brand: "Chromium", version: fp.uaMajor },
          ],
          mobile: false,
          platform: "Windows",
          platformVersion: "10.0.0",
          architecture: "x86",
          model: "",
          uaFullVersion: fp.uaFull,
          fullVersionList: [
            { brand: "Not=A?Brand", version: "99" },
            { brand: "Google Chrome", version: fp.uaFull },
            { brand: "Chromium", version: fp.uaFull },
          ],
        }),
    };
    try { Object.defineProperty(nav, "userAgentData", { value: makeNS(UAData.prototype), configurable: true }); } catch (_) {}
  }
  if (!w.Permissions) {
    const Perms = () => {};
    Perms.prototype = {
      query: (param) =>
        Promise.resolve({ state: param.name === "notifications" ? "prompt" : "granted", onchange: null }),
    };
    w.Permissions = Perms;
  }
  try {
    if (!nav.permissions) Object.defineProperty(nav, "permissions", { value: makeNS(w.Permissions.prototype), configurable: true });
  } catch (_) {}
  try {
    if (!nav.clipboard)
      Object.defineProperty(nav, "clipboard", {
        value: makeNS({ readText: () => Promise.resolve(""), writeText: () => Promise.resolve() }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.geolocation)
      Object.defineProperty(nav, "geolocation", {
        value: makeNS({
          getCurrentPosition: (s) => s && s({ coords: { latitude: 0, longitude: 0, accuracy: 1 } }),
          watchPosition: () => 1,
          clearWatch: () => {},
        }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.credentials)
      Object.defineProperty(nav, "credentials", {
        value: makeNS({ get: () => Promise.resolve(null), create: () => Promise.resolve(null), store: () => Promise.resolve(), preventSilentAccess: () => Promise.resolve() }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.storage)
      Object.defineProperty(nav, "storage", {
        value: makeNS({ estimate: () => Promise.resolve({ quota: 1e8, usage: 0 }), persisted: () => Promise.resolve(false), persist: () => Promise.resolve(false) }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.usb)
      Object.defineProperty(nav, "usb", {
        value: makeNS({ getDevices: () => Promise.resolve([]), requestDevice: () => Promise.reject(new Error("no devices")) }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.mediaDevices)
      Object.defineProperty(nav, "mediaDevices", {
        value: makeNS({ enumerateDevices: () => Promise.resolve([]), getUserMedia: () => Promise.reject(new Error("NotAllowedError")) }),
        configurable: true,
      });
  } catch (_) {}

  const screenPatch = {
    width: fp.screen.w,
    height: fp.screen.h,
    availWidth: fp.screen.w,
    availHeight: fp.screen.ah,
    availLeft: 0,
    availTop: 0,
    colorDepth: 24,
    pixelDepth: 24,
    orientation: { angle: 0, type: "landscape-primary", onchange: null },
  };
  for (const [k, v] of Object.entries(screenPatch)) {
    try { Object.defineProperty(w.screen, k, { get: () => v, configurable: true }); } catch (_) {}
  }

  w.outerWidth = fp.screen.w;
  w.outerHeight = fp.screen.h - 40;
  w.innerWidth = fp.screen.w - 16;
  w.innerHeight = fp.screen.h - 120;
  w.devicePixelRatio = 1;
}

function createNavigatorPlugins(w) {
  const indexed = [
    { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" },
    { name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", description: "" },
    { name: "Chromium PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", description: "" },
  ];
  const plugins = w.PluginArray ? Object.create(w.PluginArray.prototype) : {};
  const mockIndexed = [];
  for (let i = 0; i < indexed.length; i += 1) {
    const p = Object.create((w.Plugin && w.Plugin.prototype) || Object.prototype);
    Object.defineProperty(p, "name", { value: indexed[i].name, configurable: true, enumerable: true });
    Object.defineProperty(p, "filename", { value: indexed[i].filename, configurable: true, enumerable: true });
    Object.defineProperty(p, "description", { value: indexed[i].description, configurable: true, enumerable: true });
    Object.defineProperty(p, "length", { value: 1, configurable: true, enumerable: true });
    Object.defineProperty(p, "0", { value: p, configurable: true, enumerable: true });
    p.item = () => p;
    p.namedItem = () => p;
    plugins[i] = p;
    mockIndexed.push(p);
  }
  Object.defineProperty(plugins, "length", { value: indexed.length, configurable: true, enumerable: true });
  plugins.item = (i) => plugins[i] ?? null;
  plugins.namedItem = (name) => mockIndexed.find((p) => p.name === name) ?? null;
  plugins.refresh = () => {};
  const mimeTypes = w.MimeTypeArray ? Object.create(w.MimeTypeArray.prototype) : {};
  Object.defineProperty(mimeTypes, "length", { value: 0, configurable: true, enumerable: true });
  mimeTypes.item = () => null;
  mimeTypes.namedItem = () => null;
  return { plugins, mimeTypes };
}

// ── Behavioral priming (FeiLin human-motion buffer) ──────────────────────────
function simulateBehavior(w, durationMs = 600) {
  const { document, MouseEvent, KeyboardEvent, UIEvent } = w;
  if (!document || !MouseEvent) return;
  const fire = (type, ctor, opts) => {
    try {
      const Ctor = ctor || UIEvent;
      const ev = new Ctor(type, { bubbles: true, cancelable: true, view: w, ...opts });
      document.dispatchEvent(ev);
      if (document.body) document.body.dispatchEvent(ev);
    } catch (_) {}
  };
  let x = 140 + Math.random() * 30;
  let y = 110 + Math.random() * 20;
  const targetX = 540 + Math.random() * 40;
  const targetY = 380 + Math.random() * 30;
  const steps = 22;
  let i = 0;
  const start = Date.now();
  const moveStep = () => {
    if (i > steps) return;
    x += (targetX - x) * 0.16 + (Math.random() - 0.5) * 5;
    y += (targetY - y) * 0.16 + (Math.random() - 0.5) * 4;
    fire("mousemove", MouseEvent, {
      screenX: Math.round(x),
      screenY: Math.round(y),
      clientX: Math.round(x),
      clientY: Math.round(y),
      button: 0,
      buttons: 1,
    });
    i += 1;
    const done = Date.now() - start >= durationMs;
    if (i <= steps && !done) {
      setTimeout(moveStep, 26 + Math.floor(Math.random() * 32));
    } else {
      fire("mousedown", MouseEvent, { clientX: Math.round(x), clientY: Math.round(y), button: 0, buttons: 1 });
      fire("mouseup", MouseEvent, { clientX: Math.round(x), clientY: Math.round(y), button: 0, buttons: 0 });
      fire("click", MouseEvent, { clientX: Math.round(x), clientY: Math.round(y), button: 0 });
      try {
        fire("keyup", KeyboardEvent, { key: "a", code: "KeyA", keyCode: 65, which: 65 });
      } catch (_) {}
    }
  };
  moveStep();
}

function waitFor(cond, timeoutMs = 15_000, intervalMs = 40) {
  return new Promise((res, rej) => {
    const started = Date.now();
    const timer = setInterval(() => {
      let ok = false;
      try { ok = cond(); } catch (_) {}
      if (ok) {
        clearInterval(timer);
        res();
      } else if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        rej(new Error("timeout"));
      }
    }, intervalMs);
  });
}

// ── createDom ────────────────────────────────────────────────────────────────
async function createDom(region, prefix) {
  let cookies = [];
  const now = Date.now();
  if (_cookieCache.ts > 0 && now - _cookieCache.ts < COOKIE_CACHE_TTL_MS) {
    cookies = _cookieCache.cookies;
  } else {
    try {
      const res = await fetch(`${ORIGIN}/`, {
        headers: {
          "User-Agent": fp.userAgent,
          "sec-ch-ua": '"Chromium";v="' + fp.uaMajor + '", "Not)A;Brand";v="24"',
          "sec-ch-ua-mobile": "?0",
          "sec-ch-ua-platform": '"Windows"',
          "Accept-Language": "en-US,en;q=0.9",
        },
      });
      cookies = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
      _cookieCache = { cookies, ts: Date.now() };
    } catch (_) {}
  }

  const interceptor = makeInterceptor();

  if (!process.__capUnhandledRejectionHooked) {
    process.__capUnhandledRejectionHooked = true;
    // A single bad pe/risk-engine variant must only fail that one solve.
    process.on("uncaughtException", (err) => {
      try {
        const msg = err && err.message ? err.message : String(err);
        process.stderr.write(`[captcha-guest-uncaught] ${msg}\n`);
      } catch (_) {}
    });
  }

  const browser = new Browser({
    settings: {
      enableJavaScriptEvaluation: true,
      enableImageFileLoading: true,
      suppressInsecureJavaScriptEnvironmentWarning: true,
      navigator: { userAgent: fp.userAgent },
      viewport: { width: fp.screen.w, height: fp.screen.h, devicePixelRatio: 1 },
      fetch: {
        disableSameOriginPolicy: true,
        interceptor,
      },
    },
  });
  const page = browser.newPage();
  const w = page.mainFrame.window;

  const browserFrame = new WindowBrowserContext(w).getBrowserFrame();
  global.__browserFrame = browserFrame;
  global.__cookieContainer = browserFrame.page.context.cookieContainer;

  // Cookie priming from the origin page.
  for (const raw of cookies) {
    try {
      global.__cookieContainer.addCookies([parseSetCookie(raw, `${ORIGIN}/`)]);
    } catch (_) {}
  }

  const visitorId = crypto.randomUUID();
  const deviceMid = crypto.randomUUID();
  const pre = [
    { name: "visitor_id", value: visitorId, domain: "chat.z.ai" },
    { name: "device_mid", value: deviceMid, domain: "chat.z.ai" },
  ];
  for (const c of pre) {
    try {
      global.__cookieContainer.addCookies([{ ...c, url: ORIGIN, path: "/" }]);
    } catch (_) {}
  }

  applyPolyfills(w);
  installNativeToString(w);
  installEvalInstrumentation(w);
  if (w.Error) {
    w.Error.prepareStackTrace = Error.prepareStackTrace;
  }
  w.eval(GUEST_EVAL_PATCH);

  // Write the page HTML (loads the SDK script through the interceptor).
  w.document.write(HTML);

  // Give the SDK script a bounded window to load/execute; the solve loop
  // polls for initAliyunCaptcha anyway.
  await Promise.race([
    page.waitUntilComplete().catch(() => {}),
    new Promise((res) => setTimeout(res, 10_000)),
  ]);

  w.AliyunCaptchaConfig = { region, prefix };

  return { window: w, browserFrame, browser, page };
}

function destroyDom(dom) {
  const win = dom.window;
  try {
    const cap = win.document.getElementById("cap");
    if (cap) cap.replaceChildren();
  } catch (_) {}
  try {
    dom.browser.close();
  } catch (_) {
    try { win.happyDOM.close(); } catch (_) {}
  }
  try {
    global.__cookieContainer = null;
    global.__browserFrame = null;
  } catch (_) {}
  shutdownSyncFetchWorker();
}

function extractVerifyParam(param) {
  let verifyParam = param;
  if (param && typeof param === "object") {
    verifyParam = param.verifyParam || param.data || param.param;
  }
  if (!verifyParam || String(verifyParam).length < 20) {
    throw new Error("solver returned empty param: " + JSON.stringify(param));
  }
  const str = String(verifyParam);
  // Strict validation: a REAL Aliyun verify param is ~280 chars of base64
  // JSON containing certifyId + sceneId + isSign + a long securityToken.
  // Short junk without securityToken comes from a degraded SDK result path
  // and WILL be rejected upstream — never let it out of the solver.
  if (str.length < 200) {
    throw new Error(
      "verify param too short (" + str.length + " chars) — degraded result, refusing: " + str.slice(0, 80),
    );
  }
  try {
    const decoded = JSON.parse(Buffer.from(str, "base64").toString("utf8"));
    const secTok = decoded && (decoded.securityToken || decoded.SecurityToken);
    if (!secTok || String(secTok).length < 50) {
      throw new Error(
        "verify param missing securityToken — refusing degraded result: " + str.slice(0, 80),
      );
    }
  } catch (err) {
    if (err instanceof SyntaxError) {
      throw new Error("verify param not base64-JSON: " + str.slice(0, 80));
    }
    throw err;
  }
  return str;
}

function handleCaptchaResult(result) {
  if (result && typeof result === "object" && result.verifyResult === false) {
    throw new Error(
      "verify rejected: " + JSON.stringify({ verifyCode: result.verifyCode, certifyId: result.certifyId }),
    );
  }
  return result;
}

async function solveTraceless(opts) {
  const scene = opts.scene || "didk33e0";
  const region = opts.region || "sgp";
  const prefix = opts.prefix || "no8xfe";
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const stallMs = opts.stallMs ?? 6_000;

  const dom = await createDom(region, prefix);
  const w = dom.window;
  const solveStart = Date.now();
  try {
    await waitFor(() => typeof w.initAliyunCaptcha === "function", timeoutMs, 50);
    simulateBehavior(w, 600);

    const param = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const reqs = _requestLog
          .filter((r) => r.at >= solveStart)
          .map((r) => `${r.at - solveStart}ms ${r.method} ${String(r.url).replace(/^https?:\/\//, "").slice(0, 60)}`)
          .slice(-12);
        reject(new Error(`captcha solve timeout reqs=${JSON.stringify(reqs)}`));
      }, timeoutMs);
      // Fail-fast stall detector: healthy solves keep firing XHRs until verify.
      const stallTimer = setInterval(() => {
        const last = _requestLog[_requestLog.length - 1];
        if (last && Date.now() - last.at > stallMs) {
          clearTimeout(timer);
          clearInterval(stallTimer);
          reject(new Error(`captcha solve stall lastXhr=${last.at - solveStart}ms`));
        }
      }, 500);
      const finish = (fn) => (value) => {
        clearTimeout(timer);
        clearInterval(stallTimer);
        fn(value);
      };
      try {
        w.initAliyunCaptcha({
          SceneId: scene,
          mode: "popup",
          region,
          prefix,
          language: "en",
          element: "#cap",
          button: "#btn",
          captchaLogoImg: "",
          showErrorTip: false,
          getInstance: (inst) => {
            try {
              (inst.startTracelessVerification || inst.show).call(inst);
            } catch (e) {
              finish(reject)(new Error(`start: ${e.message}`));
            }
          },
          success: (result) => {
            try {
              finish(resolve)(handleCaptchaResult(result));
            } catch (err) {
              finish(reject)(err);
            }
          },
          fail: (err) => finish(reject)(new Error(`fail: ${JSON.stringify(err)}`)),
          onError: (err) => finish(reject)(new Error(`onError: ${JSON.stringify(err)}`)),
        });
      } catch (err) {
        clearTimeout(timer);
        reject(err);
      }
    });

    return extractVerifyParam(param);
  } finally {
    destroyDom(dom);
  }
}

// ── CLI ──────────────────────────────────────────────────────────────────────
function parseArgs(argv) {
  const args = {
    scene: process.env.CAPTCHA_SCENE || "didk33e0",
    region: process.env.CAPTCHA_REGION || "sgp",
    prefix: process.env.CAPTCHA_PREFIX || "no8xfe",
    timeoutMs: 30_000,
    attempts: Number(process.env.CAPTCHA_ATTEMPTS || 3),
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--scene") args.scene = argv[++i];
    else if (a === "--region") args.region = argv[++i];
    else if (a === "--prefix") args.prefix = argv[++i];
    else if (a === "--timeout-ms") args.timeoutMs = Number(argv[++i]);
    else if (a === "--attempts") args.attempts = Math.max(1, Number(argv[++i]));
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const started = Date.now();
  let lastErr = null;
  for (let attempt = 1; attempt <= args.attempts; attempt += 1) {
    try {
      const captcha = await solveTraceless(args);
      const out = {
        ok: true,
        captcha,
        elapsed_ms: Date.now() - started,
        attempts: attempt,
      };
      debug(`[solve-ok] attempt=${attempt} elapsed=${out.elapsed_ms}ms len=${captcha.length}`);
      process.stdout.write(JSON.stringify(out) + "\n");
      return 0;
    } catch (err) {
      lastErr = err;
      process.stderr.write(`[solve-fail] attempt=${attempt}/${args.attempts}: ${err.message}\n`);
    }
  }
  process.stdout.write(
    JSON.stringify({ ok: false, error: String((lastErr && lastErr.message) || lastErr), elapsed_ms: Date.now() - started }) + "\n",
  );
  return 1;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    process.stdout.write(JSON.stringify({ ok: false, error: String((err && err.message) || err) }) + "\n");
    process.exit(1);
  });
