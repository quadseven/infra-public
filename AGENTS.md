# This repository is public

Everything here — code, commit history, issues, pull requests, comments,
review threads, discussions, releases, wiki pages, GitHub Pages content —
is visible to anyone on the internet, forever, including after deletion
(forks, caches, and search-engine indexes outlive an edit or a delete).
Treat every write to this repo, in any surface, as something a stranger
reads the moment you make it.

This file exists because that got violated repeatedly before it was
written down. Real personal names, a real home network's addresses, real
device identifiers, and a live unrotated credential all ended up in public
issue trackers and public git history — not through carelessness in the
code, but through ordinary conversational writing in issues, PR bodies, and
code comments, where the discipline that already existed for the shipped
code was never applied. This file is the fix: the same discipline,
extended to every surface an agent writes to, not just the diff.

## The rule

**Nothing that identifies a specific person, a specific private network,
or a specific credential may appear anywhere in this repository, in any
form, ever.** Not in code. Not in a code comment. Not in an issue body. Not
in a PR description. Not in a commit message. Not in a comment reply. Not
in a test fixture. Not "just this once because it's only in a closed
issue" — closed does not mean hidden, and neither does deleted.

This is broader than "don't commit secrets." A secret scanner catches an
API key. It does not catch a sentence like *"[a real first name]'s home
server, reachable at their usual address, needed a restart"* — nothing
there matches a secret
pattern, and it is exactly the kind of sentence that put a real name, a
real domain, and a real IP into a public tracker tonight. Write for a
stranger from the first word, not just the code.

### Concretely, never write any of the following into this repo, on any surface

- A real personal name — yours, a collaborator's, anyone's. Refer to
  people by role ("the maintainer," "the operator," "a reviewer") the same
  way this file does.
- A real hostname, domain, or subdomain that resolves to a private network
  or a real person's infrastructure (a home VPN suffix, a personal tailnet
  domain, a work-in-progress product's real URL before it's meant to be
  public). Use a placeholder that is visibly fake: `example.com`,
  `your-server.internal`, `<your-domain>`.
- A real IP address on any network you actually operate — home, cloud, or
  otherwise. Use an RFC 5737 documentation range (`192.0.2.0/24`,
  `198.51.100.0/24`, `203.0.113.0/24`) or an obviously fictional one
  (`10.0.0.X` as a *labeled example* is fine; a live address copy-pasted
  from a real `curl`/`dig`/log output is not).
- A real device identifier: a serial number, a MAC address, an IMEI, a
  hardware ID, an account ID, a database GUID tied to a live system.
- A credential of any kind, live or "already rotated" — a key, a token, a
  password, a signing certificate, a webhook URL with a token embedded in
  the path. "It's already been rotated" is not a reason to leave the old
  value visible; redact it anyway, because the *pattern* (which SSM path,
  which naming convention, which provider) is itself information.
- A path that reveals a real local username (`/Users/<name>/...`,
  `C:\Users\<name>\...`) or a real machine's hostname.
- A quote attributed to a specific named person, even an accurate one.
  Paraphrase instead: "the operator decided..." not "Alice said...".
- An `@`-mention of anyone who is not already part of the conversation.
  A mention notifies that account and subscribes it to the thread, and
  neither can be undone by editing or deleting the text. `@grug` in
  particular is an unrelated real user, not the review bot: the bot is
  `grug-tribe[bot]` and takes slash commands (`/grug improve` re-runs the
  code review, `/grug recheck` re-runs the plan check). To name a handle
  in prose, put it in backticks, which GitHub does not treat as a mention.
- The name of another private repository, service, or internal system
  that isn't itself meant to be discoverable. Cross-repo references
  belong in the *private* tracker, not migrated wholesale into a public
  one.

### If you are migrating or importing content

Content that already exists elsewhere — an issue being moved from a
private repo, a comment thread being copied in, history being subtree-split
into a new repo — is not exempt from this rule because it was written
before this file existed. **Migration is not a scrub.** Before content
from anywhere else lands in this repo, on any surface, re-read it against
every bullet above and rewrite what fails. If a whole issue's substance is
inseparable from the personal/private detail it's built on, don't migrate
it — summarize the generic problem it represents instead, or leave it out.

Wholesale-copying a private issue tracker into a public one because it was
"faster" is exactly how this happened the first time.

### If you find a violation already in the repo

Fix the current tree, then say plainly in your response that older
issues/PRs/comments/history may still carry it and that this needs a
human decision, not a silent edit-and-move-on. Do not delete or rewrite
someone else's public comment without asking first — you may not always
know why it was worded that way. Editing your own agent-authored content
to remove a violation is always fine and encouraged.

## The other half: this repo must be genuinely reusable

A stranger must be able to clone this repository, supply their **own**
configuration and secrets, and have it work — without reading anything
beyond the README and an example config file to know what to change.

- Every value specific to one deployment (a hostname, an IP, a region, an
  account ID, a device identifier) is a variable, an environment variable,
  or a config file entry — never a literal baked into source, a workflow
  file, or a script.
- Ship a `.env.example` / `config.example.*` alongside any file that reads
  real config, with every key present and an obviously-placeholder value
  (`YOUR_DOMAIN_HERE`, not a real one with the last octet changed).
- If a CI/CD pipeline assumes infrastructure that doesn't ship with the
  repo (a specific runner pool, a specific cloud account, a specific
  private reusable workflow), say so explicitly in the README rather than
  let a stranger discover it as a mysterious failure. "This requires your
  own self-hosted runner and your own AWS account" is an honest
  dependency; a silent reference to `uses: <this-operator>/infra-private/...`
  is not.
- Prefer this repo's own already-public reusable workflows
  (`quadseven/infra-public/...`) over hand-rolled CI where one already
  exists — they're already written to take config as input rather than
  assume it.

## Why this file, not just a smarter secret scanner

A pattern-matching scanner catches shapes: an AWS key, a PEM block, a
32-character hex string. It cannot catch a paragraph of ordinary prose
that happens to name a real person or describe a real network in plain
words — which is where nearly everything this file exists to prevent
actually showed up. Scanners still belong in CI as a backstop for the
shapes they *can* catch; this file is the layer above that, for the judgment
a scanner doesn't have.

<!-- Everything above the next line is synced from quadseven/infra-public and replaced on every sync. Put this repo's own content below it. -->
<!-- repo-specific below -->

# Working in infra-public

This repository is the shared CI/CD library for the `quadseven` fleet: reusable
GitHub Actions workflows, the guard scripts behind them, the public-repo policy
templates, and the `spark_cave` Python package.

## Layout

| Path | Holds |
| --- | --- |
| `.github/workflows/` | `_reusable.*`, `check.*`, `build.*`, `deploy.*`, `ship.*` for callers; `self-*` and `canary.*` run them here |
| `.github/scripts/` | Guard scripts, standard library only, each with a `<name>_test.py` beside it; `close-completeness/` is Node |
| `.loom/validation.conf` | The local check list; each line names the workflow step that runs it |
| `templates/public-repo/` | Canonical `AGENTS.md` and `CLAUDE.md` for every public repo, and the README standard |
| `docs/reusables.md` | Generated inputs reference |
| `examples/` | Caller workflows to copy |
| `python/spark_cave/` | Zero-dependency package vendored by consumers |

## Commands

- Everything CI runs on a PR: `python3 .github/scripts/validation_contract.py --run`.
  It needs Python 3.11+ and Node 24. On macOS, `size_ratchet_test.py` fails while
  `TMPDIR` passes through a symlink; prefix the command with
  `TMPDIR="$(python3 -c 'import os,tempfile;print(os.path.realpath(tempfile.gettempdir()))')"`.
- `python/spark_cave`, from that directory: `uvx ruff@0.15.10 check .`,
  `uvx ruff@0.15.10 format --check .`, `uv run --frozen pytest -q`.
- Regenerate the reference after a workflow input changes:
  `python3 .github/scripts/reusable_docs.py`. CI fails a stale page.
- Scan before pushing: `python3 .github/scripts/check_private_leaks.py --staged`,
  and `--text-file <file>` for a PR body. CI also loads a deny list; a local run
  without `--deny-list-ssm <param>` covers only the shapes.

## Traps

- A merge reaches a consumer only when it bumps its pinned SHA, so a fix is live
  only where that pin moved. Keep inputs backward compatible.
- The leak scanner is the exception. Consumers fetch it from main by default, so
  a merged pattern change hits every repo at once. A PR here scans with the PR
  head, so the change tests itself first.
- Text above the `repo-specific below` marker in `AGENTS.md` or `CLAUDE.md` is
  overwritten by the docs sync. Change the policy in `templates/public-repo/`.
  Repo content goes below the marker.
- A line added to `.loom/validation.conf` must match a pull-request workflow step
  and is one command with no shell syntax. `validation_contract.py` refuses
  anything else.
- `.pre-commit-config.yaml` is managed by Pulumi. Change the standard at its source.
- `Grug - Chief` is the only required check in the ruleset. PRs also run CodeQL,
  the leak scan and CI config freshness; the merge gate below makes a red one a
  blocker.

## How work moves here

Several AI brains work this repo beside the operator: Claude Code, Muse, Dot and the Loom factory. Loom has a station that dispatches, a brain that briefs and merges, and lanes that build. No brain outranks another. Each acts under its own GitHub App, never a person's account, and signs posts `-- <name>`.

**Claims.** Put `claim:<name>` on a ticket before starting, and create the label if this repo lacks it. Check first and skip a ticket already held. One claim per ticket: the station keeps the earliest and removes the rest. A claim expires after 2 hours with no real activity, unless a comment holds it with `waiting: <what> until <time>`, or an open PR the claimer wrote and referenced holds it for up to 7 days. `claim:quadseven` is a person: leave those tickets alone. On stopping, push the work and say what is done, what is next, and the branch and head. Take over only an expired or plainly dead claim, after reading the holder's comments and open PRs.

**Labels.** Loom sets one state label per ticket:

`loom:triage` (not ready) -> `loom:queued` (no lane) -> `loom:briefed` + `lane:<name>` -> `loom:running` -> `loom:pr` -> `loom:merged` -> `loom:shipped` -> `loom:verified`, then closed.

`loom:ticket` marks every ticket. `loom:failed` means the runner gave up; the reason is on the ticket. `wait:*` names why a ticket is held. `needs:human` is an ask for a person. `loom:factory` marks a fault in the factory itself.

**CI and required checks.** Required checks differ per repo, so read them live: `gh api repos/<owner>/<repo>/rules/branches/<default-branch>`. Every repo needs a PR and `Grug - Chief`; most add test jobs; some add `Grug - Elder`. Run each command in `.loom/validation.conf` before pushing, and read the failing workflow file before guessing at a red check.

- Shared workflows live in this repo. Its own callers use `./` paths or a full SHA of itself; other repos pin by full SHA.
- Leak scan: reads the diff and the PR title and body, and a scan that cannot run is red. It has 9 rules: tailnet hosts, tailnet IPs, private-range IPs, server names, node names, user paths, MAC addresses, the stray mention, and cross-repo refs. A ref of the form `name#N` or `owner/name#N` to any other repo fails, public or not, except the one bare name allowed in `self-leak-scan.yml`, which is not to grow. Bare `#N` means this repo. A line with `leak-guard-allow` is exempt and visible in review; use it only for a real false positive.
- Trust gate: this repo has none, because its workflows default to GitHub-hosted runners. Private callers supply their own.
- Other guards: this repo defines workflow hygiene, size ratchet, CI config freshness, and close completeness, which reopens an issue closed with unticked boxes. It runs workflow hygiene and CI config freshness on itself; size ratchet and close completeness have only their tests here.
- Agent Apps cannot push `.github/workflows` changes.

**`grug`.** The bot is `grug-tribe[bot]`, driven by slash commands in a PR comment. Its handle takes no at-sign.

- `Grug - Chief` reads the PR body. It wants `## Why` (5+ words), `## Acceptance criteria` (3+ bullets), `## Out of scope`, `Size: XS|S|M|L`, and `Refs <owner>/<repo>#N` or `Refs #N` to a ticket that is an epic or sits under one. Text in HTML comments does not count. Closing keywords are out: tickets close when their boxes are ticked.
- A body with an agent marker (such as a session link) from an App author, or a Loom lane footer, gets a neutral Chief: `Hunt Plan skipped`. Neutral passes. A body with no marker gets the full check, so write the full shape every time.
- `Grug - Elder` reviews the code by LLM. `/grug improve` re-runs it. `/grug recheck` re-runs Chief only. Five pushes in ten minutes pause Elder until `/grug resume`.
- A degraded or partial review says `Degraded review (<reason>): not a verdict`. It is not a pass, whether it concludes failure or neutral. Run `/grug improve` once the backend recovers.
- `Grug - Guard` scans for security problems and is advisory unless the repo turns on blocking. Haunt, once called Sentinel, comments when a PR closes over an unresolved critical finding. It posts no check; answer its comment and re-verify.

**Merge gate.** Rulesets enforce less than this, and this binds: CI green, `Grug - Elder` success on the head (neutral is not enough), `Grug - Chief` success or neutral, not behind base, every bot thread answered, and a test that fails without the fix. `gh pr checks` does not show threads, so read `reviewThreads` by GraphQL and `pulls/<n>/reviews`. Anyone merges what they built. Loom merges docs, test and app code tiers alone and holds deploy, security and cross-repo tiers for the operator. A change to how brains merge, close or rewrite needs a fleet chat line and a 5 minute wait. Never use `--admin`.

**Human line.** People do: permanent deletion of records, creating, rotating or entering credentials, access grants, new spending, accepting terms. People decide: another brain's role and the fleet goal text. A second brain must agree, in writing, to clear a lane demotion, lift another brain's hold, or reopen a sound close. Everything else needs nobody. Ask with `needs:human` and one exact question. A relayed yes is not a yes. Never print a credential.

**When stuck.** Labels and stages: `docs/BRAIN.md` in `quadseven/loom`. Reusable workflows: `docs/reusables.md`. Chief's rules and every `/grug` verb: the README in `quadseven/grug`, whose `make chief-check` tests a body before posting. Fleet log: issue 30 in `quadseven/loom`. File a factory fault there as `loom:factory`. Poll `gh` at most once per 5 minutes.
