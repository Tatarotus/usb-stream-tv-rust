#!/usr/bin/env python3
"""B1 — the refresh CLI must adopt and finish the operation it was given.

The auditor's finding: `/api/mixed/refresh` created the operation and launched
the worker, but the worker's `--refresh` branch returned before building the
tracker. The row stayed in `preparing` for ever, which holds the tablet's single
publication slot (so every later apply/refresh answered 409) and is not even
reported as a pending problem.

These tests run the REAL command line — `mixed_generation_service.py --refresh`
as a subprocess — with a real SQLite store, and then inspect the row. A tracker
built by hand in the test process would not prove the worker adopts anything, so
none is used here.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mixed_operations as ops  # noqa: E402

SCRIPT = ROOT / "scripts" / "mixed_generation_service.py"
OP_ID = "0123456789abcdef0123456789abcdef"


class RefreshCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="refresh-cli-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "staging").mkdir()
        self.db = self.root / "ops.db"
        self.store = ops.OperationStore(self.db)
        self.state_file = self.root / "state.json"

    def create_operation(self, *, revision=9, task_id=None):
        """The row the endpoint creates before launching the worker."""
        intention = {"operation": "refresh", "confirm_interrupt": False,
                     "revision": revision, "desired_generation": None}
        if task_id:
            intention["task_id"] = task_id
        op_id, _ = self.store.begin(
            tablet_id="tablet", principal="dashboard", intention=intention,
            desired_generation=revision, user_message="Atualização da lista solicitada")
        self.store.set_phase(op_id, state="preparing", stage="launch")
        return op_id

    def run_cli(self, *extra, timeout=120):
        environment = dict(os.environ)
        environment["MIXED_OPERATIONS_DB"] = str(self.db)
        environment.pop("MIXED_APPLY_CMD", None)
        argv = [sys.executable, str(SCRIPT), "--refresh", "--json",
                "--operation-id", getattr(self, "operation_id", OP_ID),
                "--state-file", str(self.state_file),
                "--staging-base", str(self.root / "staging"),
                "--export-config", str(self.root / "export.json"),
                # Nothing listens here: the point is the BOOKKEEPING, and the
                # refresh must record its outcome even when the inventory cannot
                # be read.
                "--server-url", "http://127.0.0.1:1",
                *extra]
        return subprocess.run(argv, capture_output=True, text=True,
                              env=environment, timeout=timeout)

    def assert_slot_free(self):
        self.assertIsNone(
            self.store.active_for_tablet("tablet"),
            "the tablet slot is still held: a later apply/refresh would get 409")

    def test_the_refresh_cli_finishes_the_operation_it_was_given(self):
        self.operation_id = self.create_operation(revision=9)
        completed = self.run_cli()
        row = self.store.get(self.operation_id)
        self.assertNotIn(row["state"], ("queued", "preparing"),
                         "the worker returned without recording an outcome "
                         f"(rc={completed.returncode}, out={completed.stdout!r}, "
                         f"err={completed.stderr!r})")
        self.assert_slot_free()

    def test_the_refresh_cli_records_which_execution_ran(self):
        self.operation_id = self.create_operation(revision=9)
        self.run_cli()
        row = self.store.get(self.operation_id)
        self.assertTrue(row["executor"], "no executor was recorded")
        self.assertIn('-', row["executor"].split(':')[-1])

    def test_the_refresh_cli_refuses_a_different_reserved_revision(self):
        """The target moved between the click and the claim."""
        self.operation_id = self.create_operation(revision=9)
        self.write_state(pending_generation=10)
        completed = self.run_cli()
        payload = json.loads(completed.stdout or "{}")
        self.assertEqual(payload.get("state"), "blocked")
        self.assertEqual(payload.get("reason"), "coordination_unavailable")
        row = self.store.get(self.operation_id)
        self.assertEqual(row["state"], "preparing",
                         "a blocked worker must not touch the operation's phase")

    def test_the_refresh_cli_reports_an_already_decided_operation(self):
        self.operation_id = self.create_operation(revision=9)
        self.store.finish(self.operation_id, state="succeeded",
                          user_message="Lista atualizada")
        completed = self.run_cli()
        payload = json.loads(completed.stdout or "{}")
        self.assertTrue(payload.get("duplicate"))
        self.assertEqual(payload.get("operation_id"), self.operation_id)
        self.assertEqual(self.store.get(self.operation_id)["state"], "succeeded")

    def write_state(self, **fields):
        self.state_file.write_text(json.dumps(fields))



class BackgroundChildKeepsTheOperationId(unittest.TestCase):
    """B6: the detached child is a different process and must keep the id."""

    def test_the_detached_argv_carries_the_operation_id(self):
        from unittest import mock
        import mixed_generation_service as mgs

        args = mock.Mock(
            task_id="a1b2c3d4", operation_id=OP_ID, confirm_interrupt=False,
            json=True, background=True,
            verify=False, verify_inventory=False, status=False, refresh=False,
            state_file=Path("/tmp/state.json"), staging_base=Path("/tmp/staging"),
            export_config=Path("/tmp/export.json"), binaries_dir=Path("/tmp/bin"),
            adb_serial="127.0.0.1:25555", tablet_base="/data/local/tmp",
            live_port=18190, vod_port=18191, private_dir=None, ssh_host="",
            pin="")
        service = mock.Mock()
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(mgs, "MixedGenerationService", return_value=service), \
                mock.patch.object(mgs, "_acquire_publication_guard", return_value=None), \
                mock.patch.object(mgs, "_build_transport", return_value=None):
            rc = mgs._apply_command(args)
        self.assertEqual(rc, 0)
        argv = popen.call_args.args[0]
        # The child re-executes the publisher: without the flag it would fall
        # back to "whatever is active for the tablet", which is not a proof.
        self.assertIn("--operation-id", argv)
        self.assertEqual(argv[argv.index("--operation-id") + 1], OP_ID)
        # The child IS the publication: it must not re-detach itself.
        self.assertNotIn("--background", argv)


class EndToEndChainTests(unittest.TestCase):
    """P6: o id precisa ser NECESSÁRIO, não decorativo.

    O worker2 mostrou que a versão anterior passava com e sem id (o CLI caía na
    vaga única do tablet e terminava igual). Aqui as três corridas usam store,
    daemon e CLI reais e diferem APENAS no id:
      A) id correto        -> adota e fecha a operação;
      B) id de OUTRO tablet-> recusa, e a operação real fica intocada;
      C) sem id            -> adota pela vaga (controle: mostra que B não foi
                              recusada por outro motivo).
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="chain-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "staging").mkdir()
        self.spool = self.root / "spool"

    def _store(self, name):
        return ops.OperationStore(self.root / ("%s.db" % name))

    def _operation(self, store, *, tablet="tablet", state="preparing"):
        op_id, _ = store.begin(
            tablet_id=tablet, principal="dashboard",
            intention={"operation": "refresh", "confirm_interrupt": False,
                       "revision": None, "desired_generation": None},
            user_message="Atualização da lista solicitada")
        if state:
            store.set_phase(op_id, state=state, stage="launch")
        return op_id

    def _run_chain(self, db, *, operation_id=None):
        """Daemon real + CLI real, com (ou sem) o id vindo do request."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import mixed_bridge as bridge
        request = bridge.build_request("refresh", operation_id=operation_id)
        response_path = bridge.submit(self.spool, request)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "mixed_bridge.py"), "serve",
             "--spool", str(self.spool), "--poll-interval", "0.01", "--once",
             "--client-uid", str(os.getuid()),
             "--publisher", sys.executable, str(SCRIPT),
             "--state-file", str(self.root / "state.json"),
             "--staging-base", str(self.root / "staging"),
             "--server-url", "http://127.0.0.1:1", "--json"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, MIXED_OPERATIONS_DB=str(db)))
        self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
        # A resposta do CLI viaja no arquivo do spool; o stdout do daemon traz
        # apenas o recibo "handled request=...".
        return json.loads(response_path.read_text())

    def test_a_correct_id_adopts_and_finishes_the_operation(self):
        store = self._store("a")
        op_id = self._operation(store)
        self._run_chain(store.path, operation_id=op_id)
        row = store.get(op_id)
        self.assertTrue(row["executor"], "the real CLI did not record an execution")
        self.assertNotIn(row["state"], ("queued", "preparing"), row["state"])
        self.assertIsNone(store.active_for_tablet("tablet"))

    def test_an_id_from_another_tablet_is_refused_and_leaves_the_operation_alone(self):
        """Só o id muda em relação ao teste anterior: e a corrida é recusada."""
        store = self._store("b")
        mine = self._operation(store)                       # tablet "tablet"
        foreign = self._operation(store, tablet="outro")     # outro tablet
        payload = self._run_chain(store.path, operation_id=foreign)
        self.assertEqual(payload.get("state"), "blocked", payload)
        # A operação DESTE tablet continua intacta e ainda segura o slot.
        row = store.get(mine)
        self.assertEqual(row["state"], "preparing")
        self.assertIsNone(row["executor"])
        self.assertEqual(store.active_for_tablet("tablet")["operation_id"], mine)
        # E o outro tablet também não foi mexido por engano.
        self.assertEqual(store.get(foreign)["state"], "preparing")

    def test_without_an_id_the_cli_falls_back_to_the_tablet_slot(self):
        """Controle: mostra que a recusa acima veio do id, não de outra coisa."""
        store = self._store("c")
        op_id = self._operation(store)
        self._run_chain(store.path, operation_id=None)
        row = store.get(op_id)
        self.assertTrue(row["executor"])
        self.assertNotIn(row["state"], ("queued", "preparing"), row["state"])

    # LIMITE REGISTRADO: o filho de `--background` percorre o mesmo caminho do
    # CLI (guarda de publicação incluída), então exercitá-lo de verdade exige um
    # servidor de guarda vivo; simular a guarda seria mockar a própria ligação
    # sob teste. O que fica provado é o argv do filho com `--operation-id`
    # (BackgroundChildKeepsTheOperationId) e a adoção por id no CLI (acima).




class ReadableCatalogTests(unittest.TestCase):
    """Refresh com catálogo LEGÍVEL: onde ele realmente para sem dispositivo.

    O teste antigo apontava `--server-url` para porta fechada, então só provava
    `inventory_unavailable`. Aqui um servidor HTTP local (nada de produção/IPTV)
    serve o catálogo e o CLI roda com `--dry-run`: o caminho da publicação é
    exercitado até o ponto em que a TV tem de confirmar — que é onde ele para
    sem hardware. Isso separa "catálogo ilegível" de "falta confirmação da TV".
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="catalog-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "staging").mkdir()
        self.db = self.root / "ops.db"
        self.store = ops.OperationStore(self.db)

        payload = {"tasks": [{"id": "a1b2c3d4", "status": "ready", "title": "F",
                              "display_name": "F.mp4", "storage": "local",
                              "file_path": str(self.root / "F.mp4"),
                              "file_size": 1024}]}
        (self.root / "F.mp4").write_bytes(b"\x00" * 1024)
        body = json.dumps(payload).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802 (http.server API)
                if not self.path.startswith("/api/vod/status"):
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):          # silêncio no teste
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _operation(self):
        op_id, _ = self.store.begin(
            tablet_id="tablet", principal="dashboard",
            intention={"operation": "refresh", "confirm_interrupt": False,
                       "revision": None, "desired_generation": None},
            user_message="Atualização da lista solicitada")
        self.store.set_phase(op_id, state="preparing", stage="launch")
        return op_id

    def test_the_refresh_reads_the_catalog_instead_of_failing_on_the_network(self):
        op_id = self._operation()
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--refresh", "--json",
             "--operation-id", op_id, "--dry-run",
             "--state-file", str(self.root / "state.json"),
             "--staging-base", str(self.root / "staging"),
             "--server-url", self.base],
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ, MIXED_OPERATIONS_DB=str(self.db)))
        payload = json.loads(completed.stdout or "{}")
        row = self.store.get(op_id)
        # 1) O catálogo FOI lido: o motivo não é mais `inventory_unavailable`.
        self.assertNotEqual(row["error_code"], "inventory_unavailable",
                            completed.stdout[-500:] + completed.stderr[-800:])
        # 2) O desfecho é registrado e a vaga é liberada (sem isso, o próximo
        #    apply/refresh receberia 409 para sempre).
        self.assertNotIn(row["state"], ("queued", "preparing"), row["state"])
        self.assertIsNone(self.store.active_for_tablet("tablet"))
        # 3) Sem hardware não existe confirmação: o desfecho NÃO pode ser
        #    apresentado como publicação concluída.
        self.assertIn(payload.get("state"),
                      ("unknown", "incomplete", "blocked", "failed",
                       "needs_reconciliation"), payload)


class CrashRecoveryTests(unittest.TestCase):
    """B1-crash: matar o worker DENTRO do efeito não pode prender a vaga.

    Antes: a linha ficava `preparing` para sempre, com `executor` de um processo
    morto; `claim_execution` recusava qualquer outro executor, `preparing` não é
    estado "não resolvido" (o painel não mostrava nada) e não havia API para
    fechar. OOM/SIGKILL/reboot travava a tablet até editar SQLite à mão.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="crash-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "staging").mkdir()
        self.db = self.root / "ops.db"
        self.store = ops.OperationStore(self.db)

    def _operation(self):
        op_id, _ = self.store.begin(
            tablet_id="tablet", principal="dashboard",
            intention={"operation": "refresh", "confirm_interrupt": False,
                       "revision": None, "desired_generation": None},
            user_message="Atualização da lista solicitada")
        self.store.set_phase(op_id, state="preparing", stage="launch")
        return op_id

    def _kill_a_worker_after_it_claims(self, op_id):
        """Um worker REAL que morre com a posse gravada (como SIGKILL/OOM)."""
        child = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "import mixed_operations as ops, mixed_generation_service as mgs\n"
            "store = ops.OperationStore(%r)\n"
            "store.claim_execution(%r, task_id=None, revision=None,\n"
            "                      executor=mgs._local_execution_identity())\n"
            "os._exit(9)\n" % (str(ROOT / "scripts"), str(self.db), op_id)
        )
        completed = subprocess.run([sys.executable, "-c", child], timeout=60)
        self.assertEqual(completed.returncode, 9, "the mock worker must die abruptly")
        row = self.store.get(op_id)
        self.assertEqual(row["state"], "preparing")
        self.assertTrue(row["executor"], "the dead worker left its claim behind")
        # A vaga está presa: outra operação recebe TabletBusy.
        with self.assertRaises(ops.TabletBusy):
            self.store.begin(tablet_id="tablet", principal="dashboard",
                             intention={"operation": "refresh"},
                             user_message="outra")
        return row["executor"]

    def test_a_new_worker_adopts_the_operation_of_a_dead_one(self):
        op_id = self._operation()
        dead_executor = self._kill_a_worker_after_it_claims(op_id)

        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--refresh", "--json",
             "--operation-id", op_id,
             "--state-file", str(self.root / "state.json"),
             "--staging-base", str(self.root / "staging"),
             "--server-url", "http://127.0.0.1:1"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, MIXED_OPERATIONS_DB=str(self.db)))
        row = self.store.get(op_id)
        # (i) a linha sai de `preparing` sem edição manual de SQLite
        self.assertNotIn(row["state"], ("queued", "preparing"),
                         completed.stdout[-400:] + completed.stderr[-600:])
        # o executor é OUTRO (o novo worker), não o morto
        self.assertNotEqual(row["executor"], dead_executor)
        # (ii) a vaga libera
        self.assertIsNone(self.store.active_for_tablet("tablet"))
        # (iii) o desfecho fica consultável (pendência ou terminal visível)
        self.assertIn(row["state"], ("failed_safe", "needs_reconciliation",
                                     "interrupted", "succeeded",
                                     "already_current", "blocked"),
                      row["state"])

    def test_a_foreign_live_owner_is_still_refused(self):
        """Contraprova: posse VIVA de outro processo não é roubada."""
        op_id = self._operation()
        child = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import mixed_operations as ops, mixed_generation_service as mgs\n"
            "store = ops.OperationStore(%r)\n"
            "store.claim_execution(%r, task_id=None, revision=None,\n"
            "                      executor=mgs._local_execution_identity())\n"
            "sys.stdout.write('alive\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n" % (str(ROOT / "scripts"), str(self.db), op_id)
        )
        owner = subprocess.Popen([sys.executable, "-c", child],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (owner.kill(), owner.wait(timeout=10)))
        owner.stdout.readline()          # já reivindicou e está vivo
        row = self.store.get(op_id)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--refresh", "--json",
             "--operation-id", op_id,
             "--state-file", str(self.root / "state.json"),
             "--staging-base", str(self.root / "staging"),
             "--server-url", "http://127.0.0.1:1"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, MIXED_OPERATIONS_DB=str(self.db)))
        payload = json.loads(completed.stdout or "{}")
        self.assertEqual(payload.get("state"), "blocked", completed.stdout[-400:])
        self.assertEqual(self.store.get(op_id)["executor"], row["executor"],
                         "a LIVE owner must not be stolen")

if __name__ == "__main__":
    unittest.main()
