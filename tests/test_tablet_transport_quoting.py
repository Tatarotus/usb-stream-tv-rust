"""Regressão realista do transporte ADB: ``adb shell su -c`` tem DOIS parses.

O caminho de comando de root não é um ``execve`` de lista de argumentos: ele
atravessa dois interpretadores de shell em série.

1. ``adb shell <palavras...>`` **concatena** as palavras com um espaço e entrega
   a string resultante ao ``/bin/sh`` do dispositivo. Esse é o primeiro parse.
2. O ``su`` do tablet recebe ``-c`` e re-junta as palavras que sobraram numa
   string, que roda em um shell privilegiado. Esse é o segundo parse.

Medido no tablet (gen19): ``root_shell("tr '\\\\000' '\\\\n' < /proc/<pid>/cmdline")``
sem prover a citação do comando inteiro chegou ao ``tr`` como ``tr 000 n`` — o
shell do dispositivo consumiu as aspas e o segundo parse comeu as barras
invertidas — de modo que cada ``\\0`` virou ``n`` (``...e70`` -> ``...e7n``) e os
NULs sobreviveram. ``observe_active_supervisor`` então falhou com
``lock_owner_is_not_a_known_supervisor``. Com o comando inteiro protegido
(``_quote(command)``) o mesmo caminho devolve o ``cmdline`` correto: 9 linhas,
zero NUL, caminho da geração intacto.

Este arquivo não confia em um fake que repassa a string intacta. O runner
``_TwoParseRunner`` monta a cadeia real: concatenação do ADB -> ``/bin/sh`` ->
shim ``su`` que re-junta as palavras de ``-c`` e roda ``sh -c`` nelas. A
contraprova é um snapshot local do ``root_shell`` pré-correção
(``_PreFixRootShell``), um mock controlado do método substituído que NÃO toca o
código-fonte: cada teste de correção tem um par que mostra a mesma asserção
falhando sob o formato antigo.

Escopo: transporte. Nenhum comando destrutivo, nenhum segredo, nenhum ADB real,
nenhum sudo real, nada fora de ``tmp_path``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import mixed_generation_service as mgs  # noqa: E402

SERIAL = "127.0.0.1:25555"
SSH_HOST = "tablet-host"
# Session name keeps the digits that the unquoted path mangles (gen19-0e70 ->
# gen19-ne7n): the regression must be visible on the generation path itself.
SESSION = "mixed-gen19-0e70"
GENESIS_MANIFEST = "manifest-3"
FULL_IDENTITY = "boot-1|instance-2|4242-7777|manifest-3|digest-4"


# Emulated device su. Measured on the tablet: the words after ``-c`` are
# re-joined with single spaces and run by a privileged shell -- that is the
# second parse, and it is what turned ``tr '\\000' '\\n'`` into ``tr 000 n``.
SU_SHIM = r"""#!/bin/sh
[ "$1" = "-c" ] || { echo "emulated su: expected -c" >&2; exit 127; }
shift
cmd="$1"
[ "$#" -gt 0 ] && shift
for a in "$@"; do cmd="$cmd $a"; done
exec /bin/sh -c "$cmd"
"""

# Emulated `adb` on the ADB host. `adb shell <words...>` concatenates its words
# with single spaces and hands the string to the device shell (first parse).
ADB_SHIM = r"""#!/bin/sh
[ "$1" = "-s" ] || { echo "emulated adb: expected -s" >&2; exit 127; }
shift 2
[ "$1" = "shell" ] || { echo "emulated adb: only shell is emulated" >&2; exit 127; }
shift
cmd="$1"
[ "$#" -gt 0 ] && shift
for a in "$@"; do cmd="$cmd $a"; done
exec /bin/sh -c "$cmd"
"""

BUSYBOX_SHIM = "#!/bin/sh\nexec \"$@\"\n"

# The device-side script under test (stand-in for
# activate_live_favorites_tablet.sh). It compares whole arguments, so a token
# that lost its quoting cannot pass: it prints the pre-fix and post-fix
# behaviour differently instead of accepting anything.
DEVICE_SCRIPT = """#!/bin/sh
BASE=__BASE__
PKG=$2
OWNER=$(cat "$BASE/mode_switch.lock/pid" 2>/dev/null || true)
case "$1" in
  describe)
    echo "describe protocol=handoff.ack 1 boot_id=boot-1 generation=19 manifest_id=__MANIFEST__ package_digest=pkg-9 runtime_digest=rt-9 supervisor=$OWNER-7777 candidate=none lun=lun0 lock_owned=1 instance_id=instance-2 identity=__IDENTITY__ path=$PKG"
    ;;
  handoff-lease)
    TOK=$3; TTL=$4; PREV=$5; INC=$6
    [ "$TTL" = "60" ] || { echo "lease-refuse bad_ttl=$TTL"; exit 6; }
    [ "$PREV" = "__SESSION__" ] || { echo "lease-refuse lease_foreign prev=<$PREV>"; exit 6; }
    [ "$INC" = "__IDENTITY__" ] || { echo "lease-refuse identity_unproven inc=<$INC>"; exit 6; }
    echo "handoff-lease token=$TOK expires_in=$TTL"
    ;;
  probe)
    shift
    i=0
    for a in "$@"; do
      i=$((i + 1))
      printf 'arg%d=<%s>\\n' "$i" "$a"
    done
    ;;
  *)
    echo "device script: unknown action <$1>" >&2
    exit 9
    ;;
esac
"""


class _PreFixRootShell(mgs.TabletTransport):
    """Snapshot local do ``root_shell`` anterior à correção (contraprova).

    É exatamente o corpo pré-correção: o comando ia para ``adb shell su -c``
    sem proteção, então a concatenação do ADB e o shell do dispositivo comiam a
    citação que o shell privilegiado precisava. Substitui apenas o método em
    teste, dentro deste arquivo; o módulo de produção não é alterado.
    """

    def root_shell(self, command: str, *, check: bool = True,
                   timeout: float | None = None) -> CompletedProcess:
        return self.adb("shell", "su", "-c", command, check=check, timeout=timeout)


class _TwoParseRunner:
    """Runner que executa a cadeia real: ADB -> shell do dispositivo -> su.

    Não interpreta o comando em Python: monta a string exatamente como o ADB
    faz, entrega a um ``/bin/sh`` de verdade e deixa o shim ``su`` re-juntar e
    re-parsear as palavras. Um fake que repassasse o comando intacto passaria
    nos testes de correção por acidente; este falha com o formato antigo.
    """

    def __init__(self, device_root: Path, shims: Path, env: dict):
        self.device_root = device_root
        self.shims = shims
        self.env = env
        self.ssh_calls = 0
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if argv and argv[0] == "ssh":
            # The remote login shell parses the string ssh passes as its last
            # argument; `adb` then runs on the ADB host (the shim below).
            self.ssh_calls += 1
            completed = subprocess.run(
                ["/bin/sh", "-c", argv[-1]], capture_output=True, text=True,
                env=self.env, cwd=str(self.device_root), timeout=timeout)
            return CompletedProcess(argv, completed.returncode,
                                    completed.stdout, completed.stderr)
        if len(argv) < 4 or argv[0] != "adb" or argv[1] != "-s" or argv[3] != "shell":
            raise AssertionError(f"unexpected transport argv: {argv}")
        if argv[2] != SERIAL:
            raise AssertionError(f"unexpected adb serial: {argv[2]!r}")
        # The ADB concatenation, byte for byte: words joined with one space.
        joined = " ".join(argv[4:])
        completed = subprocess.run(
            ["/bin/sh", "-c", joined], capture_output=True, text=True,
            env=self.env, cwd=str(self.device_root), timeout=timeout)
        return CompletedProcess(argv, completed.returncode,
                                completed.stdout, completed.stderr)


class TransportQuotingTestCase(unittest.TestCase):
    """Fixture: um "tablet" isolado em tmp dir, com processo real e shims."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="adb-quoting-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = self.tmp / "data" / "local" / "tmp"
        self.package_dir = self.base / "live-favorites" / SESSION
        self.package_dir.mkdir(parents=True)
        self.shims = self.tmp / "shims"
        self.shims.mkdir()
        self._write_shim("su", SU_SHIM)
        self._write_shim("adb", ADB_SHIM)
        self._write_shim("busybox", BUSYBOX_SHIM)
        script = (DEVICE_SCRIPT
                  .replace("__BASE__", str(self.base))
                  .replace("__MANIFEST__", GENESIS_MANIFEST)
                  .replace("__IDENTITY__", FULL_IDENTITY)
                  .replace("__SESSION__", SESSION))
        device_script = self.package_dir / "activate_live_favorites_tablet.sh"
        device_script.write_text(script)
        device_script.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.shims}:{self.env.get('PATH', '/usr/bin:/bin')}"
        self.env["LC_ALL"] = "C"
        # A REAL process whose NUL-separated /proc/<pid>/cmdline names the
        # generation path. Nothing about /proc is faked: the kernel writes the
        # file and the device shell reads it through tr.
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", str(device_script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap_child)
        lock = self.base / "mode_switch.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(self.child.pid) + "\n")
        self.runner = _TwoParseRunner(self.tmp, self.shims, self.env)
        self.transport = mgs.TabletTransport(
            adb_serial=SERIAL, base=str(self.base), runner=self.runner)
        self.service = self._service(self.transport)

    def _reap_child(self):
        try:
            self.child.terminate()
            self.child.wait(timeout=10)
        except Exception:
            self.child.kill()

    def _write_shim(self, name: str, body: str):
        path = self.shims / name
        path.write_text(body)
        path.chmod(0o755)

    @staticmethod
    def _service(transport) -> mgs.MixedGenerationService:
        """Service with only the fields these read-only methods need.

        ``__new__`` avoids the constructor's staging/state directories: this
        test must not create anything in the repository.
        """
        service = mgs.MixedGenerationService.__new__(mgs.MixedGenerationService)
        service.transport = transport
        service.guard_ttl = 60
        return service

    def _pre_fix_transport(self) -> _PreFixRootShell:
        return _PreFixRootShell(
            adb_serial=SERIAL, base=str(self.base), runner=self.runner)

    def _pre_fix_service(self) -> mgs.MixedGenerationService:
        return self._service(self._pre_fix_transport())

    def probe_command(self, *args: str) -> str:
        """A command shaped like the service's own ``_script_command``."""
        return (f"busybox sh {self.package_dir}/activate_live_favorites_tablet.sh "
                + " ".join(mgs._quote(a) for a in args))

    # -- the emulation itself ------------------------------------------------
    def test_emulated_two_parses_reproduce_the_measured_damage(self):
        """O shim su re-parseia: sem isso, o restante do arquivo não valeria.

        Estratégia: comando cru (sem ``root_shell``), exatamente como o serviço
        monta o ``tr``. Medido no tablet: ``tr 000 n`` -> ``0`` virou ``n`` e os
        NULs sobreviveram.
        """
        raw_command = f"tr '\\000' '\\n' < /proc/{self.child.pid}/cmdline"
        completed = self.transport.adb("shell", "su", "-c", raw_command,
                                       check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("\x00", completed.stdout,
                      "o segundo parse deveria preservar os NULs (medido)")
        self.assertNotIn("mixed-gen19-0e70", completed.stdout)
        self.assertIn("mixed-gen19-ne7n", completed.stdout,
                      "o segundo parse deveria ter comido as barras invertidas")
        # Every '0' in the file became 'n', including the child's sleep budget:
        # the command really was re-parsed, not passed through.
        self.assertIn("time.sleep(12n)", completed.stdout)

    # -- _cmdline_of / observe_active_supervisor -----------------------------
    def test_cmdline_of_converts_nul_to_newline_through_two_parses(self):
        cmdline = self.service._cmdline_of(self.child.pid)
        self.assertIsNotNone(cmdline)
        lines = cmdline.splitlines()
        self.assertNotIn("\x00", cmdline)
        self.assertGreaterEqual(len(lines), 4)
        self.assertIn(f"{self.package_dir}/activate_live_favorites_tablet.sh",
                      lines)

    def test_pre_fix_cmdline_of_mangles_zeros_and_keeps_nuls(self):
        """Contraprova: o mesmo método, com o ``root_shell`` antigo."""
        cmdline = self._pre_fix_service()._cmdline_of(self.child.pid)
        self.assertIn("\x00", cmdline)
        self.assertNotIn("mixed-gen19-0e70", cmdline)
        self.assertIn("mixed-gen19-ne7n", cmdline)
        # One line only: NULs are not line breaks, so the generation path never
        # becomes its own token.
        self.assertNotIn(f"{self.package_dir}/activate_live_favorites_tablet.sh",
                         cmdline.splitlines())

    def test_observe_active_supervisor_identifies_the_running_instance(self):
        observation = self.service.observe_active_supervisor()
        self.assertTrue(observation["answered"], observation)
        self.assertEqual(observation["session"], SESSION)
        self.assertEqual(observation["path"], str(self.package_dir))
        self.assertEqual(observation["lock_owner"], str(self.child.pid))
        identity = observation["identity"]
        self.assertTrue(identity.incarnation.startswith(f"{self.child.pid}-"))
        self.assertTrue(identity.lock_owned)
        self.assertEqual(identity.identity, FULL_IDENTITY)
        self.assertEqual(identity.manifest_id, GENESIS_MANIFEST)

    def test_pre_fix_observation_reports_the_measured_reason(self):
        """Contraprova medida no tablet."""
        observation = self._pre_fix_service().observe_active_supervisor()
        self.assertFalse(observation["answered"])
        self.assertEqual(observation["reason"],
                         "lock_owner_is_not_a_known_supervisor")

    # -- full identity token, with pipes and zeros ---------------------------
    def test_handoff_lease_accepts_the_full_identity_token(self):
        """O token ``boot|instance|incarnation|manifest|digest`` é um argumento.

        O script do dispositivo exige os 5 componentes intactos: se o segundo
        parse virar os pipes em pipeline, a resposta não é a esperada.
        """
        self.service.handoff_lease(
            SESSION, "a" * 32, previous_session=SESSION,
            observed_incarnation=FULL_IDENTITY)

    def test_pre_fix_handoff_lease_token_becomes_a_pipeline(self):
        with self.assertRaises(mgs.PublicationError) as caught:
            self._pre_fix_service().handoff_lease(
                SESSION, "a" * 32, previous_session=SESSION,
                observed_incarnation=FULL_IDENTITY)
        message = str(caught.exception)
        self.assertIn("tablet refused the handoff lease", message)
        # Evidence of the second parse: the token components were run as
        # commands by the privileged shell.
        self.assertIn("instance-2", message)
        self.assertIn("not found", message)

    # -- literal data: $() must not execute, quotes must survive -------------
    def test_literal_dollar_paren_and_quotes_arrive_as_data(self):
        marker = self.tmp / "must-not-exist"
        tricky = f"a'b $(touch {marker}) c|d 0e70"
        completed = self.transport.root_shell(
            self.probe_command("probe", tricky), check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), f"arg1=<{tricky}>")
        self.assertFalse(marker.exists(),
                         "comando literal executado no shell privilegiado")

    def test_pre_fix_literal_dollar_paren_is_executed_by_the_second_parse(self):
        """Contraprova: o formato antigo transforma dado em comando.

        O literal aqui não contém apóstrofo de propósito: uma aspa simples
        desbalanceada no valor abriria uma citação que o próprio comando
        privilegiado consome, e o caso passaria a se auto-proteger. Sem ela, a
        citação que protegia o valor é gasta no PRIMEIRO parse, e o segundo
        executa a substituição de comando.
        """
        marker = self.tmp / "must-not-exist"
        tricky = f"x $(touch {marker}) y|0e70"
        completed = self._pre_fix_transport().root_shell(
            self.probe_command("probe", tricky), check=False)
        self.assertNotEqual(completed.stdout.strip(), f"arg1=<{tricky}>")
        self.assertTrue(marker.exists(),
                        "o segundo parse deveria ter executado o literal")

    # -- ssh hop -------------------------------------------------------------
    def test_ssh_host_layer_still_protects_the_command(self):
        """Com ``ssh_host`` há um parse a mais; a proteção precisa sobreviver."""
        transport = mgs.TabletTransport(
            ssh_host=SSH_HOST, adb_serial=SERIAL, base=str(self.base),
            runner=self.runner)
        marker = self.tmp / "must-not-exist-ssh"
        tricky = f"x'y $(touch {marker}) z|0e70"
        completed = transport.root_shell(
            self.probe_command("probe", tricky), check=False)
        self.assertEqual(self.runner.ssh_calls, 1)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), f"arg1=<{tricky}>")
        self.assertFalse(marker.exists())
        # The ssh layer must hand the device a serial and a shell subcommand.
        argv = self.runner.calls[-1]
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", SSH_HOST])

    def test_pre_fix_ssh_host_layer_loses_the_argument(self):
        """Contraprova no hop ssh, com o ``root_shell`` antigo."""
        transport = _PreFixRootShell(
            ssh_host=SSH_HOST, adb_serial=SERIAL, base=str(self.base),
            runner=self.runner)
        marker = self.tmp / "must-not-exist-ssh"
        tricky = f"x'y $(touch {marker}) z|0e70"
        completed = transport.root_shell(
            self.probe_command("probe", tricky), check=False)
        self.assertNotEqual(completed.stdout.strip(), f"arg1=<{tricky}>")

    # -- the quoting helper itself -------------------------------------------
    def test_quote_helper_round_trips_through_a_real_shell(self):
        for value in ["plain", "a'b", "a b", "$(touch x)", "p|q;r", "", "0e70"]:
            completed = subprocess.run(
                ["/bin/sh", "-c", f"printf '%s' {mgs._quote(value)}"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.stdout, value, value)


class RootShellShapeTestCase(unittest.TestCase):
    """O ``root_shell`` de produção protege o comando inteiro, uma vez."""

    def test_production_root_shell_quotes_the_whole_command(self):
        seen: list[list[str]] = []

        class _Recorder:
            def __call__(self, argv, timeout):
                seen.append(list(argv))
                return CompletedProcess(argv, 0, "", "")

        transport = mgs.TabletTransport(adb_serial=SERIAL, runner=_Recorder())
        transport.root_shell("cat /a && rm -f /b", check=False)
        self.assertEqual(
            seen[-1],
            ["adb", "-s", SERIAL, "shell", "su", "-c",
             mgs._quote("cat /a && rm -f /b")])

    def test_pre_fix_snapshot_matches_the_documented_old_shape(self):
        """A contraprova só vale se for fiel ao formato antigo."""
        seen: list[list[str]] = []

        class _Recorder:
            def __call__(self, argv, timeout):
                seen.append(list(argv))
                return CompletedProcess(argv, 0, "", "")

        transport = _PreFixRootShell(adb_serial=SERIAL, runner=_Recorder())
        transport.root_shell("cat /a && rm -f /b", check=False)
        self.assertEqual(
            seen[-1],
            ["adb", "-s", SERIAL, "shell", "su", "-c", "cat /a && rm -f /b"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
