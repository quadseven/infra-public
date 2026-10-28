# Size ratchet

The reusable checks tracked regular files by LF-delimited line count (including
binary blobs; a final line without LF counts). Symlinks and submodules are not
regular files. Default ceiling: 500 lines. Policy and record files themselves
are exempt. Existing oversized files need an explicit, reviewed adoption commit
before making this check required; the automation never creates higher records.

Put optional policy in `.size-ratchet.json`:

```json
{"default": 500, "overrides": {"tests/": 800, "tests/fixtures/": 1200}}
```

The longest matching folder prefix wins. Put existing oversized files and their
current line counts in `.size-ratchet-records.json`, for example:

```json
{"src/large.py": 740}
```

Missing policy means the default ceiling; missing records means no exceptions.
Every other file must fit its ceiling. Both documents are read from the base
commit, so editing policy in a PR cannot loosen that PR's check. Any PR edit to
the record file fails, even if it changes no source file. To change policy,
review and merge it separately; the next PR uses the new policy.

Copy `ci-size-ratchet.yml` to `.github/workflows/` and replace both placeholder
revisions with a reviewed infra-public commit containing the implementation.
Require the `ratchet / check` check in the consuming repository's branch rules.
Protect the caller and revision with review; as with any required workflow,
removing or replacing the caller must not bypass review.

On a default-branch push, a separate job reads records from the push's `before`
commit and counts the pushed commit. It only lowers records or removes them
when files disappear or reach their ceiling. It refuses growth, manually edited
records, a dirty writer checkout, or a missing base (including a first push).
It commits the change as the Actions bot and performs a normal push: a concurrent
branch update fails instead of overwriting newer work. Re-run against the newest
push to retry. If no records change, it makes no commit.

The writer needs `contents: write` and branch rules that allow its bot commit.
If those rules prohibit direct pushes, the writer fails visibly; arrange an
approved bot exemption through your repository's configuration management before
adoption. The PR job has read permission only, checks the actual PR head, and
executes only the tool from the explicitly selected infra-public revision.
Neither job executes source code from the consuming repository. The token is
not persisted in checkout or written into a remote URL.

Local checks use Python stdlib plus Git (no packages to install):

```sh
python3 .github/scripts/size_ratchet.py check --base origin/main
python3 .github/scripts/size_ratchet_test.py
```

The integration suite runs the writer's actual workflow shell step against a
temporary local Git remote, proving the bot commit lowers records and is pushed.
That is local execution evidence; adopting repositories must still observe their
own Actions run and branch permissions before claiming live deployment proof.
