import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(projectRoot, "web", "index.html"), "utf8");
const styles = fs.readFileSync(path.join(projectRoot, "web", "styles.css"), "utf8");
const expectedScripts = ["core.js", "history.js", "chat.js", "admin.js"];
const scriptUrls = [...html.matchAll(/<script src="\/assets\/([^"]+)" defer><\/script>/g)]
  .map((match) => match[1]);
const scriptSources = expectedScripts.map((name) => fs.readFileSync(path.join(projectRoot, "web", name), "utf8"));
const appSource = scriptSources.join("\n");

assert.match(html, /<link rel="stylesheet" href="\/assets\/styles\.css">/,
  "web/index.html must load the extracted stylesheet");
assert.deepEqual(scriptUrls, expectedScripts, "web/index.html must load application scripts in dependency order");
assert.ok(styles.includes(":root"), "web/styles.css must contain the console theme");
scriptSources.forEach((source, index) => {
  assert.doesNotThrow(() => new Function(source), `web/${expectedScripts[index]} must parse`);
});
assert.doesNotThrow(() => new Function(appSource), "ordered application scripts must parse as one bundle");

const duplicateValues = (values) => {
  const seen = new Set();
  const duplicates = new Set();
  values.forEach((value) => {
    if (seen.has(value)) duplicates.add(value);
    seen.add(value);
  });
  return [...duplicates].sort();
};

const ids = [...html.matchAll(/\bid=["']([^"']+)["']/g)].map((match) => match[1]);
assert.deepEqual(duplicateValues(ids), [], "HTML ids must be unique");

const idSet = new Set(ids);
const staticIdRefs = [...appSource.matchAll(/\$\(["']([^"']+)["']\)/g)].map((match) => match[1]);
const missingIds = [...new Set(staticIdRefs.filter((id) => !idSet.has(id)))].sort();
assert.deepEqual(missingIds, [], "static $(id) references must resolve to an element");

assert.match(appSource, /function showProfileMutationResult\(data, fallbackMessage\)/,
  "profile mutation feedback must share persistence-aware rendering");
assert.match(appSource, /store\.persisted === false \|\| store\.error/,
  "profile store failures must be visible in the account status banner");
assert.doesNotMatch(appSource, /window\.showToast\(`HAR 解析成功并保存登录态:/,
  "HAR upload must not claim persistence unconditionally");
assert.match(appSource, /function renderSettingsStoreStatus\(store\)/,
  "settings storage failures must have a dedicated status renderer");
assert.match(appSource, /renderSettingsStoreStatus\(data\.settings_store\)/,
  "settings storage health must update with the server status snapshot");
assert.match(appSource, /function renderHistoryStoreStatus\(store\)/,
  "history persistence failures must have a dedicated status renderer");
assert.match(appSource, /showHistoryMutationResult\(data, "已删除记录"\)/,
  "history deletion feedback must distinguish in-memory removal from persistence");

const functionNames = [...appSource.matchAll(/^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)]
  .map((match) => match[1]);
assert.deepEqual(duplicateValues(functionNames), [], "named inline functions must not be duplicated");

const timeoutHelpersStart = appSource.indexOf("const MANAGEMENT_FETCH_TIMEOUT_MS = 10000;");
const timeoutHelpersEnd = appSource.indexOf("function profileHeaders", timeoutHelpersStart);
assert.ok(timeoutHelpersStart >= 0 && timeoutHelpersEnd > timeoutHelpersStart,
  "bounded fetch helper block must exist");
const timeoutHelpersSource = appSource.slice(timeoutHelpersStart, timeoutHelpersEnd);
const buildTimeoutHarness = (fetchImpl, setTimeoutImpl, clearTimeoutImpl) => new Function(
  "fetch",
  "setTimeout",
  "clearTimeout",
  "AbortController",
  `${timeoutHelpersSource}\nreturn { fetchWithTimeout };`,
)(fetchImpl, setTimeoutImpl, clearTimeoutImpl, AbortController);
const abortError = () => Object.assign(new Error("aborted"), { name: "AbortError" });

let forwardedSignal = null;
const callerController = new AbortController();
const successfulTimeoutHarness = buildTimeoutHarness(
  async (_resource, options) => {
    forwardedSignal = options.signal;
    return { ok: true };
  },
  () => 1,
  () => {},
);
await successfulTimeoutHarness.fetchWithTimeout("/ok", { signal: callerController.signal }, 5000);
assert.ok(forwardedSignal && forwardedSignal !== callerController.signal,
  "bounded fetch must combine timeout and caller cancellation through its own signal");
assert.equal(forwardedSignal.aborted, false, "completed fetch must not remain aborted");

const timeoutHarness = buildTimeoutHarness(
  async (_resource, options) => {
    assert.equal(options.signal.aborted, true);
    throw abortError();
  },
  (callback) => { callback(); return 1; },
  () => {},
);
await assert.rejects(
  () => timeoutHarness.fetchWithTimeout("/timeout", {}, 5000),
  /本地服务响应超时/,
  "timer abort must become an actionable timeout message",
);

const abortedCaller = new AbortController();
abortedCaller.abort();
const callerAbortHarness = buildTimeoutHarness(
  async (_resource, options) => {
    assert.equal(options.signal.aborted, true);
    throw abortError();
  },
  () => 1,
  () => {},
);
await assert.rejects(
  () => callerAbortHarness.fetchWithTimeout("/cancel", { signal: abortedCaller.signal }, 5000),
  (error) => error?.name === "AbortError" && error?.message === "aborted",
  "caller cancellation must stay distinguishable from a timeout",
);

const directApiFetches = [...appSource.matchAll(/\bfetch\(\s*([`"'])(\/api\/[^`"']*)\1/g)]
  .map((match) => match[2]);
assert.deepEqual(directApiFetches, ["/api/chat"],
  "only the long-lived chat SSE may bypass the bounded fetch helper");
assert.match(appSource, /uploadAllFiles\([\s\S]{0,160}requestState\.controller\.signal/,
  "attachment uploads must share the active request cancellation signal");
assert.match(appSource, /if \(!loginPolling \|\| loginPollBusy\) return;/,
  "browser-login status polling must be single-flight");
assert.match(appSource, /if \(loginPolling && data && data\.stage\)/,
  "a late browser-login poll must not overwrite terminal feedback");

const markdownStart = appSource.indexOf("function escapeHtml(text)");
const markdownEnd = appSource.indexOf("window.copyCodeFromBlock", markdownStart);
assert.ok(markdownStart >= 0 && markdownEnd > markdownStart, "markdown renderer block must exist");
const renderMarkdown = new Function(
  `${appSource.slice(markdownStart, markdownEnd)}\nreturn renderMarkdown;`,
)();
const hostileMarkdown = renderMarkdown('<img src=x onerror="globalThis.pwned=1">');
assert.doesNotMatch(hostileMarkdown, /<img\b/i, "raw model HTML must not create elements");
assert.match(hostileMarkdown, /&lt;img/, "raw model HTML must remain visible as escaped text");
const collidingMarker = "__GLM2API_INTERNAL_CODE_BLOCK_0__";
const collisionOutput = renderMarkdown(`${collidingMarker}\n\n\`\`\`js\nconst safe = 1;\n\`\`\``);
assert.match(collisionOutput, new RegExp(collidingMarker), "literal marker-like text must be preserved");
assert.equal((collisionOutput.match(/code-block-wrapper/g) || []).length, 1,
  "one fenced block must render exactly once despite a marker collision");
assert.match(appSource, /\$\{escapeHtml\(s\.time \|\| ""\)\}/,
  "local-session metadata must be escaped before innerHTML insertion");

// Execute the actual local-session helpers from the page instead of copying
// their behavior into a second test-only implementation.
const sessionHelpersStart = appSource.indexOf("const LOCAL_SESSION_MAX_TOTAL = 120;");
const sessionHelpersEnd = appSource.indexOf("// Per-model effort memory", sessionHelpersStart);
assert.ok(sessionHelpersStart >= 0 && sessionHelpersEnd > sessionHelpersStart, "local-session helper block must exist");
const sessionHelpersSource = appSource.slice(sessionHelpersStart, sessionHelpersEnd);
const memoryStorage = {
  value: null,
  getItem() { return this.value; },
  setItem(_key, value) { this.value = value; },
};
const harnessState = { sessions: [], apiKey: "" };
const sessionHelpers = new Function(
  "state",
  "localStorage",
  "window",
  "STORAGE_KEY",
  `${sessionHelpersSource}\nreturn { compactLocalSessions, loadLocalSessions, saveLocalSessions };`,
)(harnessState, memoryStorage, { showToast() {} }, "test-sessions");

const newestFirst = Array.from({ length: 35 }, (_, index) => ({
  id: `sess_${index}`,
  profileId: "profile_a",
  title: `session ${index}`,
  messages: [{ role: "user", content: `message ${index}` }],
}));
const recent = sessionHelpers.compactLocalSessions(newestFirst);
assert.equal(recent.length, 30, "per-profile session cap must be enforced");
assert.equal(recent[0].id, "sess_0", "newest session must stay first");
assert.equal(recent.at(-1).id, "sess_29", "oldest overflow sessions must be dropped");

const manyMessages = Array.from({ length: 45 }, (_, index) => ({
  role: index % 2 ? "assistant" : "user",
  content: `${index}:` + "x".repeat(21000),
  attachments: [{ name: "evidence.txt", size: 12, type: "text/plain", lastModified: 7, secret: "drop-me" }],
}));
const normalized = sessionHelpers.compactLocalSessions([{
  id: "sess_messages",
  profileId: "profile_a",
  messages: manyMessages,
}]);
assert.equal(normalized[0].messages.length, 40, "message cap must retain the newest suffix");
assert.ok(normalized[0].messages[0].content.startsWith("5:"), "oldest message overflow must be dropped");
assert.equal(normalized[0].messages[0].content.length, 20000, "message text must be bounded");
assert.deepEqual(normalized[0].messages[0].attachments[0], {
  name: "evidence.txt",
  size: 12,
  type: "text/plain",
  lastModified: 7,
});

const budgeted = sessionHelpers.compactLocalSessions(newestFirst, {
  maxTotal: 35,
  maxPerProfile: 35,
  budget: 4096,
});
assert.ok(JSON.stringify(budgeted).length <= 4096, "stored sessions must honor the total character budget");

memoryStorage.value = '{"not":"an array"}';
harnessState.sessions = newestFirst;
sessionHelpers.loadLocalSessions();
assert.deepEqual(harnessState.sessions, [], "invalid stored root must recover to an empty session list");

console.log(
  `web checks passed: ${expectedScripts.length + 1} assets, ${ids.length} ids, `
  + `${staticIdRefs.length} static id refs, ${functionNames.length} functions`,
);
