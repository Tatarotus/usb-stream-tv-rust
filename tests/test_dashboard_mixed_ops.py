#!/usr/bin/env python3
"""Global operation polling and the persistent indicator (Gerações V2, fase 1).

Runs the dashboard's own JavaScript under node with stubs, the same way
``test_dashboard_delete_mixed.py`` does. What is proven here is the *contract* of
the indicator: one loop per page, a pending problem is never hidden by
dismissing, cadence follows the documented numbers, and a network failure
deserves backoff rather than a claim about the TV.
"""

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

BEGIN = "        // ==================== Operações de publicação (Gerações V2)"
END = "        // Autenticação Silenciosa (PIN 1233 padrão)"


def _slice() -> str:
    html = (ROOT / "dashboard.html").read_text()
    start = html.index(BEGIN)
    end = html.index(END, start)
    return html[start:end]


HARNESS = r"""
const assert = require('assert');

let timeouts = [];
let fetched = [];
let responses = [];
const el = {
    style: {},
    innerHTML: '',
    // Registra o último toggle de forma legível, preservando o nome da classe.
    classList: {
        last: null,
        toggle(name, value) { this.last = { name: name, value: value }; },
        remove(name) { this.last = { name: name, value: false }; },
        add() {},
    },
};

global.setTimeout = (fn, ms) => { timeouts.push(ms); return timeouts.length; };
global.clearTimeout = () => {};
global.escapeHtml = (s) => String(s);
global.AbortSignal = { timeout: () => ({}) };
global.getAuthPin = () => 'test-pin';
global.document = {
    getElementById: () => el,
    addEventListener: () => {},
    hidden: false,
};
global.fetch = async (url) => {
    fetched.push(url);
    const r = responses.shift();
    if (!r) throw new Error('no canned response for ' + url);
    if (r.throws) throw new Error('network down');
    return { ok: r.ok !== false, json: async () => r.body };
};

const settle = () => new Promise((res) => setImmediate(res));

async function run() {
    // A resposta canônica precisa existir ANTES do trecho rodar: o próprio
    // trecho chama startMixedOpsPolling() no fim, e sem resposta a primeira
    // sondagem cairia no backoff de falha de rede.
    responses.push({ body: { ok: true, available: true, cursor: 7,
                             events: [], operations: [], unresolved: [] } });
__SLICE__

    // --- 1. o laço arranca sozinho e usa a rota de eventos com cursor --------
    await settle();
    assert.ok(fetched.length >= 1, 'polled at least once');
    assert.ok(fetched[0].startsWith('/api/mixed/events?cursor='),
              'uses the events route, got ' + fetched[0]);
    assert.ok(timeouts.length >= 1, 'scheduled the next cycle');
    assert.strictEqual(timeouts[timeouts.length - 1], 15000,
                       'idle cadence is 15s');

    // --- 2. um único laço por página -----------------------------------------
    const scheduledBefore = timeouts.length;
    startMixedOpsPolling();
    assert.strictEqual(timeouts.length, scheduledBefore,
                       'a second start must not add a second loop');

    // --- 3. cadência acelera quando há pendência -----------------------------
    responses.push({ body: { ok: true, available: true, cursor: 9, events: [],
                             operations: [{ operation_id: 'op1', state: 'needs_reconciliation',
                                            user_message: 'Conexao perdida apos a troca',
                                            stage: 'verifying' }],
                             unresolved: ['op1'] } });
    await pollMixedEvents();
    assert.strictEqual(timeouts[timeouts.length - 1], 2000,
                       'pending problem polls at 2s');
    assert.strictEqual(el.style.display, 'block', 'indicator visible');
    assert.ok(el.innerHTML.indexOf('Conexao perdida') !== -1,
              'shows the user message');
    assert.strictEqual(el.classList.last.value, true,
                       'marked as a problem');
    assert.strictEqual(el.classList.last.name, 'is-problem',
                       'problem styling applied');

    // --- 4. dispensar NÃO esconde uma pendência ------------------------------
    dismissMixedOpsIndicator();
    assert.strictEqual(el.style.display, 'block',
                       'dismiss must not hide an unresolved problem');

    // --- 5. resultado terminal aparece e é dispensável -----------------------
    responses.push({ body: { ok: true, available: true, cursor: 11, events: [],
                             operations: [{ operation_id: 'op2', state: 'succeeded',
                                            user_message: 'Lista atualizada',
                                            stage: null }],
                             unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.classList.last.value, false, 'a success is not a problem');
    assert.ok(el.innerHTML.indexOf('Lista atualizada') !== -1);
    dismissMixedOpsIndicator();
    assert.strictEqual(el.style.display, 'none',
                       'a finished result can be dismissed');

    // --- 6. falha de rede faz backoff e não afirma nada ----------------------
    responses.push({ throws: true });
    await pollMixedEvents();
    const backoff = timeouts[timeouts.length - 1];
    assert.ok(backoff >= 2000 && backoff <= 30000,
              'backoff bounded, got ' + backoff);
    // UI8: nada de estado inventado, mas a perda de contato fica VISÍVEL.
    assert.strictEqual(el.style.display, 'block',
                       'a network failure must be visible, not silent');
    assert.ok(el.innerHTML.indexOf('Sem contato') !== -1,
              'the panel states that it lost contact');

    // --- 7. sem store a rota diz available:false e nada é exibido ------------
    responses.push({ body: { ok: true, available: false, cursor: 0,
                             events: [], operations: [], unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.style.display, 'none',
                       'no store means nothing to show');

    console.log('OK');
}

run().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_mixed_ops_indicator_contracts():
    script = HARNESS.replace("__SLICE__", _slice())
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "OK" in proc.stdout


HARNESS_UI = r"""
const assert = require('assert');

let timeouts = [];
let cleared = 0;
let fetched = [];
let responses = [];
let visibilityHandler = null;
const el = { style: {}, innerHTML: '', classList: { last: null,
    toggle(name, value) { this.last = { name: name, value: value }; },
    remove(name) { this.last = { name: name, value: false }; }, add() {} } };

global.setTimeout = (fn, ms) => { timeouts.push(ms); return timeouts.length; };
global.clearTimeout = () => { cleared += 1; };
global.escapeHtml = (s) => String(s);
// UI9: sem AbortSignal.timeout, como numa WebView antiga.
global.AbortSignal = {};
global.getAuthPin = () => 'test-pin';
global.AbortController = function () {
    this.signal = {};
    this.abort = () => {};
};
global.document = {
    getElementById: () => el,
    addEventListener: (name, fn) => { if (name === 'visibilitychange') visibilityHandler = fn; },
    hidden: false,
};
global.fetch = async (url) => {
    fetched.push(url);
    const r = responses.shift();
    if (!r) throw new Error('no canned response for ' + url);
    if (r.throws) throw new Error('network down');
    return { ok: true, json: async () => r.body };
};

const settle = () => new Promise((res) => setImmediate(res));

async function run() {
    responses.push({ body: { ok: true, available: true, cursor: 1, events: [],
                             operations: [], unresolved: [] } });
__SLICE__

    // --- UI9: o laço continua funcionando sem AbortSignal.timeout ----------
    await settle();
    assert.ok(fetched.length >= 1, 'polled without AbortSignal.timeout');

    // --- UI5/UI6: operação EM ANDAMENTO é renderizada, a 2 s ----------------
    responses.push({ body: { ok: true, available: true, cursor: 2, events: [],
                             operations: [{ operation_id: 'act1', state: 'transferring',
                                            user_message: 'Transferindo lista',
                                            stage: 'transferring',
                                            heartbeat_at: 1794000000 }],
                             unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(timeouts[timeouts.length - 1], 2000,
                       'an active operation polls at 2s, got ' + timeouts[timeouts.length - 1]);
    assert.ok(el.innerHTML.indexOf('Transferindo lista') !== -1,
              'the active operation is shown');
    assert.strictEqual(el.style.display, 'block');
    assert.ok(el.innerHTML.indexOf('Dispensar') === -1,
              'an active operation cannot be dismissed');

    // --- UI4: terminal dispensado, mas operação NOVA reabre o aviso --------
    responses.push({ body: { ok: true, available: true, cursor: 3, events: [],
                             operations: [{ operation_id: 'term1', state: 'succeeded',
                                            user_message: 'Lista atualizada' }],
                             unresolved: [] } });
    await pollMixedEvents();
    assert.ok(el.innerHTML.indexOf('Lista atualizada') !== -1);
    dismissMixedOpsIndicator();
    assert.strictEqual(el.style.display, 'none', 'a finished result is dismissible');

    responses.push({ body: { ok: true, available: true, cursor: 4, events: [],
                             operations: [{ operation_id: 'term2', state: 'succeeded',
                                            user_message: 'Outra atualizacao' }],
                             unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.style.display, 'block',
                       'a NEW result must reopen the notice');
    assert.ok(el.innerHTML.indexOf('Outra atualizacao') !== -1);

    // --- UI8: falha de rede tem estado VISÍVEL, não só backoff -------------
    responses.push({ throws: true });
    await pollMixedEvents();
    assert.ok(el.style.display === 'block', 'a network failure stays on screen');
    assert.ok(el.innerHTML.indexOf('Sem contato') !== -1,
              'the panel says it lost contact instead of going silent');

    // --- UI7: available:false não apaga pendência já visível ---------------
    responses.push({ body: { ok: true, available: true, cursor: 5, events: [],
                             operations: [{ operation_id: 'op9', state: 'needs_reconciliation',
                                            user_message: 'Falha pendente' }],
                             unresolved: ['op9'] } });
    await pollMixedEvents();
    assert.ok(el.innerHTML.indexOf('Falha pendente') !== -1);
    responses.push({ body: { ok: true, available: false, cursor: 5, events: [],
                             operations: [], unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.style.display, 'block',
                       'available:false must not erase a visible pendency');
    assert.ok(el.innerHTML.indexOf('Falha pendente') !== -1);

    // --- UI4 (repro do worker2): o aviso terminal sobrevive a um poll VAZIO --
    // O snapshot devolve unresolved + operações citadas por eventos após o
    // cursor; num fluxo silencioso o segundo poll vem vazio. Sem retenção no
    // cliente o aviso sumia no segundo ciclo.
    responses.push({ body: { ok: true, available: true, cursor: 6, events: [],
                             operations: [{ operation_id: 'silent1', state: 'succeeded',
                                            user_message: 'Lista atualizada em silencio' }],
                             unresolved: [] } });
    await pollMixedEvents();
    assert.ok(el.innerHTML.indexOf('silencio') !== -1, 'first poll shows the result');
    responses.push({ body: { ok: true, available: true, cursor: 7, events: [],
                             operations: [], unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.style.display, 'block',
                       'a silent second poll must NOT hide the result');
    assert.ok(el.innerHTML.indexOf('silencio') !== -1,
              'the same result is still on screen');
    dismissMixedOpsIndicator();
    assert.strictEqual(el.style.display, 'none', 'dismiss hidES it explicitly');
    responses.push({ body: { ok: true, available: true, cursor: 8, events: [],
                             operations: [], unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.style.display, 'none',
                       'a dismissed result must not come back by itself');

    // --- UI3: visibilitychange limpa o timer e não cria cadeia paralela ----
    assert.ok(visibilityHandler, 'the listener is registered');
    // Drena qualquer promessa pendente antes de medir: o que se mede aqui é
    // quantos ciclos o LISTENER agenda, não os que já estavam em voo.
    await settle();
    await settle();
    const before = timeouts.length;
    global.document.hidden = false;
    visibilityHandler();
    await settle();
    assert.strictEqual(cleared, 1, 'the pending timer is cleared before polling');
    // `timeoutSignal` arma um timer de 6000 ms para abortar a requisição (UI9);
    // ele não é um ciclo de polling. Contamos só os ciclos agendados.
    const cycles = timeouts.slice(before).filter((ms) => ms !== 6000);
    assert.strictEqual(cycles.length, 1,
                       'exactly one new cycle is scheduled, got ' + cycles);

    console.log('OK');
}

run().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_mixed_ops_indicator_ui_contracts():
    script = HARNESS_UI.replace("__SLICE__", _slice())
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "OK" in proc.stdout


def test_the_click_handlers_send_a_key_and_do_not_poll_on_their_own():
    """UI1/UI2: one idempotency key per click, and no per-click polling loop."""
    html = (ROOT / "dashboard.html").read_text()
    apply_at = html.index("'/api/mixed/apply'")
    refresh_at = html.index("'/api/mixed/refresh'")
    for index, name in ((apply_at, "apply"), (refresh_at, "refresh")):
        window = html[index:index + 700]
        assert "idempotency_key" in window, (
            "%s click must carry an idempotency key so a repeat is the SAME "
            "operation" % name)
    assert "while (Date.now() < deadline)" not in html, (
        "the click must not run its own 16-minute polling loop; the global "
        "loop follows the operation")
    assert "baselineApplyTimestamp" not in html, (
        "correlation by timestamp was replaced by the operation id")


# --- P1: veracidade da linha de último episódio -----------------------------
P1_BEGIN = "        // P1 (veracidade): a linha NÃO pode sugerir"
P1_END = "        async function loadLocalSavedMovies() {"

P1_HARNESS = r"""
const assert = require('assert');
const label = { hidden: null, textContent: '', title: '' };
global.document = { getElementById: () => label };
let continueWatching = [];
__SLICE__

const now = Date.now();
const daysAgo = (n) => now - n * 86400000;

function run(entries) {
    continueWatching = entries;
    renderLastEpisode();
    return { text: label.textContent, title: label.title, hidden: label.hidden };
}

// 1) `selected` sozinho: nunca afirma reprodução, nunca "Episódio N" solto,
//    e mostra a DATA (relativa no texto, absoluta no title).
let out = run([{ series_title: 'A Noiva de Istambul', season_number: 3,
                 episode_number: 164, position_source: 'selected',
                 updated_at: daysAgo(4) }]);
assert.ok(out.text.indexOf('Reprodução não registrada') !== -1,
          'selected must not claim playback, got: ' + out.text);
assert.ok(/Episódio 164/.test(out.text) ? out.text.indexOf('não registrada') !== -1 : true);
assert.ok(out.text.indexOf('há 4 dias') !== -1, 'relative date present: ' + out.text);
assert.ok(out.title && out.title.indexOf('Última seleção em') === 0,
          'absolute date in title: ' + out.title);
assert.ok(!/assistid/i.test(out.text), 'never the word "assistido" for a selection');

// 2) Seleção ANTIGA não pode ofuscar reprodução REAL: a reprodução vence.
out = run([
  { series_title: 'A Noiva de Istambul', season_number: 3, episode_number: 164,
    position_source: 'selected', updated_at: daysAgo(1) },
  { series_title: "Hell's Kitchen", season_number: 20, episode_number: 5,
    position_source: 'manual', updated_at: daysAgo(2) },
]);
assert.ok(out.text.indexOf("Hell's Kitchen") !== -1,
          'playback evidence wins over a newer selection, got: ' + out.text);
assert.ok(out.text.indexOf('Noiva') === -1, 'the selection must not be shown instead');

// 3) checkedpoint/inferred também são reprodução.
out = run([{ series_title: 'Serie X', season_number: 1, episode_number: 2,
             position_source: 'inferred_stream', updated_at: daysAgo(0) }]);
assert.ok(out.text.indexOf('Possível reprodução') === 0, 'inferred is labelled as such');

// 4) Sem nada: a linha some (não inventa "Episódio N").
out = run([]);
assert.strictEqual(out.hidden, true, 'nothing to say means no line');

// 5) Duas reproduções: a MAIS RECENTE vence (ordem do servidor não manda).
out = run([
  { series_title: 'Antiga', episode_number: 1, position_source: 'manual', updated_at: daysAgo(9) },
  { series_title: 'Recente', episode_number: 7, position_source: 'manual', updated_at: daysAgo(1) },
]);
assert.ok(out.text.indexOf('Recente') !== -1, 'newest playback wins, got: ' + out.text);

console.log('OK');
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_last_episode_line_never_claims_playback_without_evidence():
    html = (ROOT / "dashboard.html").read_text()
    start = html.index(P1_BEGIN)
    end = html.index(P1_END, start)
    script = P1_HARNESS.replace("__SLICE__", html[start:end])
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "OK" in proc.stdout


# --- P2/UI4: dois polls com payload REAL do servidor ------------------------
UI4_HARNESS = r"""
const assert = require('assert');
let timeouts = [];
const el = { style: {}, innerHTML: '', classList: { last: null,
    toggle(n, v) { this.last = { name: n, value: v }; }, remove() {}, add() {} } };
global.setTimeout = (fn, ms) => { timeouts.push(ms); return timeouts.length; };
global.clearTimeout = () => {};
global.escapeHtml = (s) => String(s);
global.AbortSignal = { timeout: () => ({}) };
global.getAuthPin = () => 'pin';
global.document = { getElementById: () => el, addEventListener: () => {}, hidden: false };
const payloads = __PAYLOADS__;
global.fetch = async () => ({ ok: true, json: async () => payloads.shift() });
const settle = () => new Promise((r) => setImmediate(r));
__SLICE__
async function run() {
    // O próprio trecho chama startMixedOpsPolling() ao carregar: o poll#1 é
    // dele. Aqui só drenamos a promessa e depois forçamos o poll silencioso.
    await settle();
    await settle();
    assert.strictEqual(el.style.display, 'block', 'poll#1 shows the result');
    assert.ok(el.innerHTML.indexOf('Lista atualizada') !== -1, el.innerHTML);
    await pollMixedEvents();
    await settle();
    assert.strictEqual(el.style.display, 'block',
                       'poll#2 (silent, cursor consumed) must STILL show it');
    assert.ok(el.innerHTML.indexOf('Lista atualizada') !== -1,
              'the same result survives the silent poll');
    console.log('OK');
}
run().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_ui4_survives_the_two_real_server_payloads(tmp_path=None):
    """Dois polls REAIS do endpoint: o segundo vem vazio e o aviso permanece.

    Worker3 recusou prova que injeta `operations:[{...}]` só na hora de mostrar;
    aqui as duas respostas são produzidas pelo código do servidor
    (`request_events` sobre o store real) e só então entregues ao laço do painel.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    import tempfile
    import mixed_operations as _ops
    import mixed_ops_api as _api

    with tempfile.TemporaryDirectory() as tmp:
        store = _ops.OperationStore(pathlib.Path(tmp) / "ops.db")
        op_id, _ = store.begin(tablet_id="tablet", principal="dashboard",
                               intention={"operation": "refresh"},
                               user_message="Atualização da lista solicitada")
        store.finish(op_id, state="succeeded", user_message="Lista atualizada")
        status1, first = _api.request_events(store, cursor=0)
        status2, second = _api.request_events(store, cursor=first["cursor"])
    assert status1 == 200 and status2 == 200
    # O FATO do servidor que causava o defeito:
    assert [o["operation_id"] for o in first["operations"]] == [op_id]
    assert second["operations"] == [], "the second poll is genuinely empty"
    assert second["unresolved"] == []

    import json
    html = (ROOT / "dashboard.html").read_text()
    start = html.index(BEGIN)
    end = html.index(END, start)
    script = (UI4_HARNESS
              .replace("__PAYLOADS__", json.dumps([
                  dict(first, available=True), dict(second, available=True)]))
              .replace("__SLICE__", html[start:end]))
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "OK" in proc.stdout
