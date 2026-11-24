# Testing done by Claude

Pre-Step-19 review and testing of `vast-troubleshooter`, commit `8f35b60`
(2026-11-23). Everything here was run, not assumed. Where a claim comes from
someone else and I did not reproduce it, it says so.

**Status of every finding below: OPEN. Nothing was fixed in this round.** The only
file changed is this one.

## 1. Scoreboard

| Area | Result |
|---|---|
| Existing test suite, Windows | **775 passed, 6 skipped** (the 6 are POSIX-only) |
| Existing test suite, real Linux (Ubuntu 24.04 container, arm64, non-root) | **730 passed, 1 skipped** (a module that needs the repository files) |
| `ruff check` / `ruff format --check` | clean |
| Line coverage (Windows) | **97%**, 66 lines uncovered, mostly error branches and Linux-only code |
| Type checker (`mypy`, temporary, not configured in the project) | **14 errors in 4 files** (see F-22) |
| Dead-code finder (`vulture`) | 3 reports, all false positives |
| My adversarial / fuzz campaign | **6 of 10 checks failed or exposed a defect**, 4 passed |
| My CLI, packaging, and hygiene smoke test | **44 of 48 passed**, 4 failed (2 are real doc defects, 2 are artifacts) |
| Independent code review agent (`/code-review high src`) | 10 findings, **all 10 reproduced by me** |
| Second reviewer's report (`CodeReviewByCodex.md`) | 7 claims reproduced by me, the rest **not** independently reproduced |
| **Distinct open findings** | **31**: 7 safety-critical, 9 medium, 15 low |

The headline: the 775 tests are green, but they did not cover several real gaps.
Three of them break a rule the project states about itself: **"unreadable input
must never look healthy"** (F-01, F-02) and **"a fix must be applied to exactly
what the operator confirmed"** (F-04).

## 2. How I tested

1. Ran the existing suites on Windows and in real Linux, plus lint, coverage, a
   type checker, and a dead-code finder.
2. Wrote my own adversarial tests, independent of the project's tests
   (`fuzz.py`, `redos.py`, `scale.py`, `report_long.py`): random hostile input into
   every collector, the rule engine, the redactor, the Markdown escaper, the runner,
   and the whole report pipeline.
3. Ran the real CLI end to end (every command, every exit code, opt-in flags),
   built the wheel, installed it in a clean virtualenv, and scanned the repository.
4. Ran an independent code review agent over `src/`, then **reproduced each of
   its claims myself** (`verify_review.py`, plus a Linux run for three of them).
5. Read the second reviewer's report and reproduced the claims that could be checked
   cheaply (`verify_codex.py`).

The scripts are in my scratch folder, not in the repository. They are small and I
can turn the useful ones into regression tests when the fixes are done.

## 3. What passed (the successes)

These are properties I actively tried to break and could not.

| Property | How I tried to break it | Result |
|---|---|---|
| **Nothing passes when nothing could be read** (as a whole) | Made every operation fail, then evaluated all rules | **0 of 15 rules passed**: 1 failed, 13 unknown, 1 skipped. (But see F-01 and F-02: this holds only when *everything* fails, not when *some* sources succeed.) |
| Rule engine robustness | 3,000 random fact sets (every status, `NaN`, `inf`, `10**30`, hostile strings, missing facts) | **No exception** |
| Runner operand injection | 27 hostile `/dev/...` operands (`;`, `&&`, `$()`, backticks, `..`, newlines, NUL, Unicode line separator, leading `-`, wrong case, trailing space) against `smartctl` | **All refused. No process was started** |
| Collector speed on hostile input | Up to 200 KB garbage lines, 5,000-deep JSON, 3,000 repeated markers | Slowest single call **0.027 s** |
| Markdown is inert | 3,000 random strings plus `<script>`, links, images, headings, tables, entities, auto-links | **No unescaped `< > [ ] ( ) * \` \| # ! &`, no newline, no `://`** |
| Redaction is idempotent on random input | 3,000 fuzz strings | **Idempotent** (but see F-03, where a *host named `host`* breaks it) |
| Report JSON always valid | Every report that was produced parsed and matched the model | **No invalid report** |
| Facts always serialise | Every fact from every collector | **No bad fact** |
| CLI exit codes | `diagnose` (3 here), `collect <unknown>` (2), `fix <unknown>` (1), demo gate (1), rejected flags (2) | **As documented** |
| No way to skip redaction or confirmation | `report --no-redact / --raw`, `fix --apply --yes / --force` | **All rejected with exit 2** |
| `diagnose --json` is machine-clean | Compared stdout and stderr | **stderr empty** without opt-in flags |
| Report contents | Wrote a real report, searched it | **No host name, no user name, no `raw_excerpt`/`source_command`**; two reports never overwrite each other |
| Packaging | `uv build`, inspected the wheel, installed it in a clean Python 3.15 venv, ran it | sdist and wheel built; **6 rule files + LICENSE shipped, no tests/e2e/docs leaked**; the clean install ran `version` and saw all 15 rules |
| Repository hygiene | Scanned all tracked files for token and key patterns, `.env`, keys, large files | **None found** |
| Real Linux behavior | Whole suite in a container as non-root, including the 6 POSIX-only tests | **Pass** |
| Real systemd | Step 18 run (separate document) | DNS fix, rollback, restart fix, report, permissions all worked |

## 4. What failed (the findings)

Severity is my judgment: **Critical** = can mislead an operator or change a host
wrongly; **Medium** = wrong or unsafe in realistic cases; **Low** = robustness,
consistency, or documentation. "Source" says who found it. **"Reproduced"** means I
ran it myself and saw the failure.

### 4.1 Safety-critical

**F-01. One readable drive makes SMART rules PASS for drives that could not be read.**
*Source: second reviewer. Reproduced by me.* The SATA drive was readable and the
NVMe query **timed out**. The result: `nvme0n1.smart_passed` is `timeout`, but
`disk.nvme_unhealthy` is **PASS** and `disk.smart_health_failed` is **PASS**. The
aggregate counters are built only from drives that were read, and are emitted as
soon as one drive was, so an unreadable drive contributes zero. This is the same
class of bug I said was prevented. *Fix:* track coverage per diagnostic; emit an
aggregate only when every applicable drive was read, or add an "unreadable drive
count" fact that makes the rules `unknown`. Keep a failure seen on any drive.

**F-02. A truncated kernel log passes the critical Xid 79 rule.**
*Second reviewer. Reproduced.* A log with `Xid 31` and `kernel_log.truncated = True`
gives **PASS** for "GPU fell off the bus (Xid 79)". The collector records
`truncated`, but no rule reads it, so a truncated capture is treated as a complete
one. *Fix:* when truncated and no Xid 79 was seen, make that rule `unknown`.

**F-03. The report guard breaks on a host named `host` or a user named `user`.**
*Code review agent. Reproduced.* `/home/user/x on host` becomes
`/home/[[user]]/x on [host]`, then `[[host]]` on the second pass. The "second pass
must not change anything" guard then fires, and **`report` refuses to run, every
time**, on such a machine. Container users named `user` are common. *Fix:* apply
the by-value host, user, and id replacements before the placeholder-producing
patterns, or skip matches inside `[...]` placeholders.

**F-04. The CLI shows one fix target and applies another.**
*Second reviewer. Reproduced at engine level.* `fix --apply` calls the engine twice:
a preview, then the apply. Each call discovers its own target (the network link).
When the default route changed between them, the preview showed `eth0` and the apply
acted on `eth1` and succeeded. The operator confirmed something different from what
ran. *Fix:* carry the previewed operands into the apply, rediscover under the lock
right before the change, and require a fresh confirmation if they differ.

**F-05. The rollback runs when it should not, and can destroy existing settings.**
*Code review agent (reproduced by me) and second reviewer (reproduced on real
systemd by them, not by me).*
(a) If the fix **worked** but a later command failed (for example `flush-caches`),
the engine reports "failed", then rolls back and **reverts the working fix**
(reproduced: calls `set`, `flush`, `undo`).
(b) If the **first command failed and changed nothing**, the rollback still runs
(reproduced: calls `set`, `undo`).
(c) `resolvectl revert <link>` resets the link to its defaults. It does not restore
what was there before. The second reviewer reports that on real systemd-resolved a
forced failure **cleared a pre-existing per-link DNS server and search domain** and
the CLI still said "rolled back". I did not reproduce (c). *Fix:* snapshot the state
before the change and restore it; run the rollback only for steps that actually
ran and only if the check does not pass; refuse the fix when the link already has
DNS servers or domains.

**F-06. A log-write failure after the change escapes as an uncaught exception.**
*Code review agent and second reviewer. Reproduced.* Only the first log write
(`apply_started`) is guarded. If a later write fails (disk full), the exception
skips the remaining commands, the check, the rollback, and the outcome. In my
reproduction `OSError(28, "No space left on device")` was raised **after the change
had been made**, and the caller got a traceback and no outcome. *Fix:* guard the
post-change writes, keep going, record a warning, and always observe and judge.

**F-07. A timed-out command run through `sudo` can hang the tool forever.**
*Code review agent and second reviewer. Reproduced on Linux by simulation.* The
runner kills the process group on timeout, swallows any `PermissionError`, then
calls `proc.wait()` with no limit. A command started with `sudo -n` runs as root and
a normal user cannot signal it. With the kill denied, a **1-second timeout returned
after 12 seconds, the command's own length** (verified in the container as uid
10001 and uid 0). A command that never exits would hang forever. This affects every
elevated operation under `--allow-root-reads` or `--use-sudo`. I simulated the
denied kill; I did not run a real `sudo` (the container user has none). *Fix:* bound
the cleanup wait, report the timeout regardless, and surface that termination could
not be verified.

### 4.2 Medium

**F-08. An empty rule directory is an all-clear.** *Second reviewer. Reproduced.*
`load_rules_dir` on an empty directory returns `[]`; the diagnosis then has 0 checks
and would exit 0. A broken installation looks healthy. *Fix:* an empty default rule
set, or a directory or decoding error, is a tool error (exit 2).

**F-09. "Docker daemon is not running" is observed but no rule reports it.**
*Second reviewer. Reproduced.* `docker.daemon_running = False [ok]` exists, no
Docker rule fails, and the NVIDIA-runtime rule is merely `unknown`. *Fix:* add a
daemon rule, and gate the runtime rule on a running daemon.

**F-10. A command that could not be started is reported as "nvidia-smi is failing".**
*Second reviewer. Reproduced.* A launch `PermissionError` with no exit code gives
`driver.nvidia_smi_responding = False` and a critical finding. *Fix:* separate a
launch failure from a non-zero exit; only the latter is a driver failure.

**F-11. The redactor misses identifiers glued to other characters.**
*Found by my fuzzing. Reproduced.* `vm_10.0.0.5_data`, `x10.0.0.5`, `10.0.0.5abc`,
`ip10.0.0.5` are **not redacted**; neither is `user_alice` or `alice_home`
(user `alice`). The IPv4 and name patterns refuse to match next to a word
character. That contradicts the "aggressive: a leak is worse than a false positive"
rule. An IPv6 address directly after a hex letter is also missed (ambiguous by
nature; limitation). *Fix:* only guard against digits and dots for IPv4, and use
alphanumeric-only boundaries for names.

**F-12. The redactor is quadratic, so long strings can hang it.**
*Found by my fuzzing. Reproduced and measured.* `Redactor.text("a" * n)` takes
0.03 s at 4,000 characters, 0.14 s at 8,000, 0.52 s at 16,000, and **over 20 s at
100,000**. `_EMAIL` (0.125 s at 8,000) and `_KEYVALUE` (2.6 s at 8,000 characters
of digits and dots) are the culprits. My first fuzz run **never finished (killed
after 10.5 minutes)** because of this. Normal values are capped at 200 characters,
so this needs unusual input (see F-13). *Fix:* cap the length before matching
(idempotently), and make the patterns linear.

**F-13. The report's host fields and evidence values bypass the 200-character limit.**
*Found by my fuzzing. Reproduced.* `host.os`, `kernel`, `gpu_model`, and the
`evidence` values are copied from facts without the safety check that
`ShareableFact` applies. A 40,000-character OS name went straight into the report
(JSON 85 KB) and building it took **12.6 s** (quadratic, see F-12). Control
characters are escaped later, so this is not a leak, but it is unbounded.
*Fix:* apply the same length and control-character check to every report field.

**F-14. Docker's root directory (and other raw strings) is exported as a normal fact.**
*Second reviewer. Not reproduced by me.* `docker.root_dir` is a shareable fact value
and appears in `diagnose --json` without redaction. A custom path segment (a
customer label) is not matched by the redaction patterns. *Fix:* define exportable
values per fact key and purpose; keep capacity and filesystem type, not the path.

**F-15. Storage does not verify who owns existing directories and files.**
*Second reviewer. Not reproduced by me.* The report directory check rejects
group/world-writable directories but accepts one owned by another user. The lock
and log files are accepted without checking owner or mode. *Fix:* validate owner and
mode on POSIX, and create files relative to the validated directory.

**F-16. Report writing can leave a single file behind on a sync failure.**
*Second reviewer. Not reproduced by me.* If the Markdown `fsync` fails after the
file is created, a lone `.md` can remain. *Fix:* clean up each file if its own write
or sync fails.

### 4.3 Low

| ID | Finding | Source / status |
|---|---|---|
| F-17 | **`system.uptime` vs `system.uptime_seconds`.** On a read failure the fact is `system.uptime`; on success `system.uptime_seconds`. Anything reading the success key sees "no data". The project's own tests assert the wrong key. | Code review agent. Reproduced. |
| F-18 | **A missing tool is reported as `not_allowed`.** `plan()` maps `MISSING` to the same block code as a policy refusal. | Code review agent. Reproduced. |
| F-19 | **Rollback steps are never planned at dry run.** A rollback that is not allowlisted still gets a clean dry run and is only found to be unusable after the change. The dry run also does not show the rollback commands. | Code review agent. Reproduced. |
| F-20 | **Parent directories are created `0755`.** `mkdir(mode=0o700, parents=True)` applies the mode only to the last level. On a fresh machine the state directory is world-readable. | Code review agent. Reproduced on Linux (`a` and `b` were `0o755`, `reports` `0o700`). |
| F-21 | **State is split by user.** Root uses `/var/lib/vast-troubleshooter`, a normal user `~/.local/state/...` (verified on Linux). A root fix run and a user `report` do not see each other's log, so the report can say "no fix has been run". Two users do not share a lock. | Code review agent. Reproduced. I knew about the lock split; the report effect is new. |
| F-22 | **Type checker: 14 errors in 4 files.** Mostly noise (`killpg` does not exist on Windows, `IO` vs `BinaryIO`). Real smells: `report.py` reuses one loop variable for two types; `Fix.commands` is typed `FixStep \| str` but normalized to `FixStep`; `RuleResult(**dict)` is not type-safe. No type checker is configured. | Mine. |
| F-23 | **IPv4-mapped IPv6 leaks the tail.** `::ffff:192.168.1.5` becomes `[redacted-ip].168.1.5`. My own earlier test only checked one octet, so it passed. | Code review agent. Reproduced. |
| F-24 | **Collectors can raise on hostile input.** A drive name over about 90 characters makes a fact key invalid (`ValidationError`); a CSV field over 131,072 characters raises `csv.Error`. 6 crashes in my 150 hostile hosts. In production `run_collectors` turns this into one error fact, so the result is safe but all of that collector's facts are lost. | Mine. Reproduced. |
| F-25 | **`df` path operand allows `..`.** `/dev/../etc/passwd` is accepted. Harmless (`df` is read-only and the collector validates its own input), but the pattern should reject it. | Mine. Reproduced. |
| F-26 | **Read-only root filesystem makes a critical finding in the container.** A Compose container with a read-only root gets `disk.root_not_writable` critical and exit 1. The environment banner helps humans; automation still sees a failure. | Second reviewer. I saw the finding myself in step 17; not re-tested here. |
| F-27 | **The Step 18 script does not fail when a check fails.** `vm()` returns the status of its logging, and several probes pipe into `head`, so a failed repair can still end with exit 0. | Second reviewer. I confirm it by reading my own script. Not run. |
| F-28 | **README still says "Design phase. No working code yet."** | My smoke test. Confirmed. |
| F-29 | **PLAN.md's status table still shows Level 1 "Draft" and Levels 2 and 3 "Outline".** | My smoke test. Confirmed. |
| F-30 | **`pyproject.toml` description is still "Add your description here".** | Second reviewer. Reproduced. |
| F-31 | **`.coverage` is not in `.gitignore`.** My coverage run created one (I deleted it). | Mine. |

## 5. Mistakes and noise in my own testing (reported, not hidden)

- **My first fuzz run was too heavy and never finished.** It fed 100 KB to 1 MB
  strings to a quadratic redactor, piped through `tail`, so I saw nothing for 10.5
  minutes. I killed it. The slowness was itself a finding (F-12, F-13).
- **I used the wrong Python interpreter twice** (the system one, not the project
  virtualenv). No effect on results.
- **My harness crashed in the report-pipeline section** because I called collectors
  directly instead of through the production wrapper. I fixed the harness. That
  crash is how I found F-24.
- **My fuzz section that planted secrets also hit F-11** (secrets surrounded by
  random characters). I read that as a finding, not noise, after checking the
  patterns' lookarounds.
- **The first coverage and type-checker runs were on Windows only,** so
  POSIX-only branches show as uncovered. The Linux run covers them.
- **Two of my four smoke-test failures were not code defects:** "working tree clean"
  failed because of the other reviewer's files and my own `.coverage`; "equals
  origin/main" failed for the same reason.
- **The earlier Step 17 and 18 write-ups overstated one thing:** I wrote that an
  unreadable log never looks clean. That is true for the kernel-log and daemon-log
  *read failing*, but not for *truncation* (F-02) or *partial* drive reads (F-01).

## 6. Bugs my own tests found during development (for completeness)

These were found and fixed earlier, each with a regression test. They show the
kinds of errors that have recurred.

| Step | Bug | Fixed |
|---|---|---|
| 3 | `shutil.which` takes `mode` second, so the search path was passed wrong | yes |
| 6 | Assumed `docker info` puts errors in JSON; real Docker writes them to stderr only | yes |
| 10 | `smartctl`'s own error text ("Smartctl open device") matched my substring check for "smart" | yes |
| 11 | pydantic `pattern=` is an unanchored search, so `"Bad Id!"` and `fix_id = "rm -rf"` passed | yes |
| 15 | Link-name pattern allowed a leading `-` | yes |
| 16 | A refused (never-run) `nvidia-smi` was reported as "failing" | yes |
| 17 | A container reported its read-only root as a critical host finding | partly (banner added; see F-26) |
| 18 | My first DNS scenario was invalid; my first leak check was too loose; the e2e stage nearly became the default build | yes |
| after 18 | A failed undo was reported as "rolled back" | yes (commit `8f35b60`) |

## 7. What was NOT tested

- **No real Vast host and no real VM.** Everything Vast-specific (the daemon, its
  log format, how a rental looks) is still unknown.
- **No NVIDIA GPU, no real SMART, no real kernel log** in any run.
- **No `amd64` run.** All Linux runs were `arm64`.
- **A real `sudo` was never run.** F-07 is a simulation of a denied kill.
- **F-05(c), F-14, F-15, F-16, F-26, F-27** were reported by the second reviewer and
  are not reproduced by me.
- **No mutation testing, no property-based testing library, no load or concurrency
  stress** beyond the unit tests.
- **The Markdown was never viewed in a renderer** (GitHub, VS Code).
- **The interactive confirmation prompt** was only exercised through the test
  runner's piped input, not typed by a human against a fix that can run.

## 8. Recommendation

Do not start Step 19 (release packaging) until the seven safety-critical findings
are fixed, because Step 19 is when the tool becomes something other people
install. Suggested order, smallest risk first:

1. **F-01, F-02, F-08, F-09, F-10**: make "could not read" and "incomplete" always
   `unknown`. These are small, mostly in collectors and rules, and each gets a
   regression test.
2. **F-03, F-11, F-12, F-13, F-23**: fix redaction ordering, boundaries, and length.
3. **F-06, F-07**: harden the runner and engine around logging and timeouts.
4. **F-04, F-05, F-19**: bind the apply to what was previewed, snapshot before the
   DNS change, restrict the rollback, and plan it at dry run.
5. **F-14, F-15, F-16, F-20, F-21**: storage ownership, permissions, and what is exported.
6. **F-17, F-18, F-22, F-24, F-25, F-28 to F-31**: cleanups and documentation.

I can turn every reproduction in this file into a regression test as part of the
fixes, so the same mistake cannot return silently.

## 9. How to reproduce

```powershell
# Suites
uv run pytest -q
docker compose run --rm tests
uv run ruff check . ; uv run ruff format --check .

# Type check and dead code (temporary tools)
uv run --with mypy mypy src --ignore-missing-imports --python-version 3.15
uv run --with vulture vulture src --min-confidence 70
uv run --with coverage coverage run --source=src -m pytest -q ; uv run --with coverage coverage report -m
```

The adversarial scripts (`fuzz.py`, `redos.py`, `scale.py`, `report_long.py`,
`verify_review.py`, `verify_codex.py`, `verify_linux.py`, `smoke.sh`) are in my
scratch folder. They are not part of the repository yet.
