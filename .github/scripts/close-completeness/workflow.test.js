"use strict";

// Runs the REAL inline github-script step of check.issue-close-completeness.yml
// against fake github/context/core objects, so a branch that reopens, notes or
// allows a close is proven by what it does to the issue, not by reading the
// YAML (infra#3125: the no-criteria branch did not exist, and the guard
// reported success over an issue it had examined nothing in).
//
// Node stdlib only. Run: node --test .github/scripts/close-completeness/

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const WORKFLOW = path.join(__dirname, "..", "..", "workflows", "check.issue-close-completeness.yml");
const PARSE = path.join(__dirname, "parse.js");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

/** The text of the `script: |` block, dedented. Throws if it is not found. */
function extractScript(yaml) {
  const lines = yaml.split("\n");
  const start = lines.findIndex((l) => /^\s*script:\s*\|\s*$/.test(l));
  assert.notEqual(start, -1, "no `script: |` block in the workflow");
  const keyIndent = lines[start].match(/^\s*/)[0].length;
  const body = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() && line.match(/^\s*/)[0].length <= keyIndent) break;
    body.push(line);
  }
  const indent = Math.min(...body.filter((l) => l.trim()).map((l) => l.match(/^\s*/)[0].length));
  return body.map((l) => l.slice(indent)).join("\n");
}

/** A workspace laid out the way the workflow checks out its own sources. */
function workspace() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ccg-"));
  const dir = path.join(root, "_close-completeness-guard-src", ".github", "scripts", "close-completeness");
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(PARSE, path.join(dir, "parse.js"));
  return root;
}

async function run(issue, { existingComments = [] } = {}) {
  const calls = { update: [], createComment: [], info: [], warning: [] };
  const github = {
    rest: {
      issues: {
        update: async (a) => calls.update.push(a),
        createComment: async (a) => calls.createComment.push(a),
        listComments: async () => existingComments,
      },
    },
    paginate: async (fn, params) => fn(params),
  };
  const context = { payload: { issue }, repo: { owner: "o", repo: "r" } };
  const core = { info: (m) => calls.info.push(m), warning: (m) => calls.warning.push(m) };
  const root = workspace();
  process.env.GITHUB_WORKSPACE = root;
  try {
    const fn = new AsyncFunction("github", "context", "core", "require", extractScript(fs.readFileSync(WORKFLOW, "utf8")));
    await fn(github, context, core, require);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
  return calls;
}

const issue = (body, extra = {}) => ({ number: 7, state_reason: "completed", labels: [], body, ...extra });

test("extraction finds the step script", () => {
  const script = extractScript(fs.readFileSync(WORKFLOW, "utf8"));
  assert.match(script, /classify\(/);
});

test("closed as not planned is allowed and touches nothing", async () => {
  const c = await run(issue("- [ ] open", { state_reason: "not_planned" }));
  assert.equal(c.update.length + c.createComment.length, 0);
});

test("force-close and wontfix labels are allowed and touch nothing", async () => {
  for (const name of ["force-close", "wontfix", "Force-Close"]) {
    const c = await run(issue("- [ ] open", { labels: [{ name }] }));
    assert.equal(c.update.length + c.createComment.length, 0, name);
  }
});

test("every item checked: the close stands, nothing posted", async () => {
  const c = await run(issue("## Acceptance criteria\n\n- [x] a\n- [x] b\n"));
  assert.equal(c.update.length + c.createComment.length, 0);
});

test("open items reopen the issue and list them", async () => {
  const c = await run(issue("## Acceptance criteria\n\n- [x] a\n- [ ] still open\n"));
  assert.equal(c.update.length, 1);
  assert.equal(c.update[0].state, "open");
  assert.equal(c.createComment.length, 1);
  assert.match(c.createComment[0].body, /Reopened by the close-completeness guard/);
  assert.match(c.createComment[0].body, /still open/);
});

test("no criteria: NOT reopened, one note posted, and it is a warning not a pass", async () => {
  const c = await run(issue("## What\n\nProse only, no checkboxes.\n"));
  assert.equal(c.update.length, 0, "must not reopen");
  assert.equal(c.createComment.length, 1);
  assert.match(c.createComment[0].body, /no acceptance criteria to verify/);
  assert.match(c.createComment[0].body, /close-completeness:no-criteria/);
  assert.equal(c.warning.length, 1);
});

test("no criteria on an empty body is noted too", async () => {
  const c = await run(issue(null));
  assert.equal(c.createComment.length, 1);
});

test("no criteria: an existing note is not duplicated", async () => {
  const c = await run(issue("Prose."), {
    existingComments: [{ body: "<!-- close-completeness:no-criteria -->\nearlier note" }],
  });
  assert.equal(c.createComment.length, 0);
  assert.equal(c.update.length, 0);
});

test("no criteria: an unrelated earlier comment does not suppress the note", async () => {
  const c = await run(issue("Prose."), { existingComments: [{ body: "looks good to me" }] });
  assert.equal(c.createComment.length, 1);
});

test("checkboxes only under Out of scope still count as no criteria", async () => {
  const c = await run(issue("Prose.\n\n## Out of scope\n\n- [x] later\n"));
  assert.equal(c.createComment.length, 1);
  assert.equal(c.update.length, 0);
});

test("a missing state_reason (older payloads) is treated as completed", async () => {
  const c = await run(issue("Prose.", { state_reason: null }));
  assert.equal(c.createComment.length, 1);
});
