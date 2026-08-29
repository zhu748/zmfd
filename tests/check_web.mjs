import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(projectRoot, "web", "index.html"), "utf8");

const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)].map((match) => match[1]);
assert.ok(scripts.length > 0, "web/index.html must contain at least one inline script");
scripts.forEach((source, index) => {
  assert.doesNotThrow(() => new Function(source), `inline script ${index} must parse`);
});

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
const staticIdRefs = [...html.matchAll(/\$\(["']([^"']+)["']\)/g)].map((match) => match[1]);
const missingIds = [...new Set(staticIdRefs.filter((id) => !idSet.has(id)))].sort();
assert.deepEqual(missingIds, [], "static $(id) references must resolve to an element");

const functionNames = [...html.matchAll(/^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)]
  .map((match) => match[1]);
assert.deepEqual(duplicateValues(functionNames), [], "named inline functions must not be duplicated");

console.log(
  `web checks passed: ${scripts.length} script, ${ids.length} ids, `
  + `${staticIdRefs.length} static id refs, ${functionNames.length} functions`,
);
