"use strict";

// Pure checklist parser for the issue-close-completeness guard
// (check.issue-close-completeness.yml). Extracted verbatim from the inline
// github-script step (infra-public#52/#53) so it has a testable seam - the
// guard mutates issue state (reopen + comment) in production and has
// already needed three rounds of post-merge parser bug fixes (2026-07-12,
// 2026-07-13 Qodo review, #47) with no test surface catching any of them.
//
// This module owns ONLY the markdown state machine: body string in,
// unchecked-item list out. No GitHub API access here - the workflow step
// keeps all Octokit/`core` I/O and the escape-hatch checks (closed reason,
// force-close/wontfix labels).

/**
 * Parse the checklist in an issue body: the unchecked (`- [ ]`) acceptance/
 * test items, and how many checkbox items there are IN TOTAL (checked or
 * not). Fenced code blocks and exempt sections (Out of scope / Blocked by /
 * Reverse-if-wrong / Candidate / Further notes) count for neither. Each
 * unchecked item is capped at 150 characters and multi-line
 * (continuation-wrapped) bullets are tracked across lines.
 *
 * `total` exists because "nothing unchecked" means two different things: every
 * criterion was met, or there were no criteria to meet. The guard used to
 * treat both as a pass (infra#3125).
 *
 * @param {string} body
 * @returns {{unchecked: string[], total: number}}
 */
function parseChecklist(body) {
  const lines = (body || "").split("\n");

  // Track state across lines
  let inCodeBlock = false; // track ``` blocks
  let skippingSection = false; // currently in an exempt section (Out of scope, etc.)
  let currentBulletChecked = null; // if we are tracking a multi-line bullet, is it checked?
  let currentBulletText = ""; // accumulated text for multi-line unchecked bullets

  const uncheckedItems = [];
  let total = 0;

  for (const raw of lines) {
    const line = raw.trim();

    // Toggle code block state (``` marks) - accepts any info string after backticks
    if (/^```[^`]*$/.test(line)) {
      inCodeBlock = !inCodeBlock;
      continue;
    }

    // Skip all content inside code blocks
    if (inCodeBlock) {
      continue;
    }

    // An ATX (#) header is unambiguous and always a real header, even
    // while tracking a multi-line bullet continuation -- unlike a
    // **bold**/__bold__ line, which could be either a real header OR
    // a trimmed continuation line, so THAT alternative is gated to
    // NOT-tracking only (we must not misclassify the latter as a
    // header). Gating both alternatives the same way silently
    // swallowed an unchecked item followed immediately by a real
    // `## Header` with no blank line in between -- a common issue
    // template shape -- since the header then fell through to the
    // continuation-line branch instead of flushing the bullet.
    const isAtxHeader = /^#{1,6}\s+/.test(line);
    const isBoldHeader = currentBulletChecked === null && /^(\*\*|__)[^*\n]*(\*\*)/.test(line);
    const isSectionHeader = isAtxHeader || isBoldHeader;

    if (isSectionHeader) {
      // Save previous section state before updating (for flushing pending bullets correctly)
      const prevSkippingSection = skippingSection;

      // Check if this is an exempt section header
      const lowerLine = line.toLowerCase().replace(/[#*_]/g, "");
      skippingSection = /out of scope|blocked by|reverse-if-wrong|candidate|further notes|notes$/.test(lowerLine);

      // If we were tracking a multi-line bullet that ended without being checked, flag it (use prev state)
      if (!prevSkippingSection && currentBulletChecked === false && currentBulletText.trim()) {
        uncheckedItems.push(currentBulletText.trim());
      }
      currentBulletChecked = null;
      currentBulletText = "";
      continue;
    }

    // If we are in an exempt section, skip all list item processing
    if (skippingSection) {
      // Still reset bullet state on new bullets within exempt sections to avoid carrying over
      if (/^[-*]\s+\[/.test(line)) {
        currentBulletChecked = null;
        currentBulletText = "";
      }
      continue;
    }

    // Detect list item start: - [ ] or - [x]
    const listItemMatch = /^[-*]\s+\[([ xX])\]\s+(.*)/.exec(line);

    if (listItemMatch) {
      total += 1;
      // Flush any still-open unchecked bullet BEFORE starting to track this
      // one (#58): consecutive `- [ ]` lines with no blank line between them
      // are the overwhelmingly common Acceptance Criteria shape, and the
      // unconditional overwrite below used to silently drop every item in
      // such a run except the last - a severe false-negative that let
      // issues stay closed-as-completed with unfinished work.
      if (currentBulletChecked === false && currentBulletText.trim()) {
        uncheckedItems.push(currentBulletText.trim());
      }

      const isChecked = /x|X/.test(listItemMatch[1]);
      const restOfLine = listItemMatch[2];

      // First line of a potential multi-line bullet
      currentBulletChecked = isChecked;
      currentBulletText = restOfLine.slice(0, 150); // Cap at 150 chars

      // If not checked, we will keep tracking continuation lines
      if (!isChecked) {
        continue;
      }

      // Item is checked - no need to track further
      currentBulletChecked = null;
      currentBulletText = "";
    } else if (currentBulletChecked === false && line.length > 0 && !/^[-*]/.test(line)) {
      // Continuation of an unchecked multi-line bullet (not a new list item)
      const remaining = Math.max(0, 150 - currentBulletText.length - 1); // -1 for leading space
      if (remaining > 0) {
        currentBulletText += " " + line.slice(0, remaining);
      }
    } else if (currentBulletChecked === false && /^[-*]/.test(line)) {
      // New bullet starts while tracking previous unchecked one -- flag the old one
      uncheckedItems.push(currentBulletText.trim());
      currentBulletChecked = null;
      currentBulletText = "";
    } else if (currentBulletChecked === false && line.length === 0) {
      // Empty line ends continuation without new bullet -- flag as incomplete
      uncheckedItems.push(currentBulletText.trim());
      currentBulletChecked = null;
      currentBulletText = "";
    }
  }

  // If ended while tracking an unchecked multi-line bullet, flag it (only if not in exempt section)
  if (!skippingSection && currentBulletChecked === false && currentBulletText.trim()) {
    uncheckedItems.push(currentBulletText.trim());
  }

  return { unchecked: uncheckedItems, total };
}

/**
 * The unchecked items only. Kept as the stable seam the fixture suite pins.
 *
 * @param {string} body
 * @returns {string[]}
 */
function findUncheckedItems(body) {
  return parseChecklist(body).unchecked;
}

/**
 * Three states, not two:
 *   "open-items"  some criteria, at least one unchecked -> not done
 *   "all-checked" some criteria, all checked            -> done
 *   "no-criteria" no checkbox criteria at all           -> unknowable
 * The third is not a pass: a guard that examined nothing cannot say the work
 * was verified (infra#3125).
 *
 * @param {string} body
 * @returns {{state: "open-items"|"all-checked"|"no-criteria", unchecked: string[], total: number}}
 */
function classify(body) {
  const { unchecked, total } = parseChecklist(body);
  if (unchecked.length > 0) return { state: "open-items", unchecked, total };
  if (total === 0) return { state: "no-criteria", unchecked, total };
  return { state: "all-checked", unchecked, total };
}

const NO_CRITERIA_MARKER = "<!-- close-completeness:no-criteria -->";

/**
 * The note posted when an issue is closed as completed with no checkbox
 * criteria. It does NOT reopen: an issue without checkboxes is legitimate to
 * have, and reopening every prose issue would punish trivial ones. The point
 * is that the close no longer looks verified when nothing was examined.
 *
 * @returns {string}
 */
function noCriteriaNote() {
  return (
    `${NO_CRITERIA_MARKER}\n` +
    "\ud83d\udd0e **Closed as completed with no acceptance criteria to verify.** " +
    "The close-completeness guard checks `- [ ]` items outside the exempt sections; " +
    "this issue has none, so the guard examined nothing and the close stands by " +
    "default, not because the work was checked.\n\n" +
    "If \"done\" rests on criteria written as prose, restate them as checkboxes. " +
    "Otherwise add one line saying how you confirmed it. Close as *not planned* or " +
    "add a `force-close` label to skip this note."
  );
}

/**
 * True when the note was already posted, so reopening and re-closing an issue
 * does not stack copies.
 *
 * @param {Array<{body?: string}>} comments
 * @returns {boolean}
 */
function hasNoCriteriaNote(comments) {
  return (comments || []).some((c) => ((c && c.body) || "").includes(NO_CRITERIA_MARKER));
}

module.exports = {
  findUncheckedItems,
  parseChecklist,
  classify,
  noCriteriaNote,
  hasNoCriteriaNote,
  NO_CRITERIA_MARKER,
};
