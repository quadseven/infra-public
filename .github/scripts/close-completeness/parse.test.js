"use strict";

// Fixture suite for parse.js (infra-public#54). Each fixture reproduces a
// bug class the guard has already regressed on in production (see
// check.issue-close-completeness.yml's header: 2026-07-12, 2026-07-13 Qodo
// review, #47) - this suite exists so the next parser change can be proven
// against these BEFORE merge, not discovered against a live issue after.
//
// Node stdlib only (node:test + node:assert) - no npm runtime dependency.
// Run: node --test .github/scripts/close-completeness/

const test = require("node:test");
const assert = require("node:assert/strict");
const { findUncheckedItems } = require("./parse.js");
const { parseChecklist, classify } = require("./parse.js");

test("checked item is not returned", () => {
  const body = "## Acceptance criteria\n\n- [x] done thing\n";
  assert.deepEqual(findUncheckedItems(body), []);
});

test("unchecked item is returned", () => {
  const body = "## Acceptance criteria\n\n- [ ] not done thing\n";
  assert.deepEqual(findUncheckedItems(body), ["not done thing"]);
});

// FIXED (#58): a new checkbox bullet immediately following an OPEN
// unchecked bullet - no blank line, no header between them - now flushes
// the open bullet to the unchecked list before tracking the new one. This
// is the common real-world Acceptance Criteria shape; the old clobber
// behavior silently dropped every item in a consecutive run except the
// last (a severe false-negative in the LIVE guard).
test("#58 fixed: consecutive unchecked bullets are ALL returned, mixed with checked ones", () => {
  const body = ["- [x] first (done)", "- [ ] second (open)", "- [X] third (done, capital X)", "- [ ] fourth (open)"].join(
    "\n",
  );
  assert.deepEqual(findUncheckedItems(body), ["second (open)", "fourth (open)"]);
});

// The regression fixture #58's acceptance criteria call for explicitly:
// 3+ consecutive unchecked items, the overwhelmingly common shape, ALL
// returned in order.
test("#58 regression: an unbroken run of three unchecked items returns all three", () => {
  const body = "- [ ] item one\n- [ ] item two\n- [ ] item three\n";
  assert.deepEqual(findUncheckedItems(body), ["item one", "item two", "item three"]);
});

test("bug class (a): line-wrapped continuation bullet is tracked and flushed on blank line", () => {
  const body = [
    "- [ ] a long acceptance criterion that wraps",
    "  onto a second line of plain continuation text",
    "",
    "next paragraph",
  ].join("\n");
  const result = findUncheckedItems(body);
  assert.equal(result.length, 1);
  assert.match(result[0], /wraps onto a second line/);
});

// FIXED (#58, same root cause as above): a continuation in progress is
// flushed - with its accumulated continuation text - when a real new
// checkbox bullet follows with no blank line.
test("#58 fixed: an in-progress continuation is flushed intact by an immediately-following real bullet", () => {
  const body = ["- [ ] first item wraps", "  continues here", "- [ ] second item"].join("\n");
  const result = findUncheckedItems(body);
  assert.deepEqual(result, ["first item wraps continues here", "second item"]);
});

test("a bullet-LIKE line that is NOT a valid checkbox correctly flushes the open bullet first (the one path that does work)", () => {
  // Contrast case: THIS is the branch the code's own comment describes
  // ("New bullet starts while tracking previous unchecked one -- flag the
  // old one") - it only actually fires for a line that starts with -/* but
  // does NOT match the stricter checkbox regex (no valid `[ ]`/`[x]`).
  const body = ["- [ ] open item", "- not a checkbox, plain bullet"].join("\n");
  assert.deepEqual(findUncheckedItems(body), ["open item"]);
});

test("bug class (a): continuation is flushed at end of body with no trailing blank line", () => {
  const body = ["## Acceptance criteria", "", "- [ ] wraps to", "  the last line of the body"].join("\n");
  const result = findUncheckedItems(body);
  assert.equal(result.length, 1);
  assert.match(result[0], /wraps to the last line of the body/);
});

test("bug class (b): a checkbox-looking line inside a fenced code block does not count", () => {
  const body = ["## Test plan", "", "```", "- [ ] this is example shell output, not a real checkbox", "```", ""].join(
    "\n",
  );
  assert.deepEqual(findUncheckedItems(body), []);
});

test("bug class (b): fence with an info string still toggles code-block state", () => {
  const body = ["```diff", "- [ ] not a real item, inside a diff fence", "```", "- [ ] this one IS real"].join("\n");
  assert.deepEqual(findUncheckedItems(body), ["this one IS real"]);
});

test("bug class (c): a real ATX header right after an unchecked bullet with no blank line does not swallow the bullet or get eaten as continuation", () => {
  const body = ["- [ ] the last unchecked item", "## Out of scope", "", "- [ ] this one is exempt, ignored"].join("\n");
  const result = findUncheckedItems(body);
  assert.deepEqual(result, ["the last unchecked item"]);
});

// KNOWN BUG, tracked in #58: isBoldHeader IS correctly gated to
// currentBulletChecked === null (so a **bold** line while tracking an open
// bullet is never misclassified as a section HEADER - that part of the
// code's comment is accurate). But a **bold** line ALSO starts with a
// literal `*`, so it falls into the SAME clobber-on-bullet-like-line path
// as #58's other cases, rather than genuine continuation text: the open
// bullet gets flushed (correctly) but the bold line's own content is
// discarded. #58's acceptance criteria accept exactly this outcome
// ("correctly flushes the prior bullet"): the bold line is emphasis
// prose, not a checklist item, so dropping ITS text while flagging the
// open bullet is the documented, deliberate behavior - no longer a bug.
test("#58 accepted behavior: a **bold** line after an open bullet flushes the bullet; the bold text itself is prose, not an item", () => {
  const body = ["- [ ] an item whose next line looks bold", "**not actually a header, just emphasis**"].join("\n");
  const result = findUncheckedItems(body);
  // The bold line's text ("not actually a header...") never appears in the
  // output at all - it is silently dropped, not appended as continuation.
  assert.deepEqual(result, ["an item whose next line looks bold"]);
});

test("bug class (c): a **bold** line with nothing open is not mistaken for an item or crash the parser", () => {
  const body = ["**Just a bold lead-in, no open bullet**", "", "- [ ] a real item after"].join("\n");
  assert.deepEqual(findUncheckedItems(body), ["a real item after"]);
});

for (const heading of ["Out of scope", "Blocked by", "Reverse-if-wrong", "Candidate", "Further notes"]) {
  test(`bug class (d): unchecked item under exempt section "${heading}" is ignored`, () => {
    const body = ["## Acceptance criteria", "", "- [ ] a real open item", "", `## ${heading}`, "", "- [ ] exempt, not counted"].join(
      "\n",
    );
    assert.deepEqual(findUncheckedItems(body), ["a real open item"]);
  });
}

test("bug class (d): leaving an exempt section for a normal section resumes counting", () => {
  const body = [
    "## Out of scope",
    "",
    "- [ ] exempt one",
    "",
    "## Test plan",
    "",
    "- [ ] this one counts",
  ].join("\n");
  assert.deepEqual(findUncheckedItems(body), ["this one counts"]);
});

test("bug class (d): a new bullet inside an exempt section resets tracking state (does not leak into the next real section)", () => {
  const body = [
    "## Out of scope",
    "",
    "- [ ] exempt item that wraps",
    "  more exempt continuation",
    "- [ ] second exempt item",
    "",
    "## Acceptance criteria",
    "",
    "- [ ] real item",
  ].join("\n");
  assert.deepEqual(findUncheckedItems(body), ["real item"]);
});

test("item text is capped at 150 characters", () => {
  const long = "x".repeat(300);
  const body = `- [ ] ${long}`;
  const result = findUncheckedItems(body);
  assert.equal(result.length, 1);
  assert.equal(result[0].length, 150);
});

test("empty body returns an empty list", () => {
  assert.deepEqual(findUncheckedItems(""), []);
});

test("null/undefined body does not throw and returns an empty list", () => {
  assert.deepEqual(findUncheckedItems(null), []);
  assert.deepEqual(findUncheckedItems(undefined), []);
});

test("a body with no checkboxes at all returns an empty list", () => {
  const body = "Just prose, no acceptance criteria section at all.";
  assert.deepEqual(findUncheckedItems(body), []);
});

test("asterisk bullets are recognized the same as hyphen bullets", () => {
  const body = "* [ ] an asterisk-style unchecked item";
  assert.deepEqual(findUncheckedItems(body), ["an asterisk-style unchecked item"]);
});

// infra#3125: "nothing unchecked" is two different facts. Every criterion met
// and no criteria at all both used to read as a pass, so an issue written in
// prose opted out of the guard while the guard reported success.

test("total counts checked and unchecked items", () => {
  const body = "## Acceptance criteria\n\n- [x] a\n- [X] b\n- [ ] c\n";
  assert.deepEqual(parseChecklist(body), { unchecked: ["c"], total: 3 });
});

test("consecutive unchecked bullets each count once", () => {
  const body = "## Acceptance criteria\n- [ ] one\n- [ ] two\n- [ ] three\n";
  assert.equal(parseChecklist(body).total, 3);
});

test("a wrapped multi-line bullet counts once", () => {
  const body = "## Acceptance criteria\n\n- [ ] first line\n  continued here\n";
  assert.equal(parseChecklist(body).total, 1);
});

test("a prose-only body has no criteria", () => {
  const body = "## What\n\nThe thing is broken. Fix it so that it works.\n";
  assert.deepEqual(parseChecklist(body), { unchecked: [], total: 0 });
  assert.equal(classify(body).state, "no-criteria");
});

test("empty, null and undefined bodies have no criteria", () => {
  for (const body of ["", null, undefined]) {
    assert.equal(classify(body).state, "no-criteria");
  }
});

test("checkboxes inside a fenced code block are not criteria", () => {
  const body = "Example template:\n\n```\n- [ ] not a real criterion\n- [x] nor this\n```\n";
  assert.equal(parseChecklist(body).total, 0);
  assert.equal(classify(body).state, "no-criteria");
});

test("checkboxes under Out of scope do not make a criteria-free issue look complete", () => {
  const body = "## What\n\nProse only.\n\n## Out of scope\n\n- [x] later thing\n- [ ] other thing\n";
  assert.equal(parseChecklist(body).total, 0);
  assert.equal(classify(body).state, "no-criteria");
});

test("an unchecked Out of scope item does not block a complete issue", () => {
  const body = "## Acceptance criteria\n\n- [x] done\n\n## Out of scope\n\n- [ ] later\n";
  assert.deepEqual(classify(body), { state: "all-checked", unchecked: [], total: 1 });
});

test("classify: some unchecked is open-items", () => {
  const body = "## Acceptance criteria\n\n- [x] a\n- [ ] b\n";
  assert.deepEqual(classify(body), { state: "open-items", unchecked: ["b"], total: 2 });
});

test("classify: all checked is all-checked", () => {
  const body = "## Acceptance criteria\n\n- [x] a\n- [x] b\n";
  assert.deepEqual(classify(body), { state: "all-checked", unchecked: [], total: 2 });
});

test("findUncheckedItems is unchanged for a criteria-free body", () => {
  assert.deepEqual(findUncheckedItems("Just prose."), []);
});

const { noCriteriaNote, hasNoCriteriaNote, NO_CRITERIA_MARKER } = require("./parse.js");

test("the no-criteria note carries its marker and says nothing was verified", () => {
  const note = noCriteriaNote();
  assert.ok(note.includes(NO_CRITERIA_MARKER));
  assert.match(note, /no acceptance criteria to verify/);
  assert.match(note, /force-close/);
});

test("hasNoCriteriaNote finds the marker and ignores everything else", () => {
  assert.equal(hasNoCriteriaNote([{ body: noCriteriaNote() }]), true);
  assert.equal(hasNoCriteriaNote([{ body: "hello" }, { body: null }, {}]), false);
  assert.equal(hasNoCriteriaNote([]), false);
  assert.equal(hasNoCriteriaNote(undefined), false);
});
