#!/usr/bin/env bash
# End-to-end POC test on a DISPOSABLE test machine: a container with systemd as
# PID 1. Writes a transcript of every step and its real output, and CHECKS the
# result of each scenario: if any check fails, this script exits non-zero.
#
#   bash e2e/run_poc.sh [transcript-file]
#
# The container needs SYS_ADMIN (systemd mounts, and the cgroup filesystem below),
# NET_ADMIN (to blackhole routes and force a real fix failure) and relaxed
# seccomp/AppArmor. It is NOT --privileged. It gets its OWN cgroup namespace and
# a read-write view of that namespace only: nothing from the host (no cgroup tree,
# no socket, no file) is mounted into it. Run it only on a development machine,
# never next to anything you care about.
set -u
export MSYS_NO_PATHCONV=1   # keep Git Bash from rewriting /paths

# A unique name, so this script never touches an unrelated container, and only
# ever removes the one it started.
NAME="vast-e2e-$$-$(date +%s)"
IMAGE=vast-troubleshooter:e2e
OUT="${1:-docs/poc-transcript.txt}"
FIXTURES=tests/fixtures/real_systemd_container

cd "$(dirname "$0")/.."
mkdir -p "$(dirname "$OUT")" "$FIXTURES"
: > "$OUT"

FAILS=0
CHECKS=0
LAST_OUT=""
LAST_EXIT=0

say()  { printf '%s\n' "$*" | tee -a "$OUT"; }
step() { printf '\n### %s\n' "$*" | tee -a "$OUT"; }

# Run a command inside the test machine, record its output and its real exit code.
vm() {
  printf '$ %s\n' "$*" | tee -a "$OUT"
  LAST_OUT=$(docker exec "$NAME" sh -c "$*" 2>&1)
  LAST_EXIT=$?
  printf '%s\n[exit %s]\n' "$LAST_OUT" "$LAST_EXIT" | tee -a "$OUT"
}
# Same, but feed one line to the command's stdin (the typed confirmation).
vm_typed() {
  local typed="$1"; shift
  printf '$ (typing: %s) %s\n' "$typed" "$*" | tee -a "$OUT"
  LAST_OUT=$(printf '%s\n' "$typed" | docker exec -i "$NAME" sh -c "$*" 2>&1)
  LAST_EXIT=$?
  printf '%s\n[exit %s]\n' "$LAST_OUT" "$LAST_EXIT" | tee -a "$OUT"
}
capture() {  # capture <fixture-file> <command...>: raw output, kept as a test fixture
  local file="$1"; shift
  docker exec "$NAME" sh -c "$*" > "$FIXTURES/$file" 2>&1
}

# Checks on the output and exit code of the last vm/vm_typed call.
verdict() {
  CHECKS=$((CHECKS + 1))
  if [ "$1" = ok ]; then
    say "  [check ok]     $2"
  else
    FAILS=$((FAILS + 1))
    say "  [CHECK FAILED] $2"
  fi
}
expect_exit()  { if [ "$LAST_EXIT" = "$1" ]; then verdict ok "$2 (exit $1)"; else verdict bad "$2 (wanted exit $1, got $LAST_EXIT)"; fi; }
expect_has()   { if printf '%s' "$LAST_OUT" | grep -q -E -- "$1"; then verdict ok "$2"; else verdict bad "$2 (not found: $1)"; fi; }
expect_lacks() { if printf '%s' "$LAST_OUT" | grep -q -E -- "$1"; then verdict bad "$2 (found: $1)"; else verdict ok "$2"; fi; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

say "POC end-to-end test, started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "Test machine: a container running systemd as PID 1 (image $IMAGE), root user, private cgroup namespace."

step "0. Boot the test machine"
# Docker mounts the container's cgroup filesystem read-only. systemd needs it writable, so
# remount the container's OWN cgroup view (its private namespace) read-write, then start init.
docker run -d --name "$NAME" --hostname poc-machine --cgroupns=private \
  --cap-add SYS_ADMIN --cap-add NET_ADMIN \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
  --entrypoint /bin/sh "$IMAGE" \
  -c 'mount -o remount,rw,bind /sys/fs/cgroup && exec /sbin/init' >/dev/null \
  || { say "could not start the machine"; exit 1; }
for _ in $(seq 1 60); do
  s=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null); [ "$s" = running ] && break; sleep 1
done
vm "systemctl is-system-running; cat /etc/os-release | head -2; uname -r; id -un"
expect_has "^running" "systemd finished booting"
vm "systemctl is-active systemd-resolved vastai.service"
expect_exit 0 "systemd-resolved and the stand-in Vast daemon are active"
# The machine's own working upstream resolver, from the container runtime. Read it now,
# before /etc/resolv.conf is replaced.
UP=$(docker exec "$NAME" awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)
say "(the machine's own upstream resolver, from the container runtime: $UP)"

step "1. Baseline: resolver is the plain systemd-resolved stub, DNS works"
vm "mkdir -p /etc/systemd/resolved.conf.d; printf '[Resolve]\nDNS=$UP\nFallbackDNS=\n' > /etc/systemd/resolved.conf.d/e2e-upstream.conf; printf 'nameserver 127.0.0.53\noptions edns0 trust-ad\n' > /etc/resolv.conf; systemctl restart systemd-resolved; sleep 2; cat /etc/resolv.conf; resolvectl status | head -5"
expect_has "nameserver 127.0.0.53" "resolv.conf is the plain stub"
vm "timeout 15 python3 -c \"import socket; print('vast.ai ->', socket.getaddrinfo('vast.ai', None)[0][4][0])\""
expect_exit 0 "vast.ai resolves at the start"
vm "vast-troubleshooter diagnose"
expect_lacks "DNS cannot resolve vast.ai" "diagnose reports no DNS problem on a working resolver"

step "2. Capture real systemd output as fixtures (active, inactive, auto-restart, failed, not found)"
capture systemctl_show_vastai_active.txt "systemctl show vastai.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
capture systemctl_is_active_resolved.txt "systemctl is-active systemd-resolved; echo exit=\$?"
vm "cat /etc/systemd/system/vastai.service | grep -c RestartSec"
vm "systemctl kill -s KILL vastai.service; sleep 1; systemctl show vastai.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
capture systemctl_show_vastai_autorestart.txt "systemctl show vastai.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
vm "systemctl stop vastai.service; systemctl show vastai.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
expect_has "ActiveState=inactive" "the stopped unit reads as inactive"
capture systemctl_show_vastai_inactive.txt "systemctl show vastai.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
vm "printf '[Service]\nExecStart=/bin/false\n' > /etc/systemd/system/e2ebad.service; systemctl daemon-reload; systemctl start e2ebad.service; sleep 1; systemctl show e2ebad.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
expect_has "ActiveState=failed" "a failing unit reads as failed"
capture systemctl_show_failed.txt "systemctl show e2ebad.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
capture systemctl_show_notfound.txt "systemctl show nonexistent-unit.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
vm "systemctl show nonexistent-unit.service -p LoadState -p ActiveState -p SubState -p NRestarts --no-pager"
expect_has "LoadState=not-found" "a missing unit reads as not-found"
capture resolvectl_status.txt "resolvectl status"
vm "systemctl reset-failed e2ebad.service; rm -f /etc/systemd/system/e2ebad.service; systemctl daemon-reload; systemctl start vastai.service; systemctl is-active vastai.service"
expect_exit 0 "the stand-in Vast daemon is running again"

# A dead upstream is NOT a usable fault here: Docker Desktop's network answers DNS sent to
# any address (observed in the first run). No upstream at all is a real, realistic fault.
step "3. Inject the fault: systemd-resolved has no upstream DNS server at all"
vm "rm -f /etc/systemd/resolved.conf.d/e2e-upstream.conf; systemctl restart systemd-resolved; resolvectl flush-caches; sleep 2; resolvectl status | head -6"
vm "timeout 30 python3 -c \"import socket,time; t=time.time()
try:
    print('resolved', socket.getaddrinfo('vast.ai', None)[0][4][0])
except Exception as e:
    print('RESOLUTION FAILED after %.1fs: %s' % (time.time()-t, e))\""
expect_has "RESOLUTION FAILED" "the injected fault really breaks name resolution"

step "4. diagnose must report the DNS finding"
vm "vast-troubleshooter diagnose"
expect_has "DNS cannot resolve vast.ai" "diagnose shows the DNS problem"
vm "vast-troubleshooter diagnose --json | python3 -c \"import json,sys; d=json.load(sys.stdin); print('exit_code', d['exit_code'], 'findings', [f['rule_id'] for f in d['findings']])\""
expect_has "network.dns_vast_ai_unresolvable" "the DNS rule is among the findings"

step "5. The DNS fix is demo only: refused without the demo flag"
vm "vast-troubleshooter fix"
expect_has "fix-dns-resolver \\(demo only\\)" "the fix list marks the DNS fix as demo only"
vm "vast-troubleshooter fix fix-dns-resolver"
expect_exit 1 "refused without the demo flag"
expect_has "BLOCKED" "reported as blocked"

step "6. With the demo flag, the real CLI is still BLOCKED (VM rental source is a stub: unknown)"
vm "vast-troubleshooter fix fix-dns-resolver --demo-disposable-host"
expect_exit 1 "blocked while the rental state is unknown"
expect_has "rental state is unknown" "the reason is the unknown rental state"
vm_typed "fix-dns-resolver" "vast-troubleshooter fix fix-dns-resolver --apply --demo-disposable-host"
expect_exit 1 "an apply is blocked too"

step "7a. Rental simulation: a container exists (evidence from the real container source)"
vm "printf 'running\n' > /var/lib/e2e/containers; vast-troubleshooter collect rentals"
vm "vast-troubleshooter fix fix-dns-resolver --demo-disposable-host"
expect_exit 1 "blocked while a container runs"
step "7b. Remove that evidence: the state becomes unknown (VM source cannot prove absence), the fix stays blocked"
vm ": > /var/lib/e2e/containers; vast-troubleshooter collect rentals"
vm "vast-troubleshooter fix fix-dns-resolver --demo-disposable-host"
expect_exit 1 "still blocked: no rental can be proven absent"

step "8. HARNESS (injected VM evidence): a running container still blocks the apply"
vm "printf 'running\n' > /var/lib/e2e/containers; python3 /opt/e2e/driver.py fix-dns-resolver --apply"
expect_exit 1 "blocked by an active rental"
expect_has "rental state is active" "the reason is the active rental"
vm ": > /var/lib/e2e/containers"

step "9. HARNESS (injected VM evidence = none): dry run, then apply the DNS fix for real"
vm "python3 /opt/e2e/driver.py fix-dns-resolver"
expect_exit 0 "the dry run succeeds"
expect_has "DRY_RUN" "it is a dry run"
vm "python3 /opt/e2e/driver.py fix-dns-resolver --apply"
expect_exit 0 "the apply succeeds"
expect_has "SUCCEEDED" "the fix is reported as succeeded"
vm "resolvectl status eth0 | head -8"
vm "timeout 20 python3 -c \"import socket; print('vast.ai ->', socket.getaddrinfo('vast.ai', None)[0][4][0])\""
expect_exit 0 "vast.ai resolves after the fix"

step "10. Re-run diagnose: the DNS finding must clear"
vm "vast-troubleshooter diagnose"
expect_lacks "DNS cannot resolve vast.ai" "the DNS problem is gone"
vm "vast-troubleshooter diagnose --json | python3 -c \"import json,sys; d=json.load(sys.stdin); print('exit_code', d['exit_code'], 'findings', [f['rule_id'] for f in d['findings']])\""
expect_lacks "network.dns_vast_ai_unresolvable" "the DNS rule no longer fires"

step "11. Undo by hand: resolvectl revert eth0 (what the fix's undo notes say)"
vm "resolvectl revert eth0; resolvectl status eth0 | head -6"

step "12. Force a REAL failure and watch the automatic rollback (blackhole the public resolvers)"
vm "ip route add blackhole 1.1.1.1/32; ip route add blackhole 8.8.8.8/32; ip route show | grep blackhole"
expect_exit 0 "both public resolvers are blackholed"
vm "python3 /opt/e2e/driver.py fix-dns-resolver --apply"
expect_exit 4 "the fix ran and failed"
expect_has "FAILED" "reported as failed"
expect_has "rolled back" "the change was rolled back"
vm "resolvectl status eth0 | head -6"
vm "ip route del blackhole 1.1.1.1/32; ip route del blackhole 8.8.8.8/32"
expect_exit 0 "the blackholes are removed"

step "12b. A link with its OWN DNS settings is refused: the fix could not restore them"
vm "resolvectl dns eth0 192.0.2.1; resolvectl domain eth0 corp.invalid; resolvectl dns eth0; resolvectl domain eth0"
expect_has "192.0.2.1" "the link has its own DNS server"
vm "python3 /opt/e2e/driver.py fix-dns-resolver --apply"
expect_exit 1 "blocked"
expect_has "already has its own DNS servers" "the reason names the existing settings"
vm "resolvectl dns eth0; resolvectl domain eth0"
expect_has "192.0.2.1" "the existing DNS server is untouched"
expect_has "corp.invalid" "the existing search domain is untouched"
vm "resolvectl revert eth0"

step "13. Other resolver setups are left to the operator (static resolv.conf, no systemd-resolved stub)"
vm "printf 'nameserver 192.0.2.1\n' > /etc/resolv.conf; python3 /opt/e2e/driver.py fix-dns-resolver --apply"
expect_exit 1 "blocked"
vm "printf 'nameserver 127.0.0.53\noptions edns0 trust-ad\n' > /etc/resolv.conf"

step "14. The other live fix: restart-vast-daemon (service stopped, so the rule fails)"
vm "systemctl stop vastai.service; vast-troubleshooter diagnose | grep -A3 'vastai.service is not running' | head -5"
vm "vast-troubleshooter fix restart-vast-daemon"
expect_exit 1 "the real CLI is blocked (rental state unknown)"
vm "python3 /opt/e2e/driver.py restart-vast-daemon"
expect_exit 0 "the dry run succeeds"
vm "python3 /opt/e2e/driver.py restart-vast-daemon --apply"
expect_exit 0 "the restart succeeds"
expect_has "SUCCEEDED" "reported as succeeded"
vm "systemctl is-active vastai.service"
expect_exit 0 "the daemon is active afterwards"
vm "python3 /opt/e2e/driver.py restart-vast-daemon --apply"
expect_exit 1 "a second apply finds nothing to fix"
expect_has "nothing to fix" "the reason is that the check already passes"

step "15. Report: written on real Linux, then checked for leaks"
vm "vast-troubleshooter report --output-dir /reports"
expect_exit 0 "the report is written"
vm "ls -la /reports"
# Identifying text must be absent: this machine's name, its addresses (container and runtime), a user@host.
vm "grep -c -i -E 'poc-machine|172\\.17\\.0\\.|192\\.168\\.65\\.|root@' /reports/*.json /reports/*.md; true"
expect_lacks ":[1-9]" "no machine name, address or user@host in the report files"
# Well-known public resolvers are left readable on purpose (our advice and the fix commands name them).
vm "grep -o -h -E '1\\.1\\.1\\.1|8\\.8\\.8\\.8' /reports/*.json /reports/*.md | sort | uniq -c"
# Any other dotted-quad address at all?
vm "grep -o -h -E '[0-9]{1,3}(\\.[0-9]{1,3}){3}' /reports/*.json /reports/*.md | grep -v -E '^(1\\.1\\.1\\.1|8\\.8\\.8\\.8)\$' | sort -u; echo '(end of other addresses)'"
expect_lacks "^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$" "no other address appears in the report"
vm "grep -o -E '\"environment\": \"[^\"]*\"' /reports/*.json"
expect_has "container" "the report says it came from a container"
vm "grep -c -E 'raw_excerpt|source_command' /reports/*.json; true"
expect_lacks ":[1-9]" "no raw excerpt or source command in the report"

step "16. Action log and file permissions"
vm "ls -la /var/lib/vast-troubleshooter"
expect_has "drwx------" "the state directory is owner-only"
vm "python3 -c \"import json; [print(json.dumps({k:v for k,v in json.loads(l).items() if k in ('time','run_id','fix_id','phase','status','block_code')}, sort_keys=True)) for l in open('/var/lib/vast-troubleshooter/actions.jsonl')]\" | tail -25"
vm "grep -c -i -E 'stdout|stderr|password|token' /var/lib/vast-troubleshooter/actions.jsonl; true"
expect_lacks "^[1-9]" "the action log holds no command output or secret-looking word"

say ""
say "Finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "$CHECKS checks, $FAILS failed."
[ "$FAILS" -eq 0 ]
